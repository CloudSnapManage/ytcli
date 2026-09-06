from typing import Any, Optional
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Header, Footer, Input, Button, Label, Select, Switch, ProgressBar, Static
from textual import work
from yt_downloader import YtDownloader
from player import StreamPlayer

def format_time(seconds: float) -> str:
    mins, secs = divmod(int(seconds), 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"

class YTPlayerApp(App):
    CSS = """
    Screen {
        layout: vertical;
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
    ]

    def __init__(self, downloader: YtDownloader, player: StreamPlayer, **kwargs: Any):
        super().__init__(**kwargs)
        self.downloader = downloader
        self.player = player
        self.player.set_callbacks(
            on_time_update=self._handle_time_update,
            on_end=self.handle_playback_end,
            on_metadata=self._handle_metadata,
        )

    def on_mount(self) -> None:
        self._update_volume_label()

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

    def action_stream(self, url: str) -> None:
        video_enabled = self.query_one("#video_toggle", Switch).value
        mode_text = "Video" if video_enabled else "Audio Only"
        self.query_one("#playback_title", Label).update(f"Loading stream ({mode_text})...")
        self.player.play(url, video_enabled=video_enabled)

    def action_download(self, url: str) -> None:
        preset_id = str(self.query_one("#quality_select", Select).value)
        self.download_worker(url, preset_id)

    @work(thread=True)
    def download_worker(self, url: str, preset_id: str) -> None:
        self.call_from_thread(self._update_download_status, "Starting download...", 0.0)

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
                self.call_from_thread(self._update_download_status, msg, percent)
            elif status == "finished":
                self.call_from_thread(self._update_download_status, "Processing audio/video...", 100.0)

        try:
            self.downloader.download(url, preset_id=preset_id, progress_hook=progress_hook)
            self.call_from_thread(self._update_download_status, "Download Complete!", 100.0)
            self.call_from_thread(self.notify, "Download finished successfully!", severity="information")
        except Exception as e:
            self.call_from_thread(self._update_download_status, f"Error: {e}", 0.0)
            self.call_from_thread(self.notify, f"Download failed: {e}", severity="error")

    def _update_download_status(self, text: str, percent: float) -> None:
        self.query_one("#download_status", Label).update(f"Status: {text}")
        bar = self.query_one("#download_progress", ProgressBar)
        bar.update(progress=percent)

    def _handle_time_update(self, current: float, total: float) -> None:
        if not (self.player.is_playing or self.player.is_paused):
            return
        percent = (current / total * 100) if total > 0 else 0
        time_text = f"{format_time(current)} / {format_time(total)}"
        track = self._track_label() or "Streaming Media"

        def update_ui() -> None:
            if not (self.player.is_playing or self.player.is_paused):
                return
            self.query_one("#playback_title", Label).update(f"Now Playing: {track}")
            self.query_one("#playback_time", Label).update(time_text)
            self.query_one("#playback_progress", ProgressBar).update(progress=percent)

        self.call_from_thread(update_ui)

    def handle_playback_end(self) -> None:
        def reset_ui() -> None:
            self.query_one("#playback_title", Label).update("Now Playing: Idle")
            self.query_one("#playback_time", Label).update("00:00 / 00:00")
            self.query_one("#playback_progress", ProgressBar).update(progress=0)

        self.call_from_thread(reset_ui)

    def _handle_playback_end(self) -> None:
        self.handle_playback_end()

    def _handle_metadata(self, _title: Optional[str], _artist: Optional[str]) -> None:
        if not (self.player.is_playing or self.player.is_paused):
            return
        track = self._track_label()
        if not track:
            return

        def update_ui() -> None:
            if not (self.player.is_playing or self.player.is_paused):
                return
            self.query_one("#playback_title", Label).update(f"Now Playing: {track}")

        self.call_from_thread(update_ui)

    def _track_label(self) -> Optional[str]:
        """Human-readable track label (``Artist — Title``) from mpv metadata."""
        title = (self.player.title or "").strip()
        if title.lower().startswith(("http://", "https://", "www.")):
            title = ""
        artist = (self.player.artist or "").strip()
        if title and artist:
            return f"{artist} — {title}"
        return title or artist or None

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

    def _update_volume_label(self) -> None:
        level = self.player.get_volume()
        muted = self.player.is_muted()
        text = f"Vol: {level}%" if not muted else f"Vol: {level}% (Muted)"
        self.query_one("#volume_label", Label).update(text)

    def on_unmount(self) -> None:
        self.player.cleanup()
