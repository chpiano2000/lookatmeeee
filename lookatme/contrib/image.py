"""
Image renderer using the Kitty graphics protocol with Unicode placeholders.

Works in Kitty, Ghostty, and WezTerm (any terminal that speaks the Kitty
graphics protocol with Unicode placeholder support). Uses the same
Unicode-placeholder approach as image.nvim, which plays nicely with TUIs.

Usage in a slide:

    ![20](path/to/image.png)   # 20 rows tall, alt text = row count
    ![](path/to/image.png)     # default 15 rows

How it works
------------
1. Transmit the image once per session with ``a=T,U=1``, creating a
   virtual placement anchored to a unique image id.
2. Render a rectangular grid of ``U+10EEEE`` placeholder characters in
   the urwid canvas, with the foreground colour set to the image id
   encoded as RGB. Each cell carries two combining diacritics that mark
   its (row, col) position within the image. The terminal composites the
   image over those placeholder cells.

Because the placeholders live in normal text cells, urwid redraws them
naturally and the image survives slide changes, scrolls, and resizes.
"""

import base64
import hashlib
import os
import sys
import tempfile

import urwid
from PIL import Image as _PILImage

import lookatme.config
from lookatme.exceptions import IgnoredByContrib


# The 297 combining diacritics defined by Kitty for row/column indices in
# Unicode placeholder mode. Source: Kitty graphics protocol docs.
_ROWCOL_DIACRITICS = [
    0x0305, 0x030D, 0x030E, 0x0310, 0x0312, 0x033D, 0x033E, 0x033F,
    0x0346, 0x034A, 0x034B, 0x034C, 0x0350, 0x0351, 0x0352, 0x0357,
    0x035B, 0x0363, 0x0364, 0x0365, 0x0366, 0x0367, 0x0368, 0x0369,
    0x036A, 0x036B, 0x036C, 0x036D, 0x036E, 0x036F, 0x0483, 0x0484,
    0x0485, 0x0486, 0x0487, 0x0592, 0x0593, 0x0594, 0x0595, 0x0597,
    0x0598, 0x0599, 0x059C, 0x059D, 0x059E, 0x059F, 0x05A0, 0x05A1,
    0x05A8, 0x05A9, 0x05AB, 0x05AC, 0x05AF, 0x05C4, 0x0610, 0x0611,
    0x0612, 0x0613, 0x0614, 0x0615, 0x0616, 0x0617, 0x0657, 0x0658,
    0x0659, 0x065A, 0x065B, 0x065D, 0x065E, 0x06D6, 0x06D7, 0x06D8,
    0x06D9, 0x06DA, 0x06DB, 0x06DC, 0x06DF, 0x06E0, 0x06E1, 0x06E2,
    0x06E4, 0x06E7, 0x06E8, 0x06EB, 0x06EC, 0x0730, 0x0732, 0x0733,
    0x0735, 0x0736, 0x073A, 0x073D, 0x073F, 0x0740, 0x0741, 0x0743,
    0x0745, 0x0747, 0x0749, 0x074A, 0x07EB, 0x07EC, 0x07ED, 0x07EE,
    0x07EF, 0x07F0, 0x07F1, 0x07F3, 0x0816, 0x0817, 0x0818, 0x0819,
    0x081B, 0x081C, 0x081D, 0x081E, 0x081F, 0x0820, 0x0821, 0x0822,
    0x0823, 0x0825, 0x0826, 0x0827, 0x0829, 0x082A, 0x082B, 0x082C,
    0x082D, 0x0951, 0x0953, 0x0954, 0x0F82, 0x0F83, 0x0F86, 0x0F87,
    0x135D, 0x135E, 0x135F, 0x17DD, 0x193A, 0x1A17, 0x1A75, 0x1A76,
    0x1A77, 0x1A78, 0x1A79, 0x1A7A, 0x1A7B, 0x1A7C, 0x1B6B, 0x1B6D,
    0x1B6E, 0x1B6F, 0x1B70, 0x1B71, 0x1B72, 0x1B73, 0x1CD0, 0x1CD1,
    0x1CD2, 0x1CDA, 0x1CDB, 0x1CE0, 0x1DC0, 0x1DC1, 0x1DC3, 0x1DC4,
    0x1DC5, 0x1DC6, 0x1DC7, 0x1DC8, 0x1DC9, 0x1DCB, 0x1DCC, 0x1DD1,
    0x1DD2, 0x1DD3, 0x1DD4, 0x1DD5, 0x1DD6, 0x1DD7, 0x1DD8, 0x1DD9,
    0x1DDA, 0x1DDB, 0x1DDC, 0x1DDD, 0x1DDE, 0x1DDF, 0x1DE0, 0x1DE1,
    0x1DE2, 0x1DE3, 0x1DE4, 0x1DE5, 0x1DE6, 0x1DFE, 0x20D0, 0x20D1,
    0x20D4, 0x20D5, 0x20D6, 0x20D7, 0x20DB, 0x20DC, 0x20E1, 0x20E7,
    0x20E9, 0x20F0, 0x2CEF, 0x2CF0, 0x2CF1, 0x2DE0, 0x2DE1, 0x2DE2,
    0x2DE3, 0x2DE4, 0x2DE5, 0x2DE6, 0x2DE7, 0x2DE8, 0x2DE9, 0x2DEA,
    0x2DEB, 0x2DEC, 0x2DED, 0x2DEE, 0x2DEF, 0x2DF0, 0x2DF1, 0x2DF2,
    0x2DF3, 0x2DF4, 0x2DF5, 0x2DF6, 0x2DF7, 0x2DF8, 0x2DF9, 0x2DFA,
    0x2DFB, 0x2DFC, 0x2DFD, 0x2DFE, 0x2DFF, 0xA66F, 0xA67C, 0xA67D,
    0xA6F0, 0xA6F1, 0xA8E0, 0xA8E1, 0xA8E2, 0xA8E3, 0xA8E4, 0xA8E5,
    0xA8E6, 0xA8E7, 0xA8E8, 0xA8E9, 0xA8EA, 0xA8EB, 0xA8EC, 0xA8ED,
    0xA8EE, 0xA8EF, 0xA8F0, 0xA8F1, 0xAAB0, 0xAAB2, 0xAAB3, 0xAAB7,
    0xAAB8, 0xAABE, 0xAABF, 0xAAC1, 0xFE20, 0xFE21, 0xFE22, 0xFE23,
    0xFE24, 0xFE25, 0xFE26, 0x10A0F, 0x10A38, 0x1D185, 0x1D186,
    0x1D187, 0x1D188, 0x1D189, 0x1D1AA, 0x1D1AB, 0x1D1AC, 0x1D1AD,
    0x1D242, 0x1D243, 0x1D244,
]

