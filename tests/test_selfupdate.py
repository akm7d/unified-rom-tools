import base64
import hashlib
import io
import tarfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import romtools.selfupdate as su


def test_version_compare():
    assert su.newer("1.10.0", "1.9.9") and not su.newer("1.9.0", "1.9.0") and not su.newer("1.8.2", "1.9.0")


@pytest.fixture
def release(tmp_path, monkeypatch):
    """A fake 9.9.9 Linux release served from local files, signed with a throwaway key."""
    key = Ed25519PrivateKey.generate()
    pub = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,
                                                         serialization.PublicFormat.Raw)).decode()
    monkeypatch.setattr(su, "RELEASE_KEYS", [pub])
    monkeypatch.setattr(su, "IS_WIN", False)
    rel_dir = tmp_path / "release"
    rel_dir.mkdir()
    name = "romtools-9.9.9-linux-x64.tar.gz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for member, data in (("romtools-9.9.9-linux-x64/rom-verifier", b"#!/bin/sh\n"),
                             ("romtools-9.9.9-linux-x64/README.md", b"new")):
            info = tarfile.TarInfo(member)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    (rel_dir / name).write_bytes(buf.getvalue())
    sums = f"{hashlib.sha256(buf.getvalue()).hexdigest()}  {name}\n".encode()
    (rel_dir / "SHA256SUMS").write_bytes(sums)
    (rel_dir / "SHA256SUMS.sig").write_text(base64.b64encode(key.sign(sums)).decode())
    old = tmp_path / "romtools-1.0.0-linux-x64"
    (old / "data" / "dats").mkdir(parents=True)
    (old / "portable.txt").touch()
    (old / "data" / "dats" / "x.dat").write_text("dat")
    monkeypatch.setattr(su, "app_dir", lambda: old)
    monkeypatch.setattr(su, "_repoint_menu", lambda a, b: None)     # never touch the real menu
    assets = {n: (rel_dir / n).as_uri() for n in (name, "SHA256SUMS", "SHA256SUMS.sig")}
    return su.Release("9.9.9", "v9.9.9", "", "", assets), rel_dir, old


def test_signed_update_installs_next_to_old(release):
    rel, _d, old = release
    folder, _msg = su._update_build(rel, lambda s, f: None)
    assert folder.name == "romtools-9.9.9-linux-x64" and (folder / "rom-verifier").exists()
    assert (folder / "data" / "dats" / "x.dat").read_text() == "dat"       # portable data came along
    assert old.exists()


def test_bad_signature_refused(release):
    rel, rel_dir, old = release
    (rel_dir / "SHA256SUMS.sig").write_text(base64.b64encode(b"\0" * 64).decode())
    with pytest.raises(su.UpdateError, match="signature"):
        su._update_build(rel, lambda s, f: None)
    assert not (old.parent / "romtools-9.9.9-linux-x64").exists()


def test_unsigned_release_refused(release):
    rel, _d, _old = release
    del rel.assets["SHA256SUMS.sig"]
    with pytest.raises(su.UpdateError, match="isn't signed"):
        su._update_build(rel, lambda s, f: None)


def test_tampered_archive_refused(release):
    rel, rel_dir, old = release
    (rel_dir / "romtools-9.9.9-linux-x64.tar.gz").write_bytes(b"not the real archive")
    with pytest.raises(su.UpdateError, match="checksum"):
        su._update_build(rel, lambda s, f: None)
    assert not (old.parent / "romtools-9.9.9-linux-x64").exists()


def test_copy_without_portable_txt_stays_per_user(release):
    rel, rel_dir, old = release
    (old / "portable.txt").unlink()
    # (a real archive brings portable.txt along)
    real = su.tarfile.open

    class WithMarker:
        def __init__(self, *a):
            self.t = real(*a)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.t.close()

        def extractall(self, path, **kw):
            self.t.extractall(path, **kw)
            (path / "romtools-9.9.9-linux-x64" / "portable.txt").touch()

    su.tarfile.open = WithMarker
    try:
        folder, _ = su._update_build(rel, lambda s, f: None)
    finally:
        su.tarfile.open = real
    assert not (folder / "portable.txt").exists() and not (folder / "data").exists()


