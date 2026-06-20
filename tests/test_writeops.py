#!/usr/bin/env python3
"""Write-operation tests (no hardware): set-clock and erase-all.

Replays each write transaction against a mock camera built from the usbmon
captures, checking the exact bytes the tool emits and the verify logic.

Run:  python tests/test_writeops.py
"""
import sys, pathlib, types
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_u = types.ModuleType("usb"); _uc = types.ModuleType("usb.core")
class USBError(Exception): pass
_uc.USBError = USBError; _u.core = _uc
sys.modules.setdefault("usb", _u); sys.modules.setdefault("usb.core", _uc)

import eos1v_tool as tool


def mkcam(mock):
    cam = tool.EOS1V.__new__(tool.EOS1V)
    cam.verbose = False; cam.transport = mock
    return cam


class SetClockCam:
    """Mock transport: plays the camera side of usbmon-set-clock (raw serial)."""
    def __init__(s): s.out = []; s.inq = bytearray(); s.clk = bytes.fromhex('260612135145')
    def send(s, data):
        ser = bytes(data); s.out.append(ser)
        if ser == b'\xff': s.inq += b'\xf4'
        elif ser == b'\xf4': pass
        elif ser == b'\xf6': s.inq += bytes.fromhex('f60e34180087441810 1c0000000000106b'.replace(' ', ''))
        elif ser == b'\xf1': s.inq += bytes.fromhex('f103011a344f')
        elif ser == b'\xf3': s.inq += bytes([0xf3, 6]) + s.clk + bytes([sum(s.clk) & 0xff])
        elif ser == b'\xa1': s.inq += bytes.fromhex('a102002424')
        elif ser == b'\xd1': s.inq += bytes.fromhex('d10b21111111111111214201 02ed'.replace(' ', ''))
        elif ser == b'\xf9': s.inq += b'\xf9'
        elif ser == tool.frame_serial([0x1a]): s.inq += b'\x01'
        elif ser == b'\xf8': s.inq += b'\xf8'
        elif len(ser) == 8 and ser[0] == 0x06: s.clk = ser[1:7]; s.inq += b'\x01'  # store written
        elif ser == b'\xf2': s.inq += b'\xf2'
    def recv(s, timeout_ms=0):
        if not s.inq: return b''
        c = bytes(s.inq[:62]); s.inq = s.inq[62:]; return c
    def close(s): pass


def test_set_clock():
    cam = mkcam(SetClockCam())
    old, clk, new = cam.set_clock(datetime(2026, 6, 12, 13, 51, 53))
    seq = [x.hex() for x in cam.transport.out]
    for need in ['f9', '011a1a', 'f8', '0626061213515 3f5'.replace(' ', '')]:
        assert need in seq, f"missing {need}"
    assert clk == bytes.fromhex('260612135153') and new == clk
    print(f"set-clock: wrote {tool.bcd6(clk)} and read it back; transaction matches capture: OK")


class EraseCam:
    """Mock transport: plays usbmon-erase-all (film count 0x12 -> 0x00, raw serial)."""
    def __init__(s): s.out = []; s.inq = bytearray(); s.films = 0x12
    def send(s, data):
        ser = bytes(data); s.out.append(ser)
        if ser == b'\xff': s.inq += b'\xf4'
        elif ser == b'\xf4': pass
        elif ser == b'\xf6': s.inq += bytes.fromhex('f60e34180087441810 1c0000000000106b'.replace(' ', ''))
        elif ser == b'\xf1': s.inq += bytes.fromhex('f103011a344f')
        elif ser == b'\xe8': s.inq += bytes.fromhex('e808ffff003f0008003f84')
        elif ser == b'\xfc': s.inq += bytes.fromhex('fc02' + ('fa00fa' if s.films == 0 else 'd700d7'))
        elif ser == b'\xe1': s.inq += bytes([0xe1, 2, 0, s.films, s.films & 0xff])
        elif ser == b'\xe2': s.films = 0; s.inq += bytes.fromhex('e2010101')
        elif ser == b'\xf2': s.inq += b'\xf2'
    def recv(s, timeout_ms=0):
        if not s.inq: return b''
        c = bytes(s.inq[:62]); s.inq = s.inq[62:]; return c
    def close(s): pass


def test_erase_abort_and_confirm():
    cam = mkcam(EraseCam())
    st, b, a = cam.erase_all(lambda c: False)
    assert st == 'aborted' and b == 0x12 and (b'\xe2' not in cam.transport.out)
    cam = mkcam(EraseCam())
    st, b, a = cam.erase_all(lambda c: c == 0x12)
    assert st == 'ok' and b == 0x12 and a == 0x00 and (b'\xe2' in cam.transport.out)
    print("erase-all: abort gate blocks 0xe2; confirmed path erases and verifies 18->0: OK")


if __name__ == '__main__':
    test_set_clock()
    test_erase_abort_and_confirm()
    print("write-op tests passed.")
