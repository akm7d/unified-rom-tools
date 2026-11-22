"""chdman updater: compare with the latest official MAME release and update.

Two ways to update, both from official sources only:
  * the distribution package (pacman: mame-tools), run in a terminal so you
    see and confirm what pacman does;
  * a build from MAME's official source tag on GitHub (tools only, no
    emulator), installed per-user under ~/.local/share/romtools/chdman/<tag>/.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .chd import mem_available
from .chdman_tool import BUILD_DIR, Version, version

API = "https://api.github.com/repos/mamedev/mame/releases/latest"
REL_URL = "https://github.com/mamedev/mame/releases/download/{tag}/"
# Official source package attached to every MAME release (a 7-Zip SFX), plus its published checksums.
SRC_ASSET = "{tag}s.exe"


@dataclass
class Release:
    tag: str            # mame0290
    number: str         # 0.290
    date: str
    url: str

    @property
    def key(self) -> tuple[int, ...]:
        return Version(self.number, self.tag, "").key


def latest_release(timeout: int = 20) -> Release:
    req = urllib.request.Request(API, headers={"User-Agent": "romtools/1.0",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    tag = d["tag_name"]
    digits = tag.removeprefix("mame")
    number = f"{digits[0]}.{digits[1:]}" if digits.isdigit() else digits
    return Release(tag, number, d.get("published_at", "")[:10], d.get("html_url", ""))


@dataclass
class PackageInfo:
    owner: str | None       # package owning /usr/bin/chdman, e.g. mame-tools
    installed: str | None
    repo: str | None        # version in the local sync database (as of your last pacman -Sy)


def package_info(binary: str = "/usr/bin/chdman") -> PackageInfo:
    if not shutil.which("pacman") or not Path(binary).exists():
        return PackageInfo(None, None, None)
    own = subprocess.run(["pacman", "-Qqo", binary], capture_output=True, text=True).stdout.strip() or None
    inst = repo = None
    if own:
        q = subprocess.run(["pacman", "-Q", own], capture_output=True, text=True).stdout.split()
        inst = q[1] if len(q) > 1 else None
        si = subprocess.run(["pacman", "-Si", own], capture_output=True, text=True).stdout
        for line in si.splitlines():
            if line.startswith("Version"):
                repo = line.split(":", 1)[1].strip()
                break
    return PackageInfo(own, inst, repo)


def built_versions() -> list[tuple[str, Path]]:
    out = []
    if BUILD_DIR.is_dir():
        for d in sorted(BUILD_DIR.iterdir(), reverse=True):
            for exe in (d / "chdman", d / "chdman.exe"):
                if exe.is_file() and (exe.suffix == ".exe" or os.access(exe, os.X_OK)):
                    out.append((d.name, exe))
    return out


TERMINALS = [
    ("konsole", ["konsole", "--hold", "-e"]),
    ("gnome-terminal", ["gnome-terminal", "--"]),
    ("kgx", ["kgx", "--"]),
    ("kitty", ["kitty", "--hold"]),
    ("alacritty", ["alacritty", "--hold", "-e"]),
    ("foot", ["foot", "--hold"]),
    ("xfce4-terminal", ["xfce4-terminal", "--hold", "-x"]),
    ("xterm", ["xterm", "-hold", "-e"]),
]


def open_in_terminal(shell_cmd: str) -> bool:
    for name, prefix in TERMINALS:
        if shutil.which(name):
            subprocess.Popen(prefix + ["bash", "-c", shell_cmd], start_new_session=True)
            return True
    return False


def pacman_update_command(pkg: str) -> str:
    # A full upgrade (-Syu) is the only supported way to update a single package on Arch.
    return (f"echo 'Updating {pkg} (and the rest of the system, as Arch requires) with pacman.'; "
            f"echo; sudo pacman -Syu {shlex.quote(pkg)}; echo; echo 'Done - you can close this window.'")


def build_dir_default() -> Path:
    """Build in RAM when there is plenty of it (no SSD writes), else on disk."""
    if mem_available() > 10 * 1024 ** 3 and Path("/dev/shm").is_dir():
        return Path("/dev/shm/romtools-build")
    return Path.home() / ".cache/romtools/build"


def build_script(tag: str, work_root: Path, jobs: int) -> str:
    """Bash script: fetch the release's official source package, check its SHA-256 against MAME's
    published SHA256SUMS, unpack, build tools only (no emulator), install chdman per-user."""
    w = shlex.quote(str(work_root / tag))
    dest = shlex.quote(str(BUILD_DIR / tag))
    base = REL_URL.format(tag=tag)
    asset = SRC_ASSET.format(tag=tag)
    digits = tag.removeprefix("mame")
    tag_num = f"{digits[0]}.{digits[1:]}" if digits.isdigit() else digits
    flags = ("TOOLS=1 EMULATOR=0 NOWERROR=1 IGNORE_GIT=1 NO_USE_MIDI=1 NO_USE_PORTAUDIO=1 "
             "NO_USE_PULSEAUDIO=1 NO_USE_PIPEWIRE=1 OPTIMIZE=2")
    return f"""set -euo pipefail
