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
    try:
        return _read_ps4(path)
    except (struct.error, IndexError, UnicodeDecodeError) as exc:
        raise PackageError("damaged or incomplete package (its header is cut off)") from exc


def _read_ps4(path: Path) -> PackageInfo:
    from .ps4pkg import pieces
    ps = pieces(path)
    if ps and path != ps[0]:
        raise PackageError(f"part {ps.index(path)} of a split package - see {ps[0].name}")
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
    if ps:
        info.note = f"split into {len(ps)} files"
    return info


# ============================================================================ Switch

class _Keys:
    def __init__(self, path: Path | None, title_path: Path | None | bool = True):
        self.k: dict[str, bytes] = {}
        self.titles: dict[str, bytes] = {}      # rights ID -> title key (as stored in tickets, titlekek-encrypted)
        self._load(path, self.k)
        if title_path is True:                  # default: the title.keys set in the settings, if any
            try:
                title_path = title_keys()
            except Exception:  # noqa: BLE001
                title_path = None
        self._load(title_path or None, self.titles)

    @staticmethod
    def _load(path, into: dict) -> None:
        if not path or not Path(path).is_file():
            return
        for line in Path(path).read_text(errors="replace").splitlines():
            sep = "=" if "=" in line else "," if "," in line else None
            if sep:
                name, val = line.split(sep, 1)
                try:
                    into[name.strip().lower()] = bytes.fromhex(val.strip())
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
            if tik in cont.files:
                enc = cont.read(tik, 0x180, 16)
            elif rights.hex() in keys.titles:
                enc = keys.titles[rights.hex()]
            else:
                raise PackageError(f"needs the title key for rights ID {rights.hex()} (no ticket in the package) - "
                                   f"set your title.keys")
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
        return self._read_raw_section(fsh, start + rel, n)

    def _read_raw_section(self, fsh: bytes, a: int, n: int) -> bytes:
        Cipher, algorithms, modes = _aes()
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
                                   "ROM Optimizer → Convert & Extract → Options")
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
                nca = next((f"{ctl}{ext}" for ext in (".nca", ".ncz") if f"{ctl}{ext}" in c.files), None)
                if not nca:
                    continue
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


@dataclass
class TitleLayout:
    """Where one title's content lives inside a Switch package (for extraction)."""
    kind: str                      # Game | Update | DLC
    title_id: str
    version: int
    ncas: dict[int, list[str]]     # content type (1 Program, 2 Data, 3 Control, ...) -> NCA file names
    tickets: list[str]             # .tik files in the container (title-key encrypted content)


def switch_layout(path: Path, keys_path: Path | None) -> list[TitleLayout]:
    """The titles in an NSP / XCI with the file names of their NCAs, from the content metadata."""
    keys = _Keys(keys_path)
    c = _Container(path)
    try:
        names = set(c.files)
        tickets = sorted(n for n in names if n.endswith(".tik"))
        out: list[TitleLayout] = []
        xmls = [n for n in names if n.endswith(".cnmt.xml")]
        if xmls:
            types = {"Program": 1, "Data": 2, "Control": 3, "HtmlDocument": 4, "LegalInformation": 5,
                     "DeltaFragment": 6}
            for n in xmls:
                x = c.read(n).decode("utf-8", "replace")
                typ = re.search(r"<Type>(\w+)</Type>", x)
                tid = re.search(r"<Id>0x([0-9a-fA-F]+)</Id>", x)
                ver = re.search(r"<Version>(\d+)</Version>", x)
                ncas: dict[int, list[str]] = {}
                for t, i in re.findall(r"<Content>\s*<Type>(\w+)</Type>\s*<Id>([0-9a-f]+)</Id>", x):
                    ncas.setdefault(types.get(t, 0), []).append(f"{i}.nca")
                out.append(TitleLayout(_CNMT_KIND.get(typ.group(1), typ.group(1)) if typ else "",
                                       tid.group(1).upper().zfill(16) if tid else "", int(ver.group(1)) if ver else 0,
                                       ncas, tickets))
        else:
            if not keys.get("header_key"):
                raise PackageError("needs your prod.keys")
            for n in sorted(x for x in names if x.endswith(".cnmt.nca")):
                for blob in _Nca(c, n, keys).pfs0_files().values():
                    tid, ver, typ, recs = _cnmt_records(blob)
                    ncas: dict[int, list[str]] = {}
                    for i, t in recs:
                        ncas.setdefault(t, []).append(f"{i}.nca")
                    out.append(TitleLayout(_CNMT_KIND.get(typ, hex(typ)), f"{tid:016X}", ver, ncas, tickets))
        for t in out:      # NSZ: the content is stored as .ncz
            for typ, files in t.ncas.items():
                t.ncas[typ] = [f if f in names else f[:-4] + ".ncz" if f[:-4] + ".ncz" in names else f
                               for f in files]
        return out
    finally:
        c.close()


