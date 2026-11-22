"""Identify console packages from their own metadata: PS4 PKG and Switch NSP / NSZ / XCI / XCZ.

There are no public DATs for these (Switch dumps are personalised, PS4 PKGs are digital packages), so instead
of hashing them we read what the package says about itself:

* PS4 ``.pkg``: the unencrypted PARAM.SFO inside (title, Title ID, content ID, type, version, required firmware).
* Switch: the content metadata (CNMT) of every title inside - plain ``.cnmt.xml`` when present, else the
  ``.cnmt.nca`` decrypted with your own ``prod.keys`` - and the game name / display version from the control
  NCA's ``control.nacp``. Without keys only the Title IDs that are readable in clear are shown.

Nothing is written; only headers are read, so even 30 GB packages take milliseconds.
"""

from __future__ import annotations

import re
import struct
from dataclasses import asdict, dataclass, field
from pathlib import Path

PS4_EXTS = {".pkg"}
SWITCH_EXTS = {".nsp", ".nsz", ".xci", ".xcz"}
PACKAGE_EXTS = PS4_EXTS | SWITCH_EXTS

GAME, UPDATE, DLC, APP = "Game", "Update", "DLC", "App"


class PackageError(Exception):
    pass


@dataclass
class Content:
    """One title inside a package (a Switch NSP can bundle a game, its update and DLC)."""
    kind: str                  # Game | Update | DLC
    title_id: str              # its own ID (Switch: 16 hex digits; PS4: CUSA…)
    version: int = 0           # Switch: raw version (65536 per update); PS4: 109 for 01.09
    version_str: str = ""      # "1.09" / "v393216"
    display_version: str = ""  # Switch NACP "1.6.0"


@dataclass
class PackageInfo:
    platform: str              # ps4 | switch
    path: str
    title: str = ""
    title_id: str = ""         # PS4: CUSA…; Switch: the main content's own ID
    base_id: str = ""          # ID of the base game this belongs to (groups game / updates / DLC)
    kind: str = ""             # Game | Update | DLC | App ("Game + Update", "Game + 2 DLC" for bundles)
    version: int = 0
    version_str: str = ""
    display_version: str = ""
    region: str = ""
    content_id: str = ""
    firmware: str = ""         # required system software, e.g. "9.00"
    backport: bool = False
    contents: list[Content] = field(default_factory=list)
    note: str = ""             # e.g. "no prod.keys: names not read"

    def as_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "PackageInfo":
        d = dict(d)
        d["contents"] = [Content(**c) for c in d.get("contents", [])]
        return PackageInfo(**d)


# ============================================================================ PS4

_PS4_CATEGORY = {"gd": GAME, "gp": UPDATE, "ac": DLC}
_REGION = {"U": "USA", "E": "Europe", "J": "Japan", "H": "Asia", "K": "Korea", "I": "World"}


def _sfo(data: bytes) -> dict:
    if data[:4] != b"\0PSF":
        raise PackageError("PARAM.SFO not found")
    ks, ds, cnt = struct.unpack("<III", data[8:20])
    out = {}
    for k in range(cnt):
        ko, fm, ln, _mx, do = struct.unpack("<HHIII", data[20 + 16 * k:36 + 16 * k])
        key = data[ks + ko:data.index(b"\0", ks + ko)].decode("ascii", "replace")
        v = data[ds + do:ds + do + ln]
        out[key] = struct.unpack("<I", v[:4])[0] if fm == 0x404 else v.rstrip(b"\0").decode("utf-8", "replace")
    return out


def _ps4_version(s: str) -> int:
    m = re.match(r"(\d+)\.(\d+)", s or "")
    return int(m.group(1)) * 100 + int(m.group(2)) if m else 0


