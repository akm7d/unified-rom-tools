"""Updating romtools itself from its GitHub Releases.

* Packaged builds (Linux tar.gz / Windows zip): the release archive for this platform is downloaded, checked
  against the release's SHA256SUMS, and unpacked NEXT TO the current folder (romtools-<version>-<platform>).
  A portable build's data folder (settings, DATs, tags, rename history) is copied into the new folder, and on
  Linux the menu entries are pointed at it. The old folder is left alone, so you can go back.
* Running from a git checkout (./install.sh launchers): ``git pull --ff-only`` and the pinned tools fetched again.

Checking reads one small JSON document from GitHub's public API; nothing about your library is sent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__
from .compat import FROZEN, IS_WIN, app_dir

# "owner/name" of the GitHub repository whose releases are the updates (set when the project is published)
GITHUB_REPO = ""
API = "https://api.github.com/repos/{repo}/releases/latest"
CHECK_INTERVAL = 12 * 3600       # the automatic check at launch runs at most twice a day


class UpdateError(Exception):
    pass


@dataclass
class Release:
    version: str
    tag: str
    notes: str
    page: str
    assets: dict[str, str]       # file name -> download URL

    @property
    def archive_name(self) -> str:
        return f"romtools-{self.version}-{'windows-x64.zip' if IS_WIN else 'linux-x64.tar.gz'}"


def _vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def newer(a: str, b: str = __version__) -> bool:
    """Is version a newer than b?"""
    return _vtuple(a) > _vtuple(b)


def _get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"romtools/{__version__}",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def latest(repo: str | None = None) -> Release:
    repo = repo or GITHUB_REPO
    if not repo:
        raise UpdateError("this copy doesn't know where its updates are published yet")
    try:
        d = json.loads(_get(API.format(repo=repo)))
    except urllib.error.HTTPError as exc:
        raise UpdateError("no release has been published yet" if exc.code == 404 else f"GitHub said {exc}") from exc
    except (OSError, ValueError) as exc:
        raise UpdateError(f"couldn't reach GitHub: {exc}") from exc
    tag = d.get("tag_name", "")
    return Release(tag.lstrip("v"), tag, d.get("body") or "", d.get("html_url", ""),
                   {a["name"]: a["browser_download_url"] for a in d.get("assets", [])})


def install_kind() -> str:
    """'build' (packaged), 'git' (source checkout with git), or 'source' (source without git)."""
    if FROZEN:
        return "build"
    return "git" if (app_dir() / ".git").exists() and shutil.which("git") else "source"


# ---------------------------------------------------------------- launch check bookkeeping

def due(cfg) -> bool:
    if not cfg["check_updates_at_launch"]:
        return False
    last = float(cfg["window_state"].get("update_last_check", 0) or 0)
    return time.time() - last > CHECK_INTERVAL


def mark_checked(cfg) -> None:
    cfg["window_state"]["update_last_check"] = time.time()
    cfg.save()


# ---------------------------------------------------------------- applying

def apply(rel: Release, progress: Callable[[str, float], None] = lambda s, f: None) -> tuple[Path | None, str]:
    """Install the release. Returns (folder of the new version or None for git updates, message)."""
    kind = install_kind()
    if kind == "git":
        return None, _update_git(progress)
    if kind == "source":
        raise UpdateError("this copy has no git history - download the new version from the release page")
    return _update_build(rel, progress)


def _update_git(progress) -> str:
    root = app_dir()
    progress("Checking your copy…", 0.05)
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root,
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        raise UpdateError("your copy has local changes - commit or stash them first, then update again")
    progress("Downloading changes (git pull)…", 0.2)
    r = subprocess.run(["git", "pull", "--ff-only"], cwd=root, capture_output=True, text=True)
    if r.returncode != 0:
        raise UpdateError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout) else "git pull failed")
    progress("Fetching the pinned tools…", 0.7)
    subprocess.run([sys.executable, str(root / "packaging" / "build.py"), "tools"], cwd=root,
                   capture_output=True, text=True)
    progress("Done", 1.0)
    return "Updated. Restart the apps to use the new version."


def _download(url: str, dest: Path, progress, lo: float, hi: float) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": f"romtools/{__version__}"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                progress(f"Downloading {dest.name} ({done / 2**20:.0f} / {total / 2**20:.0f} MB)…",
                         lo + (hi - lo) * done / total)


def _update_build(rel: Release, progress) -> tuple[Path, str]:
    name = rel.archive_name
    if name not in rel.assets:
        raise UpdateError(f"the release has no {name}")
    if "SHA256SUMS" not in rel.assets:
        raise UpdateError("the release has no SHA256SUMS file, so the download can't be checked")
    here = app_dir()
    parent = here.parent
    target = parent / name.replace(".tar.gz", "").replace(".zip", "")
    if target.exists():
        raise UpdateError(f"{target} already exists - start that one, or remove it and update again")
    sums = _get(rel.assets["SHA256SUMS"]).decode("utf-8", "replace")
    want = next((ln.split()[0].lower() for ln in sums.splitlines() if ln.strip().endswith(name)), None)
    if not want:
        raise UpdateError(f"SHA256SUMS doesn't list {name}")
    work = parent / f".romtools-update-{os.getpid()}"
    work.mkdir(exist_ok=True)
    try:
        archive = work / name
        _download(rel.assets[name], archive, progress, 0.0, 0.7)
        progress("Checking the download…", 0.72)
        h = hashlib.sha256()
        with open(archive, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != want:
            raise UpdateError("the download doesn't match its published checksum - nothing was changed")
        progress("Unpacking…", 0.8)
        unpack = work / "unpack"
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                z.extractall(unpack)
        else:
            with tarfile.open(archive) as t:
                t.extractall(unpack, filter="data")
        inner = next((p for p in unpack.iterdir() if p.is_dir()), None)
        if not inner:
            raise UpdateError("the archive is empty")
        # keep a portable build's own data (settings, DATs, tags, rename history)
        if (here / "data").is_dir() and (here / "portable.txt").exists():
            progress("Copying your settings and DATs…", 0.9)
            shutil.copytree(here / "data", inner / "data", dirs_exist_ok=True)
        os.rename(inner, target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if not IS_WIN:
        _repoint_menu(here, target)
    progress("Done", 1.0)
    return target, f"romtools {rel.version} is in {target}. Your current version stays in {here}."


def _repoint_menu(old: Path, new: Path) -> None:
    """Linux: if the menu entries / ~/.local/bin links point at the old folder, re-run the new install.sh."""
    apps = Path.home() / ".local" / "share" / "applications"
    try:
        uses_old = any(str(old) in p.read_text(errors="replace") for p in apps.glob("rom-*.desktop"))
    except OSError:
        uses_old = False
    if uses_old and (new / "install.sh").exists():
        subprocess.run(["bash", str(new / "install.sh")], cwd=new, capture_output=True)


def launcher(folder: Path, module: str) -> list[str]:
    """Command that starts one of the apps from an unpacked build folder."""
    names = {"verifier": ("ROM Verifier.exe", "rom-verifier"), "renamer": ("ROM Renamer.exe", "rom-renamer"),
             "converter": ("ROM Optimizer.exe", "rom-optimizer")}
    win, lin = names[module]
    return [str(folder / (win if IS_WIN else lin))]
