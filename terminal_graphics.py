"""Terminal capability detection, thumbnail caching, and ANSI graphic rendering for ytcli.

Supports:
- Environment detection for Kitty, Sixel, iTerm2, WezTerm, Ghostty, Foot graphics.
- Cross-platform thumbnail cache storage using pathlib and tempfile.
- Native protocol image rendering: Kitty Graphics Protocol (Kitty/Ghostty/WezTerm)
  and Sixel (Foot), sent as raw terminal escape sequences at true pixel density.
- High-res ANSI half-block (▀) RGB truecolor rendering via Pillow as a universal
  fallback for terminals without a native inline-graphics protocol.
"""
import base64
import io
import os
import re
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Optional

# The number of display columns / rows to aim for inside #thumbnail_box.
DEFAULT_CELL_COLS = 34
DEFAULT_CELL_ROWS = 12


def _load_rgb_image(path: Path | str) -> Any:
    """Open an image file as a Pillow RGB image, or return None on any failure."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        p = Path(path)
        if not p.exists():
            return None
        with Image.open(p) as img:
            return img.convert("RGB").copy()
    except Exception:
        return None


def _pick_pixel_size(cols: int, rows: int) -> tuple[int, int]:
    """Estimate target pixel dimensions for an image displayed in cols x rows cells.

    Terminal cells are typically ~1:2 wide:tall, so a 34x12 character area holds
    roughly a 16:9 340x120-340x240px image. The protocol encodes true pixels, and
    the terminal scales the image into the space we claim.
    """
    if cols <= 0 or rows <= 0:
        cols, rows = DEFAULT_CELL_COLS, DEFAULT_CELL_ROWS
    target_px_h = rows * 16
    target_px_w = min(cols * 10, round(target_px_h * 16 / 9))
    return max(32, target_px_w), max(32, target_px_h)


def detect_terminal_graphics_support() -> dict[str, Any]:
    """Detect if the current terminal environment supports native inline graphics protocols.

    Inspects environment variables: TERM, TERM_PROGRAM, KITTY_WINDOW_ID, WEZTERM_PANE,
    GHOSTTY_RESOURCES_DIR, LC_TERMINAL, etc.

    Returns:
        dict with keys:
            - supported (bool): True if a known graphics protocol is supported.
            - protocol (str | None): 'kitty', 'iterm2', 'sixel', or None.
            - terminal_name (str): Human-readable terminal name.
    """
    term = os.environ.get("TERM", "").lower()
    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    lc_terminal = os.environ.get("LC_TERMINAL", "").lower()

    protocol: Optional[str] = None
    terminal_name: str = "Standard ANSI Terminal"

    # Kitty graphics protocol (Kitty, Ghostty)
    if os.environ.get("KITTY_WINDOW_ID") or term == "xterm-kitty" or "kitty" in term_program:
        protocol = "kitty"
        terminal_name = "Kitty"
    elif "ghostty" in term_program or os.environ.get("GHOSTTY_RESOURCES_DIR"):
        protocol = "kitty"
        terminal_name = "Ghostty"
    # WezTerm supports the kitty graphics protocol out of the box (its
    # ``enable_kitty_graphics`` config option defaults to on), so route it to
    # the kitty encoder rather than the older iTerm2 inline-image protocol.
    elif os.environ.get("WEZTERM_PANE") or "wezterm" in term_program:
        protocol = "kitty"
        terminal_name = "WezTerm"
    # iTerm2
    elif "iterm" in term_program or "iterm2" in lc_terminal:
        protocol = "iterm2"
        terminal_name = "iTerm2"
    # Sixel graphics (Foot, mlterm, etc.)
    elif term == "foot" or "foot" in term_program or "sixel" in term:
        protocol = "sixel"
        terminal_name = "Foot / Sixel"

    supported = protocol is not None
    return {
        "supported": supported,
        "protocol": protocol,
        "terminal_name": terminal_name,
    }


def get_thumbnail_cache_dir() -> Path:
    """Return a cross-platform local thumbnail cache directory (e.g. /tmp/ytcli_thumbs or %TEMP%\\ytcli_thumbs)."""
    cache_dir = Path(tempfile.gettempdir()) / "ytcli_thumbs"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return cache_dir


def extract_video_id(url_or_id: str) -> Optional[str]:
    """Extract an 11-character YouTube video ID from various URL formats or return the ID if already clean."""
    if not url_or_id:
        return None
    cleaned = url_or_id.strip()
    if re.fullmatch(r"[a-zA-Z0-9_-]{11}", cleaned):
        return cleaned

    patterns = [
        r"(?:v=|\/v\/|embed\/|shorts\/|youtu\.be\/|\/watch\?v=)([a-zA-Z0-9_-]{11})",
        r"[?&]v=([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, cleaned)
        if match:
            return match.group(1)
    return None


def fetch_thumbnail_image(video_id: str, custom_url: Optional[str] = None) -> Optional[Path]:
    """Download thumbnail image to local cache directory if not already cached.

    Returns the Path to the cached JPG file or None if download fails.
    """
    if not video_id:
        return None
    try:
        cache_dir = get_thumbnail_cache_dir()
        target_path = cache_dir / f"{video_id}.jpg"
        if target_path.exists() and target_path.stat().st_size > 0:
            return target_path

        urls_to_try: list[str] = []
        if custom_url and custom_url.startswith("http"):
            urls_to_try.append(custom_url)
        urls_to_try.extend([
            f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
            f"https://i.ytimg.com/vi/{video_id}/default.jpg",
        ])

        for url in urls_to_try:
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ytcli/1.0"},
                )
                with urllib.request.urlopen(req, timeout=4) as resp:
                    if resp.status == 200:
                        data = resp.read()
                        if len(data) > 500:  # Valid image payload
                            target_path.write_bytes(data)
                            return target_path
            except Exception:
                continue
    except Exception:
        pass
    return None


def render_half_block_ansi(image_path: Path | str, width: int = 34, height: int = 12) -> str:
    """Render an image into a 24-bit TrueColor ANSI half-block (▀) string.

    Each character represents two vertical pixels (top subpixel as foreground, bottom as background).
    """
    try:
        from PIL import Image
    except ImportError:
        return generate_placeholder_thumbnail("Pillow required for art", width, height)

    try:
        path = Path(image_path)
        if not path.exists():
            return generate_placeholder_thumbnail("No image file", width, height)

        with Image.open(path) as img:
            img = img.convert("RGB")
            target_pixel_h = height * 2
            img = img.resize((width, target_pixel_h), Image.Resampling.BILINEAR)
            pixels = img.load()

            lines: list[str] = []
            for y in range(0, target_pixel_h, 2):
                line_parts: list[str] = []
                for x in range(width):
                    r_top, g_top, b_top = pixels[x, y]
                    if y + 1 < target_pixel_h:
                        r_bot, g_bot, b_bot = pixels[x, y + 1]
                    else:
                        r_bot, g_bot, b_bot = (0, 0, 0)
                    line_parts.append(
                        f"\x1b[38;2;{r_top};{g_top};{b_top}m\x1b[48;2;{r_bot};{g_bot};{b_bot}m▀"
                    )
                line_parts.append("\x1b[0m")
                lines.append("".join(line_parts))
            return "\n".join(lines)
    except Exception as e:
        return generate_placeholder_thumbnail(f"Error: {e}", width, height)


def generate_placeholder_thumbnail(title: str = "No Preview", width: int = 34, height: int = 12) -> str:
    """Generate a clean colored ANSI ASCII frame placeholder for missing or loading thumbnails."""
    width = max(10, width)
    height = max(4, height)
    lines: list[str] = []

    border_color = "\x1b[36m"
    text_color = "\x1b[33m"
    reset = "\x1b[0m"

    inner_w = width - 2
    lines.append(f"{border_color}┌{'─' * inner_w}┐{reset}")

    pad_top = (height - 3) // 2
    for _ in range(max(0, pad_top)):
        lines.append(f"{border_color}│{reset}{' ' * inner_w}{border_color}│{reset}")

    clean_title = title[: inner_w - 4]
    centered = f"[{clean_title}]".center(inner_w)
    lines.append(f"{border_color}│{reset}{text_color}{centered}{reset}{border_color}│{reset}")

    pad_bot = height - 3 - pad_top
    for _ in range(max(0, pad_bot)):
        lines.append(f"{border_color}│{reset}{' ' * inner_w}{border_color}│{reset}")

    lines.append(f"{border_color}└{'─' * inner_w}┘{reset}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Native terminal graphics protocol rendering
# ---------------------------------------------------------------------------

def render_kitty_thumbnail(
    image_path: Path | str,
    cols: int = DEFAULT_CELL_COLS,
    rows: int = DEFAULT_CELL_ROWS,
    delete_first: bool = True,
) -> str:
    """Render an image as a Kitty Graphics Protocol (TGP) escape sequence.

    Works in Kitty, Ghostty and WezTerm. The image is embedded at true pixel
    resolution and the terminal scales it into the requested ``cols`` x ``rows``
    character area, so no blocky down-sampling happens on the way in.

    Returns the raw escape sequence (plus a trailing space so the placed image
    occupies one text cell) or a placeholder frame if the image cannot load.
    """
    img = _load_rgb_image(image_path)
    if img is None:
        return generate_placeholder_thumbnail("No image", width=cols, height=rows)

    try:
        from PIL import Image
    except ImportError:
        return generate_placeholder_thumbnail("Pillow required for art", width=cols, height=rows)

    try:
        target_w, target_h = _pick_pixel_size(cols, rows)
        if img.width != target_w or img.height != target_h:
            img = img.resize((target_w, target_h), Image.Resampling.BILINEAR)

        # PNG (kitty format code 100) is used for both opaque and alpha images:
        # Pillow's PNG encoder is dependable and PNG is the most widely supported
        # kitty payload. (JPEG would be format code 101, but it gains nothing at
        # thumbnail sizes and complicates the alpha path.)
        payload_img = img.convert("RGBA")
        buffer = io.BytesIO()
        payload_img.save(buffer, format="PNG")
        payload = buffer.getvalue()

        data_b64 = base64.b64encode(payload).decode("ascii")
        # 76 columns is a safe, widely-supported base64 line width.
        chunks = [data_b64[i:i + 76] for i in range(0, len(data_b64), 76)]
        total = len(chunks)
        image_id = 1

        parts: list[str] = []
        if delete_first:
            parts.append("\x1b_Ga=d,d=1\x1b\\")
        for i, chunk in enumerate(chunks):
            more = 1 if i < total - 1 else 0
            parts.append(
                f"\x1b_Ga=T,f=100,s={payload_img.width},v={payload_img.height},"
                f"c={cols},r={rows},q=2,i={image_id},m={more};{chunk}\x1b\\"
            )
        parts.append(" ")  # the cell that holds the placed image
        return "".join(parts)
    except Exception:
        return generate_placeholder_thumbnail("Render error", width=cols, height=rows)


def _resize_for_area(img: Any, cols: int, rows: int) -> Any:
    try:
        from PIL import Image
        target_w, target_h = _pick_pixel_size(cols, rows)
        if img.width != target_w or img.height != target_h:
            return img.resize((target_w, target_h), Image.Resampling.BILINEAR)
        return img
    except Exception:
        return img


def render_sixel_thumbnail(
    image_path: Path | str,
    cols: int = DEFAULT_CELL_COLS,
    rows: int = DEFAULT_CELL_ROWS,
) -> str:
    """Render an image as a Sixel escape sequence (for Foot / other sixel terms).

    The image is quantised to an adaptive palette of up to 256 colours and emitted
    at true pixel density; ``ESC P q`` ... ``ESC \\`` is the DEC sixel preamble.
    Falls back to an ANSI placeholder frame on any failure.
    """
    img = _load_rgb_image(image_path)
    if img is None:
        return generate_placeholder_thumbnail("No image", width=cols, height=rows)

    try:
        from PIL import Image
    except ImportError:
        return generate_placeholder_thumbnail("Pillow required for art", width=cols, height=rows)

    try:
        img = _resize_for_area(img, cols, rows)
        img = img.convert("RGB")
        width, height = img.size

        # Adaptive palette (<=256 entries) plus a per-pixel palette-index grid.
        # If the frame already uses <=256 unique colours we keep them exactly;
        # otherwise Pillow quantises it down to a 256-colour adaptive palette.
        used = img.getcolors(maxcolors=1_000_000)
        if used is not None and len(used) <= 256:
            palette_colors = [color for _count, color in used]
            color_index = {color: idx for idx, color in enumerate(palette_colors)}
            px = img.load()
            index_grid = [
                [color_index[px[x, y]] for x in range(width)]
                for y in range(height)
            ]
        else:
            quantized = img.convert("P", palette=Image.ADAPTIVE, colors=256)
            quantized_pal = quantized.getpalette() or []
            palette_colors = [
                (quantized_pal[i], quantized_pal[i + 1], quantized_pal[i + 2])
                for i in range(0, min(len(quantized_pal), 256 * 3), 3)
            ]
            q_px = quantized.load()
            index_grid = [
                [q_px[x, y] for x in range(width)]
                for y in range(height)
            ]

        count = len(palette_colors)
        if count == 0:
            return generate_placeholder_thumbnail("No colours", width=cols, height=rows)

        # ESC P q  "1;1;W;H  #0;2;R;G;B ...   <sixel data>  ESC \
        # DEC colour channels are 0..100, so the 0..255 pixel values are scaled.
        parts: list[str] = ["\x1bPq", f'"1;1;{width};{height}']
        parts.extend(
            f"#{i};2;{r * 100 // 255};{g * 100 // 255};{b * 100 // 255}"
            for i, (r, g, b) in enumerate(palette_colors)
        )

        # Each data character paints one 1px-wide vertical column of up to six
        # pixels; bit 0 is the top scanline of the band. A band is six scanlines
        # tall: we emit one colour across the full width, return to the left
        # margin with '$', and descend to the next band with '-' when done.
        out_parts: list[str] = ["$"]
        band = 6
        for y0 in range(0, height, band):
            band_h = min(band, height - y0)
            # palette index -> per-column vertical bitmask for this band
            masks: dict[int, list[int]] = {}
            for y_off in range(band_h):
                row = index_grid[y0 + y_off]
                bit = 1 << y_off
                for x in range(width):
                    idx = row[x]
                    column = masks.get(idx)
                    if column is None:
                        column = masks[idx] = [0] * width
                    column[x] |= bit
            for idx in sorted(masks):
                out_parts.append(f"#{idx}")
                out_parts.append("".join(chr(63 + v) for v in masks[idx]))
                out_parts.append("$")
            out_parts.append("-")
        out_parts.append("\x1b\\")
        return "".join(parts + out_parts)
    except Exception:
        return generate_placeholder_thumbnail("Render error", width=cols, height=rows)


def render_thumbnail(
    image_path: Path | str,
    protocol: Optional[str] = None,
    cols: int = DEFAULT_CELL_COLS,
    rows: int = DEFAULT_CELL_ROWS,
) -> str:
    """Render ``image_path`` for the current terminal using its native protocol.

    ``protocol`` is one of ``'kitty'`` (Kitty/Ghostty/WezTerm), ``'sixel'``
    (Foot), or ``None``/``'iterm2'`` (ANSI fallback) and may be supplied
    directly by the caller. When omitted it is auto-detected via
    ``detect_terminal_graphics_support()``.

    - ``'kitty'`` -> Kitty Graphics Protocol at true pixel density.
    - ``'sixel'`` -> Sixel at true pixel density.
    - otherwise   -> ANSI half-block truecolor fallback (works everywhere,
      including iTerm2 which we intentionally do not drive with its proprietary
      inline-image OSC because those overlays do not integrate cleanly with a
      cell-based TUI).

    Returns a *rendering string*. For the native protocols the caller must emit
    it through the raw terminal output channel (not through Textual widget
    content, which would strip the escapes) positioned over the target area.

    Never raises: on any failure the caller still receives a placeholder frame.
    """
    if protocol is None:
        protocol = detect_terminal_graphics_support().get("protocol")

    if protocol == "kitty":
        return render_kitty_thumbnail(image_path, cols=cols, rows=rows)
    if protocol == "sixel":
        return render_sixel_thumbnail(image_path, cols=cols, rows=rows)
    return render_half_block_ansi(image_path, width=cols, height=rows)
