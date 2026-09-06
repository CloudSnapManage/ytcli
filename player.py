from typing import Callable, Optional
import mpv

class StreamPlayer:
    def __init__(self, browser_cookies: Optional[str] = None):
        self.browser_cookies = browser_cookies
        self._on_time_update: Optional[Callable[[float, float], None]] = None
        self._on_end: Optional[Callable[[], None]] = None
        self._on_metadata: Optional[Callable[[Optional[str], Optional[str]], None]] = None
        self._media_title: Optional[str] = None
        self._artist: Optional[str] = None
        self._video_enabled: bool = False
        self._is_active_playback: bool = False
        self.mpv = self._create_mpv_instance(video_enabled=False)

    def _create_mpv_instance(self, video_enabled: bool = False) -> mpv.MPV:
        opts: dict[str, str | bool | int] = {
            "ytdl": True,
            "vid": "auto" if video_enabled else "no",
            "vo": "gpu,x11,wl,null" if video_enabled else "null",
            # Automatically stop playback when the stream ends or window closes
            "keep_open": "no",
            # Native On-Screen Controller + windowed input (takes effect when a
            # windowed VO is present, i.e. video_enabled=True). mpv's default
            # window keybindings then drive the same pause/volume/seek state the
            # terminal TUI controls, so the two stay in sync.
            "osc": True,
            "osd_bar": True,
            "input_default_bindings": True,
            "input_vo_keyboard": True,
        }
        if self.browser_cookies:
            opts["ytdl_raw_options"] = f"cookies-from-browser={self.browser_cookies}"

        player = mpv.MPV(**opts)

        @player.property_observer("idle-active")
        def _idle_observer(_name: str, is_idle: Optional[bool]) -> None:
            if not is_idle:
                self._is_active_playback = True
            elif is_idle and self._is_active_playback:
                self._is_active_playback = False
                self._media_title = None
                self._artist = None
                if self._on_end:
                    self._on_end()

        @player.property_observer("time-pos")
        def _time_observer(_name: str, value: Optional[float]) -> None:
            if value is not None and self._on_time_update and self._is_active_playback:
                try:
                    duration = getattr(player, "duration", 0.0) or 0.0
                except Exception:
                    duration = 0.0
                self._on_time_update(value, duration)

        @player.property_observer("media-title")
        def _title_observer(_name: str, value: Optional[str]) -> None:
            self._media_title = value or None
            if self._on_metadata and self._is_active_playback:
                self._on_metadata(self._media_title, self._artist)

        @player.property_observer("metadata")
        def _metadata_observer(_name: str, value: Optional[dict]) -> None:
            md = value if isinstance(value, dict) else {}
            self._artist = md.get("ARTIST") or md.get("Artist") or md.get("artist")
            if self._on_metadata and self._is_active_playback:
                self._on_metadata(self._media_title, self._artist)

        @player.event_callback("end-file")
        def _end_observer(_event: dict) -> None:
            if self._is_active_playback:
                self._is_active_playback = False
                self._media_title = None
                self._artist = None
                if self._on_end:
                    self._on_end()

        return player

    def set_callbacks(
        self,
        on_time_update: Optional[Callable[[float, float], None]] = None,
        on_end: Optional[Callable[[], None]] = None,
        on_metadata: Optional[Callable[[Optional[str], Optional[str]], None]] = None,
    ) -> None:
        self._on_time_update = on_time_update
        self._on_end = on_end
        self._on_metadata = on_metadata

    def play(self, url: str, video_enabled: bool = False) -> None:
        # Re-initialize instance if video mode changed
        if self._video_enabled != video_enabled:
            volume = self.get_volume()
            muted = self.is_muted()
            self.stop()
            self.mpv.terminate()
            self._video_enabled = video_enabled
            self.mpv = self._create_mpv_instance(video_enabled=video_enabled)
            self.set_volume(volume)
            if muted:
                self.mpv.mute = True

        if not video_enabled:
            self.mpv.ytdl_format = "bestaudio/best"
        else:
            self.mpv.ytdl_format = "bestvideo+bestaudio/best"

        # Drop metadata cached from the previous file; observers re-populate it.
        self._media_title = None
        self._artist = None
        self._is_active_playback = True

        self.mpv.play(url)

    def pause(self) -> None:
        try:
            self.mpv.pause = True
        except Exception:
            pass

    def resume(self) -> None:
        try:
            self.mpv.pause = False
        except Exception:
            pass

    def toggle_pause(self) -> bool:
        try:
            new_state = not bool(self.mpv.pause)
            self.mpv.pause = new_state
            return new_state
        except Exception:
            return False

    def stop(self) -> None:
        self._is_active_playback = False
        self._media_title = None
        self._artist = None
        try:
            self.mpv.command("stop")
        except Exception:
            pass

    def seek_relative(self, seconds: float) -> None:
        """Jump forward (``seconds`` > 0) or backward (``seconds`` < 0)."""
        try:
            self.mpv.seek(seconds)  # relative keyframe seek, matching mpv's default seek
        except Exception:
            pass

    def set_volume(self, level: int) -> int:
        level = max(0, min(100, int(level)))
        try:
            self.mpv.volume = level
        except Exception:
            pass
        return level

    def get_volume(self) -> int:
        try:
            return int(getattr(self.mpv, "volume", 100) or 100)
        except Exception:
            return 100

    def volume_up(self, step: int = 5) -> int:
        return self.set_volume(self.get_volume() + step)

    def volume_down(self, step: int = 5) -> int:
        return self.set_volume(self.get_volume() - step)

    def toggle_mute(self) -> bool:
        muted = not self.is_muted()
        try:
            self.mpv.mute = muted
        except Exception:
            pass
        return muted

    def is_muted(self) -> bool:
        try:
            return bool(getattr(self.mpv, "mute", False))
        except Exception:
            return False

    @property
    def video_enabled(self) -> bool:
        return self._video_enabled

    @property
    def is_playing(self) -> bool:
        try:
            return bool(not self.mpv.core_idle and not self.mpv.pause and self._is_active_playback)
        except Exception:
            return False

    @property
    def is_paused(self) -> bool:
        try:
            return bool(self.mpv.pause and self._is_active_playback)
        except Exception:
            return False

    @property
    def title(self) -> Optional[str]:
        """Latest ``media-title`` reported by mpv (track/video title)."""
        return self._media_title

    @property
    def artist(self) -> Optional[str]:
        """Latest artist tag from file metadata (``None`` when untagged)."""
        return self._artist

    @property
    def duration(self) -> float:
        try:
            return float(getattr(self.mpv, "duration", 0.0) or 0.0)
        except Exception:
            return 0.0

    @property
    def time_pos(self) -> float:
        try:
            return float(getattr(self.mpv, "time_pos", 0.0) or 0.0)
        except Exception:
            return 0.0

    def cleanup(self) -> None:
        self._is_active_playback = False
        try:
            self.mpv.terminate()
        except Exception:
            pass
