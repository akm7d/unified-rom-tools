import struct

from romtools import packages as pk


def test_ps4_metadata_and_naming(tmp_path, make_pkg):
    f = make_pkg(tmp_path / "x.pkg", CATEGORY="gp", TITLE="GAME™ ONE", TITLE_ID="CUSA11111",
                 CONTENT_ID="UP0001-CUSA11111_00-TEST000000000000", APP_VER="01.05", VERSION="01.00",
                 SYSTEM_VER=0x09000000)
    i = pk.read(f)
    assert (i.kind, i.title_id, i.version_str, i.region, i.firmware) == ("Update", "CUSA11111", "1.05", "USA", "9.00")
    assert pk.suggested_name(i, "Game One") == "Game One [CUSA11111] (Update v1.05)"
    assert pk.clean_title("GRAVITY RUSH™ 2") == "Gravity Rush 2"
    assert pk.clean_title("13 Sentinels： Aegis Rim") == "13 Sentinels - Aegis Rim"


def test_folder_checks(tmp_path, make_pkg):
    def p(name, **kw):
        return pk.read(make_pkg(tmp_path / name, **kw))
    game = p("g.pkg", CATEGORY="gd", TITLE="Game Two", TITLE_ID="CUSA22222",
             CONTENT_ID="EP0002-CUSA22222_00-G000000000000000", APP_VER="01.00")
    old = p("u1.pkg", CATEGORY="gp", TITLE="Game Two", TITLE_ID="CUSA22222",
            CONTENT_ID="EP0002-CUSA22222_00-G000000000000000", APP_VER="01.01")
    new = p("u2.pkg", CATEGORY="gp", TITLE="Game Two", TITLE_ID="CUSA22222",
            CONTENT_ID="EP0002-CUSA22222_00-G000000000000000", APP_VER="01.05")
    dlc_us = p("d.pkg", CATEGORY="ac", TITLE="Game Two Expansion", TITLE_ID="CUSA33333",
               CONTENT_ID="UP0002-CUSA33333_00-EXP0000000000000", VERSION="01.00")
    orphan = p("o.pkg", CATEGORY="gp", TITLE="Lonely", TITLE_ID="CUSA44444",
               CONTENT_ID="EP0004-CUSA44444_00-L000000000000000", APP_VER="01.10")
    dup = p("g2.pkg", CATEGORY="gd", TITLE="Game Two", TITLE_ID="CUSA22222",
            CONTENT_ID="EP0002-CUSA22222_00-G000000000000000", APP_VER="01.00")
    probs = pk.check_set([game, old, new, dlc_us, orphan, dup])
    assert game.path not in probs and new.path not in probs
    assert "older update" in probs[old.path][0]
    assert "won't unlock" in probs[dlc_us.path][0]
    assert "no base game" in probs[orphan.path][0]
    assert "duplicate" in probs[dup.path][0]


def test_extracted_switch_folder(tmp_path):
    d = tmp_path / "Some Game [0100AAAA00000000]"
    (d / "exefs").mkdir(parents=True)
    (d / "romfs").mkdir()
    npdm = bytearray(0x600)
    npdm[:4] = b"META"
    npdm[0x70:0x78] = struct.pack("<II", 0x400, 0x100)
    npdm[0x400:0x404] = b"ACI0"
    npdm[0x410:0x418] = struct.pack("<Q", 0x0100AAAA00000000)
    (d / "exefs" / "main.npdm").write_bytes(bytes(npdm))
    assert pk.is_extracted_switch(d)
    i = pk.read(d)
    assert (i.title_id, i.title, i.kind) == ("0100AAAA00000000", "Some Game", "Game (extracted)")
    assert list(pk.iter_packages(tmp_path)) == [d]


