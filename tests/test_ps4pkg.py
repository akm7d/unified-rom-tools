"""The PS4 package reader and the beta extraction task, on tiny encrypted packages built here from scratch."""

import hashlib
import hmac
import os
import struct
import sys
import threading
import zlib

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from romtools import converter as cv
from romtools import ps4pkg, ps4tool, tasks

BS = 0x10000
CID = "UP0001-CUSA11111_00-TESTGAME00000000"
DIR, FILE = 0x416D, 0x816D


def dirent(ino, typ, name):
    n = name.encode()
    size = (16 + len(n) + 1 + 7) & ~7
    return struct.pack("<iiii", ino, typ, len(n), size) + n.ljust(size - 16, b"\0")


def pfs(tree, signed, names=None):
    """tree: list of (mode, data bytes); inode n = tree[n]. Data is laid out one block run after another."""
    isize = 0x2C8 if signed else 0xA8
    per = BS // isize
    iblocks = (len(tree) + per - 1) // per
    table, data, block = bytearray(iblocks * BS), bytearray(), 1 + iblocks
    for n, (mode, body) in enumerate(tree):
        blocks = max(1, (len(body) + BS - 1) // BS)
        ino = bytearray(isize)
        struct.pack_into("<HHIqq", ino, 0, mode, 1, 0, len(body), len(body))
        struct.pack_into("<I", ino, 0x60, blocks)
        step, at = (36, 0x64 + 32) if signed else (4, 0x64)
        for k in range(17):
            struct.pack_into("<i", ino, at + step * k, block if k == 0 else (block + k if signed and k < blocks else -1))
        at = (n // per) * BS + (n % per) * isize
        table[at:at + isize] = ino
        data += body.ljust(blocks * BS, b"\0")
        block += blocks
    head = bytearray(BS)
    struct.pack_into("<qqq", head, 0, 1, ps4pkg.PFS_MAGIC, 0)
    struct.pack_into("<H", head, 0x1C, 13 if signed else 8)
    struct.pack_into("<I", head, 0x20, BS)
    struct.pack_into("<qqqq", head, 0x30, len(tree), 0, iblocks, 0)
    head[0x370:0x380] = b"seed-seed-seed-!"
    return bytes(head) + bytes(table) + bytes(data)


def build_pkg(path, new_crypt=False, passcode="0" * 32, evil=None):
    files = {"eboot.bin": b"\x4f\x15\x3d\x1d" + os.urandom(70000), "zero.bin": b"\0" * 200000, "empty.bin": b"",
             "text.txt": b"hello " * 30000}
    sub = {"data.bin": os.urandom(BS * 2 + 123)}
    tree, root = [None, None], []
    for name, body in files.items():
        root.append(dirent(len(tree), 2, evil if evil and name == "text.txt" else name))
        tree.append((FILE, body))
    subdir = len(tree)
    tree.append(None)
    root.append(dirent(subdir, 3, "sub"))
    tree[subdir] = (DIR, dirent(len(tree), 2, "data.bin"))
    tree.append((FILE, sub["data.bin"]))
    tree[0] = (DIR, dirent(1, 3, "uroot"))
    tree[1] = (DIR, dirent(1, 4, ".") + dirent(1, 5, "..") + b"".join(root))
    inner = pfs(tree, signed=False)
    blocks = []
    for i in range(0, len(inner), BS):                     # PFSC: compressed, stored, or "all zeros" blocks
        b = inner[i:i + BS].ljust(BS, b"\0")
        z = zlib.compress(b)
        blocks.append(None if not any(b) and i else z if len(z) < BS else b)
    n = len(blocks)
    pos, offs, body = 0x10000, [], bytearray()
    for b in blocks:
        offs.append(pos)
        chunk = b if b is not None else bytes(BS + 16)      # longer than a block = zeros
        body += chunk
        pos += len(chunk)
    offs.append(pos)
    head = b"PFSC" + struct.pack("<IIIQQQQ", 0, 6, BS, BS, 0x400, 0x10000, n * BS)
    pfsc = (head.ljust(0x400, b"\0") + struct.pack(f"<{n + 1}Q", *offs)).ljust(0x10000, b"\0") + bytes(body)
    outer = pfs([(DIR, dirent(1, 3, "uroot")), (DIR, dirent(2, 2, "pfs_image.dat")), (FILE, pfsc)], signed=True)
    ek = ps4pkg._compute_key(CID, passcode, 1)
    if new_crypt:
        ek = hmac.new(ek, outer[0x370:0x380], hashlib.sha256).digest()
    k = hmac.new(ek, struct.pack("<I", 1) + outer[0x370:0x380], hashlib.sha256).digest()
    enc = bytearray(outer)
    for s in range(16, len(outer) // 0x1000):
        e = Cipher(algorithms.AES(k[16:] + k[:16]), modes.XTS(s.to_bytes(16, "little"))).encryptor()
        enc[s * 0x1000:(s + 1) * 0x1000] = e.update(outer[s * 0x1000:(s + 1) * 0x1000])
    keys, vals = b"", b""
    idx = b""
    for name, v in (("APP_VER", "01.00"), ("CATEGORY", "gd"), ("CONTENT_ID", CID), ("TITLE", "Test"),
                    ("TITLE_ID", "CUSA11111")):
        raw = v.encode() + b"\0"
        idx += struct.pack("<HHIII", len(keys), 0x204, len(raw), len(raw), len(vals))
        keys += name.encode() + b"\0"
        vals += raw
    sfo = b"\0PSF" + struct.pack("<IIII", 0x101, 20 + len(idx), 20 + len(idx) + len(keys), 5) + idx + keys + vals
    h = bytearray(0x2000)
    h[:4] = b"\x7fCNT"
    struct.pack_into(">I", h, 0x10, 1)
    struct.pack_into(">I", h, 0x18, 0x600)
    h[0x40:0x40 + len(CID)] = CID.encode()
    struct.pack_into(">Q", h, 0x408, (1 << 61) if new_crypt else 0)
    struct.pack_into(">QQ", h, 0x410, 0x2000, len(enc))
    struct.pack_into(">IIIIII", h, 0x600, 0x1000, 0, 0, 0, 0x800, len(sfo))
    h[0x800:0x800 + len(sfo)] = sfo
    path.write_bytes(bytes(h) + bytes(enc))
    return {**files, "sub/data.bin": sub["data.bin"]}, sfo


@pytest.mark.parametrize("new_crypt", [False, True])
def test_reader_gives_back_every_file(tmp_path, new_crypt):
    want, _sfo = build_pkg(tmp_path / "g.pkg", new_crypt)
    with ps4pkg.PkgReader(tmp_path / "g.pkg") as r:
        assert r.new_crypt == new_crypt
        got = {f.path: b"".join(r.chunks(f)) for f in r.files()}
    assert got == want


def test_other_passcode_and_damage_are_refused(tmp_path):
    build_pkg(tmp_path / "other.pkg", passcode="A" * 32)
    with pytest.raises(ps4pkg.RetailPackage):
        ps4pkg.PkgReader(tmp_path / "other.pkg")
    ps4pkg.PkgReader(tmp_path / "other.pkg", passcode="A" * 32).close()
    build_pkg(tmp_path / "cut.pkg")
    data = (tmp_path / "cut.pkg").read_bytes()
    (tmp_path / "cut.pkg").write_bytes(data[:-0x8000])
    with pytest.raises(ps4pkg.Ps4PkgError, match="incomplete"):
        ps4pkg.PkgReader(tmp_path / "cut.pkg")


@pytest.mark.parametrize("name", ["../evil.txt", "..", "a/b.txt", "a\\b.txt"])
def test_unsafe_names_are_refused(tmp_path, name):
    build_pkg(tmp_path / "evil.pkg", evil=name)
    with ps4pkg.PkgReader(tmp_path / "evil.pkg") as r, pytest.raises(ps4pkg.Ps4PkgError, match="unsafe"):
        r.files()


FAKE = '''import sys, pathlib
sys.path.insert(0, {root!r})
from romtools import ps4pkg
out = pathlib.Path(sys.argv[2]) / "CUSA11111"
(out / "sce_sys").mkdir(parents=True)
with ps4pkg.PkgReader(sys.argv[1]) as r:
    for f in r.files():
        p = out / f.path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"".join(r.chunks(f)))
    (out / "sce_sys" / "param.sfo").write_bytes(r.entry(0x1000))
{damage}
'''                        # no exit code for failure: the real extractor always "succeeds" too


@pytest.mark.parametrize("damage, problem", [
    ("", None),
    ("b = bytearray((out/'eboot.bin').read_bytes()); b[5000] ^= 1; (out/'eboot.bin').write_bytes(b)", "1 different"),
    ("(out/'sub'/'data.bin').unlink()", "1 file(s) missing"),
    ("import os; os.truncate(out/'text.txt', 10)", "1 different"),
    ("import shutil; shutil.rmtree(out)", "no game folder"),
])
@pytest.mark.skipif(sys.platform == "win32", reason="uses a script as the stand-in extractor")
def test_task_only_keeps_output_that_matches(tmp_path, monkeypatch, damage, problem):
    from pathlib import Path
    build_pkg(tmp_path / "g.pkg")
    before = (tmp_path / "g.pkg").read_bytes()
    tool = tmp_path / "fake_extractor"
    tool.write_text(f"#!{sys.executable}\n" + FAKE.format(root=str(Path(__file__).parent.parent), damage=damage))
    tool.chmod(0o755)
    monkeypatch.setattr(ps4tool, "program", lambda: tool)
    ctx = tasks.TaskContext(lambda s: None, lambda *a, **k: None, threading.Event(), lambda p: True)
    out = tmp_path / "out" / "CUSA11111"
    if problem is None:
        assert "every byte checked" in tasks.extract_ps4(tmp_path / "g.pkg", out)(ctx)
        assert (out / "sub" / "data.bin").is_file()
    else:
        with pytest.raises(RuntimeError, match=problem.replace("(", r"\(").replace(")", r"\)")):
            tasks.extract_ps4(tmp_path / "g.pkg", out)(ctx)
        assert not out.exists()
    assert (tmp_path / "g.pkg").read_bytes() == before
    assert not [p for p in tmp_path.rglob(".romtools-ps4-*")]


def test_planner_explains_what_it_skips(tmp_path):
    build_pkg(tmp_path / "ok.pkg")
    build_pkg(tmp_path / "new.pkg", new_crypt=True)
    build_pkg(tmp_path / "retail.pkg", passcode="A" * 32)
    jobs = {j.source.name: j for j in cv.plan(cv.PS4_EXTRACT, sorted(tmp_path.glob("*.pkg")), cv.Options())}
    assert jobs["ok.pkg"].status == "Queued" and "BETA" in jobs["ok.pkg"].detail and not jobs["ok.pkg"].trash
    assert "newer key scheme" in jobs["new.pkg"].message and "official" in jobs["retail.pkg"].message


def test_download_must_match_its_checksum():
    with pytest.raises(RuntimeError, match="checksum"):
        ps4tool.install(b"not the real download")
    assert ps4tool.program() is None
