"""chdman wrapper: binary selection, version, command/option discovery, and job execution.

Commands and options are read from `chdman help <command>` at runtime, so the
GUI always matches whichever chdman build is selected (including new options
after an update).
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .compat import find_tool, popen_session_kwargs, terminate
from .config import DATA_DIR, get_config

BUILD_DIR = DATA_DIR / "chdman"   # chdman builds made by the updater: BUILD_DIR/<tag>/chdman


def chdman_bin() -> str | None:
    """The chdman selected in settings: 'system', or a path to a binary."""
    sel = get_config()["chdman_path"] or "system"
    if sel != "system" and os.access(sel, os.X_OK):
        return sel
    return find_tool("chdman")


@dataclass
class Version:
    number: str        # "0.289"
    tag: str           # "mame0289"
    raw: str

    @property
    def key(self) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", self.number))


def version(binary: str | None = None) -> Version | None:
    binary = binary or chdman_bin()
    if not binary:
        return None
    try:
        out = _run_text([binary])
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"manager\s+(\d+\.\d+)\s*\(([^)]*)\)", out)
    if not m:
        return None
    return Version(m.group(1), m.group(2).split("-")[0], m.group(0))


# ---------------------------------------------------------------- command discovery

@dataclass
class Option:
    long: str
    short: str
    arg: str           # "" for flags
    desc: str
    required: bool

    @property
    def is_flag(self) -> bool:
        return not self.arg

    @property
    def kind(self) -> str:
        a, n = self.arg.lower(), self.long
        if self.is_flag:
            return "flag"
        if "filename" in a or a == "file":
            if n.startswith("output") and "parent" not in n:  # outputparent is an existing CHD to read
                return "outfile"
            return "infile"
        if n == "compression":
            return "compression"
        return "text"


@dataclass
class Command:
    name: str
    desc: str
    options: list[Option] = field(default_factory=list)

    @property
    def group(self) -> str:
        for g in ("create", "extract"):
            if self.name.startswith(g):
                return g
        return "meta" if self.name.endswith("meta") else "inspect"


_OPT = re.compile(r"^\s*--([\w-]+),\s*-([\w-]+):?\s*(?:<([^>]*)>)?:?\s*(.*)$")
_CMD = re.compile(r"^\s*\S*chdman\S*\s+(\w+):\s*(.*)$")
_cache: dict[tuple, list[Command]] = {}


def commands(binary: str | None = None) -> list[Command]:
    binary = binary or chdman_bin()
    if not binary:
        return []
    key = (binary, os.stat(binary).st_mtime)
    if key in _cache:
        return _cache[key]
    out = _run_text([binary])
    cmds = []
    for line in out.splitlines():
        m = _CMD.match(line)
        if not m:
            continue
        cmd = Command(m.group(1), m.group(2).strip())
        help_out = _run_text([binary, "help", cmd.name])
        for hl in help_out.splitlines():
            om = _OPT.match(hl)
            if om:
                desc = om.group(4).strip()
                req = "(required)" in desc
                cmd.options.append(Option(om.group(1), om.group(2), (om.group(3) or "").strip(),
                                          desc.replace("(required)", "").strip(), req))
        cmds.append(cmd)
    _cache[key] = cmds
    return cmds


def _run_text(argv: list[str]) -> str:
    """chdman prints usage/help to stdout or stderr depending on version: take both."""
    r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                       timeout=30)
    return r.stdout


def build_argv(cmd: str, values: dict[str, object], binary: str | None = None) -> list[str]:
    """values: long option name -> str (argument) or bool (flag). Empty/False values are omitted."""
    argv = [binary or chdman_bin() or "chdman", cmd]
    for name, val in values.items():
        if val is True:
            argv.append(f"--{name}")
        elif val not in (None, False, ""):
            argv += [f"--{name}", str(val)]
    return argv


# ---------------------------------------------------------------- running

_PCT = re.compile(r"(\d+(?:\.\d+)?)% complete")


class Process:
    """Run one chdman command; streams log lines and progress, supports cancel."""

    def __init__(self, argv: list[str], on_line: Callable[[str], None] | None = None,
                 on_progress: Callable[[float], None] | None = None,
                 on_status: Callable[[str], None] | None = None, env: dict | None = None) -> None:
        self.argv = argv
        self.env = env
        self.on_line = on_line or (lambda s: None)
        self.on_progress = on_progress or (lambda f: None)
        self.on_status = on_status or (lambda s: None)   # live progress text, e.g. "Compressing, 45% (ratio=40%)"
        self.proc: subprocess.Popen | None = None
        self.cancelled = False
        self._lock = threading.Lock()

    def run(self) -> int:
        self.on_line("$ " + " ".join(_quote(a) for a in self.argv))
        with self._lock:
            if self.cancelled:
                return -1
            self.proc = subprocess.Popen(self.argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=self.env,
                                         **popen_session_kwargs())
        buf = b""
        last_pct_line = ""
        while True:
            chunk = self.proc.stdout.read1(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                line = raw.decode(errors="replace").rstrip()
                if not line:
                    continue
                m = _PCT.search(line)
                if m:
                    self.on_status(line.strip())
                    self.on_progress(float(m.group(1)) / 100.0)
                    last_pct_line = line
                    continue  # don't flood the log with progress lines
                self.on_line(line)
        if buf.strip():
            self.on_line(buf.decode(errors="replace").strip())
        rc = self.proc.wait()
        if last_pct_line and rc == 0:
            self.on_progress(1.0)
        if self.cancelled:
            self.on_line("[cancelled]")
        return rc

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            if self.proc:
                terminate(self.proc)


def _quote(a: str) -> str:
    return f"'{a}'" if re.search(r"[\s'\"()&;]", a) else a
