#!/usr/bin/env python3
"""Compositional per-frame record layout. The record is the enabled 'shooting
data items to be recorded' concatenated in a fixed canonical order (never
reordered); the film's 8-byte items mask (hd[9:17]) says which are present, and
EVERY set bit is exactly one recorded byte (bits==bytes). frame_layout() walks the
mask MSB-first, bytes hd9..hd16 in record order, and computes each field's offset
by counting set bits before it -- so mapped fields decode correctly even when
unidentified items sit among them.

Verified byte-exact vs ES-E1 on baseline (473 frames), all-off 26-356, the
mixed-prefix roll 26-357 (which also split date/time), and 26-358 (which carries
11 unidentified item-bytes yet still decodes every known field).

Run:  python tests/test_layout.py
"""
import sys, pathlib, io, csv, tempfile, os
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import eos1v_tool as tool


def _shot_bat(mask):
    layout, _t, _u = tool.frame_layout(bytes.fromhex(mask))
    return layout.get('shotdate'), layout.get('batdate')


def test_offsets_for_known_configs():
    cases = [
        ("ffff003f0008003f", 17, 24),   # baseline (77 rolls, 0 mismatches)
        ("ffff003f0000003f", 17, 23),   # focus item off -> battery shifts left 1
        ("ffff0c3f0000003f", 19, 25),   # +bulb (2B), focus off
        ("ffff0c3f0008003f", 19, 26),   # +bulb, focus on (26-353/354/355)
    ]
    for m, so, bo in cases:
        assert _shot_bat(m) == (so, bo), f"{m}: got {_shot_bat(m)} want {(so,bo)}"
        assert tool.frame_layout(bytes.fromhex(m))[2] is False, f"{m}: unexpected unknown item"
    print("frame_layout: shot/battery offsets correct for all four anchor masks: OK")


def test_all_masks_fully_explained():
    expect = {
        "c009000000000000": {'mode', 'pad16'},                       # all items off
        "ffff003f0008003f": {'focal','maxap','Tv','Av','ISO','expcomp','flashcomp',
                             'flashmode','metering','mode','drive','afmode','pad16',
                             'shotdate','shottime','focus','batdate','battime'},  # baseline
    }
    for m, present in expect.items():
        layout, _total, unknown = tool.frame_layout(bytes.fromhex(m))
        assert unknown is False, f"{m}: unexpected unidentified item"
        assert set(layout) == present, f"{m}: present {set(layout)} != {present}"
    lo, _t, u = tool.frame_layout(bytes.fromhex("ffff0c3f0008003f"))   # all-on = baseline+bulb
    assert u is False and 'bulb' in lo
    print("frame_layout: baseline/all-off/all-on fully explained, fields match oracle: OK")


def test_date_and_time_are_separate_items():
    # 26-357: hd[12]=0x07 records TIME (bits 2,1,0) but not DATE (bit 5).
    mask = bytes.fromhex("cd6d00070008003f")
    layout, _t, unknown = tool.frame_layout(mask)
    assert unknown is False
    assert 'shottime' in layout and 'shotdate' not in layout
    assert 'batdate' in layout and 'battime' in layout
    fr = bytes.fromhex("018101" "0b2b58" "0002" "10" "08" "00"
                       "234942" "4a" "260616" "015736" "ffffff")
    row = tool.decode_frame(1, fr, film_dx=0x58, mask=mask)
    assert row['Date'] == '' and row['Time'] == '23:49:42', (row['Date'], row['Time'])
    assert row['Battery date'] == '2026-06-16' and row['Battery time'] == '01:57:36'
    assert row['Max aperture'] == '2.5' and row['Shooting mode'] == 'Program AE'
    assert not row['_untrusted'] and not row['_unknown']
    print("shot date/time are separate items; 26-357 mixed prefix decodes exactly: OK")


