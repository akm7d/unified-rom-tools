import struct

import pytest

from romtools import patching as pt

ORIG = bytes(range(256)) * 2048
MOD = ORIG[:100] + b"PATCHED!" + ORIG[108:]


def test_ips():
    ips = b"PATCH" + (100).to_bytes(3, "big") + (8).to_bytes(2, "big") + b"PATCHED!" + b"EOF"
    assert pt.apply_ips(ORIG, ips) == MOD


def test_bps_roundtrip_and_checksums(tmp_path):
    patch = pt.create_bps(ORIG, MOD)
    assert pt.apply_bps(ORIG, patch) == MOD
    (tmp_path / "p.bps").write_bytes(patch)
    (tmp_path / "orig.bin").write_bytes(ORIG)
    (tmp_path / "other.bin").write_bytes(ORIG[::-1])
    pt.apply_file(tmp_path / "orig.bin", [tmp_path / "p.bps"], tmp_path / "out.bin")
    assert (tmp_path / "out.bin").read_bytes() == MOD
    with pytest.raises(pt.PatchError, match="isn't the ROM"):          # BPS records the original's CRC32
        pt.apply_file(tmp_path / "other.bin", [tmp_path / "p.bps"], tmp_path / "out2.bin")
    with pytest.raises(pt.PatchError, match="already has the patch"):
        pt.apply_file(tmp_path / "out.bin", [tmp_path / "p.bps"], tmp_path / "out3.bin")
    assert not (tmp_path / "out2.bin").exists() and not (tmp_path / "out3.bin").exists()


def test_ppf(tmp_path):
    (tmp_path / "o.bin").write_bytes(ORIG)
    (tmp_path / "p.ppf").write_bytes(b"PPF10\x00" + b"t".ljust(50) + struct.pack("<I", 100) + bytes([8]) + b"PATCHED!")
    pt.apply_ppf(tmp_path / "o.bin", tmp_path / "p.ppf", tmp_path / "r.bin")
    assert (tmp_path / "r.bin").read_bytes() == MOD


def test_ips_change_at_the_offset_that_spells_eof():
    from romtools import patching as pt
    src = bytes(0x460000)
    rec = lambda off, data: off.to_bytes(3, "big") + len(data).to_bytes(2, "big") + data            # noqa: E731
    p = b"PATCH" + rec(0x454F46, b"Q") + rec(5, b"Z") + b"EOF"
    o = pt.apply_ips(src, p)
    assert (o[0x454F46:0x454F47], o[5:6]) == (b"Q", b"Z")
    rle = b"PATCH" + b"EOF" + b"\0\0" + b"\0\x03" + b"R" + b"EOF"                                  # the same, run-length
    assert pt.apply_ips(src, rle)[0x454F46:0x454F49] == b"RRR"
    assert pt.apply_ips(b"abcdef", b"PATCH" + rec(1, b"X") + b"EOF") == b"aXcdef"
    assert pt.apply_ips(b"abcdef", b"PATCH" + rec(1, b"X") + b"EOF" + b"\0\0\x03") == b"aXc"          # cut to 3 bytes
    assert pt.apply_ips(b"abcdef", b"PATCH" + rec(1, b"X") + b'EOF{"title": "t"}') == b"aXcdef"       # EBP
    assert pt.apply_ips(b"abcdef", b"PATCH" + rec(1, b"X") + b"EOF" + b"trailing junk") == b"aXcdef"
    import pytest
    for cut in (b"PATCH" + b"\0\0\1\0\x09abc", b"PATCH" + rec(1, b"X")):
        with pytest.raises(pt.PatchError):
            pt.apply_ips(b"abcdef", cut)


CUES = ['FILE "game.bin" BINARY', "FILE game.bin BINARY", '  file  "game.bin"   binary', 'FILE "game.bin" BINARY\r']


