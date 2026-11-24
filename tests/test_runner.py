"""The job queue: nothing half-written or wrong under a real name, and one job's problem stays its own."""

import os
import subprocess
import threading
import time

import pytest

from romtools import chdfix, tasks
from romtools import converter as cv
from romtools.chdman_tool import chdman_bin


def job(tmp_path, *scripts, out="Game.chd", force=False, cmd="createcd"):
    """A planned job whose steps are shell scripts; $O is the output path the step is given."""
    o = tmp_path / out
    steps = []
    for k, text in enumerate(scripts):
        sh = tmp_path / f"step{k}.sh"
        sh.write_text(f'#!/bin/sh\nO="$4"\n{text}\n')
        sh.chmod(0o755)
        steps.append([str(sh), cmd if k == 0 else "verify", "-i", "x", str(o)] + (["--force"] if force and k == 0 else []))
    j = cv.Job(1, "t", tmp_path / "src.iso", steps, [o])
    j.preset = cv.TO_CHD
    return j, o


def run(*jobs):
    r = cv.Runner(list(jobs), 1, lambda j: None, trash=lambda p: True)
    r.run()
    return r


def rest(tmp_path):
    return sorted(p.name for p in tmp_path.iterdir() if not p.name.startswith("step"))


pytestmark = pytest.mark.skipif(os.name == "nt", reason="shell scripts")


def test_output_gets_its_name_when_everything_worked(tmp_path):
    j, o = job(tmp_path, "echo new > $O", "test -s $O")
    run(j)
    assert (j.status, o.read_text(), rest(tmp_path)) == ("Done", "new\n", ["Game.chd"])


def test_failed_overwrite_keeps_the_old_file(tmp_path):
    for bad in ("echo half > $O; exit 1", "echo half > $O"):
        j, o = job(tmp_path, bad, "exit 1", force=True)
        o.write_text("precious")
        run(j)
        assert (j.status, o.read_text(), rest(tmp_path)) == ("Failed", "precious", ["Game.chd"])
    j, o = job(tmp_path, "echo new > $O", "exit 0", force=True)
    run(j)
    assert (j.status, o.read_text(), rest(tmp_path)) == ("Done", "new\n", ["Game.chd"])


def test_output_that_fails_its_check_isnt_kept(tmp_path):
    j, o = job(tmp_path, "echo wrong > $O", "echo 'Error: bad hash'; exit 1")
    run(j)
    assert j.status == "Failed" and "wasn't kept" in j.message and rest(tmp_path) == []


def test_stopped_extraction_leaves_nothing(tmp_path):
    j, o = job(tmp_path, "echo half > $O; echo t > \"$(dirname $O)/Game (Track 1).bin\"; sleep 30", out="Game.cue",
               cmd="extractcd")
    r = cv.Runner([j], 1, lambda j: None)
    threading.Timer(0.6, r.stop).start()
    t = time.time()
    r.run()
    assert j.status == "Cancelled" and rest(tmp_path) == [] and time.time() - t < 10


def test_extraction_with_several_files(tmp_path):
    text = "echo cue > $O; for i in 1 2; do echo t$i > \"$(dirname $O)/Game (Track $i).bin\"; done"
    j, o = job(tmp_path, text, out="Game.cue", cmd="extractcd")
    run(j)
    assert j.status == "Done" and rest(tmp_path) == ["Game (Track 1).bin", "Game (Track 2).bin", "Game.cue"]
    o.unlink()                                          # a track with that name is somebody else's: not replaced
    (tmp_path / "Game (Track 2).bin").write_text("mine")
    j, o = job(tmp_path, text, out="Game.cue", cmd="extractcd")
    run(j)
    assert j.status == "Failed" and (tmp_path / "Game (Track 2).bin").read_text() == "mine" and not o.exists()


def test_put_in_place_is_all_or_nothing(tmp_path, monkeypatch):
    w = tmp_path / "work"
    w.mkdir()
    for n in ("a", "b"):
        (w / n).write_text("new " + n)
        (tmp_path / n).write_text("old " + n)
    real, n = os.rename, [0]

    def flaky(a, b):
        n[0] += 1
        if n[0] == 4:
            raise OSError("share gone")
        return real(a, b)
    monkeypatch.setattr(cv.os, "rename", flaky)
    with pytest.raises(OSError):
        cv._put_in_place(w, tmp_path, True)
    assert [(tmp_path / x).read_text() for x in "ab"] == ["old a", "old b"]


def test_one_jobs_error_doesnt_stop_the_queue(tmp_path):
    (tmp_path / "file").write_text("x")
    bad, _o = job(tmp_path, "true")
    bad.outputs = [tmp_path / "file" / "sub" / "a.chd"]
    good = [cv.Job(i, "ok", tmp_path, [["true", "info"]]) for i in range(5)]
    run(bad, *good)
    assert bad.status == "Failed" and bad.message and [j.status for j in good] == ["Done"] * 5


