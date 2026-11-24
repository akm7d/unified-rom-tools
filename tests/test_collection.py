"""Duplicates, missing discs and BIOS files."""

import hashlib
import zipfile

import pytest

from romtools import bios
from romtools import collection as col
from romtools import scanner
from romtools.dat import Dat, DatIndex, Game, Rom
from romtools.datmanager import IndexCache
from romtools import library
from romtools.config import get_config
from test_identify import DATS, GAMES, dat, lib, put, rnd, scan, snes  # noqa: F401 (lib is a fixture)


def test_identical_copies_whatever_they_are_stored_in(lib):
    n, d = GAMES["snes"]
    a = put(lib / "snes" / n, d)
    z = lib / "snes" / "other name.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr(n, d)
    h = put(lib / "snes" / "copier.smc", bytes(512) + d)                 # with a copier header: the same ROM
    put(lib / "gba" / GAMES["gba"][0], GAMES["gba"][1])
    g, = col.duplicates(scan(lib))
    assert g.kind == col.IDENTICAL and {c.path for c in g.copies} == {a, z, h} and sum(c.keep for c in g.copies) == 1
    assert next(c for c in g.copies if c.keep).path == a and g.spare == z.stat().st_size + h.stat().st_size


def test_versions_are_kept_apart_from_copies(lib):
    n, d = GAMES["snes"]
    put(lib / "snes" / n, d)
    put(lib / "snes" / "Super Game (Japan) [T-En].sfc", snes(70))
    put(lib / "snes" / "Super Game 2 (USA).sfc", snes(71))               # another game
    g, = col.duplicates(scan(lib))
    assert g.kind == col.VERSIONS and len(g.copies) == 2 and g.copies[0].result.status == scanner.VERIFIED
    assert col.duplicates(scan(lib), versions=False) == []


@pytest.mark.parametrize("a, b, same", [
    ("Legend of Zelda, The (USA)", "The Legend of Zelda (Europe) (Rev 1)", True),
    ("Final Fantasy VII (USA) (Disc 1)", "Final Fantasy VII (USA) (Disc 2)", False),
    ("Final Fantasy VII (USA) (Disc 1)", "Final Fantasy VII (Japan) (Disc 1) [T-En]", True),
    ("Mega Man X (USA)", "Mega Man X2 (USA)", False), ("Pokemon - Red Version", "Pokemon - Blue Version", False)])
def test_which_names_are_one_game(a, b, same):
    assert (col.base_title(a) == col.base_title(b)) == same


def disc_dat(*names):
    d = Dat("x", "Sony - PlayStation")
    d.games = [Game(n, roms=[Rom(n + ".bin", 4, None, None, hashlib.sha1(n.encode()).hexdigest())], dat=d.name) for n in names]
    return DatIndex([d])


def res(tmp_path, stem, game="", status=scanner.VERIFIED, system="psx"):
    return scanner.Result(scanner.Item(system, tmp_path / f"{stem}.chd", "chd"), status, game)


def test_missing_discs_from_the_dat(tmp_path):
    idx = disc_dat("A (USA) (Disc 1)", "A (USA) (Disc 2)", "A (USA) (En,Fr) (Disc 3) (Rev 1)", "A (Japan) (Disc 1)",
                   "A (Japan) (Disc 2)", "B (USA) (Disc 1)", "B (USA) (Disc 2) (Leon)")
    rs = [res(tmp_path, "x", "A (USA) (Disc 1)"), res(tmp_path, "B (USA) (Disc 1)", "B (USA) (Disc 1)"),
          res(tmp_path, "y", "B (USA) (Disc 2) (Leon)"), res(tmp_path, "A (Japan) (Disc 2) [T-En]", status=scanner.MODIFIED)]
    got = {(m.game, tuple(m.have), tuple(m.missing), m.sure) for m in col.missing_discs(rs, {"psx": idx})}
    assert got == {("A (USA)", ("1",), ("2", "3"), True), ("A (Japan) [T-En]", ("2",), ("1",), True)}


def test_missing_discs_without_a_dat_are_gaps_only(tmp_path):
    rs = [res(tmp_path, f"C (Disc {n})", status=scanner.UNKNOWN) for n in (1, 3)] + [
        res(tmp_path, "D (Disc 1)", status=scanner.UNKNOWN)]
    m, = col.missing_discs(rs, {})
    assert (m.game, m.missing, m.sure) == ("C", ["2"], False)


def test_packages_of_one_game(tmp_path):
    def pk(name, kind, tid, title, ver=100, cid=""):
        r = res(tmp_path, name, title, scanner.IDENTIFIED, "ps4")
        r.item.kind = "package"
        r.chd_info = {"package": {"platform": "ps4", "kind": kind, "title": title, "title_id": tid, "content_id": cid,
                                  "contents": [{"kind": kind, "title_id": tid, "version": ver}]}}
        return r
    rs = [pk("a", "Game", "CUSA1", "MediEvil"), pk("b", "Game", "CUSA2", "MediEvil\u2122"), pk("c", "Update", "CUSA2", "MediEvil", 102),
          pk("d", "Update", "CUSA2", "MediEvil", 102), pk("e", "DLC", "CUSA2", "MediEvil", cid="one"), pk("f", "DLC", "CUSA2", "MediEvil", cid="two")]
    got = {(g.kind, tuple(sorted(c.path.stem for c in g.copies))) for g in col.duplicates(rs)}
    assert got == {(col.SAME_GAME, ("c", "d")), (col.VERSIONS, ("a", "b"))}


