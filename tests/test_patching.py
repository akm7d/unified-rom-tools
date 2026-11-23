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
