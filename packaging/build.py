#!/usr/bin/env python3
"""Build romtools for Linux and Windows from one source tree.

    python3 packaging/build.py linux      # dist/romtools-<version>-linux-x64.tar.gz
    python3 packaging/build.py windows    # dist/romtools-<version>-windows-x64.zip  (Wine on Linux, or native)
    python3 packaging/build.py all
    python3 packaging/build.py tools      # source checkout: fetch nodtool, nsz, xdelta3 into ./tools
    python3 packaging/build.py test-portable  # self-test the Linux build on a clean Ubuntu 24.04 (Docker)
    python3 packaging/build.py clean      # remove build/ and dist/
    python3 packaging/build.py distclean  # also remove the download / venv / Wine cache

Everything external is pinned in packaging/versions.toml and checksum-verified. Downloads, the Linux venv and
the Wine prefix are cached in ~/.cache/romtools-build (override with ROMTOOLS_BUILD_CACHE), so rebuilds are
quick. Each build runs the packaged program's --selftest and fails if it doesn't pass.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import zipfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging"
BUILD = ROOT / "build"
DIST = ROOT / "dist"
CACHE = Path(os.environ.get("ROMTOOLS_BUILD_CACHE") or Path.home() / ".cache" / "romtools-build")
DL = CACHE / "downloads"
PINS = tomllib.loads((PKG / "versions.toml").read_text())
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "romtools" / "__init__.py").read_text()).group(1)
NATIVE_WIN = os.name == "nt"


# ---------------------------------------------------------------- helpers

def step(msg: str) -> None:
    print(f"\n==> {msg}", flush=True)


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    print("   $ " + " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(8 << 20):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, sha: str | None = None, name: str | None = None, cache: bool = True) -> Path:
    DL.mkdir(parents=True, exist_ok=True)
    dest = DL / (name or url.rsplit("/", 1)[-1])
    if cache and dest.exists() and (not sha or sha256(dest) == sha):
        return dest
    print(f"   downloading {url}", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "romtools-build"})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as fh:
        shutil.copyfileobj(r, fh, 1 << 20)
    if sha and sha256(tmp) != sha:
        tmp.unlink()
        raise SystemExit(f"checksum mismatch for {url} - refusing to use it")
    tmp.replace(dest)
    return dest


def pip_requirements() -> list[str]:
    return [f"{k}=={v}" for k, v in PINS["pip"].items()]


def sevenzip_host() -> str:
    exe = shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")
    if not exe:
        raise SystemExit("7z is needed to unpack the Windows tools (pacman -S 7zip)")
    return exe


# ---------------------------------------------------------------- icons

def make_icons(out: Path) -> Path:
    """Render icons/*.svg to .png and multi-size .ico (Windows launchers) with the host's PySide6."""
    out.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841

    def png(svg: Path, size: int) -> bytes:
        img = QImage(size, size, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        QSvgRenderer(str(svg)).render(p)
        p.end()
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        return bytes(ba.data())

    for svg in sorted((ROOT / "icons").glob("*.svg")):
        sizes = [16, 24, 32, 48, 64, 128, 256]
        images = [png(svg, s) for s in sizes]
        (out / f"{svg.stem}.png").write_bytes(images[-1])
        # ICO with PNG-compressed entries (Windows Vista and later)
        header = struct.pack("<HHH", 0, 1, len(images))
        offset = 6 + 16 * len(images)
        entries, blobs = b"", b""
        for s, data in zip(sizes, images):
            entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset + len(blobs))
            blobs += data
        (out / f"{svg.stem}.ico").write_bytes(header + entries + blobs)
    return out


# ---------------------------------------------------------------- self-test

def check_selftest(report: Path, label: str) -> None:
    text = report.read_text(encoding="utf-8") if report.exists() else "(no report written)"
    print("\n".join("   " + line for line in text.splitlines()))
    if "RESULT OK" not in text:
        raise SystemExit(f"{label} self-test FAILED - not packaging a broken build")


# ---------------------------------------------------------------- portable marker

PORTABLE_TXT = """This copy of romtools is PORTABLE.

While this file is here, ROM Verifier, ROM Renamer and ROM Optimizer keep their settings, DATs, hack/translation
tags, rename history and remembered scans in the "data" folder next to the programs. Copy or move the whole
folder (USB stick, another PC) and everything comes along.

Delete this file to use the normal per-user locations instead
(Linux: ~/.config, ~/.local/share and ~/.cache; Windows: %APPDATA% and %LOCALAPPDATA%).
"""


# ---------------------------------------------------------------- Linux tools

SDL_STUB = r"""
/* chdman links SDL2 only for clipboard helpers it never calls. This stub stands in for libSDL2 so the
   bundled chdman runs on systems without SDL. */
