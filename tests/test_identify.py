"""Recognising ROMs by their content, wherever they lie and whatever they're called - and sorting them."""

import io
import os
import struct
import zipfile
import zlib
from pathlib import Path

import pytest

from romtools import identify, library, renamer, scanner
from romtools.config import DAT_DIR, get_config
from romtools.datmanager import IndexCache
from romtools.hashing import hash_file
from romtools.systems import guess_system


def rnd(n, seed=1):
    import random
    return random.Random(seed).randbytes(n)


def gba(seed=1, size=1 << 17, title=b"TESTGAME"):
    d = bytearray(rnd(size, seed))
    d[4:0x14] = identify.GBA_LOGO
    d[0xA0:0xAC] = title.ljust(12, b"\0")
    d[0xB2] = 0x96
    d[-4:] = b"\x01\x02\x03\x04"
    return bytes(d)


def gb(seed=2, color=0):
    d = bytearray(rnd(1 << 15, seed))
    d[0x104:0x11C] = identify.GB_LOGO
    d[0x134:0x144] = b"GBTITLE".ljust(16, b"\0")
    d[0x143] = color
    return bytes(d)


def snes(seed=3):
    d = bytearray(rnd(1 << 17, seed))
    h = bytearray(0x40)
    h[:21] = b"SNES TITLE".ljust(21)
    h[0x15], h[0x17] = 0x20, 8
    struct.pack_into("<HH", h, 0x1C, 0xFFFF ^ 0x1234, 0x1234)
    struct.pack_into("<H", h, 0x3C, 0x8000)
    d[0x7FC0:0x8000] = h
    return bytes(d)


def n64(seed=4):
    return b"\x80\x37\x12\x40" + rnd((1 << 16) - 4, seed)


def nes(seed=5):
    return b"NES\x1a\x02\x01" + bytes(10) + rnd(40960, seed)


def md(seed=6):
    d = bytearray(rnd(1 << 17, seed))
    d[0x100:0x110] = b"SEGA MEGA DRIVE "
    d[0x120:0x150] = b"MD TITLE".ljust(48)
    return bytes(d)


def psx_track(seed=7, sectors=40):
    """A raw (2352) PlayStation data track with a SYSTEM.CNF."""
    def rec(name, lba, size):
        n = name.encode()
        r = bytearray(33 + len(n) + (1 - len(n) % 2))
        r[0] = len(r)
        struct.pack_into("<I", r, 2, lba)
        struct.pack_into("<I", r, 10, size)
        r[32] = len(n)
        r[33:33 + len(n)] = n
        return bytes(r)
    data = [bytearray(rnd(2048, seed + i)) for i in range(sectors)]
    pvd = bytearray(2048)
    pvd[:6] = b"\x01CD001"
    pvd[8:40] = b"PLAYSTATION".ljust(32)
    pvd[40:72] = b"VOL".ljust(32)
    pvd[156:190] = rec("\0", 20, 2048)[:34]
    data[16] = pvd
    data[20] = bytearray((rec("SYSTEM.CNF;1", 21, 64)).ljust(2048, b"\0"))
    data[21] = bytearray(b"BOOT = cdrom:\\SLUS_012.34;1\r\n".ljust(2048, b"\0"))
    sync = b"\0" + b"\xff" * 10 + b"\0" + b"\0\0\0\x02" + bytes(8)
    return b"".join(sync + bytes(d) + bytes(280) for d in data)


DATS = {"gba": "Nintendo - Game Boy Advance", "gb": "Nintendo - Game Boy", "gbc": "Nintendo - Game Boy Color",
        "snes": "Nintendo - Super Nintendo Entertainment System", "n64": "Nintendo - Nintendo 64",
        "nes": "Nintendo - Nintendo Entertainment System", "genesis": "Sega - Mega Drive - Genesis",
        "psx": "Sony - PlayStation"}
TRACKS = {"Disc Game (USA) (Track 1).bin": psx_track(), "Disc Game (USA) (Track 2).bin": rnd(2352 * 20, 9)}
GAMES = {"gba": ("Advance Game (USA).gba", gba()), "gb": ("Boy Game (USA).gb", gb()),
         "gbc": ("Color Game (USA).gbc", gb(22, 0xC0)), "snes": ("Super Game (USA).sfc", snes()),
         "n64": ("N Game (USA).z64", n64()), "genesis": ("Mega Game (USA).md", md()),
         "nes": ("Nes Game (USA).nes", nes()[16:])}


def dat(name, games):
    import hashlib
    out = [f'<?xml version="1.0"?><datafile><header><name>{name}</name></header>']
    for g, roms in games:
        out.append(f'<game name="{g}">')
        for n, d in roms:
            out.append(f'<rom name="{n}" size="{len(d)}" crc="{zlib.crc32(d):08x}" md5="{hashlib.md5(d).hexdigest()}" '
                       f'sha1="{hashlib.sha1(d).hexdigest()}"/>')
        out.append("</game>")
    return "".join(out) + "</datafile>"


