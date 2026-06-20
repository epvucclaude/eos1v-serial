#!/usr/bin/env python3
"""Custom / Personal Function tests (no hardware).

Confirms register reads (with checksums), that the C.Fn and P.Fn writes reproduce
their captured register blocks exactly (C.Fn d2/d6/d8/da incl. the 0x41 trailer;
P.Fn d4/de/b0-bf), and that backup save/load round-trips.

Run:  python tests/test_functions.py
"""
import sys, pathlib, types, tempfile, os

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_u = types.ModuleType("usb"); _uc = types.ModuleType("usb.core")
class USBError(Exception): pass
_uc.USBError = USBError; _u.core = _uc
sys.modules.setdefault("usb", _u); sys.modules.setdefault("usb.core", _uc)

import eos1v_tool as tool

CFN = {0xd5: '11 11 11 11 11 11 11 11 11 01', 0xd7: '11 11 11 11 11 11 11 11 11 01',
       0xd9: '11 11 11 11 11 11 11 11 11 01', 0xd1: '21 11 11 11 11 11 11 21 42 01 02'}
PFN = {0xd3: '08 10 80 00', 0xdd: '08 00 00 00 15', 0xc5: '3f', 0xc6: 'f0', 0xc1: '10',
       0xc3: 'a0 10', 0xc4: '70 08', 0xcb: '40', 0xcc: '0e 0e 06 00', 0xca: '24',
       0xc7: '00 60', 0xc8: '01 00', 0xc0: '00 20', 0xcd: '10 82 20 08 4a', 0xcf: '02',
       0xce: '0a', 0xd1: '21 11 11 11 11 11 11 21 42 01 02'}


def framed(echo, data):
    d = bytes(data); return bytes([echo, len(d)]) + d + bytes([sum(d) & 0xff])


# every byte the camera echoes back as a write-command acknowledgement
WRITE_ECHO = {wc for wc, _ in tool.CFN_WRITE.values()} | set(tool.PFN_WRITE.values())


class RegCam:
    """Mock TRANSPORT: a fake camera speaking raw serial (no USB bridge framing).
    send(raw) feeds the protocol; recv() returns queued raw serial reply bytes."""
    def __init__(s, regs):
        s.regs = {k: bytes.fromhex(v.replace(' ', '')) for k, v in regs.items()}
        s.inq = bytearray(); s.out = []
    def send(s, data):
        ser = bytes(data); s.out.append(ser)
        if ser == b'\xff': s.inq += b'\xf4'
        elif ser == b'\xf4': pass
        elif ser == b'\xf1': s.inq += bytes.fromhex('f103011a344f')
        elif ser == b'\xf2': s.inq += b'\xf2'
        elif len(ser) == 1 and ser[0] in s.regs: s.inq += framed(ser[0], s.regs[ser[0]])
        elif len(ser) == 1 and ser[0] in WRITE_ECHO: s.inq += ser                  # write echo
        elif ser and ser[0] == len(ser) - 2: s.inq += b'\x01'                      # data ack
        else: s.inq += b'\xf4'
    def recv(s, timeout_ms=0):
        if not s.inq: return b''
        c = bytes(s.inq[:62]); s.inq = s.inq[62:]; return c
    def close(s): pass


def mkcam(regs):
    cam = tool.EOS1V.__new__(tool.EOS1V)
    cam.verbose = False; cam.transport = RegCam(regs)
    cam._WRITE_GAP = cam._WRITE_GAP_LONG = cam._CMD_GAP = 0   # no pacing in tests
    return cam


def test_read():
    cam = mkcam(CFN); regs = cam.read_custom_functions()
    assert all(ok for _, _, ok in regs)
    assert {c: d for c, d, _ in regs} == {k: bytes.fromhex(v.replace(' ', '')) for k, v in CFN.items()}
    cam = mkcam(PFN); pregs = cam.read_personal_functions()
    assert all(ok for _, _, ok in pregs) and len(pregs) == len(tool.PFN_READ)
    print(f"read C.Fn ({len(regs)} regs) and P.Fn ({len(pregs)} regs), all checksums valid: OK")


