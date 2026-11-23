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