def test_cfn_11byte_field_spans_bytes():
    # 26-358: "Custom Function settings" is an 11-byte item spanning hd13 bit6 ->
    # hd14 bit4. Identified (not unknown); its raw bytes aren't decoded to named
    # C.Fn (no ground truth), but its presence must not disturb the other fields.
    mask = bytes.fromhex("f29b00387ff00000")
    layout, total, unknown = tool.frame_layout(mask)
    assert unknown is False, "cfn is now a mapped item"
    assert layout.get('cfn') == 14 and total == 25, (layout.get('cfn'), total)
    fr = bytes.fromhex("018101" "0032" "0b" "00" "40" "10" "42" "00"
                       "260704" "2111111111111121420102" "ff")
    row = tool.decode_frame(1, fr, film_dx=0xf0, mask=mask)
    assert row['_unknown'] is False and row['_untrusted'] is False
    assert row['Focal length'] == '50mm' and row['Av'] == '2.5'
    assert row['Metering mode'] == 'Partial' and row['Shooting mode'] == 'Program AE'
    assert row['AF mode'] == 'One-Shot AF' and row['Date'] == '2026-07-04'
    print("Custom Function settings is an identified 11-byte field; 26-358 decodes exactly: OK")


def test_still_unmapped_item_decodes_known_fields():
    # There remain dialog items we've never toggled (e.g. "Focusing point achieving
    # focus", a sub-item of AF mode). Simulate one via hd13 bit7 (not part of cfn).
    # It adds a byte our table can't name -> known fields (which precede it) still
    # decode; the unknown item is flagged and omitted, never guessed.
    mask = bytearray(bytes.fromhex("c009000000000000"))   # all-off + shooting mode
    mask[4] |= 0x80                                        # hd13 bit7: an unmapped item
    layout, total, unknown = tool.frame_layout(bytes(mask))
    assert unknown is True and total == 6                  # 3 hdr + mode + pad16 + 1 unknown
    fr = bytes.fromhex("018101" "04" "00" "5a" "ffffff")   # mode, pad, 1 unknown byte
    row = tool.decode_frame(1, fr, film_dx=0x58, mask=bytes(mask))
    assert row['_unknown'] is True and row['_untrusted'] is False
    assert row['Shooting mode'] == 'Bulb'                  # known field still decodes
    print("a still-unmapped item is flagged + omitted, known fields decode: OK")


def test_focus_point_fields_printed_raw():
    # 26-360: "Focusing point achieving focus" (hd14 b3, 1B) and "Focusing point
    # selection" (hd15 b6, 7B) both enabled. Both are mapped (no unknown), printed
    # raw hex, not interpreted.
    mask = bytes.fromhex("c00b000000087f00")
    layout, total, unknown = tool.frame_layout(mask)
    assert unknown is False, "selection is now mapped"
    assert layout.get('focus') == 6 and layout.get('selection') == 7 and total == 14
    fr = bytes.fromhex("018101" "10" "42" "00" "4a" "00000004004a03" "ffffff")
    row = tool.decode_frame(1, fr, film_dx=0xf0, mask=mask)
    assert row['AF point achieving focus'] == '4a'
    assert row['AF point selection'] == '00000004004a03'
    assert row['AF mode'] == 'One-Shot AF' and row['_unknown'] is False
    print("focus achieving (1B) + selection (7B) mapped and printed raw: OK")


def test_afmode_high_bit_masked():
    # 26-359 frame 7: AF-mode byte 0xc2 -> One-Shot AF (0x40/0x80 are flags ES-E1
    # ignores). Previously decoded '?(0x82)'.
    mask = tool.FRAME_MASK_BASELINE
    af_off = tool.frame_layout(mask)[0]['afmode']
    for raw_af, want in ((0x42, 'One-Shot AF'), (0xc2, 'One-Shot AF'), (0x12, 'Manual focus')):
        fr = bytearray(b'\x01\x81\x01' + b'\x00'*27 + b'\xff\xff\xff')  # 30 content + ff pad
        fr[af_off] = raw_af
        assert tool.decode_frame(1, bytes(fr), 0xf0, mask)['AF mode'] == want, hex(raw_af)
    print("AF-mode high flag bits masked (0xc2 -> One-Shot AF): OK")