_PLACEHOLDER = "\U0010EEEE"

_next_image_id = 0


def _new_id():
    global _next_image_id
    _next_image_id += 1
    return _next_image_id


def user_warnings():
    warnings = []
    term = os.environ.get("TERM_PROGRAM", "").lower()
    if (
        term not in ("ghostty", "wezterm")
        and "KITTY_WINDOW_ID" not in os.environ
    ):
        warnings.append(
            f"image extension: TERM_PROGRAM={term!r} — Kitty graphics protocol "
            "may not be supported; images may not render"
        )
    if _in_tmux():
        warnings.append(
            "image extension: running inside tmux — images require "
            "`set -g allow-passthrough on` in tmux.conf (tmux 3.3+)"
        )
    return warnings


def root_urwid_widget(to_wrap):
    raise IgnoredByContrib()


_PNG_CACHE_DIR = os.path.join(tempfile.gettempdir(), "lookatme-kitty-images")


def _as_png_path(path):
    """Kitty's ``f=100`` requires a PNG file. If ``path`` already points at a
    PNG, return it unchanged; otherwise transcode via Pillow and cache the
    result in a temp dir keyed by (path, mtime)."""
    with open(path, "rb") as f:
        header = f.read(8)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return path

    os.makedirs(_PNG_CACHE_DIR, exist_ok=True)
    mtime = os.path.getmtime(path)
    key = hashlib.sha1(f"{path}:{mtime}".encode()).hexdigest()
    cached = os.path.join(_PNG_CACHE_DIR, f"{key}.png")
    if not os.path.exists(cached):
        img = _PILImage.open(path)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
        img.save(cached, "PNG")
    return cached