def nca_rights_id(container_path: Path, nca: str, keys_path: Path | None) -> str:
    """The NCA's rights ID (hex) if its content is title-key encrypted, else ""."""
    keys = _Keys(keys_path, title_path=None)
    hk = keys.get("header_key")
    if not hk:
        raise PackageError("needs your prod.keys")
    Cipher, algorithms, modes = _aes()
    c = _Container(container_path)
    try:
        raw = c.read(nca, 0, 0x400)
    finally:
        c.close()
    hd = b""
    for i in range(2):
        d = Cipher(algorithms.AES(hk), modes.XTS(i.to_bytes(16, "big"))).decryptor()
        hd += d.update(raw[i * 0x200:(i + 1) * 0x200]) + d.finalize()
    r = hd[0x230:0x240]
    return r.hex() if any(r) else ""


def title_key_known(rights_id: str, tickets_in_package: list[str]) -> bool:
    if not rights_id or f"{rights_id}.tik" in tickets_in_package:
        return True
    return rights_id in _Keys(None).titles


def ticket_count(path: Path) -> int:
    """How many title-key tickets an NSP / XCI carries (bundles of base + update + DLC usually have several)."""
    try:
        c = _Container(path)
    except (OSError, PackageError, struct.error, ValueError):
        return 0
    try:
        return sum(1 for n in c.files if n.endswith(".tik"))
    finally:
        c.close()


NSZ_MULTI_TICKET = ("holds several titles with their own tickets (e.g. a game bundled with its update); nsz 5.0.0 "
                    "can't compress those - a known nsz bug, fixed upstream but not in a release yet")


def nca_part_sizes(container_path: Path, nca: str, keys_path: Path | None) -> dict[int, int]:
    """Bytes each section of an NCA holds once extracted: for a RomFS the data size its integrity header records
    (for an update NCA that is the size of the whole PATCHED RomFS, not of the small update file), for a partition
    FS (ExeFS, logo) its stored size."""
    c = _Container(container_path)
    try:
        n = _Nca(c, nca, _Keys(keys_path))
        out = {}
        for i in range(4):
            start, end, fsh = n.section(i)
            if end <= start:
                continue
            if fsh[2] == 0 and fsh[8:12] == b"IVFC":
                lvl = 0x8 + 0x10 + 5 * 0x18
                out[i] = struct.unpack("<Q", fsh[lvl + 8:lvl + 16])[0]
            else:
                out[i] = end - start
        return out
    finally:
        c.close()


def nca_sections(container_path: Path, nca: str, keys_path: Path | None) -> list[tuple[int, int, int]]:
    """(index, fs type 0=RomFS 1=PartitionFS, encryption type) for each section of an NCA inside a package."""
    keys = _Keys(keys_path)
    c = _Container(container_path)
    try:
        n = _Nca(c, nca, keys)
        out = []
        for i in range(4):
            start, end, fsh = n.section(i)
            if end > start:
                out.append((i, fsh[2], fsh[4]))
        return out
    finally:
        c.close()


def copy_member(container_path: Path, name: str, dest: Path, progress=None, cancel=None) -> None:
    """Copy one file (an NCA or ticket) out of an NSP / XCI."""
    c = _Container(container_path)
    try:
        off, size = c.files[name]
        c.f.seek(off)
        done = 0
        with open(dest, "wb") as out:
            while done < size:
                if cancel is not None and cancel.is_set():
                    raise PackageError("cancelled")
                chunk = c.f.read(min(8 << 20, size - done))
                if not chunk:
                    raise PackageError("package is shorter than its header says")
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, size)
    finally:
        c.close()


