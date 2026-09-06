import argparse
import sys
from yt_downloader import YtDownloader
from player import StreamPlayer
from ui import YTPlayerApp

def main() -> None:
    parser = argparse.ArgumentParser(
        description="yt-cli-player: Terminal-based YouTube/YT Music Player & Downloader"
    )
    parser.add_argument(
        "-c",
        "--cookies",
        type=str,
        default=None,
        help="Browser name to extract cookies from (e.g. chrome, firefox, brave, edge)",
    )
    parser.add_argument(
        "-d",
        "--dir",
        type=str,
        default="./downloads",
        help="Directory to save downloaded media files (default: ./downloads)",
    )

    args = parser.parse_args()

    try:
        downloader = YtDownloader(browser_cookies=args.cookies, download_dir=args.dir)
        player = StreamPlayer(browser_cookies=args.cookies)
        app = YTPlayerApp(downloader=downloader, player=player)
        app.run()
    except KeyboardInterrupt:
        sys.exit(0)

if __name__ == "__main__":
    main()