def test_write_matches_capture():
    cam = mkcam(CFN)
    regmap = {c: bytes.fromhex(v.replace(' ', '')) for c, v in CFN.items()}
    res = cam.write_custom_functions(regmap)
    sent = [x.hex(' ') for x in cam.transport.out]
    for w in ['0b 21 11 11 11 11 11 11 21 42 01 02 ed',
              '0a 11 11 11 11 11 11 11 11 11 01 9a',
              '0b 11 11 11 11 11 11 11 11 11 01 41 db',
              '0b 11 11 11 11 11 11 11 11 11 01 41 db']:
        assert w in sent, f"missing write block {w}"
    assert [x for x in sent if x in ('d2', 'd6', 'd8', 'da')] == ['d2', 'd6', 'd8', 'da']
    assert all(ok for _, ok in res)
    print("write C.Fn reproduces captured d2/d6/d8/da blocks (incl. 0x41 trailer): OK")


def test_read_all_one_session():
    # C.Fn then P.Fn in a single session: exactly one teardown (0xf2), so the
    # camera stays in data mode without a second button press (matches ES-E1).
    cam = mkcam({**CFN, **PFN})
    cfn, pfn = cam.read_all_functions()
    assert all(ok for _, _, ok in cfn) and all(ok for _, _, ok in pfn)
    sent = [x.hex() for x in cam.transport.out]
    assert sent.count('f2') == 1, f"expected 1 teardown, got {sent.count('f2')}"
    print("read C.Fn+P.Fn in one session (single teardown, no re-wake): OK")


def test_write_pfn_matches_capture():
    # the modified P.Fn block ES-E1 wrote in data/usbmon-read-write-pfn.pcapng
    # (P.Fn-12 Standard->Slightly fast: cb 40->20, d3[2]/dd[2] enable bit set)
    written = {0xd3: '08 10 88 00', 0xdd: '08 00 08 00 15', 0xc5: '3f', 0xc6: 'f0',
               0xc1: '20', 0xc3: 'a0 10', 0xc4: '70 08', 0xcb: '20',
               0xcc: '0e 0e 06 00', 0xca: '24', 0xc7: '00 60', 0xc8: '01 00',
               0xc0: '00 20', 0xcd: '10 82 20 08 4a', 0xcf: '02', 0xce: '0a'}
    cam = mkcam(PFN)
    regmap = {c: bytes.fromhex(v.replace(' ', '')) for c, v in written.items()}
    res = cam.write_personal_functions(regmap)
    sent = [x.hex(' ') for x in cam.transport.out]
    # data blocks exactly as captured (length-prefixed + checksum)
    for blk in ['04 08 10 88 00 a0', '05 08 00 08 00 15 25', '01 3f 3f', '01 f0 f0',
                '01 20 20', '02 a0 10 b0', '02 70 08 78', '04 0e 0e 06 00 22',
                '01 24 24', '02 00 60 60', '02 01 00 01', '02 00 20 20',
                '05 10 82 20 08 4a 04', '01 02 02', '01 0a 0a']:
        assert blk in sent, f"missing P.Fn write block {blk}"
    # write commands in the captured order (d-regs +1, c-regs -0x10)
    order = ['d4', 'de', 'b5', 'b6', 'b1', 'b3', 'b4', 'bb', 'bc', 'ba',
             'b7', 'b8', 'b0', 'bd', 'bf', 'be']
    assert [x for x in sent if x in order] == order, "write-command order mismatch"
    assert all(ok for _, ok in res) and len(res) == len(tool.PFN_WRITE_ORDER)
    print("write P.Fn reproduces the captured d4/de/b0-bf block byte-for-byte: OK")


def test_write_no_data_into_silence():
    # If the camera doesn't echo the command (it's gone silent committing to
    # EEPROM), the data block must NOT be sent -- never blast bytes into a camera
    # that isn't listening (that's what desynced the old loop and risked wear).
    cam = mkcam(PFN)
    base = cam.transport.send; sent = []
    def silent_send(data):
        ser = bytes(data); sent.append(ser)
        if len(ser) == 1 and ser[0] in WRITE_ECHO:
            return                                # silent: never echo the command
        base(data)
    cam.transport.send = silent_send
    assert not cam._write_register(0xb5, b'\x3f', timeout=0.05)
    assert sent == [b'\xb5'], f"data sent without a command echo: {sent}"
    print("write sends no data block when the command isn't echoed: OK")


