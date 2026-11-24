"""Questions about a whole collection, answered from scan results: games stored more than once, and multi-disc games with
discs missing. Only looks; whatever is done about it is up to the user."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import packages
from .scanner import (BAD, ERROR, IDENTIFIED, MISNAMED, MISPLACED, MODIFIED, NODAT, PARTIAL, SKIPPED, UNKNOWN, VERIFIED,
                      Result)

IDENTICAL, SAME_GAME, VERSIONS = "identical", "same game", "other versions"
KIND_HELP = {
    IDENTICAL: "The same data, stored more than once (also when one of them is zipped, a CHD, …)",
    SAME_GAME: "The same game and version, but the data isn't the same (e.g. one is a bad or modified dump)",
    VERSIONS: "Different versions of one game: another region, revision, translation or hack",
}
_TAGS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_DISC = re.compile(r"\((?:Disc|Disk|CD|DVD|Side)\s*([0-9]{1,2}|[A-H])\b[^)]*\)", re.I)
_ORDER = {VERIFIED: 0, IDENTIFIED: 0, MISNAMED: 1, MISPLACED: 2, MODIFIED: 3, PARTIAL: 4, UNKNOWN: 5, NODAT: 5, BAD: 6}
# better ways to store the same data come first
_FORMAT = {".chd": 0, ".rvz": 0, ".nsz": 0, ".xcz": 0, ".zip": 2, ".7z": 2, ".rar": 3}


@dataclass
class Copy:
    result: Result
    keep: bool = False          # the one to keep, if only one is wanted
    why: str = ""

    @property
    def path(self) -> Path:
        return self.result.item.path

    @property
    def files(self) -> list[Path]:
        return [self.path, *self.result.item.parts]

    @property
    def removable(self) -> str:
        """"" or why this copy can't be moved to the trash from here."""
        if self.result.item.member:
            return "one game inside an archive of several"
        if self.path.is_dir():
            return "an extracted game folder"
        return ""


@dataclass
class Group:
    kind: str
    title: str
    system: str
    copies: list[Copy] = field(default_factory=list)

    @property
    def spare(self) -> int:
        """Bytes the copies take beyond the one to keep."""
        return sum(c.result.size for c in self.copies if not c.keep)


def base_title(name: str) -> str:
    """"Final Fantasy VII (USA) (Disc 1) (Rev 2) [T-En]" -> "final fantasy vii disc 1"."""
    d = _DISC.search(name)
    t = _TAGS.sub("", name).strip()
    t = re.sub(r",\s*(the|a|an)\b", "", t, flags=re.I)
    t = re.sub(r"^(the|a|an)\s+", "", t, flags=re.I)
    t = re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()
    return t + (f" disc {d[1].lower()}" if d else "")


def _system(r: Result) -> str:
    return r.found or r.item.system


def _data_key(r: Result) -> tuple | None:
    """What the data is, whatever it is stored in: the hashes of its tracks / files (without emulator headers)."""
    if not r.entries or any(not e.entry.full.sha1 for e in r.entries):
        return None
    return tuple(sorted((e.entry.headerless or e.entry.full).sha1 for e in r.entries))


def _rank(r: Result) -> tuple:
    ext = r.item.path.suffix.lower()
    return (_ORDER.get(r.status, 7), _FORMAT.get(ext, 1), r.size, str(r.item.path).lower())


def _group(kind: str, title: str, system: str, rs: list[Result]) -> Group:
    rs = sorted(rs, key=_rank)
    g = Group(kind, title, system, [Copy(r) for r in rs])
    best = g.copies[0]
    best.keep = True
    if kind == VERSIONS:
        best.why = "verified" if best.result.status in (VERIFIED, IDENTIFIED) else "best result"
    else:
        second = g.copies[1].result
        a, b = _ORDER.get(best.result.status, 7), _ORDER.get(second.status, 7)
        best.why = (best.result.status.lower() if a < b else "smaller format" if best.result.size < second.size else
                    "first of equals")
    return g


