"""Queued jobs and options that change after the files were added."""

from dataclasses import replace

import pytest

from romtools import converter as cv


@pytest.fixture
def iso(tmp_path):
    f = tmp_path / "src" / "Game.iso"
    f.parent.mkdir()
    f.write_bytes(b"\0" * 4096)
    return f


def test_changed_output_folder_is_noticed_and_applied(iso, tmp_path):
    opts = cv.Options()
    queue = cv.plan(cv.TO_RVZ, [iso], opts)
    job = queue[0]
    assert job.outputs == [iso.parent / "Game.rvz"]
    assert not cv.outdated(queue, opts)
    new = replace(opts, output_dir=str(tmp_path / "out") + "/")
    assert cv.outdated(queue, new) == [job]
    jid = job.id
    for j, fresh in cv.replan(queue, new)[0]:
        cv.take_plan(j, fresh)
    assert job.id == jid and job.status == "Queued" and job.func
    assert job.outputs == [tmp_path / "out" / "Game.rvz"]
    assert not cv.outdated(queue, replace(new, output_dir=str(tmp_path / "out")))    # trailing slash: same folder


def test_unrelated_options_and_finished_jobs_dont_count(iso):
    queue = cv.plan(cv.TO_RVZ, [iso], cv.Options())
    assert not cv.outdated(queue, cv.Options(cd_codec="zstd", processors=3, split_bin=False))
    queue[0].status = "Done"
    assert not cv.outdated(queue, cv.Options(output_dir="/elsewhere"))


def test_adding_again_updates_instead_of_dropping(iso, tmp_path):
    queue = cv.plan(cv.TO_RVZ, [iso], cv.Options())
    out = cv.Options(output_dir=str(tmp_path / "out"))
    new, updated = cv.merge_plan(queue, cv.plan(cv.TO_RVZ, [iso], out))
    assert new == [] and updated == queue
    assert queue[0].outputs == [tmp_path / "out" / "Game.rvz"]
    other, updated = cv.merge_plan(queue, cv.plan(cv.TO_CHD, [iso], out))      # another task: a job of its own
    assert len(other) == 1 and not updated


def test_overrides_survive_a_new_plan(iso, tmp_path):
    opts, over = cv.Options(), {"trash_sources": True}
    queue = cv.plan(cv.TO_RVZ, [iso], replace(opts, **over))
    queue[0].overrides = dict(over)
    assert not cv.outdated(queue, opts)
    for j, fresh in cv.replan(queue, replace(opts, output_dir=str(tmp_path / "out")))[0]:
        cv.take_plan(j, fresh)
    assert queue[0].trash and queue[0].outputs[0].parent == tmp_path / "out"


def test_chd_job_follows_a_changed_disc_type(iso):
    queue = cv.plan(cv.TO_CHD, [iso], cv.Options(iso_as="cd"))
    new = cv.Options(iso_as="dvd")
    assert cv.outdated(queue, new) == queue
    for j, fresh in cv.replan(queue, new)[0]:
        cv.take_plan(j, fresh)
    assert queue[0].title.startswith("createdvd") and queue[0].steps[0][1] == "createdvd"


def test_update_row_keeps_following_the_queued_game(iso):
    def jobs(with_update):
        game = cv._task_job("extract: Game", iso, lambda c: "", "Extracting")
        game.preset = cv.SWITCH_FS
        if not with_update:
            return [game]
        upd = cv.Job(next(cv._ids), "  ↳ apply update", iso.with_name("u.nsp"), [])
        upd.follows, upd.preset = game, cv.SWITCH_FS
        return [game, upd]
    queue = jobs(False)
    new, updated = cv.merge_plan(queue, jobs(True))
    assert updated == queue and len(new) == 1 and new[0].follows is queue[0]


def test_retry_runs_a_failed_task_again_from_a_clean_state(iso):
    job = cv.plan(cv.TO_RVZ, [iso], cv.Options())[0]
    job.func = lambda ctx: (_ for _ in ()).throw(RuntimeError("boom"))
    cv.Runner([job], 1, lambda j: None)._run_job(job)
    assert job.status == "Failed" and job.message == "boom" and job.log_total
    (_j, fresh), = cv.replan([job], cv.Options())[0]
    cv.reset(job)
    cv.take_plan(job, fresh)
    assert (job.status, job.message, job.log, job.log_total, job.progress) == ("Queued", "", [], 0, 0.0)
    assert not job.started and not job.finished
    job.func = lambda ctx: "fine now"
    cv.Runner([job], 1, lambda j: None)._run_job(job)
    assert job.status == "Done" and job.message == "fine now"


def test_retry_brings_back_a_job_skipped_when_it_was_added(iso, tmp_path, monkeypatch):
    game = cv._task_job("extract: Game.iso", iso, lambda c: "", "Extracting")
    upd = cv.Job(next(cv._ids), "  ↳ apply update", iso.with_name("u.nsp"), [])
    upd.follows = game
    monkeypatch.setattr(cv, "plan_switch_fs", lambda files, opts: [game, upd] if opts.switch_keys else
                        [cv._skipped("extract: Game.iso", f, cv.NEEDS_PROD_KEYS) for f in files])
    queue = cv.plan(cv.SWITCH_FS, [iso], cv.Options())
    assert queue[0].status == "Skipped"
    pairs, extra = cv.replan(queue, cv.Options(switch_keys="/keys"))
    cv.reset(queue[0])
    cv.take_plan(*pairs[0])
    assert queue[0].status == "Queued" and queue[0].func
    assert extra == [upd] and upd.follows is queue[0]      # its update row comes along, tied to the queued job


def test_one_unreadable_file_doesnt_block_the_others(iso, tmp_path, monkeypatch):
    cut = tmp_path / "cut.pkg"
    cut.write_bytes(b"\x7fCNT" + b"\0" * 12 + b"\0\0\0\x09" + b"\0" * 4 + b"\0\0\0\x20" + b"\0" * 40)
    job, = cv.plan(cv.PS4_EXTRACT, [cut], cv.Options())
    assert job.status == "Skipped" and "cut off" in job.message
    real = cv._plan_one
    monkeypatch.setattr(cv, "_plan_one", lambda p, f, o: 1 / 0 if f.name == "bad.iso" else real(p, f, o))
    jobs = cv.plan(cv.TO_RVZ, [iso.with_name("bad.iso"), iso], cv.Options())
    assert [j.status for j in jobs] == ["Skipped", "Queued"] and jobs[0].preset == cv.TO_RVZ