# ============================================================================ common

def is_extracted_switch(d: Path) -> bool:
    """A Switch game already extracted to folders (exefs/ with main.npdm, usually romfs/ next to it)."""
    try:
        return (d / "exefs" / "main.npdm").is_file()
    except OSError:
        return False


def iter_packages(scope: Path, max_depth: int = 4, skip: set[str] | None = None):
    """Package files (PS4 PKG, Switch NSP/XCI/…) and extracted Switch game folders under ``scope``, without
    walking into the extracted games' thousands of romfs files. ``skip``: top-level folder names to leave out."""
    import os
    scope = Path(scope)
    for dirpath, dirnames, filenames in os.walk(scope):
        here = Path(dirpath)
        depth = len(here.relative_to(scope).parts)
        keep = []
        for dn in sorted(dirnames):
            if depth == 0 and skip and dn in skip:
                continue
            sub = here / dn
            if is_extracted_switch(sub):
                yield sub
            elif depth + 1 < max_depth and not dn.startswith("."):
                keep.append(dn)
        dirnames[:] = keep
        for fn in sorted(filenames):
            if Path(fn).suffix.lower() in PACKAGE_EXTS and not fn.startswith("."):
                yield here / fn


def read_switch_dir(d: Path) -> PackageInfo:
    """An extracted Switch game: the Title ID from exefs/main.npdm (its ACI0 block), the name and version from a
    control.nacp if the dump kept one, else the name from the folder."""
    npdm = (d / "exefs" / "main.npdm").read_bytes()
    if npdm[:4] != b"META":
        raise PackageError("exefs/main.npdm isn't a valid NPDM")
    off, size = struct.unpack("<II", npdm[0x70:0x78])
    if npdm[off:off + 4] != b"ACI0":
        raise PackageError("exefs/main.npdm has no ACI0 block")
    tid = f"{struct.unpack('<Q', npdm[off + 0x10:off + 0x18])[0]:016X}"
    info = PackageInfo("switch", str(d), title_id=tid, base_id=tid, kind=GAME + " (extracted)")
    nacp = next((p for p in (d / "control.nacp", d / "control" / "control.nacp") if p.is_file()), None)
    if nacp:
        info.title, info.display_version = _nacp(nacp.read_bytes())
    if not info.title:
        info.title = re.sub(r"\s*\[[^\]]*\]|\s*\(v\d+\)", "", d.name).strip()
        info.note = "name taken from the folder name"
    parts = [p for p in ("exefs", "romfs") if (d / p).is_dir()]
    if "romfs" not in parts:
        info.note = (info.note + "; " if info.note else "") + "no romfs/ folder"
    m = re.search(r"\(v(\d+)\)", d.name)          # our extractor names updated games "Title [ID] (v65536)"
    if m:
        info.version, info.version_str = int(m.group(1)), f"v{m.group(1)}"
    info.contents = [Content(GAME, tid, info.version, info.version_str, info.display_version)]
    return info


def read(path: Path, keys_path: Path | None = None) -> PackageInfo:
    ext = path.suffix.lower()
    if path.is_dir():
        try:
            return read_switch_dir(path)
        except (OSError, struct.error, ValueError) as exc:
            raise PackageError(f"couldn't read the extracted game: {exc}") from exc
    try:
        if ext in PS4_EXTS:
            return read_ps4(path)
        if ext in SWITCH_EXTS:
            return read_switch(path, keys_path)
    except (OSError, struct.error, ValueError, IndexError, KeyError) as exc:
        raise PackageError(f"couldn't read the package: {exc}") from exc
    raise PackageError("not a console package")


def title_keys(cfg=None) -> Path | None:
    """Your title.keys (title keys for content whose package has no ticket), if set."""
    from .config import get_config
    p = (cfg or get_config())["title_keys"]
    return Path(p) if p and Path(p).is_file() else None


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
        if info.kind == DLC and not base_title:
            # no game next to it, but PS4 DLC titles usually name it: "Dark Angel (GRAVITY RUSH 2)"
            m = re.match(r"(.+?)\s*\(([^()]{3,})\)\s*$", info.title or "")
            if m:
                return f"{clean_title(m.group(2))} [{info.title_id}] (DLC - {clean_title(m.group(1))})"
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
