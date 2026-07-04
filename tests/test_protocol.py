#!/usr/bin/env python3
"""Protocol-layer tests (no hardware needed).

  1. The stream parser: lone 0xF4 is echoed+counted; framed replies are queued;
     a 0xF4 *inside* reply data is read by length and not mistaken for a sync.
  2. command() resends only on a not-ready sync, returning the real reply.
  3. download() reconstructs the full Windows-transfer capture (if present in
     data/) as a mock camera, and decodes the first frame correctly.

Run:  python tests/test_protocol.py
"""
import sys, pathlib, threading, types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

# pyusb isn't needed for these tests; stub it so the import works anywhere.
_u = types.ModuleType("usb"); _uc = types.ModuleType("usb.core")
class USBError(Exception): pass
_uc.USBError = USBError; _u.core = _uc
sys.modules.setdefault("usb", _u); sys.modules.setdefault("usb.core", _uc)

import eos1v_tool as tool
DATA = ROOT / 'data'
SYNC = 0xF4


def bare_cam():
    cam = tool.EOS1V.__new__(tool.EOS1V)
    cam._rx = bytearray(); cam._replies = []; cam._syncs = 0
    cam._rxlock = threading.Lock(); cam.verbose = False
    return cam


def test_stream_parser():
    cam = bare_cam(); sent = []
    cam._send_cmd = lambda c: sent.append(c)
    e1 = bytes.fromhex('e102001212')
    weird = bytes([0xe3, 0x03, 0xf4, 0x11, 0x22, (0xf4 + 0x11 + 0x22) & 0xff])
    cam._rx += bytes([SYNC]) + e1 + bytes([SYNC]) + weird
    cam._parse_rx()
    assert cam._syncs == 2 and sent.count(SYNC) == 2
    assert cam._replies == [e1, weird]      # 0xf4 inside data preserved
    print("stream parser (sync echo + 0xf4-in-data): OK")


def test_resend_on_not_ready():
    cam = bare_cam(); sent = []
    hdr = bytes.fromhex('e3210190402f0003312620ffff003f0008003f0048260204'
                        '144829ffffffffffffffffef')
    st = {'n': 0}
    def send(c):
        sent.append(c)
        if c == 0xe3:
            st['n'] += 1
            cam._rx += bytes([SYNC]) if st['n'] == 1 else hdr  # not-ready, then real
            cam._parse_rx()
    cam._send_cmd = send
    r = cam.command(0xe3, timeout=2.0)
    assert r == hdr and sent.count(0xe3) == 2
    print("command() resend-on-not-ready: OK")


def test_full_download():
    cap = DATA / 'usbmon-dump-windows-transfer.pcapng'
    if not cap.exists():
        print(f"full-download sim: SKIPPED (drop {cap.name} in data/)")
        return
    from usbmon import parse_pcapng, framed_replies
    reps = framed_replies(parse_pcapng(str(cap)))
    e3q = [r for r in reps if r and r[0] == 0xe3]
    e4q = [r for r in reps if r and r[0] == 0xe4]
    setupq = {c: [r for r in reps if r and r[0] == c]
              for c in (0xf6, 0xf1, 0xe8, 0xfc, 0xe1)}
    cam = bare_cam(); st = {'e3': 0, 'e4': 0, 'su': {c: 0 for c in setupq}}
    def send(c):
        if c == 0xf4:
            return
        if c == 0xe3:
            cam._rx += e3q[st['e3']] if st['e3'] < len(e3q) else bytes([0xe3, 0, 0])
            st['e3'] += 1
        elif c == 0xe4:
            cam._rx += e4q[st['e4']] if st['e4'] < len(e4q) else bytes([0xe4, 1, 0, 0])
            st['e4'] += 1
        elif c in setupq and setupq[c]:
            cam._rx += setupq[c][min(st['su'][c], len(setupq[c]) - 1)]; st['su'][c] += 1
        else:
            cam._rx += bytes([0xf4])
        cam._parse_rx()
    cam._send_cmd = send
    cam._wake = lambda attempts=12: True
    cam._start_reader = lambda: None
    cam._serial_open = lambda: None
    cam.rawlog = []
    films, _ = cam.download()
    nframes = sum(len(f['frames']) for f in films)
    hd = films[0]['hdr']
    d = tool.decode_frame(hd and films[0]['frames'][0][2], films[0]['frames'][0], hd[18])
    print(f"full-download sim: {len(films)} films / {nframes} frames; "
          f"first frame -> {d['Focal length']} {d['Tv']} f/{d['Av']} {d['Shooting mode']}")
    assert nframes > 0


def test_sync_extract_reads_0xf4_by_length():
    # The SYNCHRONOUS read path (used by set-clock/erase/register reads) mirrors the
    # background reader's rule: a 0xF4 is a sync ONLY at a frame boundary (echo+drop);
    # once a frame has started it is read BY LENGTH, so a 0xF4 in the data -- or as the
    # checksum -- is kept. This is exactly the bug that once read register cd (whose
    # checksum is 0xF4) a byte short as "(no data)". Guard it directly.
    cam = tool.EOS1V.__new__(tool.EOS1V)
    echoed = []
    cam._send_serial = lambda b: echoed.append(bytes(b))
    frame = bytes([0xcd, 0x02, SYNC, 0x00, SYNC])   # echo, len=2, data=[f4,00], cksum=f4
    cam._sbuf = bytearray([SYNC, SYNC]) + frame       # two idle syncs, then the frame
    got = cam._sync_extract(0xcd)
    assert got == frame, got.hex()                    # 0xF4 in data AND checksum kept
    assert echoed == [bytes([SYNC]), bytes([SYNC])]   # each boundary sync echoed once
    assert cam._sbuf == b'', cam._sbuf.hex()          # frame fully consumed
    # a lone trailing 0xF4 after a complete frame is a boundary sync -> echoed, not data
    cam._sbuf = bytearray(frame) + bytes([SYNC]); echoed.clear()
    assert cam._sync_extract(0xcd) == frame           # frame read; trailing sync remains
    assert cam._sync_extract(0xcd) is None            # only the lone sync left
    assert echoed == [bytes([SYNC])]                  # ...and it was echoed
    print("_sync_extract: 0xF4 read by length (data/checksum), boundary syncs echoed: OK")


def test_context_manager_closes():
    # `with EOS1V(...) as cam:` must yield the camera and always close() on exit,
    # including when the body raises. (The CLI relies on this for cleanup.)
    class FakeTransport:
        def __init__(self): self.closed = False
        def close(self): self.closed = True
    cam = tool.EOS1V.__new__(tool.EOS1V)
    cam.transport = FakeTransport()
    with cam as c:
        assert c is cam
    assert cam.transport.closed
    cam2 = tool.EOS1V.__new__(tool.EOS1V); cam2.transport = FakeTransport()
    try:
        with cam2:
            raise ValueError("boom")
    except ValueError:
        pass
    assert cam2.transport.closed, "close() must run even when the body raises"
    print("context manager yields self and always closes: OK")


if __name__ == '__main__':
    test_stream_parser()
    test_resend_on_not_ready()
    test_full_download()
    test_sync_extract_reads_0xf4_by_length()
    test_context_manager_closes()
    print("protocol tests passed.")
