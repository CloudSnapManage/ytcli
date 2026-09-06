"""Headless verification for the ytcli playback and queue changes.

Checks:
  1. OSC / native-input options (osc, osd-bar, input-default-bindings,
     input-vo-keyboard) and keep_open="no" are passed to mpv.MPV when instances are
     created.
  2. YtDownloader.extract_playlist_items parses playlist metadata and returns
     formatted track dicts.
  3. StreamPlayer queue management: add_to_queue, play_index, play_next,
     play_previous, clear_queue, and auto-advance on track end.
  4. The real YTPlayerApp (StreamPlayer + YtDownloader) mounts headlessly,
     DataTable for playback queue is populated, volume label syncs, and
     TUI bindings (up, down, m, n, p, c) dispatch to the right actions.
  5. handle_playback_end cleanly resets playback title to "Now Playing: Idle",
     time to "00:00 / 00:00", progress to 0, and refreshes queue table.
"""
import asyncio
from unittest.mock import MagicMock, patch

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


def _check_downloader_extract_playlist_items() -> None:
    downloader = YtDownloader()

    # Mock yt_dlp.YoutubeDL to test extract_playlist_items without network calls
    mock_playlist_info = {
        "id": "PL123",
        "title": "My Playlist",
        "entries": [
            {
                "id": "vid1",
                "title": "Song 1",
                "url": "https://www.youtube.com/watch?v=vid1",
                "duration": 185.0,
                "uploader": "Artist A",
            },
            {
                "id": "vid2",
                "title": "Song 2",
                "url": None,
                "duration": 210.0,
                "uploader": "Artist B",
            },
        ],
    }

    with patch("yt_dlp.YoutubeDL") as mock_ydl_cls:
        mock_ydl_instance = MagicMock()
        mock_ydl_cls.return_value.__enter__.return_value = mock_ydl_instance
        mock_ydl_instance.extract_info.return_value = mock_playlist_info

        items = downloader.extract_playlist_items("https://www.youtube.com/playlist?list=PL123")
        assert len(items) == 2, f"Expected 2 items, got {len(items)}"
        assert items[0]["title"] == "Song 1"
        assert items[0]["url"] == "https://www.youtube.com/watch?v=vid1"
        assert items[0]["duration"] == 185.0
        assert items[0]["uploader"] == "Artist A"

        # Item 2 url synthesized from id
        assert items[1]["title"] == "Song 2"
        assert items[1]["url"] == "https://www.youtube.com/watch?v=vid2"
        assert items[1]["duration"] == 210.0
        assert items[1]["uploader"] == "Artist B"

    print("Downloader extract_playlist_items OK")


def _check_player_queue() -> None:
    player = StreamPlayer()
    try:
        assert player.queue == []
        assert player.current_index == 0

        # Add items
        player.add_to_queue({"title": "Track 1", "url": "http://localhost/1", "duration": 100, "uploader": "A1"})
        player.add_to_queue("http://localhost/2")
        assert len(player.queue) == 2
        assert player.queue[1]["title"] == "http://localhost/2"

        # Play index (mocking mpv.play so we don't open sockets)
        with patch.object(player.mpv, "play") as mock_play:
            assert player.play_index(0) is True
            mock_play.assert_called_with("http://localhost/1")
            assert player.current_index == 0
            assert player.current_track["title"] == "Track 1"

            # Play next
            assert player.play_next() is True
            mock_play.assert_called_with("http://localhost/2")
            assert player.current_index == 1

            # End of queue next -> False
            assert player.play_next() is False

            # Play previous
            assert player.play_previous() is True
            assert player.current_index == 0
            mock_play.assert_called_with("http://localhost/1")

            # Play previous when at 0 replays current index
            assert player.play_previous() is True
            assert player.current_index == 0

        # Auto-advance test via _handle_track_ended
        player.current_index = 0
        player._is_active_playback = True
        with patch.object(player, "play_next", wraps=player.play_next) as mock_next:
            with patch.object(player.mpv, "play"):
                player._handle_track_ended()
                assert mock_next.called
                assert player.current_index == 1

        # Clear queue
        player.clear_queue()
        assert player.queue == []
        assert player.current_index == 0
        assert player.current_track is None

    finally:
        player.cleanup()

    print("Player queue methods & auto-advance OK")


async def _check_ui() -> None:
    downloader = YtDownloader(download_dir="./downloads")
    player = StreamPlayer()
    app = YTPlayerApp(downloader=downloader, player=player)

    async with app.run_test() as pilot:
        volume_label = app.query_one("#volume_label")
        playback_title = app.query_one("#playback_title")
        playback_time = app.query_one("#playback_time")
        playback_progress = app.query_one("#playback_progress")
        queue_table = app.query_one("#queue_table")
        initial = int(player.get_volume())

        def _label_text(lbl) -> str:
            return str(getattr(lbl, "content", None) or getattr(lbl, "renderable", None) or lbl.visual)

        # on_mount should have synced the label to the real player volume.
        assert f"Vol: {initial}%" in _label_text(volume_label), _label_text(volume_label)

        # Move focus off the URL Input so printable keys and arrows are not
        # consumed by text editing.
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

        # Queue table populated check
        player.add_to_queue({"title": "Test Song A", "url": "http://a", "duration": 120.0, "uploader": "Artist A"})
        player.add_to_queue({"title": "Test Song B", "url": "http://b", "duration": 240.0, "uploader": "Artist B"})
        app._refresh_queue_table()
        await pilot.pause()
        assert queue_table.row_count == 2

        # Press 'n' (Next Track)
        with patch.object(player.mpv, "play"):
            player.current_index = 0
            await pilot.press("n")
            assert player.current_index == 1

            # Press 'p' (Prev Track)
            await pilot.press("p")
            assert player.current_index == 0

        # Press 'c' (Clear Queue)
        await pilot.press("c")
        assert len(player.queue) == 0
        assert queue_table.row_count == 0

        # Test handle_playback_end
        app.handle_playback_end()
        await pilot.pause()
        assert "Now Playing: Idle" in _label_text(playback_title), _label_text(playback_title)
        assert "00:00 / 00:00" in _label_text(playback_time), _label_text(playback_time)
        assert playback_progress.progress == 0

        print("UI SMOKE & QUEUE BINDINGS OK")


def main() -> None:
    _check_video_osc_options()
    _check_downloader_extract_playlist_items()
    _check_player_queue()
    asyncio.run(_check_ui())


if __name__ == "__main__":
    main()