def test_output_folder_is_made(tmp_path):
    import struct
    f = tmp_path / "g.nds"
    b = bytearray(os.urandom(0x8000) + b"\xff" * (0x20000 - 0x8000))
    b[0x80:0x84], b[0x12], b[0x14] = struct.pack("<I", 0x8000), 0, 0
    f.write_bytes(b)
    out = tmp_path / "new" / "out"
    j, = cv.plan(cv.TRIM, [f], cv.Options(output_dir=str(out)))
    run(j)
    assert j.status == "Done" and (out / "g.nds").stat().st_size == 0x8000 and f.read_bytes() == bytes(b)
    k, = cv.plan(cv.UNTRIM, [out / "g.nds"], cv.Options(output_dir=str(tmp_path / "other")))
    run(k)
    assert (tmp_path / "other" / "g.nds").read_bytes() == bytes(b)


def test_trimming_a_file_that_shrinks_ends(tmp_path, monkeypatch):
    f = tmp_path / "g.nds"
    f.write_bytes(b"\xff" * 1000)
    monkeypatch.setattr(tasks, "trim_info", lambda p: (1000, 5000, 1000))
    monkeypatch.setattr(tasks, "trim_info", lambda p: (9000, 5000, 9000))
    c = tasks.TaskContext(lambda s: None, lambda *a: None, threading.Event(), lambda p: True)
    with pytest.raises(RuntimeError, match="shorter"):
        tasks.trim(f, tmp_path / "o.nds")(c)
    assert rest(tmp_path) == ["g.nds"]


def test_tools_leave_no_threads_and_can_be_stopped(tmp_path):
    ctx = tasks.TaskContext(lambda s: None, lambda *a: None, threading.Event(), lambda p: False)
    before = threading.active_count()
    for _ in range(15):
        tasks._run_tool(["true"], ctx, "x")
    time.sleep(0.6)
    assert threading.active_count() <= before + 1
    threading.Timer(0.3, ctx.cancel.set).start()
    t = time.time()
    with pytest.raises(tasks.Cancelled):
        tasks._run_tool(["sleep", "30"], ctx, "x")
    assert time.time() - t < 5


def test_sheet_that_names_files_elsewhere_never_trashes_them(tmp_path):
    (tmp_path / "Documents").mkdir()
    (tmp_path / "Documents" / "thesis.bin").write_bytes(b"x" * 23520)
    d = tmp_path / "game"
    d.mkdir()
    for name in ("../Documents/thesis.bin", str(tmp_path / "Documents" / "thesis.bin")):
        (d / "g.cue").write_text(f'FILE "{name}" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n')
        assert not cv.plan(cv.TO_CHD, [d / "g.cue"], cv.Options(trash_sources=True))[0].trash
    (d / "g.bin").write_bytes(b"x" * 23520)
    (d / "g.cue").write_text('FILE "g.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n')
    assert cv.plan(cv.TO_CHD, [d / "g.cue"], cv.Options(trash_sources=True))[0].trash


@pytest.mark.skipif(not chdman_bin(), reason="chdman isn't installed")
def test_real_chdman_create_overwrite_extract_and_fix(tmp_path, monkeypatch):
    iso = tmp_path / "Game.iso"
    iso.write_bytes(os.urandom(2048 * 300))
    data = iso.read_bytes()
    o = cv.Options(iso_as="cd", output_dir=str(tmp_path / "out"))
    j, = cv.plan(cv.TO_CHD, [iso], o)
    run(j)
    chd_file = tmp_path / "out" / "Game.chd"
    assert j.status == "Done", j.log
    j, = cv.plan(cv.TO_CHD, [iso], cv.Options(iso_as="cd", output_dir=str(tmp_path / "out"), overwrite=True))
    run(j)
    assert j.status == "Done" and [p.name for p in (tmp_path / "out").iterdir()] == ["Game.chd"]
    j, = cv.plan(cv.FROM_CHD, [chd_file], cv.Options(output_dir=str(tmp_path / "back")))
    run(j)
    assert j.status == "Done" and (tmp_path / "back" / "Game.bin").read_bytes() == data, j.log
    assert "Game.bin" in (tmp_path / "back" / "Game.cue").read_text()
    monkeypatch.setattr(chdfix, "_trash", lambda p: False)
    r = chdfix.fix(chd_file, "dvd", False, tmp_path / "tmp", threading.Event())
    assert r.ok and not r.trashed and "couldn't be moved" in r.message and (tmp_path / "out" / "Game (original).chd").exists()