#include <stdlib.h>
void SDL_free(void *p) { free(p); }
char *SDL_GetClipboardText(void) { char *s = malloc(1); if (s) s[0] = 0; return s; }
int SDL_HasClipboardText(void) { return 0; }
int SDL_SetClipboardText(const char *text) { (void)text; return -1; }
"""

CHDMAN_WRAPPER = """#!/bin/sh
# Runs the bundled chdman with its bundled libraries (see tools/lib).
d="$(dirname "$(readlink -f "$0")")"
LD_LIBRARY_PATH="$d/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" exec "$d/bin/chdman" "$@"
"""


def linux_chdman() -> Path:
    """chdman built from MAME's official source with the C++ runtime linked in (cached per MAME release)."""
    tag = PINS["chdman_linux"]["mame_tag"]
    out = CACHE / "chdman-linux" / tag
    exe = out / "bin" / "chdman"
    if not exe.exists():
        step(f"Building chdman {tag} from MAME's official source (one time per MAME release, ~2-5 min)")
        sys.path.insert(0, str(ROOT))
        from romtools.updater import build_script
        work = Path("/dev/shm/romtools-build") if Path("/dev/shm").is_dir() else CACHE / "work"
        script = build_script(tag, work, max(1, (os.cpu_count() or 4) - 2), dest_dir=out / "bin",
                              ldopts="-Wl,--as-needed -static-libstdc++ -static-libgcc")
        run(["nice", "-n", "10", "bash", "-c", script])
    lib = out / "lib" / "libSDL2-2.0.so.0"
    if not lib.exists():
        lib.parent.mkdir(parents=True, exist_ok=True)
        src = out / "sdlstub.c"
        src.write_text(SDL_STUB)
        run(["cc", "-O2", "-shared", "-fPIC", "-Wl,-soname,libSDL2-2.0.so.0", "-o", lib, src])
    return out


TOOL_LICENSES = {
    "nodtool": "nodtool {v} (https://github.com/encounter/nod), official release binary, unmodified. "
               "Licensed under the Apache License 2.0 or MIT license.",
    "nsz": "nsz {v} (https://github.com/nicoboss/nsz), official release binary, unmodified. Licensed under the MIT "
           "license. No keys are included; you supply your own prod.keys.",
    "nstool": "nstool {v} (https://github.com/jakcron/nstool), official release binary, unmodified. Licensed under "
              "the MIT license. No keys are included; you supply your own prod.keys.",
}


def _single_binaries(dest: Path, platform: str) -> None:
    """Tools shipped by their authors as one ready-made binary per platform (nodtool, nsz)."""
    for name in ("nodtool", "nsz"):
        pin = PINS[name]
        src = download(pin[f"{platform}_url"], pin[f"{platform}_sha256"])
        target = dest / (name + (".exe" if platform == "windows" else ""))
        shutil.copyfile(src, target)
        target.chmod(0o755)
        nl = "\r\n" if platform == "windows" else "\n"
        (dest / f"{name}-LICENSE.txt").write_text(TOOL_LICENSES[name].format(v=pin["version"]) + nl)
    # nstool ships as a zip holding one binary
    pin = PINS["nstool"]
    exe = "nstool" + (".exe" if platform == "windows" else "")
    with zipfile.ZipFile(download(pin[f"{platform}_url"], pin[f"{platform}_sha256"])) as zf:
        member = next(n for n in zf.namelist() if n.rsplit("/", 1)[-1] == exe)
        (dest / exe).write_bytes(zf.read(member))
    (dest / exe).chmod(0o755)
    nl = "\r\n" if platform == "windows" else "\n"
    (dest / "nstool-LICENSE.txt").write_text(TOOL_LICENSES["nstool"].format(v=pin["version"]) + nl)


