from pathlib import Path

from romtools import renamer as rn


def files(d: Path) -> list[str]:
    return sorted(str(p.relative_to(d)) for p in d.rglob("*"))


def test_validate_flags_conflicts(tmp_path):
    (tmp_path / "a.sfc").write_bytes(b"a")
    (tmp_path / "b.sfc").write_bytes(b"b")
    (tmp_path / "c.sfc").write_bytes(b"c")
    (tmp_path / "keep.sfc").write_bytes(b"k")
    ops = rn.validate([rn.Op(tmp_path / "a.sfc", tmp_path / "keep.sfc"),       # target exists (not renamed away)
                       rn.Op(tmp_path / "b.sfc", tmp_path / "x.sfc"),
                       rn.Op(tmp_path / "c.sfc", tmp_path / "x.sfc"),          # two items -> same name
                       rn.Op(tmp_path / "a.sfc", tmp_path / "sub" / "a.sfc")])  # moves need move=True
    assert ops[0].problem == "target already exists"
    assert ops[1].problem == ops[2].problem == "two items would get this name"
    assert ops[3].problem == "must stay in the same folder"


def test_apply_and_undo_restores_everything(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    for n in ("Game (USA) (1).sfc", "other.sfc", "Case.SFC"):
        (lib / n).write_bytes(n.encode())
    before = files(lib)
    ops = [rn.Op(lib / "Game (USA) (1).sfc", lib / "Game (USA).sfc"),
           rn.Op(lib / "Case.SFC", lib / "Case.sfc"),                          # case-only rename
           rn.Op(lib / "other.sfc", lib / "New Folder" / "other.sfc", move=True)]
    jpath, errors = rn.apply(ops)
    assert not errors
    assert (lib / "Game (USA).sfc").read_bytes() == b"Game (USA) (1).sfc"
    assert (lib / "New Folder" / "other.sfc").exists()
    assert "Case.sfc" in [p.name for p in lib.iterdir()]
    assert not rn.undo(jpath)
    assert files(lib) == before                       # the folder made for the move is gone again


def test_chained_renames_are_allowed(tmp_path):
    (tmp_path / "a.sfc").write_bytes(b"a")
    (tmp_path / "b.sfc").write_bytes(b"b")
    ops = rn.validate([rn.Op(tmp_path / "a.sfc", tmp_path / "b.sfc"), rn.Op(tmp_path / "b.sfc", tmp_path / "c.sfc")])
    assert all(o.ok for o in ops)                     # b.sfc is freed by the second rename


def test_apply_never_overwrites(tmp_path):
    (tmp_path / "a.nes").write_bytes(b"a")
    (tmp_path / "b.nes").write_bytes(b"b")
    _j, _err = rn.apply([rn.Op(tmp_path / "a.nes", tmp_path / "b.nes")])
    assert (tmp_path / "b.nes").read_bytes() == b"b" and (tmp_path / "a.nes").exists()


def test_cleanup_rules():
    r = rn.Rules(strip_dup_suffix=True, underscores=True, collapse_spaces=True, cifs_safe=True, the_suffix=True)
    assert rn.apply_rules("The Legend of X - Y (USA) (1).sfc", r) == "Legend of X, The - Y (USA).sfc"
    assert rn.apply_rules("Bayonetta_3.nsp", r) == "Bayonetta 3.nsp"
    assert ":" not in rn.apply_rules("Title: Sub.iso", r)


def test_chains_and_swaps_apply_and_undo(tmp_path):
    (tmp_path / "a.sfc").write_bytes(b"a")
    (tmp_path / "b.sfc").write_bytes(b"b")
    (tmp_path / "x.sfc").write_bytes(b"x")
    (tmp_path / "y.sfc").write_bytes(b"y")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    ops = [rn.Op(tmp_path / "a.sfc", tmp_path / "b.sfc"), rn.Op(tmp_path / "b.sfc", tmp_path / "c.sfc"),  # chain
           rn.Op(tmp_path / "x.sfc", tmp_path / "y.sfc"), rn.Op(tmp_path / "y.sfc", tmp_path / "x.sfc")]  # swap
    jpath, errors = rn.apply(ops)
    assert not errors
    after = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert after == {"b.sfc": b"a", "c.sfc": b"b", "y.sfc": b"x", "x.sfc": b"y"}
    assert not rn.undo(jpath)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
