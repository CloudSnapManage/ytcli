import os
from typing import Callable, Any, Optional
import yt_dlp

class YtDownloader:
    def __init__(self, browser_cookies: Optional[str] = None, download_dir: str = "./downloads"):
        self.browser_cookies = browser_cookies
        self.download_dir = download_dir
        os.makedirs(self.download_dir, exist_ok=True)

    def _get_base_opts(self) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "outtmpl": os.path.join(self.download_dir, "%(title)s.%(ext)s"),
        }
        if self.browser_cookies:
            opts["cookiesfrombrowser"] = (self.browser_cookies,)
        return opts

    def extract_info(self, url: str) -> dict[str, Any]:
        opts = self._get_base_opts()
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    def extract_playlist_items(self, url: str) -> list[dict[str, Any]]:
        """Parse YouTube/YT Music playlist or video URLs using extract_flat=True.

        Returns a list of track metadata dictionaries with keys:
        ``title``, ``url``, ``duration``, and ``uploader``.
        """
        opts = self._get_base_opts()
        opts["extract_flat"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}

        raw_entries = info.get("entries")
        if raw_entries is not None:
            entries = list(raw_entries)
        else:
            entries = [info]

        items: list[dict[str, Any]] = []
        for entry in entries:
            if not entry:
                continue
            entry_url = entry.get("url") or entry.get("webpage_url")
            video_id = entry.get("id")
            if not entry_url or not str(entry_url).startswith("http"):
                if video_id:
                    entry_url = f"https://www.youtube.com/watch?v={video_id}"
                else:
                    entry_url = url

            title = entry.get("title") or "Unknown Title"
            duration = float(entry.get("duration") or 0.0)
            uploader = entry.get("uploader") or entry.get("channel") or entry.get("artist") or "Unknown"
            thumb = entry.get("thumbnail") or (f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else "")

            items.append({
                "title": title,
                "url": entry_url,
                "duration": duration,
                "uploader": uploader,
                "id": video_id or "",
                "thumbnail": thumb,
            })

        return items

    def get_preset_formats(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "best_video",
                "label": "Best Video + Best Audio (MP4/MKV)",
                "format": "bestvideo+bestaudio/best",
                "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
            },
            {
                "id": "1080p",
                "label": "1080p Video (MP4)",
                "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
                "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
            },
            {
                "id": "720p",
                "label": "720p Video (MP4)",
                "format": "bestvideo[height<=720]+bestaudio/best[height<=720]",
                "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
            },
            {
                "id": "mp3",
                "label": "Audio Only - MP3 (High Quality)",
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            },
            {
                "id": "flac",
                "label": "Audio Only - FLAC (Lossless)",
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "flac",
                    }
                ],
            },
        ]

    def download(
        self,
        url: str,
        preset_id: str = "best_video",
        progress_hook: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> dict[str, Any]:
        opts = self._get_base_opts()

        preset = next((p for p in self.get_preset_formats() if p["id"] == preset_id), None)
        if preset:
            opts["format"] = preset["format"]
            if "postprocessors" in preset:
                opts["postprocessors"] = preset["postprocessors"]
        else:
            opts["format"] = preset_id

        if progress_hook:
            opts["progress_hooks"] = [progress_hook]

        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)
