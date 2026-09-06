import threading
from typing import Any, Optional, Callable
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.binding import Binding
from textual.message import Message
from textual.widgets import (
    Header,
    Footer,
    Input,
    Button,
    Label,
    Select,
    Switch,
    ProgressBar,
    Static,
    DataTable,
    OptionList,
)
from textual.widgets.option_list import Option
from textual import events, work
from yt_downloader import YtDownloader
from player import StreamPlayer
from terminal_graphics import (
    detect_terminal_graphics_support,
    fetch_thumbnail_image,
    generate_placeholder_thumbnail,
    render_half_block_ansi,
    render_thumbnail,
    extract_video_id,
)


def format_time(seconds: float) -> str:
    mins, secs = divmod(int(seconds), 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def format_volume_bar(volume: int, muted: bool = False) -> str:
    vol = max(0, min(100, int(volume)))
    filled = round(vol / 10)
    empty = 10 - filled
    bar = "█" * filled + "░" * empty
    if muted:
        return f"Vol: [{bar}] {vol}% (Muted)"
    return f"Vol: [{bar}] {vol}%"


# Custom OptionList for Interactive Playback Queue
class QueueOptionList(OptionList):
    BINDINGS = [
        Binding("delete", "delete_selected", "Delete Track", show=False),
        Binding("backspace", "delete_selected", "Delete Track", show=False),
    ]

    class TrackDeleteRequested(Message):
        def __init__(self, index: int) -> None:
            super().__init__()
            self.index = index

    def action_delete_selected(self) -> None:
        if self.highlighted is not None and 0 <= self.highlighted < self.option_count:
            self.post_message(self.TrackDeleteRequested(self.highlighted))


# Textual Custom Messages for Thread-Safe UI Communication
class TrackChanged(Message):
    def __init__(self, index: int, track: dict[str, Any]) -> None:
        super().__init__()
        self.index = index
        self.track = track


class TimeUpdated(Message):
    def __init__(self, current: float, total: float) -> None:
        super().__init__()
        self.current = current
        self.total = total


class PlaybackEnded(Message):
    pass


class MetadataUpdated(Message):
    def __init__(self, title: Optional[str], artist: Optional[str]) -> None:
        super().__init__()
        self.title = title
        self.artist = artist


class DownloadProgress(Message):
    def __init__(self, status_text: str, percent: float) -> None:
        super().__init__()
        self.status_text = status_text
        self.percent = percent


class SearchResultsReady(Message):
    def __init__(self, results: list[dict[str, Any]]) -> None:
        super().__init__()
        self.results = results


class ThumbnailReady(Message):
    """A thumbnail is ready to display.

    ``renderable`` is a Rich ``Text`` with the ANSI half-block copy and is always
    present (it is the guaranteed-visible base layer). For native terminals
    (``native=True``) ``path`` additionally holds the cached image file; the
    crisp native escape sequence is generated on the UI thread at delivery time
    so it can match the live on-screen size of the box, and is drawn on top of
    the ANSI base.
    """

    def __init__(
        self,
        video_id: str,
        renderable: Any,
        native: bool = False,
        path: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.video_id = video_id
        self.renderable = renderable
        self.native = native
        self.path = path


class YTPlayerApp(App):
    CSS = """
    Screen {
        layout: vertical;
        overflow: hidden;
    }
    #main_panels {
        height: 1fr;
        width: 100%;
    }
    #search_panel {
        width: 65%;
        height: 100%;
        padding: 1;
        border-right: solid $primary;
    }
    #queue_panel {
        width: 35%;
        height: 100%;
        padding: 1;
    }
    .panel-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    .controls-row {
        height: auto;
        width: 100%;
        layout: horizontal;
        align-vertical: middle;
        margin-bottom: 1;
    }
    .controls-row > * {
        margin-right: 1;
    }
    #search_input {
        width: 100%;
        margin-bottom: 1;
    }
    #quality_select {
        width: 28;
    }
    #results_table {
        height: 1fr;
        width: 100%;
    }
    #queue_list {
        height: 1fr;
        width: 100%;
    }
    #player_bar {
        dock: bottom;
        height: auto;
        width: 100%;
        background: $panel;
        border-top: heavy $accent;
        padding: 1 2;
    }
    .player-info-row {
        height: auto;
        width: 100%;
        layout: horizontal;
        align-vertical: middle;
        margin-bottom: 1;
    }
    .player-progress-row {
        height: auto;
        width: 100%;
        layout: horizontal;
        align-vertical: middle;
    }
    #player_status_icon {
        text-style: bold;
        color: $success;
        width: 3;
        min-width: 3;
    }
    #player_track_title, #playback_title {
        text-style: bold;
        width: 1fr;
        min-width: 20;
        text-wrap: nowrap;
        text-overflow: ellipsis;
        overflow: hidden;
    }
    #btn_close_video {
        height: 1;
        min-width: 16;
        margin-right: 1;
        border: none;
        padding: 0 1;
    }
    #player_badges {
        color: $text-muted;
        width: auto;
        min-width: 16;
        margin-right: 2;
    }
    #player_volume_bar, #volume_label {
        color: $accent;
        width: auto;
        min-width: 22;
    }
    #playback_time {
        width: 17;
        min-width: 17;
        text-style: bold;
    }
    #playback_progress {
        width: 1fr;
    }
    #thumbnail_box {
        height: auto;
        width: 100%;
        margin-top: 1;
        border-top: solid $primary;
        display: none;
    }
    #thumbnail_title {
        text-style: bold;
        color: $accent;
        margin-top: 1;
        margin-bottom: 1;
    }
    #thumbnail_display {
        height: 13;
        width: 100%;
        content-align: center middle;
    }
    #visualizer_box {
        height: auto;
        width: 100%;
        margin-top: 1;
        border-top: solid $primary;
        display: none;
    }
    #visualizer_title {
        text-style: bold;
        color: $accent;
        margin-top: 1;
        margin-bottom: 1;
    }
    #visualizer_display {
        height: 6;
        width: 100%;
        content-align: center middle;
    }
    """

    BINDINGS = [
        Binding("space", "toggle_pause", "Play/Pause", priority=True),
        Binding("x", "stop", "Stop", priority=True),
        Binding("left", "seek_backward", "Seek -5s", priority=True),
        Binding("right", "seek_forward", "Seek +5s", priority=True),
        Binding("up", "volume_up", "Vol +5%", priority=True),
        Binding("down", "volume_down", "Vol -5%", priority=True),
        Binding("s", "toggle_shuffle", "Shuffle", priority=True),
        Binding("r", "toggle_repeat", "Repeat", priority=True),
        Binding("t", "toggle_thumbnail", "Thumbnail", priority=True),
        Binding("v", "toggle_visualizer", "Visualizer", priority=True),
        Binding("c", "close_video", "Close Video", priority=True),
        Binding("escape", "focus_search", "Search", priority=True),
        Binding("q", "quit", "Quit", priority=True),
    ]

    def __init__(self, downloader: YtDownloader, player: StreamPlayer, **kwargs: Any):
        super().__init__(**kwargs)
        self.downloader = downloader
        self.player = player
        self._search_results: list[dict[str, Any]] = []
        self._vis_levels: list[float] = [0.0] * 16
        self._vis_targets: list[float] = [0.0] * 16
        self._vis_is_flat: bool = False
        # Native inline-graphics state. _thumbnail_protocol is "kitty" or
        # "sixel" when the host terminal can render true-pixel thumbnails via
        # raw escape sequences written straight to the driver; otherwise None
        # and the ANSI half-block path is used.
        self._thumbnail_protocol: Optional[str] = None
        self._native_thumb_path: Optional[str] = None
        self._native_thumb_escape: Optional[str] = None
        self._native_thumb_key: Optional[tuple[str, int, int]] = None
        self.player.set_callbacks(
            on_time_update=self._handle_time_update,
            on_end=self.handle_playback_end,
            on_metadata=self._handle_metadata,
            on_track_change=self._handle_track_change,
        )

    def _run_on_ui_thread(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Execute a callable safely on the main UI thread from any thread."""
        if threading.get_ident() == getattr(self, "_thread_id", None):
            fn(*args, **kwargs)
        else:
            try:
                self.call_from_thread(fn, *args, **kwargs)
            except RuntimeError:
                pass

    def on_mount(self) -> None:
        table = self.query_one("#results_table", DataTable)
        table.add_columns("#", "Title", "Duration", "Artist / Uploader")
        detected = detect_terminal_graphics_support().get("protocol")
        # iTerm2's proprietary inline-image OSC is intentionally left on the
        # ANSI fallback (it does not overlay cleanly inside a cell-based TUI).
        self._thumbnail_protocol = detected if detected in ("kitty", "sixel") else None
        self._update_player_bar()
        self._refresh_queue_list()
        self.set_interval(0.08, self._update_visualizer)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main_panels"):
            with Vertical(id="search_panel"):
                yield Static("🔍 Search & Media Input", classes="panel-title")
                yield Input(placeholder="Search YouTube or paste URL... (Enter to search, Esc to focus)", id="search_input")
                with Horizontal(classes="controls-row"):
                    yield Label("Video:")
                    yield Switch(id="video_toggle", value=False)
                    yield Label("Format:")
                    preset_options = [(p["label"], p["id"]) for p in self.downloader.get_preset_formats()]
                    yield Select(options=preset_options, value="best_video", id="quality_select", allow_blank=False)
                    yield Button("Search", variant="primary", id="btn_search")
                    yield Button("Add All", variant="default", id="btn_add_all")
                    yield Button("Download", variant="success", id="btn_download")
                yield DataTable(id="results_table", cursor_type="row")

            with Vertical(id="queue_panel"):
                yield Static("📋 Queue (Empty)", classes="panel-title", id="queue_title")
                yield QueueOptionList(id="queue_list")
                with Vertical(id="thumbnail_box"):
                    yield Static("🖼 Thumbnail Preview", classes="panel-title", id="thumbnail_title")
                    yield Static("", id="thumbnail_display")
                with Vertical(id="visualizer_box"):
                    yield Static("📊 Visualizer", classes="panel-title", id="visualizer_title")
                    yield Static("", id="visualizer_display")

        with Vertical(id="player_bar"):
            with Horizontal(classes="player-info-row"):
                yield Static("⏹", id="player_status_icon")
                yield Static("Now Playing: Idle", id="player_track_title")
                yield Button("[X] Close Video", id="btn_close_video", variant="error")
                yield Static("[🔀 OFF] [🔁 OFF]", id="player_badges")
                yield Static("Vol: [██████████] 100%", id="player_volume_bar")
            with Horizontal(classes="player-progress-row"):
                yield Static("00:00 / 00:00", id="playback_time")
                yield ProgressBar(id="playback_progress", total=100, show_eta=False)

        yield Footer()

    # Event Handlers
    def on_key(self, event: events.Key) -> None:
        focused = self.focused
        if isinstance(focused, Input):
            if event.key == "escape":
                self.set_focus(None)
                event.prevent_default()
                event.stop()
            return

        if event.key == "space":
            self.action_toggle_pause()
            event.prevent_default()
            event.stop()
        elif event.key == "x":
            self.action_stop()
            event.prevent_default()
            event.stop()
        elif event.key == "c":
            self.action_close_video()
            event.prevent_default()
            event.stop()
        elif event.key == "t":
            self.action_toggle_thumbnail()
            event.prevent_default()
            event.stop()
        elif event.key == "v":
            self.action_toggle_visualizer()
            event.prevent_default()
            event.stop()
        elif event.key == "left":
            self.action_seek_backward()
            event.prevent_default()
            event.stop()
        elif event.key == "right":
            self.action_seek_forward()
            event.prevent_default()
            event.stop()
        elif event.key == "s":
            self.action_toggle_shuffle()
            event.prevent_default()
            event.stop()
        elif event.key == "r":
            self.action_toggle_repeat()
            event.prevent_default()
            event.stop()
        elif event.key == "escape":
            self.action_focus_search()
            event.prevent_default()
            event.stop()
        elif event.key == "q":
            self.action_quit()
            event.prevent_default()
            event.stop()
        elif event.key in ("up", "down") and not isinstance(focused, (DataTable, OptionList, Select)):
            if event.key == "up":
                self.action_volume_up()
            else:
                self.action_volume_down()
            event.prevent_default()
            event.stop()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_search":
            self.action_search()
        elif event.button.id == "btn_add_all":
            self._action_add_all_to_queue()
        elif event.button.id == "btn_download":
            self._action_download_selected()
        elif event.button.id == "btn_close_video":
            self.action_close_video()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search_input":
            self.action_search()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        try:
            row_idx = int(str(event.row_key.value))
            if 0 <= row_idx < len(self._search_results):
                item = self._search_results[row_idx]
                video_enabled = self.query_one("#video_toggle", Switch).value
                self.player.add_to_queue(item)
                new_idx = len(self.player.queue) - 1
                self.player.play_index(new_idx, video_enabled=video_enabled)
                self._refresh_queue_list()
                self._update_player_bar()
                self._request_thumbnail_update(item)
                self.set_focus(None)
                self.notify(f"Playing: {item.get('title', 'Track')}")
        except Exception as e:
            self.notify(f"Selection error: {e}", severity="error")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        try:
            row_idx = int(str(event.row_key.value))
            if 0 <= row_idx < len(self._search_results):
                item = self._search_results[row_idx]
                self._request_thumbnail_update(item)
        except Exception:
            pass

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "queue_list":
            idx = event.option_index
            video_enabled = self.query_one("#video_toggle", Switch).value
            self.player.play_index(idx, video_enabled=video_enabled)
            self._refresh_queue_list()
            self._update_player_bar()
            if 0 <= idx < len(self.player.queue):
                self._request_thumbnail_update(self.player.queue[idx])
            self.set_focus(None)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        try:
            idx = event.option_index
            if 0 <= idx < len(self.player.queue):
                item = self.player.queue[idx]
                self._request_thumbnail_update(item)
        except Exception:
            pass

    def on_queue_option_list_track_delete_requested(self, event: QueueOptionList.TrackDeleteRequested) -> None:
        idx = event.index
        if 0 <= idx < len(self.player.queue):
            removed_track = self.player.queue[idx]
            title = removed_track.get("title", "Track")
            self.player.remove_at(idx)
            self._refresh_queue_list()
            self._update_player_bar()
            self.notify(f"Removed '{title}' from queue")

    # Actions
    def action_search(self) -> None:
        query = self.query_one("#search_input", Input).value.strip()
        if not query:
            self.notify("Please enter a search query or URL.", severity="warning")
            return
        self.notify("Searching / extracting media...")
        self.search_worker(query)

    @work(thread=True)
    def search_worker(self, query: str) -> None:
        try:
            url_or_query = query
            if not (query.startswith("http://") or query.startswith("https://") or query.startswith("ytsearch")):
                url_or_query = f"ytsearch10:{query}"

            items = self.downloader.extract_playlist_items(url_or_query)
            if not items:
                items = [{"title": query, "url": query, "duration": 0.0, "uploader": "Direct Stream"}]

            self.post_message(SearchResultsReady(items))
        except Exception as e:
            self._run_on_ui_thread(self.notify, f"Search error: {e}", severity="error")

    def _action_add_all_to_queue(self) -> None:
        if not self._search_results:
            self.notify("No search results to add.", severity="warning")
            return
        was_empty = len(self.player.queue) == 0
        for item in self._search_results:
            self.player.add_to_queue(item)
        if was_empty:
            video_enabled = self.query_one("#video_toggle", Switch).value
            self.player.play_index(0, video_enabled=video_enabled)
        self._refresh_queue_list()
        self._update_player_bar()
        self.notify(f"Added {len(self._search_results)} tracks to queue")

    def _action_download_selected(self) -> None:
        table = self.query_one("#results_table", DataTable)
        url = ""
        if table.cursor_row is not None and 0 <= table.cursor_row < len(self._search_results):
            url = self._search_results[table.cursor_row].get("url") or ""
        if not url:
            url = self.query_one("#search_input", Input).value.strip()
        if not url:
            self.notify("Select a search result or enter a URL to download.", severity="warning")
            return

        preset_id = str(self.query_one("#quality_select", Select).value)
        self.download_worker(url, preset_id)

    @work(thread=True)
    def download_worker(self, url: str, preset_id: str) -> None:
        self.post_message(DownloadProgress("Starting download...", 0.0))

        def progress_hook(d: dict[str, Any]) -> None:
            status = d.get("status")
            if status == "downloading":
                downloaded = d.get("downloaded_bytes", 0)
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
                percent = (downloaded / total) * 100
                speed = d.get("_speed_str", "N/A")
                eta = d.get("_eta_str", "N/A")
                filename = d.get("filename", "").split("/")[-1]
                msg = f"Downloading: {filename} | Speed: {speed} | ETA: {eta}"
                self.post_message(DownloadProgress(msg, percent))
            elif status == "finished":
                self.post_message(DownloadProgress("Processing audio/video...", 100.0))

        try:
            self.downloader.download(url, preset_id=preset_id, progress_hook=progress_hook)
            self.post_message(DownloadProgress("Download Complete!", 100.0))
            self._run_on_ui_thread(self.notify, "Download finished successfully!", severity="information")
        except Exception as e:
            self.post_message(DownloadProgress(f"Error: {e}", 0.0))
            self._run_on_ui_thread(self.notify, f"Download failed: {e}", severity="error")

    def action_close_video(self) -> None:
        self.close_video_worker()

    @work(thread=True)
    def close_video_worker(self) -> None:
        try:
            self.player.close_video()
        except Exception as e:
            self._run_on_ui_thread(self.notify, f"Error closing video: {e}", severity="error")
        self.handle_playback_end()
        self._run_on_ui_thread(self.notify, "Video closed", severity="information")

    # Queue & UI Refresh
    def _refresh_queue_list(self) -> None:
        queue_list = self.query_one("#queue_list", QueueOptionList)
        queue_list.clear_options()

        current_idx = self.player.current_index
        is_active = (self.player.is_playing or self.player.is_paused) and not getattr(self.player, "is_stopped", False)

        options: list[Option] = []
        for idx, item in enumerate(self.player.queue):
            is_current = (idx == current_idx) and is_active
            prefix = "▶ " if is_current else f"{idx + 1}. "
            title = item.get("title", "Unknown Title")
            dur = float(item.get("duration") or 0.0)
            dur_str = f" ({format_time(dur)})" if dur > 0 else ""
            uploader = item.get("uploader", "")
            up_str = f" - {uploader}" if uploader and uploader != "Unknown" else ""
            label = f"{prefix}{title}{dur_str}{up_str}"
            options.append(Option(label, id=f"q_{idx}"))

        if options:
            queue_list.add_options(options)
            if 0 <= current_idx < len(options):
                queue_list.highlighted = current_idx

        count = len(self.player.queue)
        header_text = f"📋 Queue ({count} tracks)" if count > 0 else "📋 Queue (Empty)"
        self.query_one("#queue_title", Static).update(header_text)
        # Growing/shrinking the queue moves the thumbnail box; if a native
        # overlay is showing, redraw it at its new position after the layout
        # settles.
        try:
            if (
                self.query_one("#thumbnail_box").display
                and self._native_thumb_path
                and self._native_overlay_available()
            ):
                self.set_timer(0.03, self._deliver_native_thumbnail)
        except Exception:
            pass

    def _update_playback_title(self, text: str) -> None:
        for target_id in ("#playback_title", "#player_track_title"):
            try:
                self.query_one(target_id, Static).update(text)
            except Exception:
                pass

    def _update_player_bar(self) -> None:
        # Status icon
        if self.player.is_playing:
            status_icon = "▶"
        elif self.player.is_paused:
            status_icon = "⏸"
        else:
            status_icon = "⏹"
        try:
            self.query_one("#player_status_icon", Static).update(status_icon)
        except Exception:
            pass

        # Track name
        track_name = self._track_label() or "Idle"
        self._update_playback_title(f"Now Playing: {track_name}")

        # Badges
        shuf_str = "🔀 ON" if getattr(self.player, "shuffle_enabled", False) else "🔀 OFF"
        rep_mode = getattr(self.player, "repeat_mode", "off").upper()
        rep_str = f"🔁 {rep_mode}"
        try:
            self.query_one("#player_badges", Static).update(f"[{shuf_str}] [{rep_str}]")
        except Exception:
            pass

        # Volume bar
        vol = self.player.get_volume()
        muted = self.player.is_muted()
        vol_str = format_volume_bar(vol, muted)
        for target_id in ("#player_volume_bar", "#volume_label"):
            try:
                self.query_one(target_id, Static).update(vol_str)
            except Exception:
                pass

    def _track_label(self) -> Optional[str]:
        if getattr(self.player, "is_stopped", False):
            return None
        title = (self.player.title or "").strip()
        if title.lower().startswith(("http://", "https://", "www.")):
            title = ""
        artist = (self.player.artist or "").strip()
        if title and artist:
            return f"{artist} — {title}"
        if title or artist:
            return title or artist
        curr = self.player.current_track
        if curr:
            q_title = (curr.get("title") or "").strip()
            q_uploader = (curr.get("uploader") or "").strip()
            if q_title.lower().startswith(("http://", "https://", "www.")):
                q_title = ""
            if q_title and q_uploader and q_uploader != "Unknown":
                return f"{q_uploader} — {q_title}"
            return q_title or (q_uploader if q_uploader != "Unknown" else None) or "Track"
        return None

    # Textual Message Event Handlers
    def on_search_results_ready(self, event: SearchResultsReady) -> None:
        self._search_results = event.results
        table = self.query_one("#results_table", DataTable)
        table.clear()
        for idx, item in enumerate(self._search_results):
            title = item.get("title", "Unknown Title")
            dur = float(item.get("duration") or 0.0)
            dur_str = format_time(dur) if dur > 0 else "--:--"
            uploader = item.get("uploader", "Unknown")
            table.add_row(str(idx + 1), title, dur_str, uploader, key=str(idx))
        self.notify(f"Found {len(self._search_results)} items. Select a row to play.")

    def on_track_changed(self, event: TrackChanged) -> None:
        self._refresh_queue_list()
        self._update_player_bar()
        self._request_thumbnail_update(event.track)

    def on_time_updated(self, event: TimeUpdated) -> None:
        if getattr(self.player, "is_stopped", False) or not (self.player.is_playing or self.player.is_paused):
            return
        percent = (event.current / event.total * 100) if event.total > 0 else 0
        time_text = f"{format_time(event.current)} / {format_time(event.total)}"
        try:
            self.query_one("#playback_time", Static).update(time_text)
            self.query_one("#playback_progress", ProgressBar).update(progress=percent)
            if self.player.is_playing:
                self.query_one("#player_status_icon", Static).update("▶")
            elif self.player.is_paused:
                self.query_one("#player_status_icon", Static).update("⏸")
        except Exception:
            pass

    def on_playback_ended(self, event: PlaybackEnded) -> None:
        try:
            self.query_one("#player_status_icon", Static).update("⏹")
        except Exception:
            pass
        self._update_playback_title("Now Playing: Idle")
        try:
            self.query_one("#playback_time", Static).update("00:00 / 00:00")
            self.query_one("#playback_progress", ProgressBar).update(progress=0)
        except Exception:
            pass
        self._refresh_queue_list()

    def on_metadata_updated(self, event: MetadataUpdated) -> None:
        if getattr(self.player, "is_stopped", False) or not (self.player.is_playing or self.player.is_paused):
            return
        track = self._track_label()
        if track:
            self._update_playback_title(f"Now Playing: {track}")

    def on_download_progress(self, event: DownloadProgress) -> None:
        if event.percent >= 100:
            self.notify(f"{event.status_text}", severity="information")
        elif event.status_text.startswith("Error"):
            self.notify(f"{event.status_text}", severity="error")

    def on_thumbnail_ready(self, event: ThumbnailReady) -> None:
        try:
            # Ignore results arriving after the box has been hidden again; the
            # fetch/fallback ran on a worker thread and may complete late.
            if not self.query_one("#thumbnail_box").display:
                return
            if event.native and event.path:
                self._native_thumb_path = event.path
                self._native_thumb_escape = None
                self._native_thumb_key = None
                # Lay the ANSI half-block copy as the base layer first so the box
                # is never empty: it is Textual-rendered and therefore always
                # visible. The crisp native image covers this base when it draws.
                try:
                    base = event.renderable if event.renderable is not None else ""
                    self.query_one("#thumbnail_display", Static).update(base)
                except Exception:
                    pass
                # Wait a beat so the base paint has hit the terminal, then draw
                # the native overlay on top at the live position. The second draw
                # heals sixel frames if the first landed before that paint.
                self.set_timer(0.02, self._deliver_native_thumbnail)
                self.set_timer(0.15, self._deliver_native_thumbnail)
            else:
                # A text placeholder: retire any native overlay first.
                self._clear_native_overlay()
                thumb_display = self.query_one("#thumbnail_display", Static)
                thumb_display.update(event.renderable)
        except Exception:
            pass

    # -- Native (Kitty / Sixel) thumbnail overlay -----------------------------

    def _native_overlay_available(self) -> bool:
        """True when raw driver writes for native graphics are possible."""
        if self._thumbnail_protocol not in ("kitty", "sixel"):
            return False
        driver = getattr(self, "_driver", None)
        return driver is not None and callable(getattr(driver, "write", None))

    def _write_driver_raw(self, data: str) -> bool:
        """Write raw bytes through Textual's driver (thread-safe writer)."""
        try:
            driver = getattr(self, "_driver", None)
            if driver is None or not callable(getattr(driver, "write", None)):
                return False
            driver.write(data)
            return True
        except Exception:
            return False

    def _thumbnail_box_region(self) -> Optional[tuple[int, int, int, int]]:
        """Absolute (row, col, cols, rows) of the thumbnail area, in cells.

        Uses ``#thumbnail_display`` (the 13-row canvas below the panel title).
        ``Widget.region`` is in the compositor's screen space (0-based origin at
        the top-left of the terminal). Returns None if the box is hidden or not
        yet laid out (e.g. during the first frame).
        """
        try:
            box = self.query_one("#thumbnail_box")
            if not box.display:
                return None
            display = self.query_one("#thumbnail_display", Static)
            region = display.region
            if region.width <= 0 or region.height <= 0:
                return None
            return (region.y, region.x, region.width, region.height)
        except Exception:
            return None

    def _native_diag(self, msg: str) -> None:
        """Surface a one-time in-app diagnostic for native overlay failures."""
        try:
            if getattr(self, "_native_diag_printed", False):
                return
            self._native_diag_printed = True
            self.notify(f"Thumbnail overlay: {msg}", severity="warning")
        except Exception:
            pass

    def _deliver_native_thumbnail(self) -> None:
        """Position the terminal cursor over the box and emit the image escape.

        The escape payload is (re)generated only when the video or the box
        geometry changes. The box already holds the ANSI half-block base layer
        from the worker, so a failed native write degrades to that instead of an
        empty box.
        """
        try:
            if not self._native_overlay_available():
                return
            path = self._native_thumb_path
            if not path:
                return
            box = self.query_one("#thumbnail_box")
            if not box.display:
                return
            area = self._thumbnail_box_region()
            if area is None:
                return
            top, left, area_cols, area_rows = area
            protocol = self._thumbnail_protocol or "kitty"
            # Bound the encode size to the canvas (display is 13 rows tall).
            cols = max(8, min(area_cols, 34))
            rows = max(4, min(area_rows, 13))
            key = (path, cols, rows)
            if self._native_thumb_key != key or not self._native_thumb_escape:
                self._native_thumb_escape = render_thumbnail(
                    path, protocol=protocol, cols=cols, rows=rows
                )
                self._native_thumb_key = key
            escape = self._native_thumb_escape
            if not escape:
                self._native_diag("thumbnail overlay produced no escape")
                return
            if escape.lstrip().startswith(("┌", "│")):
                # The native encode degraded to a plain-text frame (image file
                # unreadable, Pillow missing, …). The ANSI base layer already
                # shows the same fallback, so skip the pointless raw write.
                self._native_diag("thumbnail overlay fell back to a text frame")
                return
            # Center the image inside the box content area.
            col = left + max(0, (area_cols - cols) // 2) + 1
            row = top + max(0, (area_rows - rows) // 2) + 1
            ok = self._write_driver_raw(f"\x1b[{row};{col}H{escape}")
            if not ok:
                self._native_diag("native overlay write failed (driver unavailable)")
        except Exception as e:
            self._native_diag(f"native overlay error: {e!r}")

    def _clear_native_overlay(self) -> None:
        """Remove any on-screen native overlay (Kitty images persist)."""
        try:
            self._native_thumb_path = None
            self._native_thumb_escape = None
            self._native_thumb_key = None
            if self._thumbnail_protocol == "kitty" and self._native_overlay_available():
                self._write_driver_raw("\x1b_Ga=d,d=1\x1b\\")
        except Exception:
            pass

    def on_resize(self, event: events.Resize) -> None:
        # Any resize/layout change can shift or repaint the thumbnail rows and
        # erase an overlay; redraw it at the new position.
        try:
            if (
                self.query_one("#thumbnail_box").display
                and self._native_thumb_path
                and self._native_overlay_available()
            ):
                self.set_timer(0.05, self._deliver_native_thumbnail)
        except Exception:
            pass

    # Callback bridges invoked by player
    def _handle_track_change(self, index: int, track: dict[str, Any]) -> None:
        self.post_message(TrackChanged(index, track))

    def _handle_time_update(self, current: float, total: float) -> None:
        self.post_message(TimeUpdated(current, total))

    def handle_playback_end(self) -> None:
        self.post_message(PlaybackEnded())

    def _handle_playback_end(self) -> None:
        self.handle_playback_end()

    def _handle_metadata(self, title: Optional[str], artist: Optional[str]) -> None:
        self.post_message(MetadataUpdated(title, artist))

    # Keybinding Actions
    def action_toggle_pause(self) -> None:
        if self.player.is_playing or self.player.is_paused:
            paused = self.player.toggle_pause()
            self._update_player_bar()
            self.notify("Paused" if paused else "Playing")
        elif len(self.player.queue) > 0:
            video_enabled = self.query_one("#video_toggle", Switch).value
            start_idx = self.player.current_index if (0 <= self.player.current_index < len(self.player.queue)) else 0
            self.player.play_index(start_idx, video_enabled=video_enabled)
            self._update_player_bar()
            self._refresh_queue_list()

    def action_stop(self) -> None:
        self.player.stop()
        self.handle_playback_end()
        self.notify("Playback stopped")

    def action_seek_forward(self) -> None:
        self._seek_relative(5)

    def action_seek_backward(self) -> None:
        self._seek_relative(-5)

    def _seek_relative(self, seconds: float) -> None:
        if getattr(self.player, "is_stopped", False) or not (self.player.is_playing or self.player.is_paused):
            self.notify("Nothing is playing to seek.", severity="warning")
            return
        self.player.seek_relative(seconds)
        self.notify(f"Seek {seconds:+.0f}s")

    def action_volume_up(self) -> None:
        vol = self.player.volume_up(5)
        self._update_player_bar()
        self.notify(f"Volume: {vol}%")

    def action_volume_down(self) -> None:
        vol = self.player.volume_down(5)
        self._update_player_bar()
        self.notify(f"Volume: {vol}%")

    def action_toggle_shuffle(self) -> None:
        shuf = self.player.toggle_shuffle()
        self._update_player_bar()
        self.notify(f"Shuffle: {'ON' if shuf else 'OFF'}")

    def action_toggle_repeat(self) -> None:
        mode = self.player.cycle_repeat_mode()
        self._update_player_bar()
        self.notify(f"Repeat: {mode.upper()}")

    def action_toggle_thumbnail(self) -> None:
        try:
            thumb_box = self.query_one("#thumbnail_box")
            thumb_box.display = not thumb_box.display
            if thumb_box.display:
                caps = detect_terminal_graphics_support()
                if not caps["supported"]:
                    self.notify(
                        "Terminal does not support native inline graphics. Use Kitty, Ghostty, Foot, or WezTerm for full thumbnails.",
                        severity="warning",
                    )
                else:
                    self.notify(f"Thumbnail Preview: ON ({caps['terminal_name']})")
                self._update_highlighted_thumbnail()
                # The reveal paints the box rows blank; give sixel overlays a
                # second chance to be drawn above that paint if it landed late.
                if self._native_overlay_available():
                    self.set_timer(0.15, self._deliver_native_thumbnail)
            else:
                self.notify("Thumbnail Preview: OFF")
                self._clear_thumbnail()
        except Exception:
            pass

    def action_toggle_visualizer(self) -> None:
        try:
            vis_box = self.query_one("#visualizer_box")
            vis_box.display = not vis_box.display
            status = "ON" if vis_box.display else "OFF"
            self.notify(f"Visualizer: {status}")
            if not vis_box.display:
                self._clear_visualizer()
        except Exception:
            pass

    def _request_thumbnail_update(self, item: dict[str, Any]) -> None:
        try:
            thumb_box = self.query_one("#thumbnail_box")
            if not thumb_box.display:
                return
            self.fetch_thumbnail_worker(item)
        except Exception:
            pass

    def _update_highlighted_thumbnail(self) -> None:
        try:
            # Check results table cursor
            table = self.query_one("#results_table", DataTable)
            if table.cursor_row is not None and 0 <= table.cursor_row < len(self._search_results):
                self._request_thumbnail_update(self._search_results[table.cursor_row])
                return

            # Check queue list highlighted
            queue_list = self.query_one("#queue_list", QueueOptionList)
            if queue_list.highlighted is not None and 0 <= queue_list.highlighted < len(self.player.queue):
                self._request_thumbnail_update(self.player.queue[queue_list.highlighted])
                return

            # Check current playing track
            if self.player.current_track:
                self._request_thumbnail_update(self.player.current_track)
        except Exception:
            pass

    def _clear_thumbnail(self) -> None:
        try:
            self._clear_native_overlay()
            thumb_display = self.query_one("#thumbnail_display", Static)
            thumb_display.update("")
        except Exception:
            pass

    @work(thread=True)
    def fetch_thumbnail_worker(self, item: dict[str, Any]) -> None:
        try:
            title = item.get("title", "No Title")
            url = item.get("url", "")
            video_id = item.get("id") or extract_video_id(url)
            custom_thumb_url = item.get("thumbnail")

            if not video_id:
                placeholder = generate_placeholder_thumbnail(title, width=34, height=12)
                self.post_message(ThumbnailReady("", Text.from_ansi(placeholder)))
                return

            img_path = fetch_thumbnail_image(video_id, custom_url=custom_thumb_url)
            if img_path:
                native = self._thumbnail_protocol in ("kitty", "sixel")
                # Always render the ANSI half-block copy as well. Doing it on this
                # worker keeps the UI thread light, and the resulting Text is the
                # guaranteed-visible base layer in #thumbnail_display: it shows no
                # matter whether the native overlay paints. When the native escape
                # does display, it draws crisply on top of this base.
                ansi_str = render_half_block_ansi(img_path, width=34, height=12)
                if native:
                    self.post_message(
                        ThumbnailReady(
                            video_id,
                            Text.from_ansi(ansi_str),
                            native=True,
                            path=str(img_path),
                        )
                    )
                else:
                    self.post_message(ThumbnailReady(video_id, Text.from_ansi(ansi_str)))
            else:
                placeholder = generate_placeholder_thumbnail(title, width=34, height=12)
                self.post_message(ThumbnailReady(video_id, Text.from_ansi(placeholder)))
        except Exception:
            pass

    def _update_visualizer(self) -> None:
        try:
            vis_box = self.query_one("#visualizer_box")
            if not vis_box.display:
                return

            display = self.query_one("#visualizer_display", Static)
            is_playing = self.player.is_playing and not getattr(self.player, "is_stopped", False)

            if is_playing:
                self._render_active_visualizer(display)
            else:
                self._render_idle_visualizer(display)
        except Exception:
            pass

    def _render_active_visualizer(self, display: Static) -> None:
        try:
            import random
            num_bars = 16
            max_height = 5
            if len(self._vis_levels) != num_bars:
                self._vis_levels = [0.0] * num_bars
                self._vis_targets = [0.0] * num_bars

            for i in range(num_bars):
                bias = 1.0 - (i / num_bars) * 0.35
                if random.random() < 0.3 or self._vis_targets[i] <= 0.2:
                    self._vis_targets[i] = random.uniform(0.5, max_height) * bias

                target = self._vis_targets[i]
                current = self._vis_levels[i]
                if current < target:
                    self._vis_levels[i] = min(max_height, current + (target - current) * 0.5 + 0.1)
                else:
                    self._vis_levels[i] = max(0.0, current - 0.3)

            blocks = [" ", " ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
            row_colors = ["[red]", "[yellow]", "[bright_green]", "[cyan]", "[bright_blue]"]
            lines: list[str] = []
            for r in range(max_height - 1, -1, -1):
                row_chars: list[str] = []
                color = row_colors[min(r, len(row_colors) - 1)]
                for val in self._vis_levels:
                    rem = val - r
                    if rem <= 0:
                        char = " "
                    elif rem >= 1.0:
                        char = "█"
                    else:
                        idx = int(rem * 8)
                        char = blocks[max(0, min(8, idx))]
                    row_chars.append(f"{char}{char}")
                lines.append(f"{color}{' '.join(row_chars)}[/]")

            display.update("\n".join(lines))
            self._vis_is_flat = False
        except Exception:
            pass

    def _render_idle_visualizer(self, display: Static) -> None:
        try:
            has_active_levels = any(v > 0.05 for v in self._vis_levels)
            if has_active_levels:
                self._vis_levels = [max(0.0, v - 0.5) for v in self._vis_levels]
                blocks = [" ", " ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
                max_height = 5
                lines: list[str] = []
                for r in range(max_height - 1, -1, -1):
                    row_chars: list[str] = []
                    for val in self._vis_levels:
                        rem = val - r
                        if rem <= 0:
                            char = " "
                        elif rem >= 1.0:
                            char = "█"
                        else:
                            idx = int(rem * 8)
                            char = blocks[max(0, min(8, idx))]
                        row_chars.append(f"{char}{char}")
                    lines.append(f"[dim cyan]{' '.join(row_chars)}[/dim cyan]")
                display.update("\n".join(lines))
                self._vis_is_flat = False
            else:
                if not getattr(self, "_vis_is_flat", False):
                    baseline = " ".join(["▂▂"] * 16)
                    display.update(f"\n\n\n\n[dim cyan]{baseline}[/dim cyan]")
                    self._vis_is_flat = True
        except Exception:
            pass

    def _clear_visualizer(self) -> None:
        try:
            display = self.query_one("#visualizer_display", Static)
            display.update("")
            self._vis_levels = [0.0] * 16
            self._vis_is_flat = False
        except Exception:
            pass

    def action_focus_search(self) -> None:
        self.query_one("#search_input", Input).focus()

    def action_quit(self) -> None:
        self.exit()

    def on_unmount(self) -> None:
        self.player.cleanup()