def test_stopping_leaves_nothing_behind(release):
    rel, _d, old = release

    def stop(step, f):
        if step.startswith("Unpacking"):
            raise su.UpdateError("stopped")
    before = sorted(p.name for p in old.parent.iterdir())
    with pytest.raises(su.UpdateError, match="stopped"):
        su._update_build(rel, stop)
    assert sorted(p.name for p in old.parent.iterdir()) == before


def test_two_updates_at_once_dont_disturb_each_other(release):
    rel, _d, old = release
    other = []

    def progress(step, f):
        if step.startswith("Unpacking") and not other:
            other.append(None)
            other.append(su._update_build(rel, lambda s, f: None)[0])
    with pytest.raises(su.UpdateError, match="created meanwhile"):
        su._update_build(rel, progress)
    assert (other[1] / "rom-verifier").exists()
    assert not list(old.parent.glob(".romtools-update-*"))


def test_database_in_use_is_copied_whole(tmp_path):
    import sqlite3
    src, dst = tmp_path / "a", tmp_path / "b"
    (src / "cache").mkdir(parents=True)
    live = sqlite3.connect(src / "cache" / "hashes.sqlite")
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("CREATE TABLE t (x)")
    live.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(500)])
    live.commit()
    su.copy_data(src, dst)
    assert sorted(p.name for p in (dst / "cache").iterdir()) == ["hashes.sqlite"]

    def rows():
        c = sqlite3.connect(dst / "cache" / "hashes.sqlite")
        n = c.execute("SELECT count(*) FROM t").fetchone()[0]
        c.close()
        return n
    assert rows() == 500
    live.execute("INSERT INTO t VALUES (1)")
    live.commit()
    su.copy_data(src, dst)
    live.close()
    assert rows() == 501


def test_settings_saved_while_closing_come_along(release):
    import os
    rel, _d, old = release
    cfg = old / "data" / "config" / "config.json"
    cfg.parent.mkdir()
    cfg.write_text("before")
    (old / "data" / "dats" / "x.dat").write_text("dat")
    folder, _ = su._update_build(rel, lambda s, f: None)
    (folder / "portable.txt").touch()
    new = folder / "data" / "config" / "config.json"
    cfg.write_text("after closing")
    os.utime(cfg, (new.stat().st_mtime + 5,) * 2)
    newer_dat = folder / "data" / "dats" / "x.dat"
    newer_dat.write_text("updated in the new version")
    su.carry_over(folder)
    assert new.read_text() == "after closing" and newer_dat.read_text() == "updated in the new version"


def test_menu_entry_for_any_folder_name(tmp_path):
    """Exec= is read back the way desktops do it: string escapes first, then quoting, then field codes."""
    import subprocess
    from pathlib import Path
    script = (Path(su.__file__).parents[1] / "packaging" / "linux" / "install.sh").read_text()
    func = script[script.index("exec_value()"):script.index("\n}\n", script.index("exec_value()")) + 3]
    for path in ("/plain/rom", "/with space/rom", "/a&b/rom", "/it's/rom", '/q"uote/rom', "/d$ollar/rom", "/back\\slash/rom",
                 "/100%/rom", "/t`ick/rom", "/Émulation/rom"):
        out = subprocess.run(["bash", "-c", func + 'exec_value "$1"', "x", path], capture_output=True, encoding="utf-8").stdout
        s = out.replace("\\\\", "\\")                       # value of type string
        assert s[0] == s[-1] == '"'
        body, got, i = s[1:-1], "", 0
        while i < len(body):
            if body[i] == "\\":
                i += 1
            else:
                assert body[i] not in '"`$', (path, out)
            got += body[i]
            i += 1
        assert got.replace("%%", "%") == path, (path, out)