def test_patched_cue_points_to_the_patched_track(tmp_path, monkeypatch):
    from romtools import patching as pt
    monkeypatch.setattr(pt, "_identify", lambda p, c=None: ("", ""))
    for k, first in enumerate(CUES):
        d = tmp_path / str(k)
        d.mkdir()
        (d / "game.bin").write_bytes(b"A" * 5000)
        (d / "game.cue").write_bytes((first + "\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n").encode())
        (d / "p.ips").write_bytes(b"PATCH" + b"\0\0\x10" + b"\0\x01" + b"Z" + b"EOF")
        pt.patch_rom(d / "game.cue", [d / "p.ips"], d / "Caf\u00e9.cue", tmp_path / "tmp")
        assert '"Caf\u00e9.bin"' in (d / "Caf\u00e9.cue").read_bytes().decode().splitlines()[0]
        assert (d / "Caf\u00e9.bin").read_bytes()[16:17] == b"Z" and (d / "game.bin").read_bytes() == b"A" * 5000


def test_stopping_a_patch_leaves_nothing(tmp_path, monkeypatch):
    import threading
    import pytest
    from romtools import patching as pt
    from romtools.hashing import Cancelled
    monkeypatch.setattr(pt, "_identify", lambda p, c=None: ("", ""))
    src = tmp_path / "s.sfc"
    src.write_bytes(b"x" * 100000)
    (tmp_path / "p.ips").write_bytes(b"PATCH" + b"\0\0\x10" + b"\0\x01" + b"Z" + b"EOF")
    for when in range(4):
        cancel = threading.Event()
        calls = [0]
        real = pt._check

        def check(c, n=when):
            calls[0] += 1
            if calls[0] > n:
                cancel.set()
            real(c)
        monkeypatch.setattr(pt, "_check", check)
        with pytest.raises(Cancelled):
            pt.patch_rom(src, [tmp_path / "p.ips"], tmp_path / "out.sfc", tmp_path / "tmp", cancel=cancel)
        monkeypatch.setattr(pt, "_check", real)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["p.ips", "s.sfc", "tmp"] and not any((tmp_path / "tmp").iterdir())
    pt.patch_rom(src, [tmp_path / "p.ips"], tmp_path / "out.sfc", tmp_path / "tmp")
    assert (tmp_path / "out.sfc").read_bytes()[16:17] == b"Z"


def test_patching_a_chd_still_works(tmp_path, monkeypatch):
    import os
    import subprocess
    import pytest
    from romtools import patching as pt
    from romtools.chdman_tool import chdman_bin
    if not chdman_bin():
        pytest.skip("no chdman")
    monkeypatch.setattr(pt, "_identify", lambda p, c=None: ("", ""))
    iso = tmp_path / "g.iso"
    iso.write_bytes(os.urandom(2048 * 200))
    for cmd in ("createdvd", "createcd"):
        f = tmp_path / f"{cmd}.chd"
        subprocess.run([chdman_bin(), cmd, "-i", str(iso), "-o", str(f)], capture_output=True, check=True)
        (tmp_path / "p.ips").write_bytes(b"PATCH" + b"\0\0\x10" + b"\0\x04" + b"ZZZZ" + b"EOF")
        pt.patch_rom(f, [tmp_path / "p.ips"], tmp_path / f"{cmd}-out.chd", tmp_path / "tmp")
        back = tmp_path / f"{cmd}.iso"
        ex = ["extractdvd", "-o", str(back)] if cmd == "createdvd" else ["extractcd", "-o", str(tmp_path / "b.cue"), "-ob", str(back)]
        subprocess.run([chdman_bin(), ex[0], "-i", str(tmp_path / f"{cmd}-out.chd"), *ex[1:], "-f"], capture_output=True, check=True)
        assert back.read_bytes()[16:20] == b"ZZZZ" and back.read_bytes()[20:] == iso.read_bytes()[20:]
    assert not [p.name for p in tmp_path.iterdir() if p.name.startswith(".romtools")]