def test_no_sync_echo_mid_write():
    # A stray idle 0xF4 arriving mid-write must NOT be echoed between a register's
    # command and its data -- the camera reads that 0xF4 as the data-length byte
    # and wedges (it killed a full-block write-pfn at register ~5). The write still
    # completes, and we send no standalone 0xF4 during the transaction.
    cam = mkcam(PFN)
    rc = cam.transport; base = rc.send
    def send(data):
        ser = bytes(data)
        if len(ser) == 1 and ser[0] in WRITE_ECHO:
            rc.inq += b'\xf4'                 # camera emits a stray idle sync...
        base(data)                            # ...then the normal echo/ack
    rc.send = send
    assert cam._write_register(0xbd, bytes.fromhex('108210084a'), timeout=0.3)
    assert b'\xf4' not in rc.out, f"a standalone 0xF4 was sent mid-write: {[x.hex() for x in rc.out]}"
    print("write does not echo a stray idle sync mid-transaction: OK")


def test_write_only_subset():
    # `apply` writes only the registers that changed. write_personal_functions with
    # only={cb} must send just that one register's command+data (plus session
    # frames), not the whole 16-register block -- this is the EEPROM-wear win.
    cam = mkcam(PFN)
    regmap = {c: bytes.fromhex(v.replace(' ', '')) for c, v in PFN.items()}
    res = cam.write_personal_functions(regmap, only={0xcb})
    assert [wc for wc, _ in res] == [tool.PFN_WRITE[0xcb]], "wrote more than the subset"
    wcmds = [x.hex() for x in cam.transport.out if len(x) == 1 and x[0] in WRITE_ECHO]
    assert wcmds == ['bb'], f"expected only bb written, got {wcmds}"
    print("write with only={cb} touches just that register (selective apply): OK")


def test_pfn25_writable():
    # P.Fn-25 (cd) write is re-enabled (metering bytes capture-confirmed) and writes
    # SELECTIVELY -- a metering change touches only the cd register (the earlier
    # full-block special case was for a problem that turned out to be a read bug).
    data = ROOT / 'data'
    cfn, _ = tool.load_functions(str(data / 'cfn-baseline.txt'))
    pfn, _ = tool.load_functions(str(data / 'pfn-pfn25-aiservo.txt'))
    text = "P.Fn-25 = Program AE / Center Averaging / Single-frame / One-Shot AF / Automatic"
    res = tool.apply_compute(text, cfn, pfn)
    assert not res['errors'], res['errors']
    assert any(s == 'P.Fn-25' for s, _, _ in res['changes']), "P.Fn-25 not written"
    assert not res['skipped'], f"P.Fn-25 should not be skipped: {res['skipped']}"
    assert res['pfn_only'] == {0xcd}, f"a metering-only change is just cd: {res['pfn_only']}"
    # byte-for-byte the value ES-E1 wrote in data/saleae-pfn25-writes.csv
    assert res['new_pfn'][0xcd] == bytes([0x10, 0x82, 0x10, 0x08, 0x4a])
    print("P.Fn-25 writable: a metering change writes just the cd register: OK")


def test_read_frame_with_f4():
    # Regression for the cd "corruption" mystery: a framed reply whose checksum (or
    # a data byte) is 0xF4 must be read BY LENGTH, not truncated by sync-stripping.
    # cd=10 82 10 08 4a has checksum 0xF4 -- the exact value that read back as
    # "(no data)" on hardware and was misdiagnosed as camera corruption.
    regs = dict(PFN)
    regs[0xcd] = '10 82 10 08 4a'                    # checksum 0xF4
    regs[0xc3] = 'f4 f4'                              # 0xF4 *inside* data (cksum E8)
    got = {c: d for c, d, ok in mkcam(regs).read_personal_functions() if ok}
    assert got.get(0xcd) == bytes.fromhex('108210084a'), got.get(0xcd)
    assert got.get(0xc3) == bytes.fromhex('f4f4'), got.get(0xc3)
    print("read parses frames with 0xF4 as checksum/data (by length, not sync): OK")


def test_verify_after_write():
    # apply re-reads what it wrote: passes when the camera holds the new values,
    # and flags a register (e.g. cd) that won't read back.
    data = ROOT / 'data'
    cfn, _ = tool.load_functions(str(data / 'cfn-baseline.txt'))
    pfn, _ = tool.load_functions(str(data / 'pfn-pfn25-aiservo.txt'))
    text = "P.Fn-25 = Program AE / Center Averaging / Single-frame / One-Shot AF / Automatic"
    res = tool.apply_compute(text, cfn, pfn)
    good = {k: bytes(v).hex(' ') for k, v in res['new_pfn'].items()}
    assert mkcam(good).verify_written(res) == [], "verify should pass when regs match"
    broken = dict(good); del broken[0xcd]              # cd won't read -> flagged
    bad = mkcam(broken).verify_written(res)
    assert any(rc == 0xcd for rc, _ in bad), f"verify should flag cd, got {bad}"
    print("apply verify: passes on match, flags a register that won't read: OK")


