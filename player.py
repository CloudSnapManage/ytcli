from typing import Callable, Optional, Any
import mpv

class StreamPlayer:
    def __init__(self, browser_cookies: Optional[str] = None):
        self.browser_cookies = browser_cookies
        self._on_time_update: Optional[Callable[[float, float], None]] = None
        self._on_end: Optional[Callable[[], None]] = None
        self._on_metadata: Optional[Callable[[Optional[str], Optional[str]], None]] = None
        self._on_track_change: Optional[Callable[[int, dict[str, Any]], None]] = None
        self._media_title: Optional[str] = None
        self._artist: Optional[str] = None
        self._video_enabled: bool = False
        self._is_active_playback: bool = False

        # Queue management
        self.queue: list[dict[str, Any]] = []
        self.current_index: int = 0

        self.mpv = self._create_mpv_instance(video_enabled=False)

    def _create_mpv_instance(self, video_enabled: bool = False) -> mpv.MPV:
        opts: dict[str, str | bool | int] = {
            "ytdl": True,
            "vid": "auto" if video_enabled else "no",
            "vo": "gpu,x11,wl,null" if video_enabled else "null",
            # Automatically stop playback when the stream ends or window closes
            "keep_open": "no",
            # Native On-Screen Controller + windowed input
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
                self._handle_track_ended()

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
                self._handle_track_ended()

        return player

    def _handle_track_ended(self) -> None:
        if not self._is_active_playback:
            return

        # Auto-advance to next track in queue if available
        if self.current_index + 1 < len(self.queue):
            self.play_next()
        else:
            self._is_active_playback = False
            self._media_title = None
            self._artist = None
            if self._on_end:
                self._on_end()

    def set_callbacks(
        self,
        on_time_update: Optional[Callable[[float, float], None]] = None,
        on_end: Optional[Callable[[], None]] = None,
        on_metadata: Optional[Callable[[Optional[str], Optional[str]], None]] = None,
        on_track_change: Optional[Callable[[int, dict[str, Any]], None]] = None,
    ) -> None:
        self._on_time_update = on_time_update
        self._on_end = on_end
        self._on_metadata = on_metadata
        self._on_track_change = on_track_change

    # Queue Navigation Methods
    def add_to_queue(self, item: dict[str, Any] | str) -> None:
        if isinstance(item, str):
            item_dict = {"title": item, "url": item, "duration": 0.0, "uploader": "Unknown"}
        else:
            item_dict = dict(item)
        self.queue.append(item_dict)

    def clear_queue(self) -> None:
        self.queue.clear()
        self.current_index = 0

    def play_index(self, index: int, video_enabled: Optional[bool] = None) -> bool:
        if not (0 <= index < len(self.queue)):
            return False

        self.current_index = index
        track = self.queue[index]
        url = track.get("url") or ""
        v_mode = self._video_enabled if video_enabled is None else video_enabled

        self.play(url, video_enabled=v_mode)

        if self._on_track_change:
            self._on_track_change(self.current_index, track)
        return True

    def play_next(self) -> bool:
        if self.current_index + 1 < len(self.queue):
            return self.play_index(self.current_index + 1)
        return False

    def play_previous(self) -> bool:
        if self.current_index > 0 and len(self.queue) > 0:
            return self.play_index(self.current_index - 1)
        elif len(self.queue) > 0:
            return self.play_index(self.current_index)
        return False

    @property
    def current_track(self) -> Optional[dict[str, Any]]:
        if 0 <= self.current_index < len(self.queue):
            return self.queue[self.current_index]
        return None

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
        try:
            self.mpv.seek(seconds)
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
        return self._media_title

    @property
    def artist(self) -> Optional[str]:
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
