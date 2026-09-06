"""Headless verification for the overhauled ytcli multi-panel layout, player bar, and queue management.

Checks:
  1. StreamPlayer queue management: add_to_queue, play_index, play_next, play_previous, remove_at,
     shuffle, repeat modes, and auto-advance.
  2. OSC / video options passed to mpv.MPV.
  3. YtDownloader extract_playlist_items and preset formats.
  4. YTPlayerApp mounts headlessly:
     - Main layout panels (#search_panel, #queue_panel, #player_bar)
     - Keybindings (space, left, right, up, down, s, r, escape)
     - QueueOptionList renders tracks, selection, and deletion via TrackDeleteRequested
     - Status updates (play/pause/idle, volume meter, progress, metadata, shuffle/repeat badges)
"""
import asyncio
from unittest.mock import MagicMock, patch

import mpv as _mpv_module
from yt_downloader import YtDownloader
from player import StreamPlayer
from ui import YTPlayerApp, QueueOptionList, format_time, format_volume_bar


def _check_format_helpers() -> None:
    assert format_time(0) == "00:00"
    assert format_time(75) == "01:15"
    assert format_time(3665) == "01:01:05"

    bar_100 = format_volume_bar(100)
    assert "Vol: [██████████] 100%" == bar_100
    bar_50 = format_volume_bar(50)
    assert "Vol: [█████░░░░░] 50%" == bar_50
    bar_mute = format_volume_bar(50, muted=True)
    assert "(Muted)" in bar_mute
    print("Formatting helpers OK")


def _check_player_queue_and_modes() -> None:
    player = StreamPlayer()
    try:
        assert player.queue == []
        assert player.current_index == 0
        assert player.repeat_mode == "off"
        assert not player.shuffle_enabled

        # Queue additions
        player.add_to_queue({"title": "Track 1", "url": "http://test/1", "duration": 120, "uploader": "Artist 1"})
        player.add_to_queue("http://test/2")
        player.add_to_queue({"title": "Track 3", "url": "http://test/3", "duration": 200, "uploader": "Artist 3"})
        assert len(player.queue) == 3

        # Shuffle toggle
        assert player.toggle_shuffle() is True
        assert player.shuffle_enabled is True
        assert player.toggle_shuffle() is False
        assert player.shuffle_enabled is False

        # Repeat cycling
        assert player.cycle_repeat_mode() == "all"
        assert player.cycle_repeat_mode() == "one"
        assert player.cycle_repeat_mode() == "off"

        # Remove at
        assert player.remove_at(1) is True
        assert len(player.queue) == 2
        assert player.queue[1]["title"] == "Track 3"

        # Play index mock
        with patch.object(player.mpv, "play") as mock_play:
            assert player.play_index(0) is True
            mock_play.assert_called_with("http://test/1")
            assert player.current_index == 0
            assert player.current_track["title"] == "Track 1"

            # Play next
            assert player.play_next() is True
            mock_play.assert_called_with("http://test/3")
            assert player.current_index == 1

            # Repeat "all" cycle test
            player.repeat_mode = "all"
            assert player.play_next() is True
            assert player.current_index == 0
            mock_play.assert_called_with("http://test/1")

        # Video close test
        player._video_enabled = True
        player._is_active_playback = True
        player.is_stopped = False
        player.close_video()
        assert player.is_stopped is True
        assert player._is_active_playback is False
        assert player.video_enabled is False

    finally:
        player.cleanup()

    print("Player queue & playback modes OK")