def _in_tmux():
    return "TMUX" in os.environ or os.environ.get("TERM", "").startswith("tmux")


def _wrap_for_tmux(seq: bytes) -> bytes:
    # tmux passthrough: \ePtmux;<seq with each \e doubled>\e\\
    # Requires `set -g allow-passthrough on` in tmux.conf (tmux 3.3+).
    inner = seq.replace(b"\033", b"\033\033")
    return b"\033Ptmux;" + inner + b"\033\\"


def _transmit(path, image_id):
    """Transmit-and-virtual-place an image once. Idempotent for a given id."""
    png_path = _as_png_path(path)
    b64_path = base64.standard_b64encode(png_path.encode("utf-8")).decode("ascii")
    ctrl = f"a=T,U=1,i={image_id},f=100,t=f,q=2"
    seq = f"\033_G{ctrl};{b64_path}\033\\".encode("ascii")
    if _in_tmux():
        seq = _wrap_for_tmux(seq)
    os.write(1, seq)


class _KittyImage(urwid.Widget):
    """A box widget that paints a grid of Kitty Unicode placeholders. The
    terminal composites the actual image over the placeholder cells."""

    _sizing = frozenset(["box"])

    def __init__(self, path, height):
        super().__init__()
        self._path = path
        self._height = height
        self._image_id = _new_id()
        self._transmitted = False

        r = (self._image_id >> 16) & 0xFF
        g = (self._image_id >> 8) & 0xFF
        b = self._image_id & 0xFF
        self._attr = urwid.AttrSpec(
            f"#{r:02x}{g:02x}{b:02x}", "", colors=2**24
        )

    def rows(self, size, focus=False):
        return self._height

    def _line_for_row(self, row, cols):
        if row >= len(_ROWCOL_DIACRITICS):
            return " " * cols
        r_diac = chr(_ROWCOL_DIACRITICS[row])
        limit = min(cols, len(_ROWCOL_DIACRITICS))
        parts = [
            f"{_PLACEHOLDER}{r_diac}{chr(_ROWCOL_DIACRITICS[c])}"
            for c in range(limit)
        ]
        if cols > limit:
            parts.append(" " * (cols - limit))
        return "".join(parts)

    def render(self, size, focus=False):
        if not self._transmitted:
            _transmit(self._path, self._image_id)
            self._transmitted = True

        maxcol = size[0]
        maxrow = size[1] if len(size) > 1 else self._height

        image_rows = min(maxrow, self._height)
        lines = [
            urwid.Text((self._attr, self._line_for_row(r, maxcol)), wrap="clip")
            for r in range(image_rows)
        ]
        for _ in range(maxrow - image_rows):
            lines.append(urwid.Text(""))

        pile = urwid.Pile(lines)
        return pile.render((maxcol,), focus)


def image(link_uri, title, text):
    with open("/tmp/lookatme_image_debug.log", "a") as f:
        f.write(f"image() called: link_uri={link_uri!r} title={title!r}\n")

    base_dir = lookatme.config.SLIDE_SOURCE_DIR
    full_path = os.path.abspath(os.path.join(base_dir or ".", link_uri))

    if not os.path.exists(full_path):
        with open("/tmp/lookatme_image_debug.log", "a") as f:
            f.write(f"  -> not a local file, falling back: {full_path!r}\n")
        raise IgnoredByContrib()

    try:
        height = int(text) if isinstance(text, str) else 15
    except (TypeError, ValueError):
        height = 15

    with open("/tmp/lookatme_image_debug.log", "a") as f:
        f.write(f"  -> rendering {full_path!r} at height={height}\n")

    return [urwid.BoxAdapter(_KittyImage(full_path, height), height=height)]


def shutdown():
    # Delete all images created by this session.
    seq = b"\033_Ga=d,d=A,q=2\033\\"
    if _in_tmux():
        seq = _wrap_for_tmux(seq)
    os.write(1, seq)