def read_ps4(path: Path) -> PackageInfo:
    with open(path, "rb") as f:
        h = f.read(0x40)
        if h[:4] != b"\x7fCNT":
            if h[:4] == b"\x7fPKG":
                raise PackageError("PS3/PSP/Vita package, not a PS4 one")
            raise PackageError("not a PS4 package")
        n = struct.unpack(">I", h[0x10:0x14])[0]
        table = struct.unpack(">I", h[0x18:0x1C])[0]
        if n > 100000:
            raise PackageError("damaged package header")
        f.seek(table)
        t = f.read(32 * n)
        sfo = None
        for k in range(n):
            eid, _fo, _f1, _f2, off, sz = struct.unpack(">IIIIII", t[32 * k:32 * k + 24])
            if eid == 0x1000:
                f.seek(off)
                sfo = f.read(sz)
                break
    if not sfo:
        raise PackageError("PARAM.SFO not found in the package")
    d = _sfo(sfo)
    kind = _PS4_CATEGORY.get(str(d.get("CATEGORY", "")), APP)
    ver = str(d.get("VERSION" if kind == DLC else "APP_VER") or d.get("VERSION") or "")
    cid = str(d.get("CONTENT_ID", ""))
    fw = d.get("SYSTEM_VER")
    tid = str(d.get("TITLE_ID", "")) or (cid[7:16] if len(cid) >= 16 else "")
    info = PackageInfo(
        "ps4", str(path), title=str(d.get("TITLE", "")), title_id=tid, base_id=tid, kind=kind,
        version=_ps4_version(ver), version_str=re.sub(r"^0(\d)", r"\1", ver),
        region=_REGION.get(cid[:1], ""), content_id=cid,
        firmware=f"{(fw >> 24) & 0xff:x}.{(fw >> 16) & 0xff:02x}" if isinstance(fw, int) and fw else "",
        backport=bool(re.search(r"back.?port|\bBP\b", path.name, re.I)))
    info.contents = [Content(kind, tid, info.version, info.version_str)]
    return info


# ============================================================================ Switch

class _Keys:
    def __init__(self, path: Path | None):
        self.k: dict[str, bytes] = {}
        if path and Path(path).is_file():
            for line in Path(path).read_text(errors="replace").splitlines():
                if "=" in line:
                    name, val = line.split("=", 1)
                    try:
                        self.k[name.strip().lower()] = bytes.fromhex(val.strip())
                    except ValueError:
                        pass

    def get(self, name: str) -> bytes | None:
        return self.k.get(name)


def _aes():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    return Cipher, algorithms, modes


class _Container:
    """Files inside a PFS0 (NSP/NSZ) or the secure HFS0 partition of an XCI/XCZ: name -> (offset, size)."""

    def __init__(self, path: Path):
        self.f = open(path, "rb")
        self.files: dict[str, tuple[int, int]] = {}
        head = self.f.read(0x200)
        if head[:4] == b"PFS0":
            self._fs(0, 24, b"PFS0")
        elif head[0x100:0x104] == b"HEAD":
            hoff = struct.unpack("<Q", head[0x130:0x138])[0]
            root = self._fs(hoff, 0x40, b"HFS0", keep=False)
            if "secure" not in root:
                raise PackageError("XCI has no secure partition")
            self._fs(root["secure"][0], 0x40, b"HFS0")
        else:
            raise PackageError("not an NSP/NSZ/XCI/XCZ file")

    def _fs(self, base: int, esize: int, magic: bytes, keep: bool = True) -> dict:
        self.f.seek(base)
        h = self.f.read(16)
        if h[:4] != magic:
            raise PackageError(f"damaged {magic.decode()} header")
        n, st = struct.unpack("<II", h[4:12])
        ents = self.f.read(esize * n)
        names = self.f.read(st)
        data = base + 16 + esize * n + st
        out = {}
        for i in range(n):
            off, size, no = struct.unpack("<QQI", ents[i * esize:i * esize + 20])
            name = names[no:names.index(b"\0", no)].decode("utf-8", "replace")
            out[name] = (data + off, size)
        if keep:
            self.files = out
        return out

    def read(self, name: str, start: int = 0, n: int | None = None) -> bytes:
        off, size = self.files[name]
        self.f.seek(off + start)
        return self.f.read(size - start if n is None else min(n, size - start))

    def close(self):
        self.f.close()


_CNMT_KIND = {0x80: GAME, 0x81: UPDATE, 0x82: DLC, "Application": GAME, "Patch": UPDATE, "AddOnContent": DLC}


def _base_of(tid: int, kind: str) -> int:
    if kind == UPDATE:
        return tid & ~0x800
    if kind == DLC:
        return (tid & ~0xFFF) - 0x1000
    return tid