def test_untrusted_layout_is_flagged_not_guessed():
    # bits==bytes broken: real data where 0xff padding should be -> can't trust the
    # offsets, so flag dates '?(layout)' rather than emit possibly-shifted values.
    mask = bytes.fromhex("ffff003f0008003f")               # baseline: 30 mapped bytes
    good = bytes.fromhex("018101" + "00"*27 + "ffffff")     # clean 0xff padding
    bad  = bytes.fromhex("018101" + "00"*27 + "ff5aff")     # stray data in padding
    assert tool.decode_frame(1, good, 0xf0, mask)['_untrusted'] is False
    r = tool.decode_frame(1, bad, 0xf0, mask)
    assert r['_untrusted'] is True and r['Date'] == '?(layout)'
    print("record that doesn't match the mask (bits!=bytes) is flagged, not decoded: OK")


def test_plausibility_guard():
    assert tool._bcd_date3(bytes.fromhex("260703")) == "2026-07-03"
    assert tool._bcd_time3(bytes.fromhex("132759")) == "13:27:59"
    assert tool._bcd_date3(b'\xff\xff\xff') is None           # no date -> blank
    assert tool._bcd_date3(bytes.fromhex("4a4a4a")) is None   # junk (old bug's symptom)
    assert tool._bcd_date3(bytes.fromhex("261303")) is None   # month 13
    assert tool._bcd_time3(bytes.fromhex("991111")) is None   # hour 99
    print("BCD date/time plausibility accepts real/blank, rejects junk & bad values: OK")


def test_multiple_exposure_flag():
    # ME is flagged two ways: several records sharing one frame number, OR the drive
    # byte's 0x80 continuation bit. The drive offset is mask-derived, so exercise it.
    hd = bytearray(25); hd[5:8] = b'\x02\x10\x26'
    hd[9:17] = tool.FRAME_MASK_BASELINE; hd[18] = 0xf0
    doff = tool.frame_layout(tool.FRAME_MASK_BASELINE)[0]['drive']
    def frame(seq, drive=0x08):
        fr = bytearray(b'\x01\x81' + bytes([seq]) + b'\x00'*27 + b'\xff\xff\xff')
        fr[doff] = drive
        return bytes(fr)
    film = {'hdr': bytes(hd),
            'frames': [frame(1), frame(5), frame(5), frame(7, 0x88)]}  # #5 shared; #7 cont-bit
    err = io.StringIO(); old = sys.stderr; sys.stderr = err   # dates are 0x00 -> flagged; ignore
    fd, path = tempfile.mkstemp(suffix='.csv'); os.close(fd)
    try:
        tool.films_to_csv([film], path)
    finally:
        sys.stderr = old
    me = {r['Frame']: r['Multiple exposure'] for r in csv.DictReader(open(path))}
    os.unlink(path)
    assert me['1'] == 'OFF' and me['5'] == 'ON' and me['7'] == 'ON', me
    print("multiple-exposure flag: shared frame # and drive-0x80 bit both flag ON: OK")


def test_all_off_decodes_shooting_mode_only():
    mask = bytes.fromhex("c009000000000000")
    fr = bytes.fromhex("0181010400ffffffff")   # frame 1, mode 0x04 = Bulb
    row = tool.decode_frame(1, fr, film_dx=0x58, mask=mask)
    assert row['Shooting mode'] == 'Bulb', row['Shooting mode']
    assert row['Focal length'] == '' and row['Date'] == '' and row['Av'] == ''
    assert row['_untrusted'] is False and row['_unknown'] is False
    print("all-off record decodes only Shooting mode, no invented fields: OK")


def main():
    test_offsets_for_known_configs()
    test_all_masks_fully_explained()
    test_date_and_time_are_separate_items()
    test_cfn_11byte_field_spans_bytes()
    test_still_unmapped_item_decodes_known_fields()
    test_focus_point_fields_printed_raw()
    test_afmode_high_bit_masked()
    test_untrusted_layout_is_flagged_not_guessed()
    test_plausibility_guard()
    test_multiple_exposure_flag()
    test_all_off_decodes_shooting_mode_only()


if __name__ == '__main__':
    main()
