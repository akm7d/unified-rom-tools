"""Settings and tags that several windows use at once, and starting the other apps."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from romtools import compat, config, selfupdate, tags


@pytest.fixture
def fresh():
    for f in config.CONFIG_DIR.glob("config*") if config.CONFIG_DIR.is_dir() else []:
        f.unlink()
    yield
    for f in config.CONFIG_DIR.glob("config*"):
        f.unlink()
    config._config = None


def test_two_windows_dont_undo_each_others_settings(fresh):
    verifier, renamer = config.Config(), config.Config()
    renamer["switch_keys"] = "/k"
    renamer.system("psx")["dats"] = ["/d/psx.dat"]
    renamer["library_root"] = "/new"
    renamer.save()
    verifier["window_state"]["checked_systems"] = ["psx"]
    verifier["workers"] = 7
    verifier.save()
    now = config.Config()
    assert (now["switch_keys"], now["library_root"], now["workers"]) == ("/k", "/new", 7)
    assert now["systems"]["psx"]["dats"] == ["/d/psx.dat"] and now["window_state"]["checked_systems"] == ["psx"]
    assert verifier["switch_keys"] == "/k"                  # and the first window knows about it now


def test_same_setting_changed_twice_the_last_one_wins_and_removals_count(fresh):
    a, b = config.Config(), config.Config()
    a.system("x")["dats"] = ["/d/1"]
    a["window_state"]["gone"] = 1
    a.save()
    b.save()
    a["theme"] = "dark"
    a.save()
    b["theme"] = "light"
    del b["window_state"]["gone"]
    b.save()
    a.save()
    now = config.Config()
    assert now["theme"] == "light" and "gone" not in now["window_state"] and now["systems"]["x"]["dats"] == ["/d/1"]


def test_references_to_a_systems_settings_stay_valid(fresh):
    a, b = config.Config(), config.Config()
    s = a.system("psx")
    b.system("psx")["system"] = "psx"
    b.save()
    a.save()
    assert s is a.system("psx") and s["system"] == "psx"
    s["dats"] = ["/x"]
    a.save()
    assert config.Config()["systems"]["psx"] == {"system": "psx", "dats": ["/x"]}


def test_defaults_are_never_changed(fresh):
    a = config.Config()
    a.system("zzz")
    a["window_state"]["y"] = 1
    assert "zzz" not in config.DEFAULTS["systems"] and "y" not in config.DEFAULTS["window_state"]


def test_damaged_settings_are_kept(fresh):
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text('{"library_root": "/x", "syst')
    config.Config().save()
    assert json.loads(config.CONFIG_FILE.read_text())["workers"] == 4
    assert [p.read_text() for p in config.CONFIG_DIR.glob("config.damaged-*")] == ['{"library_root": "/x", "syst']


def test_many_processes_saving_at_once(fresh):
    code = ("import sys; sys.path.insert(0, %r)\nfrom romtools.config import Config\nc = Config()\n"
            "for i in range(8):\n    c.system(f'{sys.argv[1]}-{i}')['dats'] = ['x']; c.save()" % str(Path(__file__).parent.parent))
    ps = [subprocess.Popen([sys.executable, "-c", code, str(n)]) for n in range(4)]
    assert [p.wait(60) for p in ps] == [0] * 4
    assert len([k for k in config.Config()["systems"] if "-" in k]) == 32


def test_tags_from_two_windows(tmp_path):
    f = tmp_path / "marks.json"
    verifier, optimizer = tags.MarkStore(f), tags.MarkStore(f)
    optimizer.set("sha1:aa", "Translation", "x")
    assert verifier.get("sha1:aa") == "Translation"
    verifier.set("sha1:bb", "Hack")
    verifier.set("sha1:cc", "")
    optimizer.set("sha1:aa", None)
    assert sorted(tags.MarkStore(f).marks) == ["sha1:bb", "sha1:cc"] and verifier.get("sha1:aa") is None


@pytest.mark.parametrize("win", [False, True])
def test_packaged_builds_start_the_right_programs(monkeypatch, tmp_path, win):
    monkeypatch.setattr(compat, "FROZEN", True)
    monkeypatch.setattr(compat, "IS_WIN", win)
    monkeypatch.setattr(compat, "app_dir", lambda: tmp_path)
    names = {m: Path(compat.app_command(m)[0]).name for m in ("verifier", "renamer", "converter")}
    assert names == ({"verifier": "ROM Verifier.exe", "renamer": "ROM Renamer.exe", "converter": "ROM Optimizer.exe"} if win
                     else {"verifier": "rom-verifier", "renamer": "rom-renamer", "converter": "rom-optimizer"})
    assert selfupdate.launcher(tmp_path, "renamer") == compat.app_command("renamer")
    made = "".join(f.read_text(encoding="utf-8") for f in (Path(__file__).parent.parent / "packaging").glob("*.spec"))
    assert all(n.removesuffix(".exe") in made for n in names.values())          # what the build really makes


def test_ping_needs_no_window():
    r = subprocess.run([sys.executable, "-m", "romtools", "--ping"], cwd=Path(__file__).parent.parent, timeout=60,
                       env={**os.environ, "QT_QPA_PLATFORM": "does-not-exist"})
    assert r.returncode == 0