class _Nca:
    """Just enough NCA reading for a .cnmt.nca and the control NCA's RomFS (AES-CTR sections)."""

    def __init__(self, cont: _Container, name: str, keys: _Keys):
        self.c, self.name, self.keys = cont, name, keys
        hk = keys.get("header_key")
        if not hk:
            raise PackageError("prod.keys has no header_key")
        Cipher, algorithms, modes = _aes()
        raw = cont.read(name, 0, 0xC00)
        hd = b""
        for i in range(6):
            d = Cipher(algorithms.AES(hk), modes.XTS(i.to_bytes(16, "big"))).decryptor()
            hd += d.update(raw[i * 0x200:(i + 1) * 0x200]) + d.finalize()
        if hd[0x200:0x204] not in (b"NCA3", b"NCA2"):
            raise PackageError("NCA header didn't decrypt (wrong prod.keys?)")
        self.hd = hd
        gen = max(hd[0x206], hd[0x220])
        idx = max(gen - 1, 0)
        rights = hd[0x230:0x240]
        if any(rights):
            tik = f"{rights.hex()}.tik"
            if tik not in cont.files:
                raise PackageError("title key ticket missing")
            enc = cont.read(tik, 0x180, 16)
            kek = keys.get(f"titlekek_{idx:02x}")
            if not kek:
                raise PackageError(f"prod.keys has no titlekek_{idx:02x}")
            self.key = Cipher(algorithms.AES(kek), modes.ECB()).decryptor().update(enc)
        else:
            kak = keys.get(f"key_area_key_application_{idx:02x}")
            if not kak:
                raise PackageError(f"prod.keys has no key_area_key_application_{idx:02x} (keys too old?)")
            area = Cipher(algorithms.AES(kak), modes.ECB()).decryptor().update(hd[0x300:0x340])
            self.key = area[0x20:0x30]

    def section(self, i: int) -> tuple[int, int, bytes]:
        st, en = struct.unpack("<II", self.hd[0x240 + 16 * i:0x248 + 16 * i])
        return st * 0x200, en * 0x200, self.hd[0x400 + 0x200 * i:0x600 + 0x200 * i]

    def read_section(self, i: int, rel: int, n: int) -> bytes:
        start, end, fsh = self.section(i)
        Cipher, algorithms, modes = _aes()
        a = start + rel
        aligned = a & ~0xF
        data = self.c.read(self.name, aligned, n + (a - aligned))
        if fsh[4] == 1:          # not encrypted
            return data[a - aligned:]
        iv = fsh[0x140:0x148][::-1] + (aligned >> 4).to_bytes(8, "big")
        out = Cipher(algorithms.AES(self.key), modes.CTR(iv)).decryptor().update(data)
        return out[a - aligned:]

    def pfs0_files(self, i: int = 0) -> dict[str, bytes]:
        start, end, _ = self.section(i)
        sec = self.read_section(i, 0, end - start)
        at = sec.find(b"PFS0")
        if at < 0:
            raise PackageError("metadata section didn't decrypt")
        n, st = struct.unpack("<II", sec[at + 4:at + 12])
        ents = [struct.unpack("<QQI", sec[at + 16 + 24 * k:at + 36 + 24 * k]) for k in range(n)]
        names = sec[at + 16 + 24 * n:at + 16 + 24 * n + st]
        data = at + 16 + 24 * n + st
        return {names[no:names.index(b"\0", no)].decode(): sec[data + off:data + off + size] for off, size, no in ents}

    def romfs_file(self, wanted: str, i: int = 0) -> bytes | None:
        _s, _e, fsh = self.section(i)
        if fsh[8:12] != b"IVFC":
            return None
        lvl = 0x8 + 0x10 + 5 * 0x18                   # 6th (data) level of the integrity tree
        data_off = struct.unpack("<Q", fsh[lvl:lvl + 8])[0]
        hdr = self.read_section(i, data_off, 0x50)
        vals = struct.unpack("<10Q", hdr)
        fmeta_off, fmeta_size, fdata_off = vals[7], vals[8], vals[9]
        meta = self.read_section(i, data_off + fmeta_off, fmeta_size)
        p = 0
        while p + 0x20 <= len(meta):
            _par, _sib, off, size, _h, nlen = struct.unpack("<IIQQII", meta[p:p + 0x20])
            name = meta[p + 0x20:p + 0x20 + nlen].decode("utf-8", "replace")
            if name == wanted:
                return self.read_section(i, data_off + fdata_off + off, size)
            p += 0x20 + ((nlen + 3) & ~3)
        return None