def test_restore_diff():
    # write-pfn/write-cfn write only the registers that differ from the camera's
    # current state; an unreadable (empty) current register counts as differing so
    # a corrupt register still gets restored.
    order = [0xd3, 0xc5, 0xc1, 0xcd]
    backup = {0xd3: b'\x08', 0xc5: b'\x3f', 0xc1: b'\x20',
              0xcd: bytes.fromhex('108210084a')}
    current = {0xd3: b'\x08', 0xc5: b'\x3f', 0xc1: b'\x10', 0xcd: b''}  # c1 differs, cd empty
    assert tool.restore_diff(backup, current, order) == [0xc1, 0xcd]
    assert tool.restore_diff(backup, backup, order) == []   # identical -> write nothing
    print("restore_diff: write-pfn/cfn write only differing/unreadable registers: OK")


def test_settings_roundtrip():
    # Dumping the current state then checking that file back must report NO changes
    # and NO invalid values -- this catches any dump/resolve canonical mismatch.
    data = ROOT / 'data'
    cfn, _ = tool.load_functions(str(data / 'cfn-baseline.txt'))
    for pf in ('pfn-changes-10.txt', 'pfn-changes-7.txt', 'pfn-baseline.txt'):
        pfn, _ = tool.load_functions(str(data / pf))
        report = tool.settings_check(tool.settings_dump(cfn, pfn), cfn, pfn)
        assert 'INVALID' not in report, f"{pf}: dump produced invalid values\n{report}"
        assert 'No changes' in report, f"{pf}: dump->check not clean\n{report}"
    print("settings dump->check round-trip is clean (no spurious changes): OK")


def test_encode_decode_identity():
    # Encoding the current decoded values then decoding again must reproduce every
    # setting (value-level -- shutter/aperture bytes may shift <=1 LSB but must
    # still decode to the same rung). Exercises every encoder incl. P.Fn-4/5/25.
    data = ROOT / 'data'
    cfn, _ = tool.load_functions(str(data / 'cfn-baseline.txt'))
    for pf in ('pfn-changes-10.txt', 'pfn-changes-9.txt', 'pfn-pfn25-aiservo.txt'):
        pfn, _ = tool.load_functions(str(data / pf))
        cur = tool.current_settings(cfn, pfn)
        new_pfn = tool.encode_pfn(pfn, {n: cur[f'P.Fn-{n}'] for n in range(1, 31)})
        cv = {n: v for n, v in tool.decode_cfn_bank(cfn[0xd1]).items() if 1 <= n <= 19}
        new_cfn = dict(cfn); new_cfn[0xd1] = tool.encode_cfn_bank(cfn[0xd1], cv, True)
        after = tool.current_settings(new_cfn, new_pfn)
        for sid in cur:
            assert tool._norm(after[sid]) == tool._norm(cur[sid]), \
                f"{pf} {sid}: {after[sid]!r} != {cur[sid]!r}"
    print("encode->decode reproduces every setting value (all encoders): OK")


def test_backup_roundtrip():
    cam = mkcam(CFN); regs = cam.read_custom_functions()
    fd, path = tempfile.mkstemp(suffix='.txt'); os.close(fd)
    tool.save_functions(path, 'Custom', regs)
    loaded, kind = tool.load_functions(path)
    os.unlink(path)
    assert kind == 'Custom' and loaded == {c: d for c, d, _ in regs}
    print("C.Fn backup save/load round-trip: OK")


if __name__ == '__main__':
    test_read()
    test_read_all_one_session()
    test_write_matches_capture()
    test_write_pfn_matches_capture()
    test_write_no_data_into_silence()
    test_no_sync_echo_mid_write()
    test_write_only_subset()
    test_pfn25_writable()
    test_read_frame_with_f4()
    test_verify_after_write()
    test_restore_diff()
    test_settings_roundtrip()
    test_encode_decode_identity()
    test_backup_roundtrip()
    print("function tests passed.")
