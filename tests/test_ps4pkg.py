"""The PS4 package reader / extractor, on tiny encrypted and signed packages built here from scratch."""

import hashlib
import hmac
import os
import struct
import threading
import zlib

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from romtools import converter as cv
from romtools import ps4pkg, tasks

BS = 0x10000
CID = "UP0001-CUSA11111_00-TESTGAME00000000"
DIR, FILE = 0x416D, 0x816D
SEED = b"seed-seed-seed-!"


def dirent(ino, typ, name):
    n = name.encode()
    size = (16 + len(n) + 1 + 7) & ~7
    return struct.pack("<iiii", ino, typ, len(n), size) + n.ljust(size - 16, b"\0")


def pfs(tree, sign_key=None):
    """tree: list of (mode, data bytes); inode n = tree[n]. Data is laid out one block run after another. With a
    sign_key it's a signed PFS: every block's HMAC is in the inode, or in signed tables it points to."""
    signed = sign_key is not None
    isize = 0x2C8 if signed else 0xA8
    per = BS // isize
    iblocks = (len(tree) + per - 1) // per
    table, data, block, tables = bytearray(iblocks * BS), bytearray(), 1 + iblocks, []
    at0 = 1 + iblocks + sum(max(1, (len(b) + BS - 1) // BS) for _m, b in tree)      # signature tables go last

    def mac(b):
        return hmac.new(sign_key, b, hashlib.sha256).digest()
    for n, (mode, body) in enumerate(tree):
        blocks = max(1, (len(body) + BS - 1) // BS)
        ino = bytearray(isize)
        struct.pack_into("<HHIqq", ino, 0, mode, 1, 0, len(body), len(body))
        struct.pack_into("<I", ino, 0x60, blocks)
        raw = body.ljust(blocks * BS, b"\0")
        if signed:
            sigs = [mac(raw[k * BS:(k + 1) * BS]) + struct.pack("<i", block + k) for k in range(blocks)]
            slots = sigs[:12] + [bytes(32) + struct.pack("<i", -1)] * (12 - len(sigs[:12]))
            if len(sigs) > 12:
                t = b"".join(sigs[12:]).ljust(BS, b"\0")
                assert len(t) == BS
                slots.append(mac(t) + struct.pack("<i", at0 + len(tables)))
                tables.append(t)
            ino[0x64:0x64 + 36 * len(slots)] = b"".join(slots)
        else:
            for k in range(17):
                struct.pack_into("<i", ino, 0x64 + 4 * k, block if k == 0 else -1)
        at = (n // per) * BS + (n % per) * isize
        table[at:at + isize] = ino
        data += raw
        block += blocks
    head = bytearray(BS)
    struct.pack_into("<qqq", head, 0, 1, ps4pkg.PFS_MAGIC, 0)
    struct.pack_into("<H", head, 0x1C, 13 if signed else 8)
    struct.pack_into("<I", head, 0x20, BS)
    struct.pack_into("<qqqq", head, 0x30, len(tree), 0, iblocks, 0)
    head[0x370:0x380] = SEED
    return bytes(head) + bytes(table) + bytes(data) + b"".join(tables)


def build_pkg(path, new_crypt=False, passcode="0" * 32, evil=None, kind="gd", ver="01.00", only=None, extra=None):
    files = {"eboot.bin": b"\x4f\x15\x3d\x1d" + os.urandom(70000), "zero.bin": b"\0" * 200000, "empty.bin": b"",
             "text.txt": b"hello " * 30000}
    if only is not None:                                   # an update: some of the game's files, and new ones
        files = {n: os.urandom(90000) for n in only}
    files.update(extra or {})
    sub = {"data.bin": os.urandom(BS * 14 + 123)}        # more than 12 blocks: needs a signature table
    tree, root = [None, None], []
    for name, body in files.items():
        root.append(dirent(len(tree), 2, evil if evil and evil != "entry" and name == "text.txt" else name))
        tree.append((FILE, body))
    subdir = len(tree)
    tree.append(None)
    root.append(dirent(subdir, 3, "sub"))
    tree[subdir] = (DIR, dirent(len(tree), 2, "data.bin"))
    tree.append((FILE, sub["data.bin"]))
    tree[0] = (DIR, dirent(1, 3, "uroot"))
    tree[1] = (DIR, dirent(1, 4, ".") + dirent(1, 5, "..") + b"".join(root))
    inner = pfs(tree)
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
    ek = ps4pkg._compute_key(CID, passcode, 1)
    if new_crypt:
        ek = hmac.new(ek, SEED, hashlib.sha256).digest()
    k = hmac.new(ek, struct.pack("<I", 1) + SEED, hashlib.sha256).digest()
    outer = pfs([(DIR, dirent(1, 3, "uroot")), (DIR, dirent(2, 2, "pfs_image.dat")), (FILE, pfsc)],
                sign_key=hmac.new(ek, struct.pack("<I", 2) + SEED, hashlib.sha256).digest())
    enc = bytearray(outer)
    for s in range(16, len(outer) // 0x1000):
        e = Cipher(algorithms.AES(k[16:] + k[:16]), modes.XTS(s.to_bytes(16, "little"))).encryptor()
        enc[s * 0x1000:(s + 1) * 0x1000] = e.update(outer[s * 0x1000:(s + 1) * 0x1000])
    keys, vals = b"", b""
    idx = b""
    for name, v in (("APP_VER", ver), ("CATEGORY", kind), ("CONTENT_ID", CID), ("TITLE", "Test"),
                    ("TITLE_ID", "CUSA11111")):
        raw = v.encode() + b"\0"
        idx += struct.pack("<HHIII", len(keys), 0x204, len(raw), len(raw), len(vals))
        keys += name.encode() + b"\0"
        vals += raw
    sfo = b"\0PSF" + struct.pack("<IIII", 0x101, 20 + len(idx), 20 + len(idx) + len(keys), 5) + idx + keys + vals
    h = bytearray(0x2000)
    h[:4] = b"\x7fCNT"
    names = b"\0" + b"param.sfo\0" + (b"../../up.png" if evil == "entry" else b"icon0.png") + b"\0"
    secret = b"NPTD" + bytes(range(150))                   # not a multiple of 16, like the real npbind.dat
    rows = [(0x200, 0, 0, 0, 0x1000, len(names)), (0x1000, 1, 0, 0, 0x800, len(sfo)), (0x1200, 11, 0, 0, 0x1200, 9),
            (0x402, 0, 0x80000000, 2 << 12, 0x1400, len(secret)), (0x10, 0, 0, 0, 0x1600, 16),
            (0x1, 0, 0, 0, 0x1700, 6 * 32)]
    struct.pack_into(">I", h, 0x10, len(rows))
    struct.pack_into(">I", h, 0x18, 0x600)
    h[0x40:0x40 + len(CID)] = CID.encode()
    struct.pack_into(">Q", h, 0x408, (1 << 61) if new_crypt else 0)
    struct.pack_into(">QQ", h, 0x410, 0x2000, len(enc))
    for i, row in enumerate(rows):
        struct.pack_into(">IIIIII", h, 0x600 + 32 * i, *row)
    h[0x800:0x800 + len(sfo)] = sfo
    h[0x1000:0x1000 + len(names)] = names
    h[0x1200:0x1209] = b"PNG-bytes"
    ik = hashlib.sha256(bytes(h[0x600 + 96:0x600 + 128]) + ps4pkg._compute_key(CID, passcode, 2)).digest()
    ct = Cipher(algorithms.AES(ik[16:]), modes.CBC(ik[:16])).encryptor().update(secret.ljust(160, b"\0"))
    h[0x1400:0x1400 + 160] = ct
    digests = b"".join(hashlib.sha256(bytes(h[o:o + ((s + 15) & ~15 if fl else s)])).digest()
                       for _i, _n, fl, _k, o, s in rows[:-1]) + bytes(32)
    h[0x1700:0x1700 + len(digests)] = digests
    h[0x140:0x160] = hashlib.sha256(digests).digest()
    path.write_bytes(bytes(h) + bytes(enc))
    return ({**files, "sub/data.bin": sub["data.bin"]},
            {"sce_sys/param.sfo": sfo, "sce_sys/icon0.png": b"PNG-bytes", "sce_sys/nptitle.dat": secret})


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


def ctx(cancel=None):
    return tasks.TaskContext(lambda s: None, lambda *a, **k: None, cancel or threading.Event(), lambda p: True)


def tree_of(d):
    return {str(p.relative_to(d)).replace("\\", "/"): p.read_bytes() for p in d.rglob("*") if p.is_file()}


@pytest.mark.parametrize("new_crypt", [False, True])
def test_task_extracts_game_and_system_files(tmp_path, new_crypt):
    game, system = build_pkg(tmp_path / "g.pkg", new_crypt)
    before = (tmp_path / "g.pkg").read_bytes()
    out = tmp_path / "out" / "CUSA11111"
    assert "matched the package's signatures" in tasks.extract_ps4(tmp_path / "g.pkg", out)(ctx())
    assert tree_of(out) == {**game, **system}              # incl. the decrypted entry; not the internal key table
    assert (tmp_path / "g.pkg").read_bytes() == before
    with pytest.raises(tasks.TaskSkipped):
        tasks.extract_ps4(tmp_path / "g.pkg", out)(ctx())


def test_any_damage_is_caught_and_nothing_is_kept(tmp_path):
    game, system = build_pkg(tmp_path / "g.pkg")
    want, caught, unused = {**game, **system}, 0, 0
    good = (tmp_path / "g.pkg").read_bytes()
    with ps4pkg.PkgReader(tmp_path / "g.pkg") as r:
        data = [0x2000 + b * BS for b in r._signed if b * BS < r.pfsc_at + r.map[-1]]
    assert len(data) > 12                                  # so the signature table is covered too
    for n, at in enumerate(data + [len(good) - BS + 5]):   # every data block, then the signature table itself
        bad = bytearray(good)
        bad[at + 777 * (n + 1) % BS if n < len(data) else at] ^= 1
        (tmp_path / "bad.pkg").write_bytes(bad)
        out = tmp_path / f"out{n}"
        try:
            tasks.extract_ps4(tmp_path / "bad.pkg", out)(ctx())
            assert tree_of(out) == want                    # only bytes nothing uses (a run stored as zeros) changed
            unused += 1
        except ps4pkg.Ps4PkgError as exc:
            assert "damaged" in str(exc) and not out.exists()
            caught += 1
        assert not list(tmp_path.glob(".romtools-ps4-*"))
    assert caught >= len(data) - 4 and unused <= 4
    for at in (0x805, 0x1203, 0x1405, 0x1702, 0x150):      # param.sfo, icon, encrypted entry, digests, header hash
        bad = bytearray(good)
        bad[at] ^= 1
        (tmp_path / "bad.pkg").write_bytes(bad)
        with pytest.raises(ps4pkg.Ps4PkgError, match="damaged"):
            tasks.extract_ps4(tmp_path / "bad.pkg", tmp_path / "sys")(ctx())
        assert not (tmp_path / "sys").exists()


def test_bad_write_stop_and_full_disk_keep_nothing(tmp_path, monkeypatch):
    import shutil
    build_pkg(tmp_path / "g.pkg")
    out, real = tmp_path / "out", ps4pkg.hashlib.sha256
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(Exception):
        tasks.extract_ps4(tmp_path / "g.pkg", out)(ctx(stopped))
    assert not out.exists() and not list(tmp_path.glob(".romtools-ps4-*"))
    monkeypatch.setattr(shutil, "disk_usage", lambda p: type("U", (), {"free": 1000})())
    with pytest.raises(RuntimeError, match="not enough space"):
        tasks.extract_ps4(tmp_path / "g.pkg", out)(ctx())
    monkeypatch.undo()
    flipped = []

    def progress(done, total, what):                       # corrupt a file after it's written, before it's re-read
        if what == "Checking what was written" and not flipped:
            flipped.append(1)
            f = tmp_path / "work" / "sub" / "data.bin"
            b = bytearray(f.read_bytes())
            b[-1] ^= 1
            f.write_bytes(b)
    (tmp_path / "work").mkdir()
    with pytest.raises(ps4pkg.Ps4PkgError, match="reads back differently"):
        ps4pkg.extract(tmp_path / "g.pkg", tmp_path / "work", progress)
    assert real is ps4pkg.hashlib.sha256


def test_unsafe_system_file_name_is_refused(tmp_path):
    build_pkg(tmp_path / "evil.pkg", evil="entry")
    with pytest.raises(ps4pkg.Ps4PkgError, match="unsafe"):
        tasks.extract_ps4(tmp_path / "evil.pkg", tmp_path / "out")(ctx())
    assert not (tmp_path / "up.png").exists() and not (tmp_path.parent / "up.png").exists()


def test_planner_explains_what_it_skips(tmp_path):
    build_pkg(tmp_path / "ok.pkg")
    build_pkg(tmp_path / "new.pkg", new_crypt=True)
    build_pkg(tmp_path / "retail.pkg", passcode="A" * 32)
    jobs = {j.source.name: j for j in cv.plan(cv.PS4_EXTRACT, sorted(tmp_path.glob("*.pkg")), cv.Options())}
    assert jobs["ok.pkg"].status == "Queued" and "BETA" in jobs["ok.pkg"].detail and not jobs["ok.pkg"].trash
    assert jobs["new.pkg"].status == "Queued" and "official" in jobs["retail.pkg"].message


# ---------------------------------------------------------------- a game and its update in one folder

def game_and_update(d, **kw):
    d.mkdir(parents=True, exist_ok=True)
    game = build_pkg(d / "game.pkg")
    upd = build_pkg(d / "upd.pkg", kind="gp", ver="01.05", only=["eboot.bin"], extra={"NEW.bin": b"new" * 999}, **kw)
    return {**game[0], **game[1]}, {**upd[0], **upd[1]}


def run_all(jobs):
    runner = cv.Runner(jobs, 1, lambda j: None)
    for j in jobs:
        if j.status == "Queued" and j.follows is None:
            runner._run_and_keep_log(j)


def test_update_is_merged_into_the_game(tmp_path):
    game, upd = game_and_update(tmp_path / "in")
    before = tree_of(tmp_path / "in")
    for pick in (["game.pkg"], ["upd.pkg"], ["game.pkg", "upd.pkg"]):       # either one finds the other next to it
        out = tmp_path / "-".join(pick)
        jobs = cv.plan(cv.PS4_EXTRACT, [tmp_path / "in" / n for n in pick], cv.Options(output_dir=str(out)))
        assert [(j.status, bool(j.follows)) for j in jobs] == [("Queued", False), ("Queued", True)], [j.title for j in jobs]
        assert "Game v1.00; update v1.05 (next row) is merged in → CUSA11111/" in jobs[0].detail
        run_all(jobs)
        assert [j.status for j in jobs] == ["Done", "Done"], jobs[0].message
        assert tree_of(out) == {f"CUSA11111/{k}": v for k, v in {**game, **upd}.items()}    # the update wins, file by file
    assert tree_of(tmp_path / "in") == before


def test_merging_can_be_turned_off(tmp_path):
    game, upd = game_and_update(tmp_path / "in")
    opts = cv.Options(output_dir=str(tmp_path / "out"), ps4_merge=False)
    run_all(cv.plan(cv.PS4_EXTRACT, sorted((tmp_path / "in").glob("*.pkg")), opts))
    assert tree_of(tmp_path / "out") == {**{f"CUSA11111/{k}": v for k, v in game.items()},
                                         **{f"CUSA11111-patch/{k}": v for k, v in upd.items()}}


def test_newest_usable_update_wins(tmp_path):
    game_and_update(tmp_path)
    build_pkg(tmp_path / "old.pkg", kind="gp", ver="01.02", only=["eboot.bin"])
    build_pkg(tmp_path / "official.pkg", kind="gp", ver="01.09", only=["eboot.bin"], passcode="A" * 32)
    jobs = {j.source.name: j for j in cv.plan(cv.PS4_EXTRACT, [tmp_path / "game.pkg"], cv.Options())}
    assert jobs["upd.pkg"].follows is jobs["game.pkg"] and jobs["game.pkg"].status == "Queued"
    assert jobs["old.pkg"].status == "Skipped" and "newer" in jobs["old.pkg"].message
    assert jobs["official.pkg"].status == "Skipped" and "not applied" in jobs["official.pkg"].message


def test_update_goes_into_an_extracted_game_and_keeps_what_it_replaces(tmp_path):
    game, upd = game_and_update(tmp_path / "in")
    out = tmp_path / "out"
    tasks.extract_ps4(tmp_path / "in" / "game.pkg", out / "CUSA11111")(ctx())
    (out / "CUSA11111" / "mod.txt").write_bytes(b"mine")
    os.rename(out / "CUSA11111" / "sub", out / "CUSA11111" / "SUB")          # another spelling: the same folder
    only_update = tmp_path / "elsewhere"
    only_update.mkdir()
    os.rename(tmp_path / "in" / "upd.pkg", only_update / "upd.pkg")
    opts = cv.Options(output_dir=str(out))
    jobs = cv.plan(cv.PS4_EXTRACT, [only_update / "upd.pkg"], opts)
    run_all(jobs)
    assert [j.status for j in jobs] == ["Done"], jobs[0].message
    got = tree_of(out / "CUSA11111")
    want = {k.replace("sub/", "SUB/"): v for k, v in {**game, **upd, "mod.txt": b"mine"}.items()}
    assert got == want
    kept = tree_of(out / "CUSA11111 (replaced by update v1.05)")
    assert kept == {k.replace("sub/", "SUB/"): game[k] for k in game if k in upd}
    again = cv.plan(cv.PS4_EXTRACT, [only_update / "upd.pkg"], opts)
    assert again[0].status == "Skipped" and "already at v1.05" in again[0].message
    # picking the game again, with the update next to it, doesn't extract anything twice either
    again = cv.plan(cv.PS4_EXTRACT, [tmp_path / "in" / "game.pkg", only_update / "upd.pkg"], opts)
    run_all(again)
    assert not any(j.status == "Done" for j in again) and tree_of(out / "CUSA11111") == want


def test_failed_update_leaves_the_game_as_it_was(tmp_path, monkeypatch):
    game_and_update(tmp_path / "in")
    home = tmp_path / "out" / "CUSA11111"
    tasks.extract_ps4(tmp_path / "in" / "game.pkg", home)(ctx())
    before = tree_of(tmp_path / "out")
    real, n = os.rename, [0]

    def flaky(a, b):
        if ".romtools-ps4-" in str(a):
            n[0] += 1
            if n[0] == 3:
                raise OSError("disk trouble")
        return real(a, b)
    monkeypatch.setattr(tasks.os, "rename", flaky)
    with pytest.raises(OSError):
        tasks.apply_ps4_update(tmp_path / "in" / "upd.pkg", home, "1.05")(ctx())
    monkeypatch.undo()
    assert tree_of(tmp_path / "out") == before and [p.name for p in (tmp_path / "out").iterdir()] == ["CUSA11111"]


def test_split_package_is_read_as_one(tmp_path):
    want, system = build_pkg(tmp_path / "g.pkg")
    data = bytearray((tmp_path / "g.pkg").read_bytes())
    struct.pack_into(">Q", data, 0x430, len(data))
    (tmp_path / "g.pkg").unlink()
    cut = len(data) // 3 + 5
    for i in range(3):
        (tmp_path / f"G-A0100-V0100_{i}.pkg").write_bytes(data[i * cut:(i + 1) * cut])
    (tmp_path / "other_1.pkg").write_bytes(b"x")                             # not part of anything
    ps = sorted(tmp_path.glob("G-*.pkg"))
    assert all(ps4pkg.pieces(p) == ps for p in ps) and ps4pkg.pieces(tmp_path / "other_1.pkg") == []
    with ps4pkg.PkgReader(ps[0]) as r:
        assert {f.path: b"".join(r.chunks(f)) for f in r.files()} == want
    jobs = cv.plan(cv.PS4_EXTRACT, ps, cv.Options(output_dir=str(tmp_path / "out")))
    assert len(jobs) == 1
    run_all(jobs)
    assert tree_of(tmp_path / "out" / "CUSA11111") == {**want, **system}
    from romtools import renamer, scanner
    assert [i.parts for i in scanner.enumerate_system("ps4", tmp_path) if i.path == ps[0]] == [ps[1:]]
    ops, notes = renamer.plan_packages([("ps4", tmp_path)], None)
    assert not [o for o in ops if o.src in ps] and any("split" in n for n in notes)


def test_changed_merge_option_replans_queued_jobs(tmp_path):
    game_and_update(tmp_path)
    queue = cv.plan(cv.PS4_EXTRACT, sorted(tmp_path.glob("*.pkg")), cv.Options(ps4_merge=False))
    assert all(j.follows is None for j in queue)
    new = cv.Options(ps4_merge=True)
    assert cv.outdated(queue, new) == queue
    pairs, extra = cv.replan(queue, new)
    for j, f in pairs:
        cv.take_plan(j, f)
    by = {j.source.name: j for j in queue}
    assert by["upd.pkg"].follows is by["game.pkg"] and not extra
    for j, f in cv.replan(queue, cv.Options(ps4_merge=False))[0]:
        cv.take_plan(j, f)
    assert all(j.follows is None and j.func for j in queue)