def _cnmt_records(blob: bytes) -> tuple[int, int, int, list[tuple[str, int]]]:
    """(title id, version, type, [(nca id, content type)]) from a binary .cnmt."""
    tid, ver, typ = struct.unpack("<QIB", blob[:13])
    ext_size, count = struct.unpack("<HH", blob[0x0E:0x12])
    recs = []
    p = 0x20 + ext_size
    for _ in range(count):
        r = blob[p:p + 0x38]
        recs.append((r[0x20:0x30].hex(), r[0x36]))
        p += 0x38
    return tid, ver, typ, recs


def _nacp(nacp: bytes) -> tuple[str, str]:
    title = ""
    for lang in range(16):
        name = nacp[lang * 0x300:lang * 0x300 + 0x200].split(b"\0", 1)[0].decode("utf-8", "replace").strip()
        if name and len(name) < 200 and "\ufffd" not in name and all(ch.isprintable() for ch in name):
            title = name
            break
    disp = nacp[0x3060:0x3070].split(b"\0", 1)[0].decode("utf-8", "replace").strip()
    return title, disp


def read_switch(path: Path, keys_path: Path | None) -> PackageInfo:
    keys = _Keys(keys_path)
    c = _Container(path)
    info = PackageInfo("switch", str(path))
    found: list[tuple[Content, str | None]] = []     # (content, control nca name)
    problems = []
    try:
        names = list(c.files)
        xmls = [n for n in names if n.endswith(".cnmt.xml")]
        if xmls:
            for n in xmls:
                x = c.read(n).decode("utf-8", "replace")
                typ = re.search(r"<Type>(\w+)</Type>", x)
                tid = re.search(r"<Id>0x([0-9a-fA-F]+)</Id>", x)
                ver = re.search(r"<Version>(\d+)</Version>", x)
                ctl = re.search(r"<Type>Control</Type>\s*<Id>([0-9a-f]+)</Id>", x)
                if typ and tid:
                    kind = _CNMT_KIND.get(typ.group(1), typ.group(1))
                    v = int(ver.group(1)) if ver else 0
                    found.append((Content(kind, tid.group(1).upper().zfill(16), v, f"v{v}"),
                                  ctl.group(1) if ctl else None))
        else:
            if not keys.get("header_key"):
                raise PackageError("needs your prod.keys to read - set it in ROM Verifier (Switch keys) or "
                                   "ROM Optimizer → Tasks → Options")
            for n in [n for n in names if n.endswith(".cnmt.nca")]:
                try:
                    for blob in _Nca(c, n, keys).pfs0_files().values():
                        tid, ver, typ, recs = _cnmt_records(blob)
                        kind = _CNMT_KIND.get(typ, hex(typ))
                        ctl = next((i for i, t in recs if t == 3), None)
                        found.append((Content(kind, f"{tid:016X}", ver, f"v{ver}"), ctl))
                except PackageError as exc:
                    problems.append(str(exc))
        if not found:
            # last resort: Title IDs of tickets, which are named after them
            for n in names:
                if n.endswith(".tik") and len(n) == 36:
                    tid = int(n[:16], 16)
                    kind = UPDATE if tid & 0x800 else GAME
                    found.append((Content(kind, f"{tid:016X}"), None))
            if not found:
                raise PackageError(problems[0] if problems else "no title metadata found")
        order = {GAME: 0, UPDATE: 1, DLC: 2}
        found.sort(key=lambda t: (order.get(t[0].kind, 3), t[0].title_id))
        main, main_ctl = found[0]
        info.contents = [ct for ct, _ in found]
        info.title_id = main.title_id
        info.base_id = f"{_base_of(int(main.title_id, 16), main.kind):016X}"
        info.version, info.version_str = main.version, main.version_str
        kinds = [ct.kind for ct, _ in found]
        info.kind = main.kind
        extra = []
        if UPDATE in kinds[1:]:
            extra.append("Update")
        ndlc = kinds[1:].count(DLC)
        if ndlc:
            extra.append(f"{ndlc} DLC" if main.kind != DLC else f"{ndlc + 1} DLC")
        if extra and main.kind == GAME:
            info.kind = GAME + " + " + " + ".join(extra)
        # name and display version from the control NCA (the update's, when bundled, has the newest version)
        if keys.get("header_key"):
            for ct, ctl in sorted(found, key=lambda t: order.get(t[0].kind, 3) == 1, reverse=True):
                if not ctl:
                    continue
                nca = next((f"{ctl}.nca" for _ in [0] if f"{ctl}.nca" in c.files), None)
                if not nca:
                    continue          # NSZ: compressed .ncz, can't read the name cheaply
                try:
                    blob = _Nca(c, nca, keys).romfs_file("control.nacp")
                except (PackageError, struct.error, ValueError, IndexError):
                    continue
                if blob:
                    t, disp = _nacp(blob)
                    info.title = info.title or t
                    ct.display_version = disp
                    if ct.kind == UPDATE or not info.display_version:
                        info.display_version = disp
        else:
            info.note = "no prod.keys: game names not read"
    finally:
        c.close()
    if not info.title:
        info.title = re.sub(r"\s*\[[^\]]*\]|\s*\(\+[^)]*\)", "", Path(path).stem).replace("_", " ").strip()
        info.note = info.note or "name taken from the file name"
    return info


