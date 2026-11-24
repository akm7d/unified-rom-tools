"""Identifying Wii U archives (.wua) and extracted games, on small archives built here."""

import hashlib
import os
import struct

import pytest

from romtools import identify, integrity, renamer, scanner, wiiu
from romtools import packages as pk
from romtools.config import get_config

zstd = pytest.importorskip("compression.zstd")


def meta(tid, name="THE GAME\nSecond Line", region="00000002", product="WUP-P-ABCE", version=0):
    return (f'<?xml version="1.0" encoding="utf-8"?><menu type="complex"><product_code type="string">{product}</product_code>'
            f'<title_version type="unsignedInt">{version}</title_version><title_id type="hexBinary">{tid}</title_id>'
            f'<region type="hexBinary">{region}</region><longname_en type="string">{name}</longname_en></menu>').encode()


def wua(path, titles, pad=200_000):
    """titles: {"0005…_v0": meta.xml bytes or None}."""
    names, offs = bytearray(), {}

    def nm(n):
        if n not in offs:
            offs[n] = len(names)
            b = n.encode()
            names.extend(bytes([len(b)]) + b if len(b) < 128 else bytes([0x80 | len(b) & 0x7F, len(b) >> 7]) + b)
        return offs[n]
    data, tree = bytearray(os.urandom(pad)), [None]       # the XML doesn't start at a block's beginning

    def add(items):                                        # entries of one folder stand next to each other
        first = len(tree)
        tree.extend([None] * len(items))
        for k, (n, v) in enumerate(sorted(items.items())):
            if isinstance(v, dict):
                at, count = add(v)
                tree[first + k] = (nm(n), at, count, 0)
            else:
                off = len(data)
                data.extend(v)
                tree[first + k] = (0x80000000 | nm(n), off & 0xFFFFFFFF, len(v), (off >> 32) << 16)
        return first, len(items)
    at, count = add({t: {"code": {"a" * 200 + ".rpx": b"x"}, "content": {}, "meta": {"meta.xml": m} if m else {}}
                     for t, m in titles.items()})
    tree[0] = (0x7FFFFFFF, at, count, 0)
    blob, records = bytearray(), bytearray()
    blocks = [bytes(data[i:i + 0x10000]).ljust(0x10000, b"\0") for i in range(0, len(data), 0x10000)]
    for r in range(0, len(blocks), 16):
        sizes, start = [], len(blob)
        for b in blocks[r:r + 16]:
            z = zstd.compress(b)
            z = z if len(z) < 0x10000 else b
            blob += z
            sizes.append(len(z) - 1)
        records += struct.pack(">Q16H", start, *(sizes + [0] * (16 - len(sizes))))
    blob = blob.ljust((len(blob) + 7) & ~7, b"\0")
    t = b"".join(struct.pack(">IIII", *e) for e in tree)
    secs, pos, body = [], 0, b""
    for part in (bytes(blob), bytes(records), bytes(names), t, b"", b""):
        secs.append((pos, len(part)))
        body += part
        pos += len(part)
    foot = b"".join(struct.pack(">QQ", *x) for x in secs)
    tail = struct.pack(">QII", len(body) + 144, wiiu.VERSION_1, wiiu.MAGIC)
    sha = hashlib.sha256(body + foot + bytes(32) + tail).digest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body + foot + sha + tail)
    return path


G, U, D = "0005000010143500", "0005000E10143500", "0005000C10143500"


def test_archive_says_what_it_is(tmp_path):
    p = wua(tmp_path / "x.wua", {f"{G.lower()}_v0": meta(G)})
    i = pk.read(p)
    assert (i.platform, i.kind, i.title_id, i.base_id, i.version_str) == ("wiiu", "Game", G, G, "v0")
    assert (i.title, i.region, i.product) == ("The Game - Second Line", "USA", "WUP-P-ABCE")
    assert pk.suggested_name(i) == "The Game - Second Line (USA)" and "WUP-P-ABCE" in pk.describe(i)
    (tmp_path / "noext").write_bytes(p.read_bytes())
    hit = identify.identify(tmp_path / "noext")
    assert (hit.system, hit.container) == ("wiiu", "package")


def test_several_titles_in_one_archive(tmp_path):
    p = wua(tmp_path / "x.wua", {f"{U}_v80": meta(U, version=80), f"{D}_v16": None, f"{G}_v0": meta(G, region="00000007")})
    i = pk.read(p)
    assert i.kind == "Game + Update + DLC" and i.region == "World" and [c.version for c in i.contents] == [0, 80, 16]
    assert pk.suggested_name(i) == "The Game - Second Line (World) (+Update v80) (+DLC)"


def test_update_and_dlc_on_their_own(tmp_path):
    u, d = (pk.read(wua(tmp_path / f"{t}.wua", {f"{t}_v48": meta(t)})) for t in (U, D))
    assert (u.kind, u.base_id, d.kind) == ("Update", G, "DLC")
    assert pk.suggested_name(u).endswith("(Update v48)") and pk.suggested_name(d).endswith("(DLC)")
    assert "no base game" in pk.check_set([u])[u.path][0]
    g = pk.read(wua(tmp_path / "g.wua", {f"{G}_v0": meta(G)}))
    assert pk.check_set([g, u, d]) == {}