trap 'echo; echo "== Cleaning up build folder"; rm -rf {w}; rmdir {shlex.quote(str(work_root))} 2>/dev/null || true' EXIT
for t in gcc g++ make python3 curl 7z sha256sum; do
  command -v $t >/dev/null || {{ echo "Missing tool: $t (e.g. sudo pacman -S base-devel 7zip)"; exit 2; }}
done
mkdir -p {w}
cd {w}
echo "== Downloading official MAME {tag} source package from GitHub releases"
curl -L --fail -sS -o SHA256SUMS {shlex.quote(base + "SHA256SUMS")}
curl -L --fail -sS -o {asset} {shlex.quote(base + asset)}
echo "== Checking SHA-256 against MAME's published SHA256SUMS"
grep -E ' [*]?{asset}$' SHA256SUMS | sed 's/ [*]/  /' | sha256sum -c -
echo "== Unpacking (skipping artwork, docs, ROM hash lists and other non-code folders)"
7z x -y -bd -bso0 -bsp0 {asset} -x!hash -x!artwork -x!docs -x!samples -x!language -x!ctrlr
rm -f {asset}
echo "== Building tools only (no emulator) with {jobs} jobs - this takes a while"
make -j{jobs} {flags}
test -x chdman
./chdman > version.txt 2>&1 || true
head -1 version.txt
grep -q "manager {tag_num}" version.txt || {{ echo "Built chdman reports an unexpected version"; exit 3; }}
mkdir -p {dest}
install -m755 chdman {dest}/chdman
echo "== Installed {dest}/chdman"
"""


# ---------------------------------------------------------------- Windows: official binaries

WIN_ASSET = "{tag}b_x64.exe"   # MAME's official 64-bit Windows release (a 7-Zip self-extractor)


def install_official_windows(tag: str, log, cancel) -> Path:
    """Download MAME's official Windows release, check it against the published SHA256SUMS, and take
    chdman.exe out of it into BUILD_DIR/<tag>/chdman.exe. Returns that path."""
    import hashlib
    import tempfile
    from .hashing import SEVENZIP

    if not SEVENZIP:
        raise RuntimeError("7z.exe is missing from the tools folder")
    base = REL_URL.format(tag=tag)
    asset = WIN_ASSET.format(tag=tag)
    work = Path(tempfile.mkdtemp(prefix="chdman-dl-"))
    try:
        def fetch(name: str, dest: Path, progress: bool = False) -> None:
            req = urllib.request.Request(base + name, headers={"User-Agent": "romtools/1.1"})
            with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as fh:
                total = int(r.headers.get("Content-Length") or 0)
                done, last = 0, -1
                while chunk := r.read(1 << 20):
                    if cancel.is_set():
                        raise RuntimeError("cancelled")
                    fh.write(chunk)
                    done += len(chunk)
                    pct = int(done * 100 / total) if total else 0
                    if progress and pct // 10 != last:
                        last = pct // 10
                        log(f"   {done >> 20} / {total >> 20} MB")

        log(f"== Downloading MAME {tag} checksums and Windows release from GitHub")
        fetch("SHA256SUMS", work / "SHA256SUMS")
        fetch(asset, work / asset, progress=True)
        log("== Checking SHA-256 against MAME's published SHA256SUMS")
        want = ""
        for line in (work / "SHA256SUMS").read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == asset:
                want = parts[0].lower()
        h = hashlib.sha256()
        with open(work / asset, "rb") as fh:
            while chunk := fh.read(8 << 20):
                h.update(chunk)
        if not want or h.hexdigest() != want:
            raise RuntimeError("checksum mismatch - download discarded")
        log(f"   {asset}: OK")
        log("== Extracting chdman.exe")
        r = subprocess.run([SEVENZIP, "e", "-y", f"-o{work}", str(work / asset), "chdman.exe"],
                           capture_output=True, text=True)
        exe = work / "chdman.exe"
        if r.returncode != 0 or not exe.is_file():
            raise RuntimeError(f"couldn't extract chdman.exe: {r.stderr.strip() or r.stdout.strip()}")
        v = version(str(exe))
        if not v:
            raise RuntimeError("the extracted chdman.exe doesn't run")
        log(f"   {v.raw}")
        dest = BUILD_DIR / tag
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(exe, dest / "chdman.exe")
        log(f"== Installed {dest / 'chdman.exe'}")
        return dest / "chdman.exe"
    finally:
        shutil.rmtree(work, ignore_errors=True)