@pytest.fixture
def lib(tmp_path):
    DAT_DIR.mkdir(parents=True, exist_ok=True)
    for f in DAT_DIR.glob("*"):
        f.unlink()
    for s, (n, d) in GAMES.items():
        # one name is also in another system's DAT (with other data), like real multi-platform games
        more = [("Nes Game (USA)", [("Nes Game (USA).z64", rnd(99, 1))])] if s == "n64" else []
        (DAT_DIR / (DATS[s] + ".dat")).write_text(dat(DATS[s], [(Path(n).stem, [(n, d)])] + more))
    (DAT_DIR / (DATS["psx"] + ".dat")).write_text(dat(DATS["psx"], [("Disc Game (USA)", list(TRACKS.items()))]))
    cfg = get_config()
    cfg["systems"] = {}
    root = tmp_path / "lib"
    root.mkdir()
    cfg["library_root"] = str(root)
    return root


def put(p: Path, data: bytes) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def scan(root, cache=None):
    cfg = get_config()
    from romtools.datmanager import ensure_mapping, library_folders
    ensure_mapping(cfg, [f for f in library_folders(root) if library.is_system_folder(cfg, f)])
    systems, _o, _l = library.split(cfg, root)
    items, idx = library.collect(cfg, root, systems + [library.OTHER], IndexCache())
    sc = scanner.make_scanner(cfg, cache)
    out = []
    sc.run(items, idx, out.append)
    return out


def one(root, path):
    return next(r for r in scan(root) if r.item.path == path)


# ---------------------------------------------------------------- headers

@pytest.mark.parametrize("data, system", [(gba(), "gba"), (gb(), "gb"), (gb(1, 0x80), "gbc"), (snes(), "snes"),
                                          (bytes(512) + snes(), "snes"), (n64(), "n64"), (nes(), "nes"), (md(), "genesis"),
                                          (psx_track(), "psx")])
def test_header_says_the_system(data, system):
    assert identify.from_bytes(data).system == system


def test_random_and_everyday_files_are_not_roms():
    for seed in range(300):
        assert not identify.from_bytes(rnd(1 << 14, seed) + bytes(4096))
    for d in (b"%PDF-1.7" + bytes(9000), b"\x89PNG\r\n\x1a\n" + rnd(9000), b"hello world\n" * 3000, b"\x7fELF" + rnd(9000),
              b"MZ" + rnd(70000), bytes(1 << 17), b"\xff" * (1 << 17)):
        assert not identify.from_bytes(d)


def test_byte_orders_and_copier_formats_hash_like_the_dat(tmp_path):
    import array
    import hashlib
    want = hashlib.sha1(n64()).hexdigest()
    for code, name in (("H", "a.bin"), ("I", "b.bin")):
        a = array.array(code, n64())
        a.byteswap()
        assert hash_file(put(tmp_path / name, a.tobytes())).headerless.sha1 == want
    smd = bytearray(512)
    smd[8:10] = b"\xaa\xbb"
    for i in range(0, len(md()), 16384):
        b = md()[i:i + 16384]
        smd += b[1::2] + b[0::2]
    e = hash_file(put(tmp_path / "x.smd", bytes(smd)))
    assert e.headerless.sha1 == hashlib.sha1(md()).hexdigest() and ".smd" in e.norm
    assert hash_file(put(tmp_path / "y.bin", bytes(512) + snes())).headerless.sha1 == hashlib.sha1(snes()).hexdigest()


def test_folder_names():
    for name, want in {"Super Nintendo": "snes", "Nintendo - Game Boy Advance": "gba", "GBA Games": "gba", "DS": "nds",
                       "Sony PlayStation 2": "ps2", "Games": None, "Nintendo": None, "Music": None, "roms": None}.items():
        g = guess_system(name)
        assert (g.id if g else None) == want, name


# ---------------------------------------------------------------- scanning a mess

def test_wrong_folder_is_named_and_flagged(lib):
    for s, (n, d) in GAMES.items():
        put(lib / ("gba" if s != "gba" else "snes") / n, d)
    for r in scan(lib):
        s = next(k for k, v in GAMES.items() if v[0] == r.item.path.name)
        assert (r.status, r.found, r.game) == (scanner.MISPLACED, s, Path(GAMES[s][0]).stem), r.note
        assert r.misplaced


