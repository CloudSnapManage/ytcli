import threading
from typing import Any, Optional, Callable
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
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
)
from textual import work
from yt_downloader import YtDownloader
from player import StreamPlayer

def format_time(seconds: float) -> str:
    mins, secs = divmod(int(seconds), 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"

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

class YTPlayerApp(App):
    CSS = """
    Screen {
        layout: vertical;
        overflow-y: auto;
        padding: 1;
    }
    .box {
        border: round $primary;
        padding: 1;
        margin-bottom: 1;
    }
    .section-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    .control-row {
        height: auto;
        align-vertical: middle;
        margin-top: 1;
        margin-bottom: 1;
    }
    .control-row > * {
        margin-right: 2;
    }
    .status-row {
        height: auto;
        align-vertical: middle;
        margin-top: 1;
        margin-bottom: 1;
    }
    .status-row > * {
        margin-right: 2;
    }
    .status-row > #playback_time {
        width: 1fr;
    }
    #url_input {
        width: 100%;
        margin-bottom: 1;
    }
    #quality_select {
        width: 35;
    }
    #playback_progress, #download_progress {
        width: 100%;
        margin-top: 1;
    }
    #queue_table {
        height: 8;
        width: 100%;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("space", "toggle_pause", "Play/Pause"),
        ("s", "stop_playback", "Stop"),
        ("left", "seek_backward", "Seek -10s"),
        ("right", "seek_forward", "Seek +10s"),
        ("up", "volume_up", "Volume +5%"),
        ("down", "volume_down", "Volume -5%"),
        ("m", "toggle_mute", "Mute"),
        ("n", "next_track", "Next Track"),
        ("p", "previous_track", "Prev Track"),
        ("c", "clear_queue", "Clear Queue"),
    ]

    def __init__(self, downloader: YtDownloader, player: StreamPlayer, **kwargs: Any):
        super().__init__(**kwargs)
        self.downloader = downloader
        self.player = player
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
        self._update_volume_label()
        table = self.query_one("#queue_table", DataTable)
        table.add_columns("#", "Title", "Duration", "Artist / Uploader")

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(classes="box"):
            yield Static("🔗 Media Source & Controls", classes="section-title")
            yield Input(placeholder="Paste YouTube or YouTube Music URL...", id="url_input")
            with Horizontal(classes="control-row"):
                yield Label("Video Mode:")
                yield Switch(id="video_toggle", value=False)
                yield Label("Format:")
                preset_options = [(p["label"], p["id"]) for p in self.downloader.get_preset_formats()]
                yield Select(options=preset_options, value="best_video", id="quality_select", allow_blank=False)
                yield Button("Stream Now", variant="primary", id="btn_stream")
                yield Button("Download", variant="success", id="btn_download")

        with Container(classes="box"):
            yield Static("🎵 Active Playback", classes="section-title")
            yield Label("Now Playing: Idle", id="playback_title")
            with Horizontal(classes="status-row"):
                yield Label("00:00 / 00:00", id="playback_time")
                yield Label("Vol: 100%", id="volume_label")
            yield ProgressBar(id="playback_progress", total=100, show_eta=False)

        with Container(classes="box"):
            yield Static("📋 Playback Queue", classes="section-title")
            yield DataTable(id="queue_table", cursor_type="row")

        with Container(classes="box"):
            yield Static("⬇️ Download Queue", classes="section-title")
            yield Label("Status: Idle", id="download_status")
            yield ProgressBar(id="download_progress", total=100, show_eta=True)

        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        url = self.query_one("#url_input", Input).value.strip()
        if not url:
            self.notify("Please enter a valid YouTube URL.", severity="warning")
            return

        if event.button.id == "btn_stream":
            self.action_stream(url)
        elif event.button.id == "btn_download":
            self.action_download(url)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        try:
            row_idx = int(str(event.row_key.value))
            video_enabled = self.query_one("#video_toggle", Switch).value
            self.player.play_index(row_idx, video_enabled=video_enabled)
            self._refresh_queue_table()
        except Exception:
            pass

    def action_stream(self, url: str) -> None:
        video_enabled = self.query_one("#video_toggle", Switch).value
        mode_text = "Video" if video_enabled else "Audio Only"
        self.query_one("#playback_title", Label).update(f"Loading stream ({mode_text})...")
        self.stream_worker(url, video_enabled)

    @work(thread=True)
    def stream_worker(self, url: str, video_enabled: bool) -> None:
        try:
            items = self.downloader.extract_playlist_items(url)
            if not items:
                items = [{"title": "Media Stream", "url": url, "duration": 0.0, "uploader": "Unknown"}]

            def apply_stream() -> None:
                self.player.clear_queue()
                for item in items:
                    self.player.add_to_queue(item)
                self.player.play_index(0, video_enabled=video_enabled)
                self._refresh_queue_table()
                if len(items) > 1:
                    self.notify(f"Loaded playlist ({len(items)} tracks)")
                else:
                    self.notify("Streaming track")

            self._run_on_ui_thread(apply_stream)
        except Exception as e:
            def fallback_stream() -> None:
                self.player.clear_queue()
                self.player.add_to_queue({"title": url, "url": url, "duration": 0.0, "uploader": "Unknown"})
                self.player.play_index(0, video_enabled=video_enabled)
                self._refresh_queue_table()
                self.notify(f"Direct stream: {e}", severity="warning")

            self._run_on_ui_thread(fallback_stream)

    def action_download(self, url: str) -> None:
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

    def _refresh_queue_table(self) -> None:
        table = self.query_one("#queue_table", DataTable)
        table.clear()
        for idx, item in enumerate(self.player.queue):
            is_current = (idx == self.player.current_index) and (self.player.is_playing or self.player.is_paused)
            prefix = "▶ " if is_current else f"{idx + 1}."
            title = item.get("title", "Unknown Title")
            dur = float(item.get("duration") or 0.0)
            dur_str = format_time(dur) if dur > 0 else "--:--"
            uploader = item.get("uploader", "Unknown")
            table.add_row(prefix, title, dur_str, uploader, key=str(idx))

    # Event Handlers (Textual Message Bus)
    def on_track_changed(self, event: TrackChanged) -> None:
        self._refresh_queue_table()
        title = event.track.get("title") or "Streaming Media"
        uploader = event.track.get("uploader") or ""
        display_name = f"{uploader} — {title}" if uploader and uploader != "Unknown" else title
        self.query_one("#playback_title", Label).update(f"Now Playing: {display_name}")

    def on_time_updated(self, event: TimeUpdated) -> None:
        if not (self.player.is_playing or self.player.is_paused):
            return
        percent = (event.current / event.total * 100) if event.total > 0 else 0
        time_text = f"{format_time(event.current)} / {format_time(event.total)}"
        track = self._track_label() or "Streaming Media"
        self.query_one("#playback_title", Label).update(f"Now Playing: {track}")
        self.query_one("#playback_time", Label).update(time_text)
        self.query_one("#playback_progress", ProgressBar).update(progress=percent)

    def on_playback_ended(self, event: PlaybackEnded) -> None:
        self.query_one("#playback_title", Label).update("Now Playing: Idle")
        self.query_one("#playback_time", Label).update("00:00 / 00:00")
        self.query_one("#playback_progress", ProgressBar).update(progress=0)
        self._refresh_queue_table()

    def on_metadata_updated(self, event: MetadataUpdated) -> None:
        if not (self.player.is_playing or self.player.is_paused):
            return
        track = self._track_label()
        if track:
            self.query_one("#playback_title", Label).update(f"Now Playing: {track}")

    def on_download_progress(self, event: DownloadProgress) -> None:
        self.query_one("#download_status", Label).update(f"Status: {event.status_text}")
        self.query_one("#download_progress", ProgressBar).update(progress=event.percent)

    # Callback bridges invoked by player / downloader
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

    def _track_label(self) -> Optional[str]:
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
            q_title = curr.get("title") or ""
            q_uploader = curr.get("uploader") or ""
            if q_title and q_uploader and q_uploader != "Unknown":
                return f"{q_uploader} — {q_title}"
            return q_title or q_uploader or None
        return None

    def action_toggle_pause(self) -> None:
        if self.player.is_playing or self.player.is_paused:
            paused = self.player.toggle_pause()
            status = "Paused" if paused else "Playing"
            self.notify(f"Playback {status}")

    def action_stop_playback(self) -> None:
        self.player.stop()
        self.handle_playback_end()
        self.notify("Playback stopped")

    def action_seek_forward(self) -> None:
        self._seek_relative(10)

    def action_seek_backward(self) -> None:
        self._seek_relative(-10)

    def _seek_relative(self, seconds: float) -> None:
        if not (self.player.is_playing or self.player.is_paused):
            self.notify("Nothing is playing to seek.", severity="warning")
            return
        self.player.seek_relative(seconds)
        self.notify(f"Seek {seconds:+.0f}s")

    def action_volume_up(self) -> None:
        self.player.volume_up()
        self._update_volume_label()

    def action_volume_down(self) -> None:
        self.player.volume_down()
        self._update_volume_label()

    def action_toggle_mute(self) -> None:
        muted = self.player.toggle_mute()
        self._update_volume_label()
        self.notify("Muted" if muted else "Unmuted")

    def action_next_track(self) -> None:
        if self.player.play_next():
            self._refresh_queue_table()
            self.notify("Skipped to next track")
        else:
            self.notify("End of queue reached", severity="information")

    def action_previous_track(self) -> None:
        if self.player.play_previous():
            self._refresh_queue_table()
            self.notify("Replaying previous track")
        else:
            self.notify("Start of queue reached", severity="information")

    def action_clear_queue(self) -> None:
        self.player.clear_queue()
        self._refresh_queue_table()
        self.notify("Playback queue cleared")

    def _update_volume_label(self) -> None:
        level = self.player.get_volume()
        muted = self.player.is_muted()
        text = f"Vol: {level}%" if not muted else f"Vol: {level}% (Muted)"
        self.query_one("#volume_label", Label).update(text)

    def on_unmount(self) -> None:
        self.player.cleanup()
