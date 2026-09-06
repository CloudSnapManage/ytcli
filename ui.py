import json
import os
import threading
import time
from typing import Any, Optional, Callable
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.binding import Binding
from textual.message import Message
from textual.screen import ModalScreen
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
import config
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


class ConfigLoaded(Message):
    """Settings successfully read from disk (delivered on the UI thread)."""

    def __init__(self, settings: dict[str, Any]) -> None:
        super().__init__()
        self.settings = settings


class QueueImportReady(Message):
    """A playlist file was parsed off-thread; items are ready to load."""

    def __init__(self, items: list[dict[str, Any]], source: str) -> None:
        super().__init__()
        self.items = items
        self.source = source


# -- Queue serialisation helpers (pure, callable from worker threads) --------

def _queue_to_json(items: list[dict[str, Any]]) -> str:
    return json.dumps(items, indent=2, ensure_ascii=False)


def _queue_to_m3u(items: list[dict[str, Any]]) -> str:
    lines = ["#EXTM3U"]
    for item in items:
        dur = int(float(item.get("duration") or 0.0))
        title = str(item.get("title") or "Unknown Title").replace("\n", " ").replace("\r", "")
        lines.append(f"#EXTINF:{dur},{title}")
        lines.append(str(item.get("url") or ""))
    return "\n".join(lines) + "\n"