# ---------------------------------------------------------------- BIOS

def test_bios_files_are_compared_by_hash(tmp_path):
    good, other, old = rnd(1 << 19, 1), rnd(1 << 18, 2), rnd(1 << 19, 3)
    def rom(n, d):
        return f'\trom ( name {n} size {len(d)} crc 0 md5 {hashlib.md5(d).hexdigest()} sha1 {hashlib.sha1(d).hexdigest()} )'
    text = "\n".join(['clrmamepro (', '\tname "System"', '\tcomment "x"', ")", "game (", '\tname "System"',
                      '\tcomment "Sony - PlayStation"', rom("scph5501.bin", good), rom("scph1001.bin", old),
                      '\tcomment "Sega - Dreamcast"', rom("dc/dc_boot.bin", other), rom('"dc/my flash.bin"', b"f" * 100), ")"])
    known = bios.parse(text)
    assert [(b.system, b.name) for b in known] == [("Sony - PlayStation", "scph5501.bin"), ("Sony - PlayStation", "scph1001.bin"),
                                                   ("Sega - Dreamcast", "dc/dc_boot.bin"), ("Sega - Dreamcast", "dc/my flash.bin")]
    put(tmp_path / "sys" / "scph5501.bin", good)
    put(tmp_path / "sys" / "a" / "BOOT.BIN", other)
    put(tmp_path / "sys" / "SCPH1001.BIN", b"z" * len(old))
    put(tmp_path / "sys" / "readme.txt", b"hi")
    rep = {r.system: {(f.state, f.path.name, f.bios.file) for f in r.found} for r in bios.check([tmp_path / "sys"], known)}
    assert rep == {"Sony - PlayStation": {(bios.GOOD, "scph5501.bin", "scph5501.bin"), (bios.WRONG, "SCPH1001.BIN", "scph1001.bin")},
                   "Sega - Dreamcast": {(bios.MISNAMED, "BOOT.BIN", "dc_boot.bin")}}


def test_bios_list_is_checked_before_its_kept(tmp_path, monkeypatch):
    import io
    monkeypatch.setattr(bios, "LIST", tmp_path / "bios" / "System.dat")

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None
    monkeypatch.setattr(bios.urllib.request, "urlopen", lambda *a, **k: R(b"<html>no</html>"))
    with pytest.raises(RuntimeError):
        bios.download()
    assert not bios.LIST.exists() and bios.list_age_days() is None


def test_a_link_is_not_a_copy(lib, tmp_path):
    n, d = GAMES["snes"]
    real = put(lib / "store" / n, d)
    (lib / "snes").mkdir()
    (lib / "snes" / n).symlink_to(real)
    assert col.duplicates(scan(lib)) == []
    other = put(lib / "snes" / "copy.sfc", d)                       # a real copy next to a file and a link to it
    g, = col.duplicates(scan(lib))
    assert {c.path for c in g.copies} == {real, other}


def test_linked_folder_is_not_a_copy_either(lib, tmp_path):
    n, d = GAMES["gba"]
    real = put(tmp_path / "elsewhere" / "Game" / n, d)
    put(lib / "gba" / "x" / "other.gba", GAMES["snes"][1])
    (lib / "gba" / "A").symlink_to(real.parent)
    (lib / "gba" / "B").symlink_to(real.parent)
    res = scan(lib)
    assert sum(r.game == "Advance Game (USA)" for r in res) == 2     # both are shown …
    assert col.duplicates(res) == []                                 # … but there is one file


def test_changed_or_missing_copies_are_noticed(lib):
    import os
    n, d = GAMES["snes"]
    a, b = put(lib / "snes" / n, d), put(lib / "snes" / "copy.sfc", d)
    g, = col.duplicates(scan(lib))
    assert [c.problem() for c in g.copies] == ["", ""]
    b.write_bytes(snes(99))
    os.utime(b, (1, 1))
    a.unlink()
    assert {c.path.name: c.problem() for c in g.copies} == {n: "isn't there any more",
                                                            "copy.sfc": "has changed since it was scanned"}


def test_disc_whose_tracks_lie_elsewhere_cant_be_trashed(lib):
    put(lib / "x" / "t.bin", b"x" * 5000)
    it = scanner.Item("psx", put(lib / "psx" / "g.cue", b""), "cueset", [lib / "psx" / ".." / "x" / "t.bin"])
    assert "outside" in col.Copy(scanner.Result(it, scanner.VERIFIED)).removable