def test_nested_renamed_and_without_extension(lib):
    files = {}
    for i, (s, (_n, d)) in enumerate(GAMES.items()):
        files[put(lib / "Stuff" / "a" / "b" / "c" / "d" / f"x{i}", d)] = s
        files[put(lib / f"Game {i}" / "rom.bin", d)] = s
        if s != "nes":                  # a NES ROM without its header has no signature at all
            files[put(lib / "dl" / f"y{i}.part", d)] = s
    res = {r.item.path: r for r in scan(lib)}
    for p, s in files.items():
        assert res[p].found == s and res[p].status == scanner.MISNAMED and not res[p].misplaced, (p, res[p].note)


def test_archives_by_content_and_nested(lib):
    n, d = GAMES["snes"]
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr(n, d)
    as_sfc = put(lib / n, b.getvalue())
    twice = io.BytesIO()
    with zipfile.ZipFile(twice, "w") as z:
        z.writestr("inner.zip", b.getvalue())
    nested = put(lib / "sub" / "Super Game (USA).zip", twice.getvalue())
    res = {r.item.path: r for r in scan(lib)}
    for p in (as_sfc, nested):
        assert res[p].status == scanner.VERIFIED and res[p].found == "snes", res[p].note


def test_same_name_on_another_system_is_not_a_bad_dump_of_it(lib):
    r = one(lib, put(lib / "Nes Game (USA).nes", nes(77)))
    assert r.found == "nes" and r.dat != DATS["n64"]


def test_hack_in_the_wrong_folder_is_recognised_by_its_header(lib):
    r = one(lib, put(lib / "snes" / "hack.sfc", gba(50, title=b"MYHACK")))
    assert r.status == scanner.UNKNOWN and r.found == "gba" and r.found_by == "header" and r.misplaced
    assert "MYHACK" in r.ident.title


def test_trimmed_and_padded_dumps(lib):
    d = bytearray(gba())
    d[-5000:] = b"\xff" * 5000
    name = GAMES["gba"][0]
    (DAT_DIR / (DATS["gba"] + ".dat")).write_text(dat(DATS["gba"], [(Path(name).stem, [(name, bytes(d))])]))
    r = one(lib, put(lib / "gba" / "t.gba", bytes(d[:-4000])))
    assert (r.status, r.tag, r.game) == (scanner.MODIFIED, "Trimmed", Path(name).stem)
    r = one(lib, put(lib / "gba" / "p.gba", bytes(d) + b"\0" * 777))
    assert (r.status, r.tag) == (scanner.MODIFIED, "Overdump")


def test_tracks_without_their_cue(lib):
    for n, d in TRACKS.items():
        put(lib / n, d)
    r, = scan(lib)
    assert r.status in (scanner.VERIFIED, scanner.MISNAMED) and r.game == "Disc Game (USA)" and r.found == "psx"


def test_extracted_games_and_other_files_are_left_alone(lib):
    put(lib / "ps3" / "Big Game" / "PS3_GAME" / "USRDIR" / "data.bin", gba())
    put(lib / "Docs" / "readme.md", b"# hi\n" * 1000)
    put(lib / "Docs" / "setup.exe", b"MZ" + rnd(50000))
    put(lib / "Docs" / "x.docx", b"PK\x03\x04" + rnd(50000))
    got = {r.item.path.name: r.item.kind for r in scan(lib)}
    assert got == {"Big Game": "folder", "Docs": "folder"}


def test_file_remembered_by_an_older_version_is_hashed_again(lib, tmp_path):
    from romtools.cache import HashCache
    p = put(lib / "nes.bin", nes())
    c = HashCache(tmp_path / "c.sqlite")
    st = p.stat()
    e = hash_file(p)
    e.headerless = None                         # 1.12 didn't look for headers in .bin files
    c.put(p, st.st_size, st.st_mtime, "file", {}, [e])
    r = next(r for r in scan(lib, c) if r.item.path == p)
    assert r.found == "nes" and r.status == scanner.MISNAMED


def test_enhanced_in_official_names_is_no_hack_tag():
    from romtools.tags import tag_from_name
    for n in ("Silver (USA, Europe) (SGB Enhanced)", "Yellow (CGB+SGB Enhanced)", "X (USA) (NDSi Enhanced)"):
        assert tag_from_name(n) is None
    assert tag_from_name("X Enhanced Edition")[0] == "Improvement"


# ---------------------------------------------------------------- sorting