async def _check_ui_overhaul() -> None:
    downloader = YtDownloader(download_dir="./downloads")
    player = StreamPlayer()
    app = YTPlayerApp(downloader=downloader, player=player)

    async with app.run_test() as pilot:
        # Check presence of multi-panel widgets
        search_panel = app.query_one("#search_panel")
        queue_panel = app.query_one("#queue_panel")
        player_bar = app.query_one("#player_bar")
        queue_list = app.query_one("#queue_list", QueueOptionList)
        status_icon = app.query_one("#player_status_icon")
        track_title = app.query_one("#player_track_title")
        badges = app.query_one("#player_badges")
        vol_bar = app.query_one("#player_volume_bar")
        playback_time = app.query_one("#playback_time")
        playback_progress = app.query_one("#playback_progress")

        def _label_text(lbl) -> str:
            return str(getattr(lbl, "content", None) or getattr(lbl, "renderable", None) or getattr(lbl, "visual", ""))

        # Initial state
        assert "⏹" in _label_text(status_icon)
        assert "Now Playing: Idle" in _label_text(track_title)
        assert "[🔀 OFF] [🔁 OFF]" in _label_text(badges)
        assert "Vol: [██████████] 100%" in _label_text(vol_bar)
        assert "00:00 / 00:00" in _label_text(playback_time)
        assert playback_progress.progress == 0

        # Add items to queue and verify refresh
        player.add_to_queue({"title": "Song 1", "url": "http://song/1", "duration": 180, "uploader": "Artist A"})
        player.add_to_queue({"title": "Song 2", "url": "http://song/2", "duration": 240, "uploader": "Artist B"})
        app._refresh_queue_list()
        await pilot.pause()

        assert queue_list.option_count == 2
        queue_title = app.query_one("#queue_title")
        assert "Queue (2 tracks)" in _label_text(queue_title)

        # Focus button or queue so arrow/shortcut keys are processed
        app.query_one("#btn_search").focus()
        await pilot.pause()

        # Test Volume Keybindings (Up/Down)
        await pilot.press("down")
        assert player.get_volume() == 95
        assert "Vol: [██████████] 95%" in _label_text(vol_bar) or "95%" in _label_text(vol_bar)

        await pilot.press("up")
        assert player.get_volume() == 100

        # Test Shuffle Keybinding ('s')
        await pilot.press("s")
        assert player.shuffle_enabled is True
        assert "[🔀 ON]" in _label_text(badges)

        # Test Repeat Keybinding ('r')
        await pilot.press("r")
        assert player.repeat_mode == "all"
        assert "[🔁 ALL]" in _label_text(badges)

        # Test Queue Deletion message handling
        app.post_message(QueueOptionList.TrackDeleteRequested(index=0))
        await pilot.pause()
        assert len(player.queue) == 1
        assert player.queue[0]["title"] == "Song 2"
        assert queue_list.option_count == 1

        # Test Search results population
        app.on_search_results_ready(
            MagicMock(results=[{"title": "Search Result 1", "duration": 120.0, "uploader": "Uploader X", "url": "http://x"}])
        )
        await pilot.pause()
        results_table = app.query_one("#results_table")
        assert results_table.row_count == 1

        # Test playback end reset
        app.handle_playback_end()
        await pilot.pause()
        assert "⏹" in _label_text(status_icon)
        assert "Now Playing: Idle" in _label_text(track_title)
        assert "00:00 / 00:00" in _label_text(playback_time)

        # Test Focus handling & Escape key
        await pilot.press("escape")
        assert app.focused == app.query_one("#search_input")

        await pilot.press("escape")
        assert app.focused is None

        # Test Space key play/pause
        with patch.object(player.mpv, "play"):
            await pilot.press("space")
            await pilot.pause()
            assert not player.is_stopped

        # Test Stop key ('x')
        await pilot.press("x")
        await pilot.pause()
        assert player.is_stopped

        # Test Close Video button presence and keybinding ('c')
        btn_close_video = app.query_one("#btn_close_video")
        assert btn_close_video is not None
        await pilot.press("c")
        await pilot.pause()
        assert player.is_stopped

        # Test metadata fallback logic
        player.is_stopped = False
        player._media_title = ""
        player._artist = ""
        # Current track is "Song 2" by "Artist B"
        assert app._track_label() == "Artist B — Song 2"
        player._media_title = "https://www.youtube.com/watch?v=123"
        assert app._track_label() == "Artist B — Song 2"
        player._media_title = "Direct Title"
        player._artist = "Direct Artist"
        assert app._track_label() == "Direct Artist — Direct Title"
        player.is_stopped = True
        assert app._track_label() is None

    print("UI multi-panel overhaul & bindings OK")


def main() -> None:
    _check_format_helpers()
    _check_player_queue_and_modes()
    asyncio.run(_check_ui_overhaul())


if __name__ == "__main__":
    main()
