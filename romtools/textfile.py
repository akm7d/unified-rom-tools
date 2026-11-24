"""Reading and rewriting small text files that other programs made (.cue, .gdi, .m3u): whatever their encoding, line ends
or byte order mark, and the same on every system. (Python's own default is the Windows code page there, so "é" in a UTF-8
.cue pointed at files that don't exist.)"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

MAX_SIZE = 1 << 20          # no sheet or playlist is bigger; a disc image with such a name mustn't be read into memory


@dataclass
class TextFile:
    text: str
    encoding: str = "utf-8"
    bom: bytes = b""

    def encode(self) -> bytes:
        """The text as bytes again, the way the file was (UTF-8 if it holds something the old encoding can't)."""
        try:
            return self.bom + self.text.encode(self.encoding)
        except UnicodeEncodeError:
            return self.bom + self.text.encode("utf-8")

    def with_text(self, text: str) -> "TextFile":
        return replace(self, text=text)


def decode(raw: bytes) -> TextFile:
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if raw.startswith(bom):
            try:
                return TextFile(raw[len(bom):].decode(enc), enc, bom)
            except UnicodeDecodeError:
                break
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return TextFile(raw.decode(enc), enc)
        except UnicodeDecodeError:
            continue
    return TextFile(raw.decode("latin-1"), "latin-1")       # (never reached: latin-1 takes every byte)


def read(path: Path) -> TextFile:
    """Raises OSError, also for a file far too big to be one."""
    with open(path, "rb") as fh:
        raw = fh.read(MAX_SIZE + 1)
    if len(raw) > MAX_SIZE:
        raise OSError(f"{Path(path).name} is too big to be a text file")
    return decode(raw)


def read_text(path: Path) -> str:
    """Its text with plain line ends."""
    return read(path).text.replace("\r\n", "\n").replace("\r", "\n")
