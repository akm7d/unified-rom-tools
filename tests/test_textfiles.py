"""Sheets, playlists and archives made by other programs: any encoding, line ends and names."""

import os
import subprocess

import pytest

from romtools import hashing, identify, patching, renamer, scanner, textfile
from test_identify import GAMES, put, rnd


@pytest.mark.parametrize("raw", ["Pok\u00e9mon\r\n".encode("utf-8"), "Pok\u00e9mon\n".encode("latin-1"),
                                 b"\xef\xbb\xbf" + "Pok\u00e9mon\n".encode("utf-8"),
                                 b"\xff\xfe" + "Pok\u00e9mon\r\n".encode("utf-16-le"), b"plain\n", b"\x81\x8d\xff", b""])
def test_text_comes_back_byte_for_byte(raw):
    t = textfile.decode(raw)
    assert t.encode() == raw
    if b"mon" in raw:
        assert t.text.startswith("Pok\u00e9mon")


def test_text_the_old_encoding_cant_hold_becomes_utf8():
    t = textfile.decode("Pok\u00e9mon".encode("latin-1"))
    assert t.with_text("\u65e5\u672c").encode() == "\u65e5\u672c".encode("utf-8")


@pytest.mark.parametrize("enc", ["utf-8", "utf-8-sig", "latin-1"])
def test_cue_tracks_with_accents_whatever_the_systems_encoding(tmp_path, enc, monkeypatch):
    import locale
    monkeypatch.setattr(locale, "getencoding", lambda: "cp1252")
    t = put(tmp_path / "Pok\u00e9mon (Track 1).bin", b"x")
    (tmp_path / "g.cue").write_bytes(('FILE "' + t.name + '" BINARY\r\n  TRACK 01 MODE1/2352\r\n').encode(enc))
    (tmp_path / "g.gdi").write_bytes(('1\r\n1 0 4 2352 "' + t.name + '" 0\r\n').encode(enc))
    assert scanner._cue_parts(tmp_path / "g.cue") == [t] and scanner._gdi_parts(tmp_path / "g.gdi") == [t]


def test_no_file_is_read_with_the_systems_encoding():
    """On Windows that's the code page: every text file has to be opened with an encoding of its own."""
    import re
    from pathlib import Path
    bad = []
    for f in (Path(__file__).parent.parent / "romtools").rglob("*.py"):
        lines = f.read_text(encoding="utf-8").splitlines()
        for n, line in enumerate(lines, 1):
            line = line if "encoding=" in line else line + " " + " ".join(x for x in lines[n:n + 3] if "encoding=" in x)
            if re.search(r"read_text\((?!.*encoding=)(?!\w)|text=True|\.write_text\((?!.*encoding=)", line) and \
                    "textfile.read_text" not in line and "def read_text" not in line and not re.search(
                        r'write_text\("x"\)|write_text\(xml\)', line):
                bad.append(f"{f.name}:{n}: {line.strip()}")
    assert not bad, "\n".join(bad)


def test_huge_file_called_cue_isnt_read_into_memory(tmp_path):
    p = tmp_path / "x.cue"
    with open(p, "wb") as f:
        f.truncate(3 << 30)
    import resource
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert scanner._cue_parts(p) == [] and scanner._gdi_parts(p) == []
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before < 64 << 10


@pytest.mark.parametrize("enc, nl, bom", [("latin-1", "\r\n", b""), ("utf-8", "\n", b"\xef\xbb\xbf"), ("utf-8", "\r\n", b"")])
def test_playlists_keep_their_encoding_and_undo_is_exact(tmp_path, enc, nl, bom):
    a, b = put(tmp_path / "A (Disc 1).chd", b"1"), put(tmp_path / "B (Disc 2).chd", b"2")
    raw = bom + nl.join(["A (Disc 1).chd", "B (Disc 2).chd", "# Pok\u00e9mon", ""]).encode(enc)
    m = tmp_path / "g.m3u"
    m.write_bytes(raw)
    ops = renamer.validate(renamer.add_playlist_ops([renamer.Op(a, tmp_path / "Caf\u00e9 (Disc 1).chd")]))
    j, errs = renamer.apply(ops)
    assert not errs and m.read_bytes() == raw.replace(b"A (Disc 1)", "Caf\u00e9 (Disc 1)".encode(enc))
    assert renamer.undo(j) == [] and m.read_bytes() == raw and a.is_file() and b.is_file()


def test_journals_from_older_versions_can_still_be_undone(tmp_path):
    import json
    from romtools.config import JOURNAL_DIR
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    m = put(tmp_path / "g.m3u", b"new.chd\n")
    f = put(tmp_path / "new.chd", b"x")
    j = JOURNAL_DIR / "old.json"
    j.write_text(json.dumps({"created": "x", "undone": False, "entries": [
        {"kind": "rename", "src": str(tmp_path / "old.chd"), "dst": str(f), "old_text": None, "new_text": None, "done": True},
        {"kind": "m3u-edit", "src": str(m), "dst": str(m), "old_text": "old.chd\n", "new_text": "new.chd\n", "done": True}]}))
    assert renamer.undo(j) == [] and m.read_bytes() == b"old.chd\n" and (tmp_path / "old.chd").is_file()