def test_extracted_folders(tmp_path):
    lib = tmp_path / "wiiu"
    f = lib / "Some Game" / "meta" / "meta.xml"
    f.parent.mkdir(parents=True)
    f.write_bytes(meta(G))
    (lib / "Some Game" / "code").mkdir()
    (lib / "Some Game" / "content").mkdir()
    (lib / "Some Game" / "content" / "rom.gba").write_bytes(b"x" * 5000)      # a game's own file isn't a ROM of the library
    nus = lib / "nus"
    nus.mkdir()
    h = bytearray(0x1E0)
    h[:4], h[0x18C:0x194], h[0x1DC:0x1DE] = b"\0\x01\0\x04", bytes.fromhex(U), b"\0\x50"
    (nus / "title.tmd").write_bytes(h)
    (nus / "00000000.app").write_bytes(b"x")
    wii = lib / "wii"
    wii.mkdir()
    h[0x18C:0x194] = bytes.fromhex("0001000052534245")                        # a Wii title: not ours to name
    (wii / "title.tmd").write_bytes(h)
    (wii / "00000000.app").write_bytes(b"x")
    sc = scanner.make_scanner(get_config())
    got = {r.item.path.name: (r.status, (r.chd_info or {}).get("package", {}).get("title_id", "")) for r in
           (sc.check(i, None) for i in scanner.enumerate_system("wiiu", lib))}
    assert got == {"Some Game": (scanner.IDENTIFIED, G), "nus": (scanner.ATTENTION, U), "wii": (scanner.SKIPPED, "")} or \
        got == {"Some Game": (scanner.IDENTIFIED, G), "nus": (scanner.IDENTIFIED, U), "wii": (scanner.SKIPPED, "")}
    i = pk.read(nus)
    assert i.version_str == "v80" and "encrypted" in i.note


def test_integrity_and_damage(tmp_path):
    p = wua(tmp_path / "x.wua", {f"{G}_v0": meta(G)})
    assert integrity.check(p).state == "intact"
    data = p.read_bytes()
    for at in (5, len(data) // 2, len(data) - 400, len(data) - 100, len(data) - 40, len(data) - 2):
        b = bytearray(data)
        b[at] ^= 1
        p.write_bytes(b)
        assert integrity.check(p).state == "damaged", at
    for cut in (data[:-10], data[:100], b""):
        p.write_bytes(cut)
        assert integrity.check(p).state == "damaged"
        with pytest.raises(pk.PackageError):
            pk.read(p)


def test_hostile_archives_are_refused_not_followed(tmp_path):
    import random
    good = wua(tmp_path / "x.wua", {f"{G}_v0": meta(G)}, pad=1000).read_bytes()
    rnd = random.Random(3)
    for _ in range(400):                                    # tables and footer full of nonsense: an error or an answer, no hang
        b = bytearray(good)
        for _k in range(rnd.randint(1, 6)):
            b[rnd.randrange(len(b) - 500, len(b) - 8)] = rnd.randrange(256)
        p = tmp_path / "y.wua"
        p.write_bytes(b)
        try:
            pk.read(p)
        except pk.PackageError:
            pass
    evil = b'<!DOCTYPE a [<!ENTITY x "' + b"A" * 99 + b'">]>' + meta(G)
    with pytest.raises(pk.PackageError):
        pk.read(wua(tmp_path / "e.wua", {f"{G}_v0": evil}))


def test_renamer_keeps_names_that_already_say_it(tmp_path):
    lib = tmp_path / "wiiu"
    wua(lib / "Game, The - Second Line (USA).wua", {f"{G}_v0": meta(G)})       # No-Intro style: same words
    g2 = G[:-1] + "1"
    wua(lib / "dump01.wua", {f"{g2}_v0": meta(g2, "Other")})
    wua(lib / "Other.wua", {f"{U[:-1]}1_v32": meta(U[:-1] + "1", "Other")})     # its name hides that it's an update
    ops, _n = renamer.plan_packages([("wiiu", lib)], None)
    assert {(o.src.name, o.dst.name) for o in ops} == {("dump01.wua", "Other (USA).wua"),
                                                       ("Other.wua", "Other (USA) (Update v32).wua")}


def test_without_zstd_the_id_still_counts(tmp_path, monkeypatch):
    p = wua(tmp_path / "My Game (USA).wua", {f"{G}_v0": meta(G)})

    def no(_d):
        raise wiiu.NoZstd("no zstd")
    monkeypatch.setattr(wiiu, "_unpack", no)
    i = pk.read(p)
    assert (i.title_id, i.title, i.note) == (G, "My Game", "no zstd")


def test_packages_are_read_whatever_they_are_called(tmp_path, make_pkg):
    w = wua(tmp_path / "wiiu" / "x.wua", {f"{G}_v0": meta(G)}).rename(tmp_path / "wiiu" / "lost")
    assert pk.read(w).title_id == G and integrity.check(w).state == "intact"
    k = make_pkg(tmp_path / "a.pkg", TITLE="T", TITLE_ID="CUSA11111", CATEGORY="gd", APP_VER="01.00",
                 CONTENT_ID="UP0001-CUSA11111_00-TEST000000000000").rename(tmp_path / "a.bin")
    assert pk.read(k).title_id == "CUSA11111"
    r = scanner.make_scanner(get_config()).check(scanner.enumerate_system("gba", w.parent, "gba")[0], None)
    assert (r.status, r.found) == (scanner.MISPLACED, "wiiu")