def _parse_m3u_text(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    title = ""
    duration = 0.0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#EXTM3U"):
            continue
        if line.startswith("#EXTINF:"):
            meta = line[len("#EXTINF:") :]
            if "," in meta:
                dur_part, title_part = meta.split(",", 1)
                try:
                    duration = max(0.0, float(dur_part.strip()))
                except ValueError:
                    duration = 0.0
                title = title_part.strip()
            continue
        if line.startswith("#"):
            continue
        url = line
        if url:
            items.append(
                {
                    "title": title or url,
                    "url": url,
                    "duration": duration,
                    "uploader": "Unknown",
                }
            )
            title = ""
            duration = 0.0
    return items


class SettingsScreen(ModalScreen):
    """Modal settings panel. ``dismiss()`` returns the edited settings dict
    (or ``None`` when cancelled)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    CSS = """
    #settings_panel {
        width: 78;
        max-height: 90%;
        padding: 1 2;
        background: $surface;
        border: heavy $primary;
        margin: 1 4;
        layer: modal;
    }
    #settings_panel .s-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #settings_panel .s-caption {
        color: $text-muted;
        margin-top: 1;
        margin-bottom: 1;
    }
    #settings_panel .s-row {
        height: auto;
        width: 100%;
        layout: horizontal;
        align-vertical: middle;
        margin-bottom: 1;
    }
    #settings_panel .s-row Label {
        width: 34;
        min-width: 34;
        text-style: bold;
    }
    #settings_panel Select,
    #settings_panel Input {
        width: 1fr;
    }
    #settings_actions {
        height: auto;
        width: 100%;
        layout: horizontal;
        align-horizontal: right;
        margin-top: 1;
    }
    #settings_actions Button {
        margin-left: 1;
    }
    """

    def __init__(
        self,
        settings: dict[str, Any],
        preset_options: list[tuple[str, str]],
    ) -> None:
        super().__init__()
        self._settings = settings
        self._preset_options = preset_options or []
        preset_ids = [pid for (_label, pid) in self._preset_options]
        count = int(settings.get("search_results_count", 10))
        self._init_count = count if count in (5, 10, 20, 50) else 10
        fmt = str(settings.get("default_format", "best_video"))
        self._init_format = fmt if fmt in preset_ids else (preset_ids[0] if preset_ids else "best_video")
        toast = float(settings.get("toast_duration", 3.0))
        self._init_toast = toast if toast in (1, 2, 3, 5, 8) else 3.0

    def _collect(self) -> dict[str, Any]:
        startup = dict(self._settings.get("startup") or {})
        startup["thumbnails"] = self.query_one("#set_thumb", Switch).value
        startup["visualizer"] = self.query_one("#set_vis", Switch).value
        startup["autoplay_next"] = self.query_one("#set_autoplay", Switch).value
        fmt = str(self.query_one("#set_format", Select).value)
        preset_ids = [pid for (_label, pid) in self._preset_options]
        return {
            "search_results_count": int(self.query_one("#set_count", Select).value),
            "default_format": fmt if fmt in preset_ids else "best_video",
            "download_dir": str(self.query_one("#set_dir", Input).value).strip(),
            "startup": startup,
            "toast_duration": float(self.query_one("#set_toast", Select).value),
            "search_history": list(self._settings.get("search_history") or []),
        }

    def compose(self) -> ComposeResult:
        startup = self._settings.get("startup") or {}
        with Vertical(id="settings_panel"):
            yield Static("⚙ Settings", classes="s-title")
            with Horizontal(classes="s-row"):
                yield Label("Search results per query")
                yield Select(
                    options=[(f"{n} results", n) for n in (5, 10, 20, 50)],
                    value=self._init_count,
                    id="set_count",
                    allow_blank=False,
                )
            with Horizontal(classes="s-row"):
                yield Label("Default media format")
                yield Select(
                    options=self._preset_options,
                    value=self._init_format,
                    id="set_format",
                    allow_blank=False,
                )
            with Horizontal(classes="s-row"):
                yield Label("Download output directory")
                yield Input(value=str(self._settings.get("download_dir", "")), id="set_dir")
            yield Static("Startup toggles (applied when the app starts)", classes="s-caption")
            with Horizontal(classes="s-row"):
                yield Label("Start with thumbnails visible")
                yield Switch(value=bool(startup.get("thumbnails", False)), id="set_thumb")
            with Horizontal(classes="s-row"):
                yield Label("Start with visualizer visible")
                yield Switch(value=bool(startup.get("visualizer", False)), id="set_vis")
            with Horizontal(classes="s-row"):
                yield Label("Auto-play next track")
                yield Switch(value=bool(startup.get("autoplay_next", True)), id="set_autoplay")
            with Horizontal(classes="s-row"):
                yield Label("Toast notification duration")
                yield Select(
                    options=[(f"{n}s", float(n)) for n in (1, 2, 3, 5, 8)],
                    value=self._init_toast,
                    id="set_toast",
                    allow_blank=False,
                )
            with Horizontal(id="settings_actions"):
                yield Button("Save", variant="primary", id="btn_save_settings")
                yield Button("Cancel", id="btn_cancel_settings")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_save_settings":
            try:
                self.dismiss(self._collect())
            except Exception as e:
                self.notify(f"Settings error: {e}", severity="error")
        elif event.button.id == "btn_cancel_settings":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


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
    #results_stats {
        height: auto;
        width: 100%;
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
        margin-bottom: 1;
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
        Binding("x", "stop", "Stop", priority=True),
        Binding("left", "seek_backward", "Seek -5s", priority=True),
        Binding("right", "seek_forward", "Seek +5s", priority=True),
        # Volume Up/Down are deliberately NOT bound here: bound arrows would
        # swallow the key before a focused DataTable/Input/OptionList can use it
        # for navigation. Volume is handled in on_key only when no such widget
        # owns the key.
        Binding("s", "toggle_shuffle", "Shuffle", priority=True),
        Binding("r", "toggle_repeat", "Repeat", priority=True),
        Binding("t", "toggle_thumbnail", "Thumbnail", priority=True),
        Binding("v", "toggle_visualizer", "Visualizer", priority=True),
        Binding("c", "close_video", "Close Video", priority=True),
        Binding("escape", "focus_search", "Search", priority=True),
        Binding("q", "quit", "Quit", priority=True),
        # Multi-selection & batch actions (search results table). Space is
        # contextual: selecting a highlighted row when the results table is
        # focused, play/pause everywhere else.
        Binding("space", "space_action", "Play/Pause or Select Row", priority=True),
        Binding("m", "toggle_mark_current", "Select Row"),
        Binding("ctrl+a", "select_all_rows", "Select All"),
        Binding("u", "unselect_all_rows", "Clear Selection"),
        Binding("ctrl+d", "unselect_all_rows", "Clear Selection"),
        Binding("a", "add_marked_to_queue", "Add Selected"),
        Binding("d", "download_selected", "Download Selected"),
        # Power features.
        Binding("S", "toggle_settings", "Settings"),
        Binding("ctrl+s", "toggle_settings", "Settings", show=False),
        Binding("ctrl+e", "export_queue", "Export Queue"),
        Binding("ctrl+i", "import_queue", "Import Queue"),
    ]

    def __init__(
        self,
        downloader: YtDownloader,
        player: StreamPlayer,
        load_config_file: bool = True,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.downloader = downloader
        self.player = player
        # Tests pass load_config_file=False so they never read/write the real
        # user configuration and startup toggles stay deterministic.
        self._load_config_file = load_config_file
        # Settings, asynchronously merged with ~/.config/ytcli/config.json.
        self._settings: dict[str, Any] = config.default_config()
        self._search_count: int = int(self._settings.get("search_results_count", 10))
        self._toast_duration: Optional[float] = float(self._settings.get("toast_duration", 3.0))
        self._search_history: list[str] = list(self._settings.get("search_history") or [])
        self._hist_index: Optional[int] = None
        # Multi-selection state over the current search results (row indexes).
        self._marked: set[int] = set()
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

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: str = "information",
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        """Honor the configured toast duration unless a caller overrides it."""
        if timeout is None:
            timeout = getattr(self, "_toast_duration", None)
        super().notify(message, title=title, severity=severity, timeout=timeout, **kwargs)

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
        table.add_column("#", width=6, key="col_num")
        table.add_column("Title", key="col_title")
        table.add_column("Duration", width=10, key="col_duration")
        table.add_column("Artist / Uploader", key="col_uploader")
        detected = detect_terminal_graphics_support().get("protocol")
        # iTerm2's proprietary inline-image OSC is intentionally left on the
        # ANSI fallback (it does not overlay cleanly inside a cell-based TUI).
        self._thumbnail_protocol = detected if detected in ("kitty", "sixel") else None
        self._update_player_bar()
        self._refresh_queue_list()
        self._update_results_stats()
        # Persisted settings are loaded off the UI thread; defaults already
        # apply, so the app is usable immediately either way.
        if self._load_config_file:
            self.config_load_worker()
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
                    preset_ids = [pid for (_label, pid) in preset_options]
                    default_fmt = self._settings.get("default_format", "best_video")
                    if default_fmt not in preset_ids and preset_ids:
                        default_fmt = preset_ids[0]
                    yield Select(options=preset_options, value=default_fmt, id="quality_select", allow_blank=False)
                    yield Button("Search", variant="primary", id="btn_search")
                    yield Button("Add All", variant="default", id="btn_add_all")
                    yield Button("Download", variant="success", id="btn_download")
                yield Static("", id="results_stats")
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
            elif focused.id == "search_input" and event.key in ("up", "down"):
                delta = -1 if event.key == "up" else 1
                self._cycle_search_history(delta)
                event.prevent_default()
                event.stop()
            return

        if event.key == "space":
            self.action_space_action()
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
        elif event.key in ("up", "down") and not isinstance(focused, (DataTable, OptionList, Select, Input)):
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
        count = int(getattr(self, "_search_count", 10) or 10)
        self.search_worker(query, count)

    @work(thread=True)
    def search_worker(self, query: str, count: int = 10) -> None:
        try:
            url_or_query = query
            if not (query.startswith("http://") or query.startswith("https://") or query.startswith("ytsearch")):
                url_or_query = f"ytsearch{max(1, min(100, count))}:{query}"

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

    # -- Search results: rendering & multi-selection --------------------------

    def _refresh_results_table(self) -> None:
        """Rebuild the results DataTable, preserving the highlighted row."""
        try:
            table = self.query_one("#results_table", DataTable)
            cursor = table.cursor_row
            if not isinstance(cursor, int) or not (0 <= cursor < len(self._search_results)):
                cursor = None
            table.clear()
            for idx, item in enumerate(self._search_results):
                title = str(item.get("title", "Unknown Title")).replace("\r", " ").replace("\n", " ").strip()
                dur = float(item.get("duration") or 0.0)
                dur_str = format_time(dur) if dur > 0 else "--:--"
                uploader = str(item.get("uploader", "Unknown")).replace("\r", " ").replace("\n", " ").strip()
                check = "✓ " if idx in self._marked else ""
                table.add_row(f"{check}{idx + 1}", title, dur_str, uploader, key=str(idx))
            if cursor is not None and 0 <= cursor < len(self._search_results):
                table.move_cursor(row=cursor, animate=False)
        except Exception:
            pass
        self._update_results_stats()

    def _update_results_stats(self) -> None:
        try:
            total = len(self._search_results)
            marked = len(self._marked)
            if not total:
                text = "No results yet"
            elif marked:
                text = f"Selected: {marked} / {total} tracks"
            else:
                text = f"{total} results — highlight a row, press space/m to select"
            self.query_one("#results_stats", Static).update(text)
        except Exception:
            pass

    def _marked_indexes(self) -> list[int]:
        return sorted(i for i in self._marked if 0 <= i < len(self._search_results))

    def _selected_items(self) -> list[dict[str, Any]]:
        return [self._search_results[i] for i in self._marked_indexes()]

    def action_toggle_mark_current(self) -> None:
        table = self.query_one("#results_table", DataTable)
        row = table.cursor_row
        if not isinstance(row, int) or not (0 <= row < len(self._search_results)):
            self.notify("No highlighted search row to select.", severity="warning")
            return
        if row in self._marked:
            self._marked.discard(row)
        else:
            self._marked.add(row)
        self._refresh_results_table()

    def action_select_all_rows(self) -> None:
        self._marked = set(range(len(self._search_results)))
        self._refresh_results_table()
        self.notify(f"Selected {len(self._marked)} of {len(self._search_results)} rows.")

    def action_unselect_all_rows(self) -> None:
        count = len(self._marked)
        self._marked = set()
        self._refresh_results_table()
        if count:
            self.notify("Cleared search selection.")

    def action_space_action(self) -> None:
        """Space: select on the search results table, play/pause elsewhere."""
        focused = self.focused
        if isinstance(focused, DataTable) and focused.id == "results_table":
            self.action_toggle_mark_current()
            return
        self.action_toggle_pause()

    # -- Batch actions -------------------------------------------------------

    def action_add_marked_to_queue(self) -> None:
        """Append every selected search row to the playback queue."""
        items = self._selected_items()
        if not items:
            self.notify("Nothing selected. Highlight rows and press space/m first.", severity="warning")
            return
        was_empty = len(self.player.queue) == 0
        for item in items:
            self.player.add_to_queue(item)
        if was_empty:
            try:
                video_enabled = self.query_one("#video_toggle", Switch).value
                self.player.play_index(0, video_enabled=video_enabled)
            except Exception:
                pass
        self._refresh_queue_list()
        self._update_player_bar()
        self.notify(f"Added {len(items)} selected track(s) to the queue.")

    def _pick_download_urls(self) -> list[str]:
        """Selected rows, else the highlighted row, else the typed URL."""
        urls = [str(it.get("url") or "") for it in self._selected_items()]
        urls = [u for u in urls if u]
        if urls:
            return urls
        try:
            table = self.query_one("#results_table", DataTable)
            row = table.cursor_row
            if isinstance(row, int) and 0 <= row < len(self._search_results):
                url = str(self._search_results[row].get("url") or "")
                if url:
                    return [url]
        except Exception:
            pass
        url = self.query_one("#search_input", Input).value.strip()
        if url:
            return [url]
        return []

    def action_download_selected(self) -> None:
        self._action_download_selected()

    def _action_download_selected(self) -> None:
        """Download selected rows (falling back to the highlighted row / URL).

        One @work(thread=True) worker per track keeps the event loop free.
        """
        urls = self._pick_download_urls()
        if not urls:
            self.notify("Select search rows (space/m) or enter a URL to download.", severity="warning")
            return
        preset_id = str(self.query_one("#quality_select", Select).value)
        if len(urls) > 1:
            self.notify(f"Downloading {len(urls)} track(s) in the background…")
        for url in urls:
            self.download_worker(url, preset_id)

    # -- Settings / config (file I/O always on worker threads) ----------------

    @work(thread=True)
    def config_load_worker(self) -> None:
        try:
            settings = config.load_config()
            self.post_message(ConfigLoaded(settings))
        except Exception:
            pass

    @work(thread=True)
    def config_save_worker(self, settings: dict[str, Any]) -> None:
        try:
            config.save_config(settings)
        except Exception as e:
            self._run_on_ui_thread(self.notify, f"Could not save settings: {e}", severity="error")

    def on_config_loaded(self, event: ConfigLoaded) -> None:
        try:
            self._apply_settings(event.settings, startup=True)
        except Exception:
            pass

    def _apply_settings(self, settings: dict[str, Any], startup: bool = False) -> None:
        """Merge ``settings`` and apply the actionable parts.

        With ``startup=True`` (boot config load) the thumbnail/visualizer boxes
        are set to their saved startup states; an in-app save persists those
        toggles but leaves the boxes as the user currently has them.
        """
        merged = dict(self._settings)
        merged.update(dict(settings))
        self._settings = merged
        try:
            self._search_count = int(merged.get("search_results_count", 10))
        except (TypeError, ValueError):
            self._search_count = 10
        self._search_history = list(merged.get("search_history") or [])
        try:
            self._toast_duration = float(merged.get("toast_duration", 3.0))
        except (TypeError, ValueError):
            self._toast_duration = 3.0

        try:
            default_fmt = str(merged.get("default_format", "best_video"))
            preset_ids = [p["id"] for p in self.downloader.get_preset_formats()]
            if default_fmt not in preset_ids and preset_ids:
                default_fmt = preset_ids[0]
            self.query_one("#quality_select", Select).value = default_fmt
        except Exception:
            pass

        out_dir = os.path.expanduser(str(merged.get("download_dir") or ""))
        if out_dir:
            try:
                self.downloader.set_download_dir(out_dir)
            except Exception:
                pass

        startup_cfg = merged.get("startup") or {}
        if startup:
            try:
                self.query_one("#thumbnail_box").display = bool(startup_cfg.get("thumbnails", False))
                self.query_one("#visualizer_box").display = bool(startup_cfg.get("visualizer", False))
                if bool(startup_cfg.get("thumbnails", False)):
                    self._update_highlighted_thumbnail()
            except Exception:
                pass

        try:
            self.player.autoplay_next = bool(startup_cfg.get("autoplay_next", True))
        except Exception:
            pass

        self._update_results_stats()

    # -- Search history -------------------------------------------------------

    def _remember_search(self) -> None:
        try:
            query = self.query_one("#search_input", Input).value.strip()
        except Exception:
            query = ""
        if not query:
            return
        hist = list(self._search_history)
        if hist and hist[-1] == query:
            return
        if query in hist:
            hist.remove(query)
        hist.append(query)
        self._search_history = hist[-config.MAX_HISTORY :]
        self._hist_index = None
        if not self._load_config_file:
            return
        settings = dict(self._settings)
        settings["search_history"] = list(self._search_history)
        self.config_save_worker(settings)

    def _cycle_search_history(self, direction: int) -> None:
        hist = self._search_history
        if not hist:
            self.notify("No previous searches yet.", severity="warning")
            return
        if self._hist_index is None:
            self._hist_index = len(hist)
        target = (self._hist_index or 0) + direction
        if target < 0:
            target = 0
        if target >= len(hist):
            self._hist_index = None  # past the newest entry: back to typed text
            return
        self._hist_index = target
        query = hist[target]
        try:
            input_box = self.query_one("#search_input", Input)
            input_box.value = query
            input_box.cursor_position = len(query)
        except Exception:
            pass

    # -- Queue export / import ------------------------------------------------

    def action_export_queue(self) -> None:
        items = self.player.snapshot_items()
        if not items:
            self.notify("Queue is empty — nothing to export.", severity="warning")
            return
        out_dir = os.path.expanduser(str(self._settings.get("download_dir") or "")) or os.getcwd()
        self.export_queue_worker(items, out_dir)

    @work(thread=True)
    def export_queue_worker(self, items: list[dict[str, Any]], out_dir: str) -> None:
        try:
            os.makedirs(out_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            base = os.path.join(out_dir, f"{config.QUEUE_EXPORT_PREFIX}{stamp}")
            with open(base + ".json", "w", encoding="utf-8") as fh:
                fh.write(_queue_to_json(items))
            with open(base + ".m3u", "w", encoding="utf-8") as fh:
                fh.write(_queue_to_m3u(items))
            self._run_on_ui_thread(
                self.notify,
                f"Exported {len(items)} track(s) → {base}.json",
                severity="information",
            )
        except Exception as e:
            self._run_on_ui_thread(self.notify, f"Export failed: {e}", severity="error")

    def action_import_queue(self) -> None:
        in_dir = os.path.expanduser(str(self._settings.get("download_dir") or "")) or os.getcwd()
        self.import_queue_worker(in_dir)

    @work(thread=True)
    def import_queue_worker(self, in_dir: str) -> None:
        try:
            os.makedirs(in_dir, exist_ok=True)
            candidates: list[str] = []
            for fname in os.listdir(in_dir):
                if fname.startswith(config.QUEUE_EXPORT_PREFIX):
                    candidates.append(os.path.join(in_dir, fname))
            candidates.sort(key=os.path.getmtime, reverse=True)
            if not candidates:
                self._run_on_ui_thread(
                    self.notify,
                    "No ytcli-queue-* files found in the download directory.",
                    severity="warning",
                )
                return
            chosen = candidates[0]
            with open(chosen, "r", encoding="utf-8") as fh:
                text = fh.read()
            ext = os.path.splitext(chosen)[1].lower()
            if ext == ".m3u":
                items = _parse_m3u_text(text)
            else:
                try:
                    data = json.loads(text)
                    items = data if isinstance(data, list) else (data.get("items") if isinstance(data, dict) else [])
                except json.JSONDecodeError:
                    items = _parse_m3u_text(text)
            if not isinstance(items, list):
                items = []
            self.post_message(QueueImportReady(items, chosen))
        except Exception as e:
            self._run_on_ui_thread(self.notify, f"Import failed: {e}", severity="error")

    def on_queue_import_ready(self, event: QueueImportReady) -> None:
        try:
            if not event.items:
                self.notify(f"No playable entries in {os.path.basename(event.source)}.", severity="warning")
                return
            was_active = self.player.is_playing or self.player.is_paused
            count = self.player.load_items(event.items)
            if was_active:
                self.player.stop()
            self._refresh_queue_list()
            self._update_player_bar()
            self.notify(f"Imported {count} track(s) from {os.path.basename(event.source)}.")
        except Exception as e:
            self.notify(f"Import error: {e}", severity="error")

    # -- Settings modal -------------------------------------------------------

    def action_toggle_settings(self) -> None:
        try:
            preset_options = [(p["label"], p["id"]) for p in self.downloader.get_preset_formats()]
            self.push_screen(SettingsScreen(self._settings, preset_options), self._on_settings_dismissed)
        except Exception as e:
            self.notify(f"Could not open settings: {e}", severity="error")

    def _on_settings_dismissed(self, result: Optional[dict[str, Any]]) -> None:
        if not isinstance(result, dict):
            return
        try:
            self._apply_settings(result, startup=False)
            settings = dict(self._settings)
            settings["search_history"] = list(self._search_history)
            self.config_save_worker(settings)
            self.notify("Settings saved.", severity="information")
        except Exception as e:
            self.notify(f"Could not apply settings: {e}", severity="error")

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
        # A fresh result set invalidates marks tied to the previous rows.
        self._marked = set()
        # Remember the query (debounced history write) before rebuilding.
        self._remember_search()
        self._refresh_results_table()
        self.notify(f"Found {len(self._search_results)} items. Highlight a row to play.")

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
        if isinstance(self.focused, (DataTable, Input)):
            return
        vol = self.player.volume_up(5)
        self._update_player_bar()
        self.notify(f"Volume: {vol}%")

    def action_volume_down(self) -> None:
        if isinstance(self.focused, (DataTable, Input)):
            return
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
                return

            self._render_idle_thumbnail()
        except Exception:
            pass

    def _render_idle_thumbnail(self) -> None:
        try:
            self._clear_native_overlay()
            thumb_box = self.query_one("#thumbnail_box")
            if not thumb_box.display:
                return
            placeholder = generate_placeholder_thumbnail("No Media Selected", width=34, height=12)
            display = self.query_one("#thumbnail_display", Static)
            display.update(Text.from_ansi(placeholder))
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
                    display.update(f"[dim]Visualizer Idle[/dim]\n\n\n[dim cyan]{baseline}[/dim cyan]")
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
        # Persist current settings (and any search-history additions) when
        # leaving. Done synchronously because a worker spawned here would not be
        # guaranteed to finish before the process exits. Skipped when the app was
        # started without config I/O (tests).
        if self._load_config_file:
            try:
                settings = dict(self._settings)
                settings["search_history"] = list(self._search_history)
                config.save_config(settings)
            except Exception:
                pass
