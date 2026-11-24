"""Finding out that a downloaded DAT is out of date (the server is replaced by files here)."""

import io
import urllib.request

import pytest

from romtools import datmanager as dm
from romtools.config import DAT_DIR
from romtools.systems import BY_ID

GB = BY_ID["gb"]


def dat(games, name="Nintendo - Game Boy"):
    return (f'<?xml version="1.0"?><datafile><header><name>{name}</name></header>' + "".join(
        f'<game name="{g}"><rom name="{g}.gb" size="4" sha1="{h * 40}"/></game>' for g, h in games.items()) +
        "</datafile>").encode()


@pytest.fixture
def server(monkeypatch):
    for f in DAT_DIR.glob("*") if DAT_DIR.is_dir() else []:
        f.unlink()
    st = {"data": dat({"A": "1", "B": "2"}), "etag": '"v1"', "gets": 0, "down": False}

    class Resp(io.BytesIO):
        headers: dict = {}

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return None

    def urlopen(req, timeout=0):
        if st["down"]:
            raise OSError("no network")
        head = req.get_method() == "HEAD"
        st["gets"] += not head
        r = Resp(b"" if head else st["data"])
        r.headers = {"ETag": st["etag"]}
        return r
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return st


def test_up_to_date_needs_no_download(server):
    path = dm.download(GB)
    assert server["gets"] == 1 and dm.check_update(GB).state == "current" and server["gets"] == 1
    assert dm.downloaded_dats() == [(GB, path)] and dm.oldest_check_days() == 0


def test_new_version_is_compared_and_only_replaced_when_asked(server):
    path = dm.download(GB)
    old = path.read_bytes()
    server["data"], server["etag"] = dat({"A": "9", "C": "3", "D": "4"}), '"v2"'
    u = dm.check_update(GB)
    assert (u.state, u.added, u.changed, u.removed) == ("new", 2, 1, 1) and path.read_bytes() == old
    dm.discard_update(u)
    assert path.read_bytes() == old and not list(DAT_DIR.glob("*.new"))
    u = dm.check_update(GB)                          # still out of date
    assert u.state == "new"
    dm.apply_update(u)
    assert path.read_bytes() == server["data"] and dm.check_update(GB).state == "current"


def test_dat_from_before_versions_were_remembered(server):
    path = dm.download(GB)
    (DAT_DIR / "dat-info.json").unlink()
    assert dm.check_update(GB).state == "current"     # same games: nothing to ask, and its version is known now
    gets = server["gets"]
    assert dm.check_update(GB).state == "current" and server["gets"] == gets
    assert path.is_file()


def test_bad_answers_change_nothing(server):
    path = dm.download(GB)
    old = path.read_bytes()
    server["data"], server["etag"] = b"<html>rate limited</html>", '"x"'
    assert dm.check_update(GB).state == "error"
    server["data"] = dat({})
    assert dm.check_update(GB).state == "error"
    server["down"] = True
    assert dm.check_update(GB).state == "error"
    assert path.read_bytes() == old and not list(DAT_DIR.glob("*.new"))
