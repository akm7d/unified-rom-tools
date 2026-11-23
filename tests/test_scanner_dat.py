import zlib

from romtools import scanner as sc
from romtools.dat import DatIndex, parse_dat
from romtools.hashing import hash_file


def test_header_detection(tmp_path):
    p = tmp_path / "t.nes"
    data = b"NES\x1a" + b"\0" * 12 + bytes(range(256)) * 64
    p.write_bytes(data)
    e = hash_file(p)
    assert e.full.crc == f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"
    assert e.headerless and e.header_len == 16


def test_verified_misnamed_unknown(tmp_path):
    rom = b"test"
    xml = ('<?xml version="1.0"?><datafile><header><name>Test</name></header>'
           f'<game name="Game (USA)"><rom name="Game (USA).bin" size="4" crc="{zlib.crc32(rom):08x}" '
           'sha1="a94a8fe5ccb19ba61c4c0873d391e987982fbbd3"/></game></datafile>')
    (tmp_path / "t.dat").write_text(xml)
    idx = DatIndex([parse_dat(tmp_path / "t.dat")])
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "Game (USA).bin").write_bytes(rom)
    (lib / "wrong name.bin").write_bytes(rom)
    (lib / "other.bin").write_bytes(b"nope")
    s = sc.Scanner(tmp_path)
    st = {p: s.check(sc.Item("test", lib / p, "file"), idx).status
          for p in ("Game (USA).bin", "wrong name.bin", "other.bin")}
    assert st == {"Game (USA).bin": sc.VERIFIED, "wrong name.bin": sc.MISNAMED, "other.bin": sc.UNKNOWN}