# ============================================================================ common

def read(path: Path, keys_path: Path | None = None) -> PackageInfo:
    ext = path.suffix.lower()
    try:
        if ext in PS4_EXTS:
            return read_ps4(path)
        if ext in SWITCH_EXTS:
            return read_switch(path, keys_path)
    except (OSError, struct.error, ValueError, IndexError, KeyError) as exc:
        raise PackageError(f"couldn't read the package: {exc}") from exc
    raise PackageError("not a console package")


def switch_keys(cfg=None) -> Path | None:
    """The prod.keys ROM Optimizer uses, else one found in ~/.switch or the library."""
    from .config import get_config
    from .tasks import find_switch_keys
    cfg = cfg or get_config()
    for saved in (cfg["switch_keys"], (cfg["window_state"].get("converter_opts") or {}).get("switch_keys")):
        if saved and Path(saved).is_file():
            return Path(saved)
    root = cfg["library_root"]
    return find_switch_keys([Path(root)] if root else [])


# ---------------------------------------------------------------- names

_TM = re.compile(r"[™®©]")


def clean_title(t: str) -> str:
    """A file-name-safe title: no ™/®, full-width or plain colons become " - ", Samba-unsafe characters dropped,
    ALL-CAPS titles get normal capitals (roman numerals and short words like "HD" stay)."""
    t = _TM.sub("", t or "").replace("：", ":").replace("’", "'")
    t = re.sub(r"\s*:\s*", " - ", t)
    t = re.sub(r'[\\/*?"<>|]', "", t)
    letters = [ch for ch in t if ch.isalpha()]
    if letters and all(ch.isupper() for ch in letters) and len(letters) > 4:
        def fix(w: str) -> str:
            if re.fullmatch(r"[IVXLC]+|HD|3D|DX|EX|VR|PS\d|[A-Z]\d*", w):
                return w
            return w.capitalize()
        t = " ".join(fix(w) for w in t.split(" "))
    return re.sub(r"\s{2,}", " ", t).strip(" -")


def _dlc_name(dlc_title: str, base_title: str) -> str:
    n = clean_title(dlc_title)
    if base_title:
        b = re.escape(clean_title(base_title))
        n = re.sub(rf"\s*\(?\s*{b}\s*\)?\s*", " ", n, flags=re.I).strip(" -")
    return n or "DLC"


def suggested_name(info: PackageInfo, base_title: str = "", base_sfo_title: str = "") -> str:
    """File name (without extension) in the library's style:
    PS4:    "Title [CUSA00000]", "Title [CUSA00000] (Update v1.09)", "Title [CUSA00000] (DLC - Name)"
    Switch: "Title [0100…000][v0]", bundles add "(+Update v393216)" / "(+2 DLC)" like the rest of the library."""
    title = clean_title(base_title or info.title) or "Unknown"
    if info.platform == "ps4":
        if info.kind == UPDATE:
            return f"{title} [{info.title_id}] (Update v{info.version_str})"
        if info.kind == DLC:
            name = _dlc_name(info.title, base_sfo_title or base_title)
            if base_sfo_title and name == clean_title(info.title):
                name = _dlc_name(info.title, base_title)
            return f"{title} [{info.title_id}] (DLC - {name})" if base_title else \
                f"{clean_title(info.title)} [{info.title_id}] (DLC)"
        return f"{title} [{info.title_id}]"
    s = f"{title} [{info.title_id}][v{info.version}]"
    if info.kind == DLC:
        s = (f"{title} [{info.title_id}][v{info.version}] (DLC)" if base_title else
             f"{clean_title(info.title)} [{info.title_id}][v{info.version}] (DLC)")
    if info.display_version and info.kind == UPDATE:
        s += f"[{info.display_version}]"
    upd = next((c for c in info.contents[1:] if c.kind == UPDATE), None)
    if upd:
        s += f" (+Update v{upd.version})"
    ndlc = sum(1 for c in info.contents[1:] if c.kind == DLC)
    if ndlc:
        s += f" (+{ndlc} DLC)"
    return s