def test_ticket_count_and_nsz_skip(tmp_path):
    from romtools import converter as cv

    def nsp(path, names):
        table, entries = b"", b""
        for n in names:
            entries += struct.pack("<QQII", 0, 0, len(table), 0)
            table += n.encode() + b"\0"
        path.write_bytes(b"PFS0" + struct.pack("<II", len(names), len(table)) + b"\0" * 4 + entries + table)
        return path
    one = nsp(tmp_path / "one.nsp", ["a.nca", "0100000000010000000000000000000a.tik"])
    two = nsp(tmp_path / "two.nsp", ["a.nca", "0100000000010000000000000000000a.tik",
                                    "0100000000010800000000000000000b.tik"])
    assert (pk.ticket_count(one), pk.ticket_count(two)) == (1, 2)
    keys = tmp_path / "prod.keys"
    keys.write_text("header_key = " + "00" * 32 + "\n")
    job = cv.plan(cv.TO_NSZ, [two], cv.Options(switch_keys=str(keys)))[0]
    assert job.status == "Skipped" and "several titles" in job.message


def nsp(path, files):
    import struct as st
    names = b"".join(n.encode() + b"\0" for n in files)
    ents, off, no = b"", 0, 0
    for n, d in files.items():
        ents += st.pack("<QQII", off, len(d), no, 0)
        off += len(d)
        no += len(n) + 1
    path.write_bytes(b"PFS0" + st.pack("<III", len(files), len(names), 0) + ents + names + b"".join(files.values()))


def test_switch_integrity_by_content_hash(tmp_path):
    import hashlib
    import os as o
    from romtools import integrity
    a, b = o.urandom(50000), o.urandom(70000)
    files = {hashlib.sha256(a).hexdigest()[:32] + ".nca": a, hashlib.sha256(b).hexdigest()[:32] + ".cnmt.nca": b, "x.tik": b"t"}
    p = tmp_path / "g.nsp"
    nsp(p, files)
    r = integrity.check(p)
    assert r.state == "intact" and "2 content files" in r.note
    data = bytearray(p.read_bytes())
    data[-30000] ^= 1
    p.write_bytes(data)
    assert integrity.check(p).state == "damaged"
    nsp(p, files)
    p.write_bytes(p.read_bytes()[:-60000])
    assert "incomplete" in integrity.check(p).note
    nsp(p, {"0" * 32 + ".ncz": b"packed"})
    assert integrity.check(p).state == "unknown"            # no keys: says so instead of guessing
    p.write_bytes(b"junk" * 100)
    assert integrity.check(p).state == "damaged"


def test_converted_game_card_dump_is_told_apart_from_damage(tmp_path):
    """Converters flip one encrypted header byte: every hash changes, but nothing is damaged."""
    import hashlib
    import os as o
    from romtools import integrity
    from romtools import packages as pk
    hk = o.urandom(32)
    (tmp_path / "prod.keys").write_text("header_key = " + hk.hex())
    Cipher, algorithms, modes = pk._aes()

    def nca(dist, body):
        hd = bytearray(0x200)
        hd[:4], hd[4] = b"NCA3", dist
        e = Cipher(algorithms.AES(hk), modes.XTS((1).to_bytes(16, "big"))).encryptor()
        return bytes(0x200) + e.update(bytes(hd)) + e.finalize() + body
    bodies = [o.urandom(30000), o.urandom(50000)]
    names = [hashlib.sha256(nca(1, b)).hexdigest()[:32] + ".nca" for b in bodies]       # named when they were on the card
    p = tmp_path / "g.nsp"
    nsp(p, {n: nca(0, b) for n, b in zip(names, bodies)})
    r = integrity.check(p, tmp_path / "prod.keys")
    assert r.state == "intact" and "game-card" in r.note
    assert integrity.check(p).state == "unknown"                                        # without keys: can't tell
    nsp(p, {names[0]: nca(0, bodies[0]), names[1]: nca(0, bodies[1][:-1] + b"?")})
    assert integrity.check(p, tmp_path / "prod.keys").state != "intact"


