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