# ---------------------------------------------------------------- library checks

def check_set(infos: list[PackageInfo], complete: bool = True) -> dict[str, list[str]]:
    """Problems found by looking at all packages together: path -> list of messages.
    ``complete`` = the whole folder was looked at (otherwise "no base game" isn't reported)."""
    out: dict[str, list[str]] = {}

    def add(i: PackageInfo, msg: str):
        out.setdefault(i.path, []).append(msg)

    def norm(t: str) -> str:
        return re.sub(r"[^a-z0-9]", "", clean_title(t).lower())

    # what each base game ID has: games (incl. bundled), updates, DLC
    games: dict[str, list[PackageInfo]] = {}
    for i in infos:
        for c in i.contents or [Content(i.kind, i.title_id, i.version, i.version_str)]:
            if c.kind == GAME:
                games.setdefault(_group_id(i.platform, c.title_id, GAME), []).append(i)
    games_by_title: dict[str, list[PackageInfo]] = {}
    for gl in games.values():
        for g in gl:
            games_by_title.setdefault(norm(g.title), []).append(g)

    for i in infos:
        if i.kind in (UPDATE, DLC) and complete and i.base_id not in games:
            same = [g for g in games_by_title.get(norm(i.title), []) if g.base_id != i.base_id]
            if i.platform == "ps4" and same:
                g = same[0]
                add(i, f"for {i.title_id} ({i.region or '?'}), but your game is {g.title_id} ({g.region or '?'}) - "
                       f"it won't apply to that version")
            elif i.platform == "ps4" and i.kind == DLC:
                hit = next((gl[0] for gl in games.values()
                            for g in gl if norm(g.title) and norm(g.title) in norm(i.title)), None)
                if hit and hit.title_id != i.title_id:
                    add(i, f"DLC for {i.title_id} ({i.region or '?'}), but your game is {hit.title_id} "
                           f"({hit.region or '?'}) - it won't unlock on that version")
                else:
                    add(i, f"no base game ({i.base_id}) found next to this {i.kind.lower()}")
            else:
                add(i, f"no base game ({i.base_id}) found next to this {i.kind.lower()}")

    # duplicates: same content, same version, same kind
    seen: dict[tuple, PackageInfo] = {}
    for i in sorted(infos, key=lambda x: x.path):
        key = (i.platform, i.content_id or i.title_id, i.kind, i.version, len(i.contents))
        if key in seen:
            add(i, f"duplicate of {Path(seen[key].path).name}")
        else:
            seen[key] = i

    # older updates when a newer one is present
    newest: dict[str, PackageInfo] = {}
    for i in infos:
        for c in i.contents:
            if c.kind == UPDATE:
                b = i.base_id
                if b not in newest or c.version > _upd_version(newest[b]):
                    newest[b] = i
    for i in infos:
        if i.kind == UPDATE and i.base_id in newest and newest[i.base_id] is not i \
                and _upd_version(newest[i.base_id]) > i.version:
            add(i, f"older update - {Path(newest[i.base_id].path).name} is newer")
    return out


def _upd_version(i: PackageInfo) -> int:
    return max((c.version for c in i.contents if c.kind == UPDATE), default=0)


def _group_id(platform: str, title_id: str, kind: str) -> str:
    if platform == "switch":
        return f"{_base_of(int(title_id, 16), kind):016X}"
    return title_id


def describe(info: PackageInfo) -> str:
    """One-line summary for the Verifier's Notes column."""
    bits = [info.kind]
    if info.kind.startswith(GAME) and info.platform == "switch":
        bits = [info.kind]
    if info.version_str and not (info.platform == "switch" and info.kind.startswith(GAME) and info.version == 0):
        bits.append(f"v{info.version_str.lstrip('v')}" if info.platform == "ps4" else info.version_str)
    if info.display_version:
        bits.append(f"({info.display_version})")
    bits.append(info.title_id)
    if info.region:
        bits.append(info.region)
    if info.firmware:
        bits.append(f"needs FW {info.firmware}")
    if info.backport:
        bits.append("backport")
    if info.note:
        bits.append(info.note)
    return " · ".join(bits)