def duplicates(results: list[Result], versions: bool = True) -> list[Group]:
    """Groups of copies, the most certain kind first. A file is only in one group."""
    rs = [r for r in results if r.status not in (SKIPPED, ERROR) and not (r.item.kind == "folder")]
    out: list[Group] = []
    used: set[int] = set()

    def take(kind: str, key, title) -> None:
        groups: dict = {}
        for r in rs:
            if id(r) not in used:
                k = key(r)
                if k is not None:
                    groups.setdefault(k, []).append(r)
        for members in groups.values():
            if len({(m.item.path, m.item.member) for m in members}) > 1:
                used.update(map(id, members))
                out.append(_group(kind, title(members[0]), _system(members[0]), members))

    def pkg(r: Result) -> dict | None:
        return (r.chd_info or {}).get("package")

    def name(r: Result) -> str:
        return r.game or r.item.stem
    take(IDENTICAL, lambda r: None if pkg(r) else _data_key(r), name)

    def same(r: Result):
        p = pkg(r)
        if p:
            contents = tuple(sorted((c["kind"], c["title_id"], c["version"]) for c in p.get("contents", [])))
            # (PS4: all DLC of a game share its title ID and version, only the content ID tells them apart)
            return ("pkg", p["platform"], p.get("content_id", ""), contents) if contents and not r.item.path.is_dir() else None
        return ("dat", r.dat, r.game) if r.game and r.status not in (UNKNOWN, NODAT) else None
    take(SAME_GAME, same, name)
    if versions:
        def family(r: Result):
            p = pkg(r)
            if p:
                # one release of a game: its updates and DLC belong to it and aren't versions of it
                if not str(p.get("kind", "")).startswith(packages.GAME) or r.item.path.is_dir():
                    return None
                return (p["platform"], base_title(packages.clean_title(p.get("title", "")) or r.item.stem))
            t = base_title(name(r))
            return (_system(r), t) if t else None
        take(VERSIONS, family, lambda r: _TAGS.sub("", name(r)).strip() or name(r))
    order = [IDENTICAL, SAME_GAME, VERSIONS]
    return sorted(out, key=lambda g: (order.index(g.kind), -g.spare, g.title.lower()))


# ---------------------------------------------------------------- missing discs

@dataclass
class MissingDiscs:
    game: str
    system: str
    have: list[str]                 # "1", "2" …
    missing: list[str]
    sure: bool                      # the DAT lists them (else: there's a gap in the numbers)
    names: list[str] = field(default_factory=list)      # what the DAT calls the missing ones
    folder: Path | None = None


def disc_of(name: str) -> tuple[str, str] | None:
    """(game without its disc, disc) - "FF VII (USA) (Disc 2) (Rev 1)" -> ("ff vii|usa", "2")."""
    m = _DISC.search(name)
    if not m:
        return None
    before = name[:m.start()]
    region = re.search(r"\(([^)]*)\)", before)
    title = re.sub(r"[^a-z0-9]+", " ", _TAGS.sub("", before).lower()).strip()
    return f"{title}|{region[1].lower() if region else ''}", m[1].upper()


def _sort(d: str):
    return (not d.isdigit(), int(d) if d.isdigit() else 0, d)


def missing_discs(results: list[Result], indexes: dict[str, object]) -> list[MissingDiscs]:
    """Games of which some discs are there and others aren't. ``indexes``: system id -> its DatIndex."""
    have: dict[tuple[str, str], dict[str, Result]] = {}
    for r in results:
        if r.status in (SKIPPED, ERROR):
            continue
        for n in dict.fromkeys((r.game, r.item.stem)):
            d = disc_of(n) if n else None
            if d:
                have.setdefault((_system(r), d[0]), {}).setdefault(d[1], r)
                break
    listed: dict[str, dict[str, dict[str, str]]] = {}
    for sysid, index in indexes.items():
        if index is None or not any(s == sysid for s, _g in have):
            continue
        table = listed.setdefault(sysid, {})
        for dat in index.dats:
            for g in dat.games:
                d = disc_of(g.name)
                if d:
                    table.setdefault(d[0], {}).setdefault(d[1], g.name)
    out = []
    for (sysid, game), discs in have.items():
        first = next(iter(discs.values()))
        shown = _DISC.sub("", first.game or first.item.stem).strip()
        shown = re.sub(r"\s{2,}", " ", shown)
        known = listed.get(sysid, {}).get(game, {})
        if known:
            gone = sorted(set(known) - set(discs), key=_sort)
            names = [known[d] for d in gone]
        else:
            nums = [int(d) for d in discs if d.isdigit()]
            gone = [str(n) for n in range(1, max(nums, default=0)) if n not in nums]
            names = []
        if gone:
            out.append(MissingDiscs(shown, sysid, sorted(discs, key=_sort), gone, bool(known), names,
                                    first.item.path.parent))
    return sorted(out, key=lambda m: (m.system, m.game.lower()))
