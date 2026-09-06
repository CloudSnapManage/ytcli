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
        self._is_loading: bool = False
        self._is_active_playback: bool = False
        self.is_stopped: bool = True
        self._volume: int = 100
        self._muted: bool = False

        # Queue management
        self.queue: list[dict[str, Any]] = []
        self.current_index: int = 0
        self.repeat_mode: str = "off"  # "off", "all", "one"
        self.shuffle_enabled: bool = False
        # When False, playback stops at the end of the current track instead of
        # advancing to the next one in the queue (repeat-mode "one" still loops).
        self.autoplay_next: bool = True

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
                self._is_loading = False
                self._is_active_playback = True
                self.is_stopped = False
            elif is_idle and self._is_active_playback and not self._is_loading:
                if not self.is_stopped:
                    self._handle_track_ended()

        @player.property_observer("time-pos")
        def _time_observer(_name: str, value: Optional[float]) -> None:
            if value is not None and self._on_time_update and self._is_active_playback and not self.is_stopped:
                try:
                    duration = getattr(player, "duration", 0.0) or 0.0
                except Exception:
                    duration = 0.0
                self._on_time_update(value, duration)

        @player.property_observer("media-title")
        def _title_observer(_name: str, value: Optional[str]) -> None:
            self._media_title = value or None
            if self._on_metadata and self._is_active_playback and not self.is_stopped:
                self._on_metadata(self._media_title, self._artist)

        @player.property_observer("metadata")
        def _metadata_observer(_name: str, value: Optional[dict]) -> None:
            md = value if isinstance(value, dict) else {}
            self._artist = md.get("ARTIST") or md.get("Artist") or md.get("artist")
            if self._on_metadata and self._is_active_playback and not self.is_stopped:
                self._on_metadata(self._media_title, self._artist)

        return player

    def _handle_track_ended(self) -> None:
        if not self._is_active_playback or self._is_loading or self.is_stopped:
            return

        if self.repeat_mode == "one" and len(self.queue) > 0:
            self.play_index(self.current_index)
            return

        # Advance to the next track only when auto-advance is enabled; otherwise
        # playback stops here (the repeat-mode "all" wrap is part of advancing).
        if self.autoplay_next and self.play_next():
            return

        self._is_active_playback = False
        self._is_loading = False
        self.is_stopped = True
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

    def snapshot_items(self) -> list[dict[str, Any]]:
        """Return a deep-ish copy of the queue suitable for serialising."""
        return [dict(item) for item in self.queue]

    def load_items(self, items: list[dict[str, Any]]) -> int:
        """Replace the queue with imported tracks. Returns how many were loaded."""
        parsed: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            track = dict(item)
            url = str(track.get("url") or "").strip()
            if not url:
                url = str(track.get("title") or "").strip()
            if not url:
                continue
            track["url"] = url
            track["title"] = str(track.get("title") or url)
            track["duration"] = float(track.get("duration") or 0.0)
            track["uploader"] = str(track.get("uploader") or "Unknown")
            parsed.append(track)
        if parsed:
            self.clear_queue()
            self.queue.extend(parsed)
        return len(parsed)

    def remove_at(self, index: int) -> bool:
        if not (0 <= index < len(self.queue)):
            return False
        self.queue.pop(index)
        if self.current_index > index:
            self.current_index -= 1
        elif self.current_index >= len(self.queue):
            self.current_index = max(0, len(self.queue) - 1)
        return True

    def toggle_shuffle(self) -> bool:
        self.shuffle_enabled = not self.shuffle_enabled
        return self.shuffle_enabled

    def cycle_repeat_mode(self) -> str:
        modes = ["off", "all", "one"]
        curr_idx = modes.index(self.repeat_mode) if self.repeat_mode in modes else 0
        self.repeat_mode = modes[(curr_idx + 1) % len(modes)]
        return self.repeat_mode

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
        if not self.queue:
            return False

        if self.shuffle_enabled and len(self.queue) > 1:
            import random
            available = [i for i in range(len(self.queue)) if i != self.current_index]
            if available:
                return self.play_index(random.choice(available))

        if self.current_index + 1 < len(self.queue):
            return self.play_index(self.current_index + 1)
        elif self.repeat_mode == "all" and len(self.queue) > 0:
            return self.play_index(0)
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

    def close_video(self) -> None:
        """Cleanly terminate the mpv video instance and revert to an audio-only instance."""
        volume = self.get_volume()
        muted = self.is_muted()
        self.stop()
        if self._video_enabled:
            try:
                self.mpv.terminate()
            except Exception:
                pass
            self._video_enabled = False
            self.mpv = self._create_mpv_instance(video_enabled=False)
            self.set_volume(volume)
            if muted:
                try:
                    self.mpv.mute = True
                except Exception:
                    pass
        self.is_stopped = True
        self._is_loading = False
        self._is_active_playback = False
        self._media_title = None
        self._artist = None
        if self._on_end:
            self._on_end()

    def play(self, url: str, video_enabled: bool = False) -> None:
        self.is_stopped = False
        self._is_loading = True
        self._is_active_playback = False

        # Re-initialize instance if video mode changed
        if self._video_enabled != video_enabled:
            volume = self.get_volume()
            muted = self.is_muted()
            self.stop()
            try:
                self.mpv.terminate()
            except Exception:
                pass
            self._video_enabled = video_enabled
            self.mpv = self._create_mpv_instance(video_enabled=video_enabled)
            self.set_volume(volume)
            if muted:
                try:
                    self.mpv.mute = True
                except Exception:
                    pass

        if not video_enabled:
            self.mpv.ytdl_format = "bestaudio/best"
        else:
            self.mpv.ytdl_format = "bestvideo+bestaudio/best"

        # Drop metadata cached from the previous file; observers re-populate it.
        self._media_title = None
        self._artist = None

        try:
            self.mpv.pause = False
        except Exception:
            pass

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
        if self.is_stopped or (not self._is_active_playback and not self._is_loading):
            return False
        try:
            new_state = not bool(getattr(self.mpv, "pause", False))
            self.mpv.pause = new_state
            return new_state
        except Exception:
            return False

    def stop(self) -> None:
        self.is_stopped = True
        self._is_loading = False
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
        self._volume = max(0, min(100, int(level)))
        try:
            self.mpv.volume = self._volume
        except Exception:
            pass
        return self._volume

    def get_volume(self) -> int:
        try:
            val = getattr(self.mpv, "volume", None)
            if val is not None:
                self._volume = max(0, min(100, int(val)))
        except Exception:
            pass
        return self._volume

    def volume_up(self, step: int = 5) -> int:
        return self.set_volume(self.get_volume() + step)

    def volume_down(self, step: int = 5) -> int:
        return self.set_volume(self.get_volume() - step)

    def toggle_mute(self) -> bool:
        self._muted = not self.is_muted()
        try:
            self.mpv.mute = self._muted
        except Exception:
            pass
        return self._muted

    def is_muted(self) -> bool:
        try:
            val = getattr(self.mpv, "mute", None)
            if val is not None:
                self._muted = bool(val)
        except Exception:
            pass
        return self._muted

    @property
    def video_enabled(self) -> bool:
        return self._video_enabled

    @property
    def is_playing(self) -> bool:
        if self.is_stopped:
            return False
        if self._is_loading:
            return True
        if not self._is_active_playback:
            return False
        try:
            return not bool(getattr(self.mpv, "pause", False)) and not bool(getattr(self.mpv, "idle_active", False))
        except Exception:
            return False

    @property
    def is_paused(self) -> bool:
        if self.is_stopped or (not self._is_active_playback and not self._is_loading):
            return False
        try:
            return bool(getattr(self.mpv, "pause", False))
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
        self.is_stopped = True
        self._is_loading = False
        self._is_active_playback = False
        try:
            self.mpv.terminate()
        except Exception:
            pass
