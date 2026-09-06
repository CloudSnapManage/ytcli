"""Headless verification for the ytcli playback changes.

Checks:
  1. OSC / native-input options (osc, osd-bar, input-default-bindings,
     input-vo-keyboard) and keep_open="no" are passed to mpv.MPV when instances are
     created.
  2. The real YTPlayerApp (StreamPlayer + YtDownloader) mounts headlessly, the
     volume label syncs to the real player volume, and the up/down/m/arrow
     TUI bindings dispatch to the right actions.
  3. handle_playback_end cleanly resets playback title to "Now Playing: Idle",
     time to "00:00 / 00:00", and progress to 0.
"""
import asyncio

import mpv as _mpv_module
from yt_downloader import YtDownloader
from player import StreamPlayer
from ui import YTPlayerApp

OSC_KEYS = ("osc", "osd_bar", "input_default_bindings", "input_vo_keyboard")


def _check_video_osc_options() -> None:
    captured: list[dict] = []
    instances: list = []
    orig_mpv = _mpv_module.MPV

    class RecordingMPV:
        def __init__(self, *args, **kwargs):
            captured.append(kwargs)
            self._inner = orig_mpv(*args, **kwargs)
            instances.append(self._inner)

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __setattr__(self, name, value):
            if name.startswith("_"):
                object.__setattr__(self, name, value)
            else:
                setattr(self._inner, name, value)

    _mpv_module.MPV = RecordingMPV
    try:
        audio = StreamPlayer()
        audio._create_mpv_instance(video_enabled=True)  # video-mode instance
    finally:
        _mpv_module.MPV = orig_mpv

    for inst in instances:
        try:
            inst.terminate()
        except Exception:
            pass

    for kw in captured:
        assert kw.get("keep_open") == "no", f"keep_open not 'no' in kwargs: {kw}"

    video_kw = captured[-1]
    for key in OSC_KEYS:
        assert video_kw.get(key) is True, f"{key} not set in mpv.MPV kwargs: {video_kw}"
    assert video_kw.get("vid") == "auto"
    print(f"OSC & keep_open options OK (video kwargs: {video_kw})")


async def _check_ui() -> None:
    downloader = YtDownloader(download_dir="./downloads")
    player = StreamPlayer()
    app = YTPlayerApp(downloader=downloader, player=player)

    async with app.run_test() as pilot:
        volume_label = app.query_one("#volume_label")
        playback_title = app.query_one("#playback_title")
        playback_time = app.query_one("#playback_time")
        playback_progress = app.query_one("#playback_progress")
        initial = int(player.get_volume())

        def _label_text(lbl) -> str:
            return str(getattr(lbl, "content", None) or getattr(lbl, "renderable", None) or lbl.visual)

        # on_mount should have synced the label to the real player volume.
        assert f"Vol: {initial}%" in _label_text(volume_label), _label_text(volume_label)

        # Move focus off the URL Input so printable keys (m) and arrows are not
        # consumed by text editing; Buttons only handle space/enter.
        app.query_one("#btn_stream").focus()
        await pilot.pause()

        # Volume up clamps at 100.
        for _ in range(10):
            await pilot.press("up")
        assert player.get_volume() == 100
        assert "Vol: 100%" in _label_text(volume_label), _label_text(volume_label)

        # Volume down steps by 5.
        await pilot.press("down")
        assert player.get_volume() == 95
        assert "Vol: 95%" in _label_text(volume_label), _label_text(volume_label)

        # Mute toggles the label suffix.
        await pilot.press("m")
        assert player.is_muted()
        assert "Muted" in _label_text(volume_label), _label_text(volume_label)
        await pilot.press("m")
        assert not player.is_muted()

        # Seek with nothing loaded should be a harmless no-op + warning notify,
        # and must not raise.
        await pilot.press("left")
        await pilot.press("right")

        # Test handle_playback_end
        app.handle_playback_end()
        await pilot.pause()
        assert "Now Playing: Idle" in _label_text(playback_title), _label_text(playback_title)
        assert "00:00 / 00:00" in _label_text(playback_time), _label_text(playback_time)
        assert playback_progress.progress == 0

        print("UI SMOKE OK")

    # App teardown (on_unmount) already terminated the player via cleanup().


def main() -> None:
    _check_video_osc_options()
    asyncio.run(_check_ui())


if __name__ == "__main__":
    main()