# ---------------------------------------------------------------- archives

pytestmark_7z = pytest.mark.skipif(not hashing.SEVENZIP, reason="7z isn't installed")
NAMES = ["-x.nes", "@list.nes", "a b [!].nes", "Pok\u00e9mon.nes", "\u65e5\u672c.nes", "sub/dir/deep.nes", "empty.nes", "w*ld?.nes",
         "--.nes"]


def pack(tmp_path, names, *opts, fmt="7z"):
    src = tmp_path / "src"
    want = {}
    for k, n in enumerate(names):
        data = b"" if n == "empty.nes" else rnd(3000 + 777 * k, k)
        put(src / n, data)
        want[n] = data
    out = tmp_path / f"t.{fmt}"
    subprocess.run([hashing.SEVENZIP, "a", "-bd", f"-t{fmt}", *opts, str(out), "."], cwd=src, capture_output=True, check=True)
    return out, want


@pytestmark_7z
@pytest.mark.parametrize("fmt, opts", [("7z", ["-ms=on"]), ("7z", ["-ms=off"]), ("zip", [])])
def test_archive_members_with_awkward_names(tmp_path, fmt, opts):
    import hashlib
    arc, want = pack(tmp_path, NAMES, *opts, fmt=fmt)
    for how in (hashing.hash_7z, lambda p: [hashing._hash_member(p, e, None, None) for e in hashing._7z_entries(p)]):
        got = {e.name.replace(os.sep, "/"): e.full.sha1 for e in how(arc)}
        assert got == {n: hashlib.sha1(d).hexdigest() for n, d in want.items()}


@pytestmark_7z
def test_wrong_order_falls_back_to_one_by_one(tmp_path, monkeypatch):
    arc, want = pack(tmp_path, ["a.nes", "b.nes", "c.nes"])
    real = hashing._7z_entries
    monkeypatch.setattr(hashing, "_7z_entries", lambda p: real(p)[::-1])
    assert hashing._hash_in_one_go(arc, hashing._7z_entries(arc), None, None) is None
    assert sorted((e.name, e.full.size) for e in hashing.hash_7z(arc)) == sorted((n, len(d)) for n, d in want.items())


@pytestmark_7z
def test_damaged_and_protected_archives_are_errors_not_wrong_hashes(tmp_path):
    arc, _w = pack(tmp_path, ["a.nes", "b.nes"], "-mx=0")
    d = bytearray(arc.read_bytes())
    d[200] ^= 1
    arc.write_bytes(d)
    with pytest.raises(hashing.ArchiveError):
        hashing.hash_7z(arc)
    (tmp_path / "p").mkdir()
    for opts in (["-psecret"], ["-psecret", "-mhe=on"]):
        arc, _w = pack(tmp_path / "p", ["a.nes"], *opts)
        with pytest.raises(hashing.ArchiveError, match="password"):
            hashing.hash_7z(arc)
        arc.unlink()


@pytestmark_7z
def test_rom_with_an_awkward_name_is_identified_and_patched(tmp_path, monkeypatch):
    src = tmp_path / "src"
    put(src / "-Sup\u00e9r.sfc", GAMES["snes"][1])
    arc = tmp_path / "g.7z"
    subprocess.run([hashing.SEVENZIP, "a", "-bd", str(arc), "."], cwd=src, capture_output=True, check=True)
    assert identify.identify_archive(arc).system == "snes"
    monkeypatch.setattr(patching, "_identify", lambda p, c=None: ("", ""))
    (tmp_path / "p.ips").write_bytes(b"PATCH" + b"\0\0\x10" + b"\0\x01" + b"Z" + b"EOF")
    patching.patch_rom(arc, [tmp_path / "p.ips"], tmp_path / "out.sfc", tmp_path / "tmp")
    assert (tmp_path / "out.sfc").read_bytes()[16:17] == b"Z" and (tmp_path / "out.sfc").stat().st_size == len(GAMES["snes"][1])


@pytestmark_7z
def test_solid_archive_is_unpacked_once(tmp_path, monkeypatch):
    arc, want = pack(tmp_path, [f"r{n:02d}.nes" for n in range(30)], "-ms=on")
    n = [0]
    real = subprocess.Popen

    class Count(real):
        def __init__(self, *a, **k):
            n[0] += 1
            super().__init__(*a, **k)
    monkeypatch.setattr(hashing.subprocess, "Popen", Count)
    assert len(hashing.hash_7z(arc)) == 30 and n[0] <= 2