def test_content_rewritten_by_a_converter_is_checked_from_inside(tmp_path):
    """Removing a title key or decrypting changes a content file's hash; its own hash trees still tell."""
    import hashlib
    import os as o
    import struct as st
    from romtools import integrity
    from romtools import packages as pk
    hk = o.urandom(32)
    (tmp_path / "prod.keys").write_text("header_key = " + hk.hex() + "\nkey_area_key_application_00 = " + o.urandom(16).hex())
    Cipher, algorithms, modes = pk._aes()
    sha = lambda b: hashlib.sha256(b).digest()            # noqa: E731
    data, bs = o.urandom(70000), 0x1000                   # section 0: one hash table (like ExeFS)
    table = b"".join(sha(data[i:i + bs]) for i in range(0, len(data), bs))
    sec0 = table.ljust(0x1000, b"\0") + data
    sec0 = sec0.ljust((len(sec0) + 0x1FF) & ~0x1FF, b"\0")
    fs0 = bytearray(0x200)
    fs0[2], fs0[3], fs0[4] = 1, 2, 1
    fs0[8:0x28] = sha(table)
    st.pack_into("<IIQQQQ", fs0, 0x28, bs, 2, 0, len(table), 0x1000, len(data))
    body, levels, hashes = [], [], o.urandom(40000)       # section 1: six levels (like RomFS)
    raw = hashes
    chain = [raw]
    for _ in range(5):
        t = b"".join(sha(chain[0][i:i + 0x4000].ljust(0x4000, b"\0")) for i in range(0, len(chain[0]), 0x4000))
        chain.insert(0, t)
    pos = 0
    for lv in chain:
        levels.append((pos, len(lv)))
        body.append(lv.ljust((len(lv) + 0x3FFF) & ~0x3FFF, b"\0"))
        pos += len(body[-1])
    sec1 = b"".join(body)
    fs1 = bytearray(0x200)
    fs1[2], fs1[3], fs1[4], fs1[8:12] = 0, 3, 1, b"IVFC"
    for n, (off, sz) in enumerate(levels):
        st.pack_into("<QQI", fs1, 0x18 + 0x18 * n, off, sz, 14)
    fs1[0xC8:0xE8] = sha(chain[0].ljust(0x4000, b"\0"))

    def nca(update=False):
        f1 = bytearray(fs1)
        if update:
            f1[0x100] = 1
        hd = bytearray(0xC00)
        a, b = 0xC00, 0xC00 + len(sec0)
        hd[0x200:0x204], hd[0x204] = b"NCA3", 0
        st.pack_into("<Q", hd, 0x208, b + len(sec1))
        st.pack_into("<II", hd, 0x240, a // 0x200, b // 0x200)
        st.pack_into("<II", hd, 0x250, b // 0x200, (b + len(sec1)) // 0x200)
        hd[0x280:0x2A0], hd[0x2A0:0x2C0] = sha(bytes(fs0)), sha(bytes(f1))
        hd[0x400:0x600], hd[0x600:0x800] = fs0, f1
        out = b""
        for i in range(6):
            e = Cipher(algorithms.AES(hk), modes.XTS(i.to_bytes(16, "big"))).encryptor()
            out += e.update(bytes(hd[i * 0x200:(i + 1) * 0x200])) + e.finalize()
        return out + sec0 + sec1
    p, name = tmp_path / "g.nsp", "ab" * 16 + ".nca"      # named after what it was before it was rewritten
    keys = tmp_path / "prod.keys"
    nsp(p, {name: nca()})
    r = integrity.check(p, keys)
    assert r.state == "intact" and "rewritten" in r.note, r
    good = nca()
    for at in (0xC00 + 10, 0xC00 + 0x1000 + 69000, 0xC00 + len(sec0) + 5, len(good) - 0x4000 * 2 - 100, 0x405):
        d = bytearray(good)
        d[at] ^= 1
        nsp(p, {name: bytes(d)})
        assert integrity.check(p, keys).state == "damaged", at
    nsp(p, {name: nca(update=True)})                      # an update's patch data needs the game
    assert integrity.check(p, keys).state == "unknown"