def tree(root):
    import hashlib
    return {str(p.relative_to(root)): hashlib.sha1(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}


def test_sort_apply_and_undo(lib):
    put(lib / "Super Nintendo" / "keep.sfc", snes(90))                       # the folder already used for SNES
    put(lib / "gba" / GAMES["snes"][0], GAMES["snes"][1])                    # wrong folder
    put(lib / "gba" / "Super Game (USA).srm", b"save")
    put(lib / "loose" / "what.bin", GAMES["gba"][1])                         # loose, unnamed
    put(lib / "x" / "Disc Game.cue", b'FILE "Disc Game (USA) (Track 1).bin" BINARY\nFILE "Disc Game (USA) (Track 2).bin" '
                                     b'BINARY\n')
    for n, d in TRACKS.items():
        put(lib / "x" / n, d)
    put(lib / "x" / "Disc Game.m3u", b"Disc Game.cue\n")
    put(lib / "loose" / "hack", md(91))                                      # no DAT knows it: its header does
    put(lib / "loose" / "notes.doc", b"hello" * 1000)
    before = tree(lib)
    res = scan(lib)
    sorting, _notes = renamer.plan_sorting(res, lib, get_config())
    ops = renamer.validate(renamer.add_playlist_ops(renamer.merge(renamer.plan_from_results(res), sorting)))
    assert all(o.ok for o in ops), [(o.src.name, o.problem) for o in ops]
    j, errs = renamer.apply(ops)
    assert not errs
    after = tree(lib)
    assert sorted(after.values()) == sorted(before.values())                 # nothing lost, nothing changed
    assert {k for k in after if k not in before} == {
        "Super Nintendo/Super Game (USA).sfc", "Super Nintendo/Super Game (USA).srm", "gba/Advance Game (USA).gba",
        "psx/x/Disc Game.cue", "psx/x/Disc Game (USA) (Track 1).bin", "psx/x/Disc Game (USA) (Track 2).bin",
        "psx/x/Disc Game.m3u", "genesis/hack.md"}
    assert not any(r.misplaced for r in scan(lib))                           # and now everything is in its place
    assert renamer.undo(j) == []
    assert tree(lib) == before and not (lib / "genesis").exists() and not (lib / "psx").exists()


def test_sorting_never_overwrites_and_can_leave_headers_out(lib):
    put(lib / "snes" / "Super Game (USA).sfc", snes(95))
    put(lib / "gb" / "Super Game (USA).sfc", GAMES["snes"][1])
    put(lib / "hack.gba", gba(96))
    res = scan(lib)
    ops = renamer.validate(renamer.plan_sorting(res, lib, get_config(), by_header=False)[0])
    assert [(o.src.parent.name, o.problem) for o in ops] == [("gb", "target already exists")]
    assert len(renamer.plan_sorting(res, lib, get_config())[0]) == 2


def test_key_search_is_shallow(tmp_path):
    from romtools import tasks
    put(tmp_path / "a" / "b" / "c" / "d" / "prod.keys", b"x")
    assert tasks._search_keys(tmp_path) is None
    k = put(tmp_path / "switch" / "prod.keys", b"x")
    assert tasks._search_keys(tmp_path) == k


def test_picked_files_know_their_place(lib):
    cfg = get_config()
    a = put(lib / "snes" / "sub" / "deep" / GAMES["gba"][0], GAMES["gba"][1])
    b = put(lib / "Downloads" / GAMES["gba"][0], GAMES["gba"][1])
    assert library.place_of(cfg, a) == "snes" and library.place_of(cfg, b) == ""
    for pick in (a, a.parent):                          # the file, or the folder it's in
        items, idx = library.collect_paths(cfg, [pick], IndexCache())
        out = []
        scanner.make_scanner(cfg).run(items, idx, out.append)
        assert [(r.status, r.found, r.misplaced) for r in out] == [(scanner.MISPLACED, "gba", True)]


def test_one_track_stands_for_its_disc(lib):
    cue = put(lib / "d" / "x.cue", b'FILE "Disc Game (USA) (Track 1).bin" BINARY\nFILE "Disc Game (USA) (Track 2).bin" BINARY')
    for n, d in TRACKS.items():
        put(lib / "d" / n, d)
    items, _idx = library.collect_paths(get_config(), [lib / "d" / n for n in TRACKS], IndexCache())
    assert [(i.path, i.kind, len(i.parts)) for i in items] == [(cue, "cueset", 2)]


def test_cleanup_rules_leave_disc_tracks_alone(lib):
    put(lib / "d" / "My_Disc.cue", b'FILE "My_Disc_1.bin" BINARY')
    t = put(lib / "d" / "My_Disc_1.bin", b"x")
    o = put(lib / "d" / "My_Rom.bin", b"x")
    rules = renamer.Rules(underscores=True)
    assert sorted(op.src.name for op in renamer.plan_from_rules({"d": lib / "d"}, rules)) == ["My_Disc.cue", "My_Rom.bin"]
    assert [op.src for op in renamer.plan_rules_for([t, o], rules)] == [o]


def test_package_scope_is_the_whole_console_folder(lib):
    g = put(lib / "ps4" / "Game" / "a.pkg", b"x")
    for pick in (g, g.parent, lib / "ps4"):
        assert library.package_folders(get_config(), [pick]) == [("ps4", lib / "ps4", set())]
    f = put(lib / "dl" / "a.pkg", b"x")
    assert library.package_folders(get_config(), [f]) == [(library.OTHER, lib / "dl", set())]
