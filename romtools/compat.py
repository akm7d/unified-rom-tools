"""Platform differences (Linux / Windows) in one place.

Linux is the primary platform. On Windows:
  * settings/data/cache live under %APPDATA% / %LOCALAPPDATA%
  * the RAM streaming mode for CHDs is unavailable (it relies on Linux tmpfs, hole punching and
    SIGSTOP), so CHDs always use temp files ("SSD mode")
  * bundled tools (chdman.exe, 7z.exe) are taken from the "tools" folder next to the program
  * child processes never pop up console windows
  * the trash is the Recycle Bin

Portable mode (packaged builds): when a "portable.txt" file sits next to the programs, settings, DATs, tags,
rename history and remembered scans are kept in a "data" folder beside them instead of the user profile,
so the whole folder can be copied or moved (USB stick, another PC). Delete portable.txt to switch back.
ROMTOOLS_HOME=<folder> forces that layout anywhere (used by the self-test).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

IS_WIN = os.name == "nt"
FROZEN = bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Folder of the installed program (Windows build) or of the source checkout."""
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def resource_dir() -> Path:
    """Read-only files shipped with the program (icons). Inside packaged builds: PyInstaller's _internal."""
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", app_dir()))
    return app_dir()


PORTABLE_MARKER = "portable.txt"


def portable_marker() -> bool:
    return FROZEN and (app_dir() / PORTABLE_MARKER).is_file()


def _writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write-test"
        probe.write_text("x")
        probe.unlink()
        return True
    except OSError:
        return False


def portable_root() -> Path | None:
    """Folder holding everything in portable mode, or None for the normal per-user locations."""
    forced = os.environ.get("ROMTOOLS_HOME")
    if forced:
        return Path(forced)
    if portable_marker():
        d = app_dir() / "data"
        if _writable(d):          # e.g. extracted into a read-only location: fall back to the user profile
            return d
    return None


def romtools_dirs() -> tuple[Path, Path, Path]:
    """(config, data, cache) folders for romtools itself."""
    root = portable_root()
    if root is not None:
        return root / "config", root, root / "cache"
    c, d, k = user_dirs()
    return c / "romtools", d / "romtools", k / "romtools"


def user_dirs() -> tuple[Path, Path, Path]:
    """(config, data, cache) base folders; romtools/ is appended by the caller."""
    if IS_WIN:
        roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        return roaming, local, local / "Cache"

    def xdg(var: str, default: str) -> Path:
        return Path(os.environ.get(var) or Path.home() / default)

    return xdg("XDG_CONFIG_HOME", ".config"), xdg("XDG_DATA_HOME", ".local/share"), xdg("XDG_CACHE_HOME", ".cache")


def default_tmp_dir() -> str:
    if IS_WIN:   # the system temp folder: nothing left in the user profile (matters for portable copies)
        import tempfile
        return str(Path(tempfile.gettempdir()) / "romtools")
    return "/var/tmp/romtools"   # local disk, not tmpfs: DVD CHDs extract to ~4-8 GB


def find_tool(*names: str) -> str | None:
    """A bundled tool (tools/ next to the program) or one on PATH."""
    for n in names:
        exe = n + (".exe" if IS_WIN and not n.endswith(".exe") else "")
        bundled = app_dir() / "tools" / exe
        if bundled.is_file():
            return str(bundled)
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    return None


def ram_mode_supported() -> bool:
    return not IS_WIN


def popen_session_kwargs() -> dict:
    """Run a child in its own process group (POSIX) so it can be stopped with its children."""
    return {} if IS_WIN else {"start_new_session": True}


def terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if IS_WIN:
            proc.terminate()
        else:
            import signal
            os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass


def trash(path: Path) -> bool:
    """Move to the desktop trash / Recycle Bin. False if that isn't possible (file left in place)."""
    if not IS_WIN and shutil.which("gio"):
        if subprocess.run(["gio", "trash", str(path)], capture_output=True).returncode == 0:
            return True
    try:
        from PySide6.QtCore import QFile
        ok = QFile.moveToTrash(str(path))
        return bool(ok[0] if isinstance(ok, tuple) else ok)
    except Exception:  # noqa: BLE001
        return False


APP_FILES = {"verifier": ("ROM Verifier.exe", "rom-verifier"), "renamer": ("ROM Renamer.exe", "rom-renamer"),
             "converter": ("ROM Optimizer.exe", "rom-optimizer")}


def app_file(folder: Path, module: str) -> Path:
    """The program that starts one of the three apps in a packaged build's folder."""
    win, lin = APP_FILES[module]
    return folder / (win if IS_WIN else lin)


def app_command(module: str) -> list[str]:
    return [str(app_file(app_dir(), module))] if FROZEN else [sys.executable, "-m", f"romtools.gui.{module}"]


def start_app(argv: list[str], folder: Path) -> None:
    env = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"} if FROZEN else None
    subprocess.Popen(argv, cwd=str(folder), env=env)


def launch_app(module: str) -> None:
    """Start another of the three apps (verifier / renamer / converter). Raises OSError if it can't be started."""
    start_app(app_command(module), app_dir())


def lies_inside(path: Path, folder: Path) -> bool:
    """Does this file really lie in (or below) that folder? (A .cue can name any path - "../x", a full path, a link.)"""
    try:
        return folder.resolve() in path.resolve().parents
    except OSError:
        return False


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 10.0):
    """Keep other romtools windows from changing the same file at the same time (``path`` + ".lock"). Advisory; if it
    can't be had in time, go on without it rather than fail."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = None
    try:
        fh = open(path.with_name(path.name + ".lock"), "a+b")
        end = time.monotonic() + timeout
        while True:
            try:
                if IS_WIN:
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > end:
                    break
                time.sleep(0.02)
    except OSError:
        pass                            # e.g. a read-only folder: the write itself will say so
    try:
        yield
    finally:
        if fh is not None:
            if IS_WIN:
                import msvcrt
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            fh.close()                  # (closing releases it on POSIX)


def clean_library_path(current: str, build: Path) -> str:
    """LD_LIBRARY_PATH without the folders of this or another romtools build (an older version that started us)."""
    def ours(entry: str) -> bool:
        d = Path(entry)
        return d == build or (d.name == "_internal" and ((d.parent / "rom-verifier").exists() or not d.exists()))
    return ":".join(e for e in current.split(":") if e and not ours(e))


def restore_environment() -> None:
    """Linux build: its launcher points LD_LIBRARY_PATH at the bundled libraries. Our own process has them by now; other
    programs must not get them (file managers, terminals and browsers crash with another Qt)."""
    if IS_WIN or not FROZEN:
        return
    orig = os.environ.pop("LD_LIBRARY_PATH_ORIG", "")
    os.environ.pop("LD_LIBRARY_PATH", None)
    keep = clean_library_path(orig, resource_dir())
    if keep:
        os.environ["LD_LIBRARY_PATH"] = keep


def hide_console_windows() -> None:
    """Windows: stop every child process (chdman, 7z) from flashing a console window."""
    if not IS_WIN:
        return
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    original = subprocess.Popen.__init__

    def patched(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | flag
        original(self, *args, **kwargs)

    if getattr(subprocess.Popen.__init__, "_romtools", False):
        return
    patched._romtools = True
    subprocess.Popen.__init__ = patched


def pid_alive(pid: int) -> bool:
    if IS_WIN:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    return Path(f"/proc/{pid}").exists()


def mem_available() -> int:
    if IS_WIN:
        import ctypes

        class MEMSTAT(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMSTAT()
        st.dwLength = ctypes.sizeof(MEMSTAT)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return int(st.ullAvailPhys)
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0