def linux_tools(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    built = linux_chdman()
    shutil.copytree(built / "bin", dest / "bin", dirs_exist_ok=True)
    shutil.copytree(built / "lib", dest / "lib", dirs_exist_ok=True)
    (dest / "chdman").write_text(CHDMAN_WRAPPER)
    (dest / "chdman").chmod(0o755)
    tag = PINS["chdman_linux"]["mame_tag"]
    (dest / "chdman-LICENSE.txt").write_text(
        f"chdman is part of MAME {tag} (https://www.mamedev.org), built unmodified from the official source\n"
        f"package of that release. MAME is licensed under the GPL-2.0-or-later; source code:\n"
        f"https://github.com/mamedev/mame/tree/{tag}\n"
        f"lib/libSDL2-2.0.so.0 is a small stub (public domain) for 4 clipboard functions chdman never uses.\n",
        encoding="utf-8")
    xd = PINS["xdelta3"]
    with tarfile.open(download(xd["linux_url"], xd["linux_sha256"])) as tf:
        member = next(m for m in tf.getmembers() if m.name.endswith("/xdelta3"))
        (dest / "xdelta3").write_bytes(tf.extractfile(member).read())
    (dest / "xdelta3").chmod(0o755)
    (dest / "xdelta3-LICENSE.txt").write_text(f"xdelta3 {xd['version']} (https://github.com/jmacd/xdelta), official "
                                              f"release binary, unmodified. Licensed under the Apache License 2.0.\n")
    _single_binaries(dest, "linux")
    sz = PINS["sevenzip_linux"]
    tarball = download(sz["url"], sz["sha256"])
    with tarfile.open(tarball) as tf:
        for member, target in (("7zzs", "7zz"), ("License.txt", "7-Zip-License.txt")):
            data = tf.extractfile(member).read()
            (dest / target).write_bytes(data)
    (dest / "7zz").chmod(0o755)


def source_tools() -> Path:
    """For running from the source checkout (install.sh launchers): put the same pinned, checksum-verified
    nodtool, nsz, nstool and xdelta3 the builds bundle into <repo>/tools, where romtools.compat.find_tool looks.
    chdman and 7-Zip are left to the system (and ROM Optimizer's chdman choice), as before."""
    dest = ROOT / "tools"
    dest.mkdir(exist_ok=True)
    xd = PINS["xdelta3"]
    with tarfile.open(download(xd["linux_url"], xd["linux_sha256"])) as tf:
        member = next(m for m in tf.getmembers() if m.name.endswith("/xdelta3"))
        (dest / "xdelta3").write_bytes(tf.extractfile(member).read())
    (dest / "xdelta3").chmod(0o755)
    _single_binaries(dest, "linux")
    return dest


# ---------------------------------------------------------------- Linux

def build_linux(skip_selftest: bool) -> Path:
    step(f"Linux build {VERSION}")
    pyl = PINS["python_linux"]
    base = CACHE / "python-linux" / pyl["version"]
    if not (base / "bin" / "python3").exists():
        step(f"Unpacking portable Python {pyl['version']} (python-build-standalone)")
        tarball = download(pyl["url"], pyl["sha256"])
        shutil.rmtree(base, ignore_errors=True)
        base.mkdir(parents=True)
        with tarfile.open(tarball) as tf:
            tf.extractall(base, filter="data")
        inner = base / "python"           # archives contain a top-level "python/" folder
        for item in inner.iterdir():
            item.rename(base / item.name)
        inner.rmdir()
    venv = CACHE / f"venv-linux-{pyl['version']}"
    py = venv / "bin" / "python"
    if not py.exists():
        run([base / "bin" / "python3", "-m", "venv", venv])
    run([py, "-m", "pip", "install", "-q", "--upgrade", "pip"])
    run([py, "-m", "pip", "install", "-q", *pip_requirements()])

    icons = make_icons(BUILD / "icons")
    work, distp = BUILD / "linux" / "work", BUILD / "linux" / "dist"
    run([py, "-m", "PyInstaller", PKG / "romtools.spec", "--noconfirm", "--log-level", "WARN",
         "--distpath", distp, "--workpath", work], env={**os.environ, "ROMTOOLS_ICON_DIR": str(icons)})

    name = f"romtools-{VERSION}-linux-x64"
    out = DIST / name
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(distp / "romtools", out)
    shutil.copytree(ROOT / "icons", out / "icons")
    shutil.copy2(PKG / "linux" / "install.sh", out / "install.sh")
    shutil.copy2(ROOT / "README.md", out / "README.md")
    shutil.copy2(ROOT / "LICENSE", out / "LICENSE.txt")
    (out / "install.sh").chmod(0o755)
    step("Bundling chdman and 7-Zip")
    linux_tools(out / "tools")
    (out / "portable.txt").write_text(PORTABLE_TXT)

    if not skip_selftest:
        step("Linux self-test")
        report = BUILD / "linux" / "selftest.txt"
        report.unlink(missing_ok=True)
        subprocess.run([out / "rom-verifier", "--selftest", report], env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                       timeout=300)
        check_selftest(report, "Linux")

    step("Packing")
    archive = DIST / f"{name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(out, arcname=name)
    print(f"   {archive}  ({archive.stat().st_size >> 20} MB)")
    return archive


# ---------------------------------------------------------------- Windows

def wine_env() -> dict:
    return {**os.environ, "WINEPREFIX": str(CACHE / "wine"), "WINEARCH": "win64", "WINEDEBUG": "-all",
            "WINEDLLOVERRIDES": "mscoree,mshtml="}


def winpath(p: Path) -> str:
    if NATIVE_WIN:
        return str(p)
    return subprocess.run(["winepath", "-w", str(p)], env=wine_env(), capture_output=True, text=True,
                          check=True).stdout.strip()


def windows_tools(dest: Path) -> None:
    """chdman.exe from MAME's official release, 7z.exe/7z.dll from 7-Zip, both checksum-verified."""
    dest.mkdir(parents=True, exist_ok=True)
    seven = sevenzip_host()
    tag = PINS["chdman_windows"]["mame_tag"]
    base = f"https://github.com/mamedev/mame/releases/download/{tag}/"
    sums = download(base + "SHA256SUMS", name=f"{tag}-SHA256SUMS")
    asset = f"{tag}b_x64.exe"
    want = next((ln.split()[0] for ln in sums.read_text().splitlines()
                 if ln.split()[-1].lstrip("*") == asset), None)
    if not want:
        raise SystemExit(f"{asset} not listed in MAME's SHA256SUMS")
    mame = download(base + asset, want)
    run([seven, "e", "-y", f"-o{dest}", mame, "chdman.exe"], stdout=subprocess.DEVNULL)

    xd = PINS["xdelta3"]
    run([seven, "e", "-y", f"-o{dest}", download(xd["windows_url"], xd["windows_sha256"]), "*/xdelta3.exe"],
        stdout=subprocess.DEVNULL)
    (dest / "xdelta3-LICENSE.txt").write_text(f"xdelta3 {xd['version']} (https://github.com/jmacd/xdelta), official "
                                              f"release binary, unmodified. Licensed under the Apache License 2.0.\r\n")
    _single_binaries(dest, "windows")
    sz = PINS["sevenzip_windows"]
    inst = download(sz["url"], sz["sha256"])
    run([seven, "e", "-y", f"-o{dest}", inst, "7z.exe", "7z.dll", "License.txt"], stdout=subprocess.DEVNULL)
    (dest / "License.txt").replace(dest / "7-Zip-License.txt")
    (dest / "chdman-LICENSE.txt").write_text(
        f"chdman.exe is part of MAME {tag} (https://www.mamedev.org), taken unmodified from the official\n"
        f"release {asset}. MAME is licensed under the GPL-2.0-or-later; source code:\n"
        f"https://github.com/mamedev/mame/tree/{tag}\n", encoding="utf-8")


def build_windows(skip_selftest: bool) -> Path:
    step(f"Windows build {VERSION}" + ("" if NATIVE_WIN else " (using Wine)"))
    env = os.environ.copy() if NATIVE_WIN else wine_env()
    wine = [] if NATIVE_WIN else ["wine"]
    pyw = PINS["python_windows"]
    if NATIVE_WIN:
        py_cmd = [sys.executable]
    else:
        if not shutil.which("wine"):
            raise SystemExit("Wine is needed to build the Windows version on Linux (pacman -S wine)")
        target = CACHE / "wine" / "drive_c" / "Python314" / "python.exe"
        stamp = CACHE / "wine" / "python-version"
        if not target.exists() or not stamp.exists() or stamp.read_text().strip() != pyw["version"]:
            step(f"Installing Windows Python {pyw['version']} into the build's Wine prefix")
            run(["wineboot", "-u"], env=env)
            inst = download(pyw["url"], pyw["sha256"])
            run(["wine", inst, "/quiet", "InstallAllUsers=0", "TargetDir=C:\\Python314", "Include_test=0",
                 "Include_tcltk=0", "Include_launcher=0", "Include_doc=0", "Shortcuts=0", "AssociateFiles=0",
                 "PrependPath=0"], env=env)
            stamp.write_text(pyw["version"])
        py_cmd = ["wine", "C:\\Python314\\python.exe"]
    run([*py_cmd, "-m", "pip", "install", "-q", "--disable-pip-version-check", *pip_requirements()], env=env)

    step("Fetching chdman.exe and 7-Zip")
    tools = BUILD / "windows" / "tools"
    shutil.rmtree(tools, ignore_errors=True)
    windows_tools(tools)

    icons = make_icons(BUILD / "icons")
    work, distp = BUILD / "windows" / "work", BUILD / "windows" / "dist"
    step("PyInstaller")
    run([*py_cmd, "-m", "PyInstaller", winpath(PKG / "romtools.spec"), "--noconfirm", "--log-level", "WARN",
         "--distpath", winpath(distp), "--workpath", winpath(work)],
        env={**env, "ROMTOOLS_ICON_DIR": winpath(icons)})

    name = f"romtools-{VERSION}-windows-x64"
    out = DIST / name
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(distp / "romtools", out)
    shutil.copytree(tools, out / "tools")
    shutil.copy2(PKG / "windows" / "README-Windows.txt", out / "README-Windows.txt")
    shutil.copy2(ROOT / "README.md", out / "README.md")
    shutil.copy2(ROOT / "LICENSE", out / "LICENSE.txt")
    (out / "portable.txt").write_text(PORTABLE_TXT.replace("\n", "\r\n"))

    if not skip_selftest:
        step("Windows self-test" + ("" if NATIVE_WIN else " (under Wine)"))
        report = BUILD / "windows" / "selftest.txt"
        report.unlink(missing_ok=True)
        subprocess.run([*wine, out / "ROM Verifier.exe", "--selftest", winpath(report)],
                       env={**env, "QT_QPA_PLATFORM": "offscreen"}, timeout=600)
        check_selftest(report, "Windows")

    step("Packing")
    archive = DIST / f"{name}.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in sorted(out.rglob("*")):
            zf.write(f, Path(name) / f.relative_to(out))
    print(f"   {archive}  ({archive.stat().st_size >> 20} MB)")
    return archive


# ---------------------------------------------------------------- portability test

TEST_IMAGE = "ubuntu:24.04"   # oldest mainstream distro the Linux build targets (glibc 2.39)


def test_portable() -> None:
    """Run the Linux build's self-test in a clean container without chdman/7-Zip, folder mounted read-only."""
    builds = sorted(DIST.glob("romtools-*-linux-x64"))
    if not builds:
        raise SystemExit("no Linux build in dist/ - run `make linux` first")
    if not shutil.which("docker"):
        raise SystemExit("Docker is needed for this test")
    step(f"Self-testing {builds[-1].name} on {TEST_IMAGE}")
    script = ("export DEBIAN_FRONTEND=noninteractive; apt-get update -qq >/dev/null && apt-get install -y -qq "
              "--no-install-recommends libglib2.0-0t64 libfontconfig1 libfreetype6 libx11-6 libx11-xcb1 "
              "libxkbcommon0 libegl1 libgl1 libdbus-1-3 libxcb1 fonts-dejavu-core >/dev/null 2>&1; "
              'echo "   $(. /etc/os-release; echo $PRETTY_NAME), $(ldd --version | head -1)"; '
              "QT_QPA_PLATFORM=offscreen /app/rom-verifier --selftest /tmp/r.txt >/dev/null 2>&1; cat /tmp/r.txt")
    r = subprocess.run(["docker", "run", "--rm", "-v", f"{builds[-1]}:/app:ro", TEST_IMAGE, "bash", "-c", script],
                       capture_output=True, text=True)
    print("\n".join("   " + ln for ln in r.stdout.splitlines()))
    if "RESULT OK" not in r.stdout:
        print(r.stderr[-2000:])
        raise SystemExit("portable Linux build FAILED on " + TEST_IMAGE)


# ---------------------------------------------------------------- cleaning

def _remove(path: Path, must_contain: str) -> None:
    """Delete a build folder, refusing anything that isn't clearly one of ours."""
    path = path.resolve()
    if not path.exists():
        return
    if must_contain not in path.name or path in (Path.home().resolve(), Path("/").resolve(), ROOT):
        raise SystemExit(f"refusing to delete {path}")
    print(f"   removing {path}")
    shutil.rmtree(path)


def clean(cache_too: bool) -> None:
    for p in (BUILD, DIST):
        if p.resolve().parent != ROOT:
            raise SystemExit(f"refusing to delete {p}")
        if p.exists():
            print(f"   removing {p}")
            shutil.rmtree(p)
    if cache_too:
        _remove(CACHE, "romtools-build")


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", choices=["linux", "windows", "all", "tools", "test-portable", "clean", "distclean"])
    ap.add_argument("--skip-selftest", action="store_true", help="don't run the packaged program's self-test")
    args = ap.parse_args()
    if args.target in ("clean", "distclean"):
        clean(args.target == "distclean")
        return 0
    if args.target == "tools":
        if sys.platform == "win32":
            raise SystemExit("`tools` is for a Linux source checkout; Windows builds bundle their tools")
        step(f"Fetching nodtool, nsz, nstool and xdelta3 into {source_tools()}")
        return 0
    if args.target == "test-portable":
        test_portable()
        return 0
    DIST.mkdir(exist_ok=True)
    made = []
    if args.target in ("linux", "all"):
        made.append(build_linux(args.skip_selftest))
    if args.target in ("windows", "all"):
        made.append(build_windows(args.skip_selftest))
    step("Done")
    for m in made:
        print(f"   {m}")
    for platform in [p for p, t in (("linux", "linux"), ("windows", "windows")) if args.target in (t, "all")]:
        prune_old_builds(platform)
    write_release_files()
    return 0


def write_release_files() -> None:
    """dist/SHA256SUMS (the in-app updater only installs archives listed there) and dist/RELEASE_NOTES.md
    (this version's CHANGELOG.md section), ready to attach to a GitHub release."""
    archives = sorted(DIST.glob(f"romtools-{VERSION}-*.tar.gz")) + sorted(DIST.glob(f"romtools-{VERSION}-*.zip"))
    if not archives:
        return
    (DIST / "SHA256SUMS").write_text("".join(f"{sha256(a)}  {a.name}\n" for a in archives))
    log = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(VERSION)}\s*$(.*?)(?=^## |\Z)", log, re.M | re.S)
    (DIST / "RELEASE_NOTES.md").write_text((m.group(1).strip() if m else f"romtools {VERSION}") + "\n",
                                           encoding="utf-8")
    print(f"   {DIST / 'SHA256SUMS'}\n   {DIST / 'RELEASE_NOTES.md'}")


def prune_old_builds(platform: str) -> None:
    """After a successful build, remove builds of OTHER versions for the same platform from dist/
    (folder and archive), so dist/ only holds the current version."""
    old = [p for p in DIST.glob(f"romtools-*-{platform}-x64*")
           if not p.name.startswith(f"romtools-{VERSION}-{platform}-x64")]
    for p in old:
        shutil.rmtree(p) if p.is_dir() else p.unlink()
        print(f"   removed old build: {p.name}")


if __name__ == "__main__":
    sys.exit(main())
