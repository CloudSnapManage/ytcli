# ytcli

ytcli is a fast, keyboard-driven YouTube and YouTube Music client and media player TUI (Terminal User Interface) built with Python, Textual, mpv, and yt-dlp. It provides a lightweight terminal environment for searching, streaming, managing queues, and downloading YouTube audio and video without opening a web browser.

## Features

### Search and Query History
- Real-time video and track search powered by yt-dlp.
- Persistent local search history with instant recall using Up and Down arrow keys in the search input box.
- Context-aware key navigation that prevents search input and table scrolling from interfering with global shortcuts.

### Playback and Controls
- Direct streaming playback via mpv.
- Play, pause, seek (+/-5s), volume controls (+/-5%), and hard stop.
- Playback modes including track shuffle and three-state repeat (off, loop all, loop single).
- Dedicated audio visualizer container and terminal thumbnail rendering (ANSI half-block, Kitty, Sixel, and iTerm2 graphics protocols).
- Option to close floating video windows and seamlessly fall back to audio-only playback.

### Batching and Multi-Select
- Select individual rows using Space or `m`.
- Select all search results using `Ctrl+a` or clear selections with `u` / `Ctrl+d`.
- Batch-add marked items to the active playback queue with `a`.
- Batch-download marked items in the background with `d` using configurable quality presets (MP4, MP3, M4A, FLAC).

### Playlist Import and Export
- Snapshot the entire active queue to disk with `Ctrl+e`.
- Generates both portable JSON metadata and standard M3U playlist formats.
- Load previously saved queues automatically with `Ctrl+i`.

### Configuration and Storage
- In-app interactive settings modal accessible via `Shift+S`.
- Configure default download directories, preferred quality presets, result limits, startup panels, and notification durations.
- Atomic JSON persistence to `~/.config/ytcli/config.json` handled via non-blocking worker threads.

## Prerequisites

- Python >= 3.10
- mpv (compiled with libmpv support)
- yt-dlp
- ffmpeg (required for audio extraction and format conversion)

### System Dependencies

#### Arch Linux
```bash
sudo pacman -S python mpv yt-dlp ffmpeg
```

#### Debian / Ubuntu
```bash
sudo apt update
sudo apt install python3 python3-pip libmpv-dev mpv yt-dlp ffmpeg
```

#### macOS (Homebrew)
```bash
brew install python mpv yt-dlp ffmpeg
```

## Installation

### Fast Install (Recommended)

Install globally in an isolated environment using `pipx`:

```bash
pipx install git+https://github.com/CloudSnapManage/ytcli.git
```

Then run the application directly from anywhere in your terminal:

```bash
ytcli
```

### Arch Linux (AUR)

If you are using an AUR helper such as `yay` or `paru`:

```bash
yay -S ytcli
```

or

```bash
paru -S ytcli
```

### Building from Source

1. Clone the repository:
```bash
git clone https://github.com/CloudSnapManage/ytcli.git
cd ytcli
```

2. (Optional) Create and activate a virtual environment:
```bash
python -m venv venv
source venv/bin/activate
```

3. Install the package and dependencies:
```bash
pip install .
```

Alternatively, install in editable mode for development:
```bash
pip install -e .
```

## Usage

Start the player from anywhere in your terminal:

```bash
ytcli
```

Or run directly from the source directory:

```bash
python main.py
```

### Command-Line Options

```text
usage: ytcli [-h] [-c COOKIES] [-d DIR]

yt-cli-player: Terminal-based YouTube/YT Music Player & Downloader

options:
  -h, --help            show this help message and exit
  -c, --cookies COOKIES Browser name to extract cookies from (e.g. chrome, firefox, brave, edge)
  -d, --dir DIR         Directory to save downloaded media files (default: ./downloads)
```

## Keybindings

### Search & Navigation
| Key | Context | Action |
| --- | --- | --- |
| `Escape` | Global / Search | Focus search input box / Clear active focus |
| `Up` / `Down` | Search Input | Cycle through previous search history queries |
| `Enter` | Search Input | Execute YouTube search query |
| `Up` / `Down` | Results Table | Navigate search results rows |
| `Enter` | Results Table | Play highlighted track immediately |
| `q` | Global | Quit application |

### Table Selection & Batch Actions
| Key | Context | Action |
| --- | --- | --- |
| `Space` | Results Table | Toggle selection for highlighted row |
| `m` | Results Table | Toggle selection for highlighted row |
| `Ctrl+a` | Results Table | Select all search results |
| `u` / `Ctrl+d` | Results Table | Clear all selected rows |
| `a` | Results Table | Add selected rows (or highlighted row) to queue |
| `d` | Results Table | Download selected rows (or highlighted row) in background |

### Playback Controls
| Key | Context | Action |
| --- | --- | --- |
| `Space` | Outside Table | Toggle Play / Pause |
| `x` | Global | Stop playback |
| `Left` / `Right` | Global | Seek backward (-5s) / Seek forward (+5s) |
| `Up` / `Down` | Outside Input/Table | Increase volume (+5%) / Decrease volume (-5%) |
| `s` | Global | Toggle Shuffle mode |
| `r` | Global | Cycle Repeat mode (`off` -> `all` -> `one`) |
| `c` | Global | Close video window / switch to audio-only |
| `t` | Global | Toggle thumbnail preview panel |
| `v` | Global | Toggle audio visualizer panel |

### Queue Management
| Key | Context | Action |
| --- | --- | --- |
| `Enter` | Queue Panel | Play highlighted queue track |
| `Delete` / `Backspace` | Queue Panel | Remove highlighted track from queue |
| `Ctrl+e` | Global | Export active queue to JSON and M3U files |
| `Ctrl+i` | Global | Import newest queue file from download directory |

### Settings
| Key | Context | Action |
| --- | --- | --- |
| `Shift+S` | Global | Open / close Settings configuration modal |

## Configuration

Settings are stored at `~/.config/ytcli/config.json`. The configuration file is generated automatically when settings are updated in-app, but can also be manually edited:

```json
{
  "search_results_count": 10,
  "default_format": "best_video",
  "download_dir": "~/Downloads/ytcli",
  "startup": {
    "thumbnails": false,
    "visualizer": false,
    "autoplay_next": true
  },
  "toast_duration": 3.0,
  "search_history": []
}
```

## License

This project is licensed under the MIT License. See the LICENSE file for details.
