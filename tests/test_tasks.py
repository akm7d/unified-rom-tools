import hashlib
import os
import threading

import pytest

from romtools import tasks


def ctx(trashed):
    def trash(p):
        trashed.append(p.name)
        os.remove(p)
        return True
    return tasks.TaskContext(lambda s: None, lambda *a, **k: None, threading.Event(), trash)


def nds(path, used=0x100000, cart=0x400000):
    data = bytearray(os.urandom(used))
    data[0x80:0x84] = used.to_bytes(4, "little")
    data[0x12] = 0                                 # plain DS game (not DSi-enhanced)
    data[0x14] = 5                                 # 4 MiB cartridge
    img = bytes(data) + b"\xff" * (cart - used)
    path.write_bytes(img)
    return hashlib.sha1(img).hexdigest()


def test_trim_untrim_roundtrip(tmp_path):
    f = tmp_path / "Game (USA).nds"
    orig = nds(f)
    trashed = []
    tasks.trim(f, f)(ctx(trashed))
    assert f.stat().st_size == 0x100000 and trashed == ["Game (USA) (original).nds"]
    tasks.untrim(f, f)(ctx(trashed))
    assert hashlib.sha1(f.read_bytes()).hexdigest() == orig


def test_inplace_replace_keeps_original_on_failure(tmp_path, monkeypatch):
    f = tmp_path / "Game (USA).nds"
    orig = nds(f)
    real, calls = os.rename, []

    def flaky(a, b):
        calls.append(1)
        if len(calls) == 2:                        # the "put the new file in place" step fails
            raise OSError("simulated network error")
        return real(a, b)
    monkeypatch.setattr(tasks.os, "rename", flaky)
    with pytest.raises(OSError):
        tasks.trim(f, f)(ctx([]))
    monkeypatch.setattr(tasks.os, "rename", real)
    assert hashlib.sha1(f.read_bytes()).hexdigest() == orig
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Game (USA).nds"]
