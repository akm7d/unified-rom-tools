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


def test_move_never_replaces_a_file_of_the_same_name(tmp_path):
    """The exception for names that differ only in case is for one folder; in another folder it's another file."""
    from romtools import renamer as rn
    a, b = tmp_path / "a" / "Game.pkg", tmp_path / "b" / "Game.pkg"
    for f, d in ((a, b"new"), (b, b"precious")):
        f.parent.mkdir()
        f.write_bytes(d)
    op = rn.Op(a, b, move=True)
    assert rn.validate([op])[0].problem == "target already exists"
    import pytest
    with pytest.raises(FileExistsError):
        rn._rename(a, b)
    with pytest.raises(FileExistsError):
        rn._rename(a, tmp_path / "b" / "Game.pkg")
    assert a.read_bytes() == b"new" and b.read_bytes() == b"precious"
    c = tmp_path / "a" / "game.PKG"
    rn._rename(a, c)                    # only the case: still works
    assert c.read_bytes() == b"new"


def test_empty_folders_are_found_removed_and_made_again(tmp_path):
    from romtools import renamer as rn
    lib = tmp_path / "lib"

    def put(rel, data=b"x"):
        f = lib / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
        return f
    a = put("dl/deep/er/a.sfc")
    put("dl/pic/cover.jpg")
    put("hid/.hidden")
    put("ps3/Game/PS3_GAME/PARAM.SFO")
    (lib / "ps3/Game/PS3_GAME/USRDIR/empty").mkdir(parents=True)        # a game's own empty folder
    (lib / "old/older").mkdir(parents=True)
    (lib / "snes").mkdir()                                              # a system's folder stays, empty or not
    (lib / "snes/nothing").mkdir()
    others = {lib / "dl", lib / "hid", lib / "old"}
    tree = rn.FolderTree([lib / "snes", lib / "ps3", *sorted(others)], others)
    move = rn.Op(a, lib / "snes" / "a.sfc", move=True)
    ops = rn.validate([move])
    dirs = rn.plan_empty_folders(tree, ops)
    assert [str(o.src.relative_to(lib)).replace("\\", "/") for o in dirs] == \
        ["snes/nothing", "dl/deep/er", "dl/deep", "old/older", "old"]
    move.enabled = False                                                # not moved: its folders stay
    assert {o.src.name for o in rn.plan_empty_folders(tree, ops)} == {"nothing", "older", "old"}
    move.enabled = True
    put("old/older/just arrived.txt")                                   # filled after the preview: it stays too
    j, errs = rn.apply(ops + dirs)
    assert not errs
    assert not (lib / "dl/deep").exists() and not (lib / "snes/nothing").exists()
    assert (lib / "old/older/just arrived.txt").is_file() and (lib / "dl/pic/cover.jpg").is_file()
    assert (lib / "ps3/Game/PS3_GAME/USRDIR/empty").is_dir()
    assert rn.undo(j) == []
    assert a.is_file() and (lib / "snes/nothing").is_dir()
