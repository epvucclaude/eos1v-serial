#!/usr/bin/env python3
"""Minimal pcapng + Linux usbmon parser, and a serial-transaction reassembler.

Used by the tests to turn a usbmon capture (.pcapng, from `usbmon` / Wireshark)
of an ES-E1 session into the EOS-1V serial command/response stream, so decoded
output can be compared against the Windows ES-E1 CSV exports.

The camera link is a USB->serial bridge:
  bulk OUT 0x02 = [n][00][n serial bytes]   (host -> camera)
  bulk IN  0x81 = [n][00][n serial bytes]   (camera -> host)
A serial reply is framed [echo][len][len data bytes][checksum].
"""
import struct


def _pcapng_blocks(buf):
    off, n = 0, len(buf)
    while off + 8 <= n:
        btype, blen = struct.unpack_from('<II', buf, off)
        if blen < 12 or off + blen > n:
            break
        yield btype, buf[off:off + blen]
        off += blen


def _parse_usbmon(data):
    if len(data) < 64:
        return None
    (urbid, ev_type, xfer_type, epnum, devnum, busnum,
     flag_setup, flag_data, ts_sec, ts_usec, status,
     length, len_cap) = struct.unpack_from('<Q B B B B H B B q i i I I', data, 0)
    return dict(ev=chr(ev_type), xfer=xfer_type, epnum=epnum,
                ts=ts_sec + ts_usec / 1e6, setup=data[40:48],
                length=length, len_cap=len_cap, payload=data[64:64 + len_cap])


def parse_pcapng(path):
    """Return a list of usbmon packet dicts, in capture order."""
    buf = open(path, 'rb').read()
    out = []
    for btype, b in _pcapng_blocks(buf):
        if btype == 6:                       # Enhanced Packet Block
            caplen, = struct.unpack_from('<I', b, 20)
            p = _parse_usbmon(b[28:28 + caplen])
            if p:
                out.append(p)
        elif btype == 3:                     # Simple Packet Block
            origlen, = struct.unpack_from('<I', b, 8)
            p = _parse_usbmon(b[12:12 + origlen])
            if p:
                out.append(p)
    return out


def serial_events(packets):
    """Deframe the bulk endpoints into ordered ('OUT'|'IN', bytes) events."""
    ev = []
    for p in sorted(packets, key=lambda x: x['ts']):
        if p['xfer'] != 3 or p['len_cap'] <= 0:
            continue
        pl = bytes(p['payload'])
        n = pl[0] if pl else 0
        if p['ev'] == 'S' and p['epnum'] == 0x02:
            ev.append(('OUT', pl[2:2 + n]))
        elif p['ev'] == 'C' and p['epnum'] == 0x81:
            ev.append(('IN', pl[2:2 + n]))
    return ev


def command_replies(packets):
    """Pair each OUT command with the IN bytes that follow it (until the next
    OUT), returning a list of (out_bytes, in_bytes). IN bytes are concatenated,
    so a multi-packet framed reply is reassembled."""
    ev = serial_events(packets)
    pairs, i = [], 0
    while i < len(ev):
        d, b = ev[i]
        if d == 'OUT':
            rep, j = b'', i + 1
            while j < len(ev) and ev[j][0] == 'IN':
                rep += ev[j][1]
                j += 1
            pairs.append((b, rep))
            i = j
        else:
            pairs.append((b'', b))           # unsolicited IN
            i += 1
    return pairs


def framed_replies(packets, echo=None):
    """Reassemble the IN stream into framed replies [echo][len][data][cksum].
    Lone 0xF4 sync bytes are skipped. If `echo` is given, only replies with that
    echo byte are returned. Yields bytes objects (the full framed reply)."""
    stream = bytearray()
    for d, b in serial_events(packets):
        if d == 'IN':
            stream += b
    out, i = [], 0
    while i < len(stream):
        if stream[i] == 0xF4:                # lone sync
            i += 1
            continue
        if i + 1 >= len(stream):
            break
        need = stream[i + 1] + 3
        rep = bytes(stream[i:i + need])
        if echo is None or (rep and rep[0] == echo):
            out.append(rep)
        i += need
    return out


if __name__ == '__main__':
    import sys
    for c, r in command_replies(parse_pcapng(sys.argv[1])):
        print(f"OUT {c.hex(' '):<36} -> IN {r.hex(' ')}")
