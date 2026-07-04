# Canon EOS-1V / ES-E1 shooting-data protocol — reverse-engineering notes

A standalone Python tool (`eos1v_tool.py`) downloads and decodes the EOS-1V's
in-camera shooting-data memory over the original "Canon EOS USB Cable" with no
Windows/XP/ES-E1 software. Output matches the ES-E1 export field-for-field
(validated to 0 mismatches across 27 rolls). This is the condensed spec.

---

## 1. USB side — the cable is a USB→serial bridge

- **Device:** VID `0x04A9` (Canon), PID `0x3040`, product string "Canon EOS USB
  Cable", vendor class `0xFF`. Internally a USB-to-TTL-serial bridge; the lead to
  the camera's remote-control/data-transfer terminal carries only **TX, RX, GND**
  (no modem/handshake lines).
- **Endpoints (interface 0, alt 0):** bulk OUT `0x02`, bulk IN `0x81` (both 64-byte
  max packet), interrupt IN `0x83` (present but **never used**).
- **Open sequence (pyusb/libusb):**
  1. find `04A9:3040`; detach kernel driver on interface 0 if present.
  2. `set_configuration()` **only if not already configured** — re-issuing it on an
     already-configured device resets the link on Linux and the camera goes mute.
  3. claim interface 0.
- **Bridge/UART init = 7 vendor control transfers** (`bmRequestType=0x41` =
  host→device | vendor | interface; `wIndex=0`), sent before any bulk traffic:

  | bRequest | wValue | data            |
  |----------|--------|-----------------|
  | 0x01     | 0x0000 | `05 04 08 00 00`|
  | 0x01     | 0x0000 | `05 06 08 00 00`|
  | 0x03     | 0x0003 | —               |
  | 0x01     | 0x0000 | `05 06 08 00 00`|
  | 0x03     | 0x0003 | —               |
  | 0x03     | 0x0003 | —               |
  | 0x01     | 0x0000 | `05 06 08 00 00`|

  These are exactly what the Windows USB-serial driver emits for
  `SetCommState(9600,8,N,1)` + line-control. (The 3rd data byte is `0x07` in an
  already-running session and `0x08` cold; both work.)
- **USB framing of every bulk transfer** (64-byte buffer; only a short prefix is
  meaningful, the tail is the bridge's stale double-buffer):
  - **OUT:** `[01][00][cmd]` + zero pad. Byte 0 = number of serial bytes to send;
    the bridge transmits `cmd` on TX.
  - **IN:**  `[n][00][n serial bytes]` + stale padding. Byte 0 = count of valid
    serial bytes. Concatenate the `n` payloads across packets to rebuild a reply.
- **Reads must be overlapped:** keep a read pending on `0x81` continuously (a
  background reader thread); the camera answers tens of ms after each write.

---

## 2. Serial protocol (after de-framing)

- **Line settings:** 9600 baud, 8 data bits, no parity, 1 stop bit, no flow control.
- **Wake:** host → `0xFF`; camera → `0xF4` (~70 ms later); host → `0xF4` (echo).
  The camera **beeps** when it wakes, same as with ES-E1.
- **`0xF4` is a sync/flow byte everywhere.** The camera emits a lone `0xF4` when
  idle, as a trailing byte after a reply, and **instead of a reply when it is not
  ready**. Rule: **echo `0xF4` for every `0xF4` received**; if the `0xF4` came in
  place of a reply, **re-send the command**. (Resend only on an observed sync, not
  on a plain timeout, or you can advance the frame pointer and skip a frame.)
- **Command = one byte. Reply = `[echo][len][len data bytes][checksum]`**, where
  `checksum = sum(data) & 0xFF`. Read reply data **by length**, so a `0xF4` that
  occurs inside data is never mistaken for a sync.
- **Post-wake status queries** (camera/film info; safe to issue, mostly for display):
  `0xF6, 0xF1, 0xE8, 0xFC, 0xE1`.
- **Download loop:**
  - `0xE3` → film header (reply len `0x21` = 33 data bytes), one per roll.
  - `0xE4` → next frame record (len `0x21` = 33), `seq` increments 1,2,3…
  - `0xE4` returns `E4 01 00 00` (len 1, data `00`) at **end of a film**.
  - `0xE3` returns `E3 01 00 00` at **end of all films** → stop.
  - Pattern: `E3`, then `E4` until end-of-film marker; repeat until E3 end marker.
- **Teardown:** best-effort (`0xF2`); not fully characterized (use a short timeout).

---

## 3. Write operations (set clock, erase) — they modify the camera

Both follow the usual wake + status preamble, then differ. **Multi-byte writes
are framed exactly like replies:** `[len][data…][checksum]`, checksum =
`sum(data) & 0xFF`. The acknowledgements to the clock-write steps are **bare
bytes** (not framed), so a download-style framed parser must not be used for
them; the tool drives these with a small blocking, single-threaded I/O path.

### Read clock — `0xF3`
Reply `F3 06 YY MM DD HH MM SS <cksum>` (BCD). Pure read; safe.

### Set clock — 4-step write transaction
After wake/status (and the harmless state reads `0xF3`, `0xA1`, `0xD1` that
ES-E1 issues), send, waiting for each ack:

| send (serial bytes)              | meaning                | camera ack |
|----------------------------------|------------------------|-----------|
| `F9`                             | begin write            | `F9`      |
| `01 1A 1A`  (`[len][0x1A][cks]`) | select clock register  | `01`      |
| `F8`                             | write-data follows     | `F8`      |
| `06 YY MM DD HH MM SS <cks>`     | the 6 BCD clock bytes  | `01`      |

`YY` is the 2-digit year. Verify by reading `0xF3` back. Captured example wrote
`06 26 06 12 13 51 53 F5` = 2026-06-12 13:51:53.

### Erase all data — `0xE2`  (DESTRUCTIVE, irreversible)
A **single command** erases everything: send `0xE2`, reply `E2 01 01` (data
`01` = success). Confirm by reading `0xE1` before/after — its 4th byte is the
stored-roll count and drops to `00` (e.g. `E1 02 00 12` → `E1 02 00 00`). There
is **no separate arming step**, so gate this behind an explicit user
confirmation and download first.

---

## 4. Custom Functions (C.Fn) & Personal Functions (P.Fn)

Both are stored in camera registers, each read by a single command byte and
returned framed `[echo][len][data][cksum]` like any other reply.

**Custom Functions** — read `D5 D7 D9 D1`. Of these, **`D1` is the active/current
settings bank and `D5`/`D7`/`D9` are the three alternate banks** the camera lets the
user switch between. `D5/D7/D9` are 10 data bytes; `D1` is 11 — the active bank
carries one extra trailing byte (`0x02` in our baseline) whose meaning is not yet
known (bank id? C.Fn 19? checksum?). Writable: the write command is the read
command + 1
(`D5→D6, D7→D8, D9→DA, D1→D2`), via the same `cmd → echo → [len][data][cksum] →
01` transaction as the clock, written in the order `D2 D6 D8 DA`. One quirk: the
`D8`/`DA` write blocks append a constant `0x41` byte that the `D7`/`D9` reads do
not return, so a write is *not* a naive echo of the read — the tool reproduces
this exactly, so a backup→restore round-trips byte-for-byte.

**Personal Functions** — read `D3 DD C5 C6 C1 C3 C4 CB CC CA C7 C8 C0 CD CF CE D1`
(register lengths 1–5 bytes; e.g. `C5`→`3F`, `C6`→`F0`, `CD`→`10 82 20 08 4A`).

**P.Fn write commands (captured 2026-06-15, `data/usbmon-read-write-pfn.pcapng`).**
Each read register has a write twin: the **D-registers add 1** (`D3→D4`, `DD→DE`),
the **C-registers subtract `0x10`** (`C0→B0`, `C1→B1`, … `CF→BF`). Each write is
the clock/C.Fn transaction — `cmd → echo → [len][data][cksum] → 01`. ES-E1 writes
the **whole 16-register block** back in read order (even unchanged registers);
`D1` (the C.Fn active bank) is *not* written. Enabling/disabling a P.Fn flips its
bit (same byte-3-first layout as the read enable mask) in **`D3` always, and in
`DD` unless the function is a "-0" one (P.Fn-16 or P.Fn-21)** — `DD` is a parallel
enable mask covering only the "-1" functions. (`DD` byte 4 = `0x15` is a constant,
not an enable bit.) Confirmed two ways: changing P.Fn-12 (a "-1" func)
Standard→Slightly fast moved `CB 40→20`, `D3[2] 80→88`, `DD[2] 00→08`; toggling
P.Fn-16 (a "-0" func) off moved only `D3[2] 80→00` with `DD` unchanged. So our
`apply`/`write-pfn` read the block, compute the new value register(s) + `D3`/`DD`
enable bits per this rule, and write back **only the registers that differ**
(selective — fewer EEPROM writes, and it avoids the whole-16-block write the
camera rejects on its last register). Each register written is byte-exact to
ES-E1's; we just send a subset. (ES-E1 itself rewrites all 16 every time.)

**C.Fn value encoding — fully solved (validated 2026-06-14)** against two
ground-truth states (baseline C.Fn 0=1,2=1,16=1,17=1,18=2; then C.Fn 0→0, 19→5).
**Every value is one-hot** (`1 << value`), same style as exposure mode `fr[13]`.
The 11 bytes of `D1` map to all 20 functions:

| C.Fn | location in the data field | encoding |
|---|---|---|
| **0** | byte 10, low nibble (active bank `D1` only) | one-hot nibble |
| **1–18** | bytes 0–8, **low-nibble-first**, slot = C.Fn−1 | one-hot nibble (≤ value 3) |
| **19** | byte 9, the whole byte | one-hot 8-bit, values 0–5 → bits 0–5 |

- C.Fn 18=2 reads as `4` not `3` ⇒ one-hot, not linear. C.Fn 19=0→`0x01`, 5→`0x20`.
  C.Fn 0=1→`2`, 0→`1`.
- **C.Fn 0 is a body-level setting stored only in the active register's extra
  11th byte** — the three switchable banks `D5/D7/D9` are 10 bytes and don't carry
  it. That is why ES-E1 (which walks the banks) never shows C.Fn 0 even though the
  camera body exposes it.
- Loose end: byte 10 **high** nibble — always `0` so far, unknown/reserved.

The encoding is confirmed; `decode-cfn` decodes this table.

**P.Fn value encoding — fully solved (2026-06-15)** by eight calibration captures
(`read-pfn`, change one P.Fn, `read-pfn`, `diff-fn`), each cross-checked against
the ES-E1 *Combination* tab. `decode-pfn` implements all of the below.

- **Enable bitmask = `D3`** (4 bytes, byte-3-first): P.Fn-N enabled = byte
  `3-(N-1)//8`, bit `(N-1)%8`. So byte 3 holds P.Fn-1..8, byte 2 = 9..16, byte 1 =
  17..24, byte 0 = 25..30. (`DD` mirrors `D3` byte 3 but differs elsewhere —
  purpose unknown; not the enable mask.)
- **On/off P.Fn carry no value byte** — their entire state *is* the enable bit
  (18 of them). **P.Fn-6** is likewise enable-only here; its preset shooting/
  metering modes are stored on the camera body, not in the P.Fn register block.

| P.Fn | register(s) | value encoding |
|---|---|---|
| 1 | `C5` | disable-bitmask of 6 shooting modes; mode *i* (dialog order) = bit `5-i`, **set = allowed** (default `0x3f`) |
| 2 | `C6` | disable-bitmask of 4 metering modes (`0x10`=CWA, `0x20`=Eval, `0x40`=Partial, `0x80`=Spot), set = allowed (default `0xf0`) |
| 3 | `C1` | metering one-hot, same bits as `METERING` (`0x10/0x20/0x40/0x80`) |
| 4 | `C3` | `[max,min]` shutter as Canon 1/8-EV **Tv codes** (30"=`0x10` … 1/8000=`0xA0`) |
| 5 | `C4` | `[max,min]` aperture as 1/8-EV **Av codes** (f/1.0=`0x70`, f/91=`0x08`) |
| 12 | `CB` | AI-Servo sensitivity, Standard-centred: Fast `0x00`, Std `0x40`, Slow `0x80`, ±`0x20`/level |
| 19 | `CC` | booster fps, **`fps = 10 - byte/2`**; Low=`CC[0]` (mirrored in `CC[1]`), High=`CC[2]`, Ultra=`CC[3]` |
| 20 | `CA` | max frames in burst, plain byte (2–36) |
| 23 | `C7`,`C8`,`C0` | three timers (6-sec, 16-sec, post-release); each a **16-bit big-endian** value = `seconds × 16` |
| 25 | `CD` (5 bytes) | CLEAR defaults: `[0]`=shooting (`EXPOSURE` one-hot), `[1]`=AF mode (low bits one-hot: One-Shot `0x02`, AI-Servo `0x04`) + focus point (**bit 7**: set=Auto, clear=Center), `[2]`=metering (`METERING` one-hot), `[3]`=film advance (one-hot: Single `0x08`, Continuous `0x40`, Low-speed-cont `0x01`), `[4]`=`0x4A` constant marker |
| 30 | `CF` | film-ID density, **bit 7**: set=Light, clear=Dark |

P.Fn-25 AF mode has only two options (One-Shot `0x02`, AI Servo `0x04`) — the
EOS-1V has no AI Focus AF. Film advance has **seven** dropdown options; bytes are
known for three (Single `0x08`, Continuous `0x40`, Low-speed `0x01`), the other
four (High-speed / Ultra-high-speed continuous, 10-/2-sec self-timer) are not yet
mapped. Loose ends (never observed changing, no known meaning): reg `CE`=`0x0a`,
`DD` byte 4=`0x15`. The tool reads, annotates (`dump-fn`), diffs (`diff-fn`),
decodes (`decode-cfn`/`decode-pfn`), and (for C.Fn) writes the raw registers with
verified checksums.

### Shooting-data record list ("To Be Recorded")

The ES-E1 "Shooting Data Items to be Recorded" dialog sets which fields the camera
stores per frame (fewer items → more rolls fit in memory). It is **not** a P.Fn —
it's a separate **8-byte bitmask**, read by **`E8`** and written by **`E9`**
(read+1). `E8` is already one of our `SETUP_SEQUENCE` queries, so the tool fetches
this block on every connect (currently discarded). A write also issues a companion
1-byte `E7` (`0x20` observed, unchanged) — probably the "Data Processing" tab
value; unconfirmed. Captured 2026-06-15 in `data/usbmon-read-write-tbr.pcapng`.

The bitmask is **per data-record field, not per UI checkbox**: deselecting 2 items
(Bulb exposure time, Focusing point selection) cleared 3 bits
(`ff ff 0c 3f 00 08 00 3f` → `ff ff 00 3f 00 00 00 3f`: byte 2 bits 2&3, byte 5
bit 3), and the all-on block has ~31 bits set for ~16 checkboxes. Per-item bit
mapping needs single-checkbox calibration captures (same method as P.Fn). This is
why bulb-duration was blank in earlier exports — its record item was unchecked.

**This same 8-byte mask is stamped into every film's header at `hd[9:17]`,** so each
downloaded roll carries the layout it was shot under — that is what makes the
per-frame record layout compositional (see §5, which maps the full bit↔field
table). Every bit this mask exposes is now accounted for at the record level.

---

## 5. Camera data — record structure & decoding

### Film header (reply to `0xE3`), 33-byte data field `hd[]`
- `hd[0:3]` = `01 90 40` block tag.
- `hd[5:7]` = 3-digit film number, **BCD** (`02 18` → "218").
- `hd[7]`   = 2-digit user prefix, **BCD** (`0x26` → "26"). Film ID = `26-218`.
- `hd[18]`  = DX-read ISO (Sv byte; `0xF0` = no DX code on the cassette).
- `hd[19:25]` = film-loaded date/time, **BCD** `YY MM DD HH MM SS`.

### Frame record (reply to `0xE4`), 33-byte data field `fr[]`
- `fr[0:3]` = `01 81 seq` block header (`seq` = frame number).
- `fr[3:5]` = **focal length, 2-byte big-endian, mm** (`0xFFFF` = no lens; lenses
  > 255 mm need the high byte — a single-byte read is wrong for those).
- `fr[5]`  = lens max aperture: `f = 2**(b/8)` (4 counts/stop).
- `fr[6]`  = **Tv** shutter: `Tv_apex = (b−20)/4`, `T = 2**(−Tv_apex)` (`0x14` = 1 s).
- `fr[7]`  = **Av** aperture: `f = 2**(b/8)` (`0x18` = f/8.0).
- `fr[8]`  = **taking ISO** (Sv): `ISO = 50·2**((b−0x40)/8)` (8 counts/stop).
- `fr[9]`  = **exposure compensation** (signed, eighths of a stop): `0x08` = +1.0,
  `0xF8` = −1.0, `0x05` = +0.7. ES-E1 blanks it in Manual and Bulb.
- `fr[10]` = **flash exposure compensation** (same signed-eighths encoding).
- `fr[11]` = flash mode: bit `0x08` = flash fired; among fired frames the `0xC0`
  bits mean **E-TTL** (EX flash), otherwise **TTL autoflash**. Seen: `0x02`/`0xC1`
  OFF, `0x0A` TTL autoflash, `0xC9` E-TTL.
- `fr[12]` = metering (high nibble): `0x10` Center Averaging, `0x20` Evaluative,
  `0x40` Partial, `0x80` Spot.
- `fr[13]` = exposure mode (mask `0xFC`, one-hot in bits 2–7): `0x04` Bulb, `0x08`
  Depth-of-field AE, `0x10` Program, `0x20` Tv-pri, `0x40` Av-pri, `0x80` Manual.
  Low 2 bits are a burst counter. A Bulb frame also carries `fr[6]` (Tv) = `0xF0`,
  which ES-E1 shows as a blank shutter speed.
- `fr[14]` = film advance (mask `0x7F`, one-hot): `0x08` Single, `0x04`
  Ultra-high-speed continuous, `0x40` Continuous (body only), `0x10` 2-s timer,
  `0x20` 10-s timer. Bit `0x80` flags a **multiple-exposure** continuation record.
- `fr[15]` = AF mode (mask `0xBF`): `0x02` One-Shot, `0x04` AI Servo, `0x12` Manual
  focus. (Bit `0x40` is an unrelated flag — mask it off.)
The offsets above (`fr[3]` focal … `fr[24:30]` battery date, `fr[30:33]` padding)
are the **baseline layout**. They are not fixed — see the compositional model
below. `fr[23]`'s `0x4A` is the **Focusing point** record item, and the shot and
battery dates are `fr[17:23]` / `fr[24:30]` **only** in the baseline arrangement.

**The record layout is compositional.** The per-frame record is the enabled
"shooting data items to be recorded" concatenated in a fixed canonical order
(fields are never reordered), each a fixed size, then `0xFF` padding — there is
**no fixed prefix**. The per-film items bitmask `hd[9:17]` (same 8-byte format as
the global `E8` block) says which items are present. The bit↔field map was derived
and reproduces every mask we hold with **zero unexplained bits** — all-off
`c0 09 00 00 00 00 00 00` (only the mandatory Shooting mode + a pad byte survive;
`26-356.CSV` byte-exact), baseline, all-on, and both knob variants — plus both
hardware anchors. The rule:

> the mask reads **MSB-first within each byte, bytes `hd[9]..hd[16]` in record
> order**, and **each field occupies exactly (its byte-size) consecutive bits**.

So a field is present iff its bit is set, and its offset in `fr[]` =
`3 + Σ(sizes of present fields before it)`. `frame_layout(mask)` returns this;
`FRAME_FIELDS` is the table:

These are the 18 checkboxes of the ES-E1 "Shooting Data Items to be Recorded"
dialog, plus two mandatory items (Shooting mode; a reserved `0x00` byte). ES-E1
grays out checkboxes once a per-frame **storage budget** is spent (its "Recordable
film rolls remaining" tracks total record size), so not every item can be enabled
at once — there is no reachable "all items on" mask. That's fine: the decoder
computes offsets per-mask and never needs a maximal capture.

| Item (ES-E1 dialog) | Mask bit | Size | Item (ES-E1 dialog) | Mask bit | Size |
|---|---|---|---|---|---|
| Focal length | `hd9` b5 | 2 | Shooting mode **(mandatory)** | `hd10` b3 | 1 |
| Maximum aperture | `hd9` b3 | 1 | Film advance mode | `hd10` b2 | 1 |
| Shutter speed (Tv) | `hd9` b2 | 1 | AF mode | `hd10` b1 | 1 |
| Aperture (Av) | `hd9` b1 | 1 | (reserved, `0x00`) **(mandatory)** | `hd10` b0 | 1 |
| Manually-set ISO | `hd9` b0 | 1 | Bulb exposure time | `hd11` b3 | 2 |
| Exposure comp | `hd10` b7 | 1 | Date `YY MM DD` | `hd12` b5 | 3 |
| Flash exp comp | `hd10` b6 | 1 | Time `HH MM SS` | `hd12` b2 | 3 |
| Flash mode | `hd10` b5 | 1 | **Custom Function settings** | `hd13` b6 | **11** |
| Metering mode | `hd10` b4 | 1 | Focusing point achieving focus (`0x4A`) | `hd14` b3 | 1 |
| | | | **Focusing point selection** | `hd15` b6 | **7** |
| | | | Battery-loaded date | `hd16` b5 | 3 |
| | | | Battery-loaded time | `hd16` b2 | 3 |

(`hd9` bits 7,6 are two mandatory **header**-level items, not part of the frame
record.) Notes on the non-obvious ones, all ground-truthed against the ES-E1 dialog
+ CSVs: **Date and Time are separate checkboxes** (`hd12` 5-3 / 2-0) — roll 26-357
recorded time but not date; **Battery-loaded date and time** is a single checkbox
(6 bytes, `hd16`). Three items are recorded but **not exported by ES-E1's CSV**, so
we identify them and print/skip their raw bytes without interpreting (rule #1):
**Custom Function settings** — one checkbox, **11 bytes** spanning two mask bytes
(`hd13` b6 → `hd14` b4; constant `21 11 11 11 11 11 11 21 42 01 02` on roll 26-358),
counted for offsets and skipped in output; **Focusing point achieving focus** — 1
byte, an AF-point code that varies per frame (`0x4A`, `0x8A`, `0xFF`=none…), printed
raw; **Focusing point selection** — **7 bytes** (`hd15`, a point bitmap + selected-
point code, roll 26-360), printed raw. The two focus items are independent
checkboxes (both on in 26-360). The AF-mode enum is the low 6 bits; bits `0x40`/
`0x80` are flags ES-E1 ignores (so `0xC2` reads One-Shot AF, not `?(0x82)`).

Because **every set bit is exactly one recorded byte**, a field's offset is just
`3 + (count of set bits before it)`, so known fields decode correctly even when an
item we can't name sits among them. Three guards, all tested: `_unknown` (a set bit
not in the table → NOTE + omit, known fields still shown), `_untrusted` (record
doesn't match the mask, i.e. `bits≠bytes` or truncated → dates `?(layout)`, warn),
implausible-BCD date/time → `?(layout)`. Confirmed byte-exact vs ES-E1 on baseline
(473 frames), all-off 26-356, mixed-prefix 26-357, 26-358, 26-359 (achieving focus),
and 26-360 (both focus items) — `tests/test_layout.py`. **Every dialog checkbox is
now identified.** `read-items` prints the whole ON/off table for any mask (live or
from a dump).

**Frame numbering & multiple exposure:** the frame number is `fr[2]`, not a running
index. A multiple exposure is stored as several records sharing one `fr[2]` value
(the continuation records also set `0x80` on `fr[14]`); ES-E1 lists them as repeats
of the same frame number with "Multiple exposure = ON".

### Display conventions (to match ES-E1 exactly)
- Tv/Av/ISO are **snapped to Canon's standard 1/3-stop ladders**.
- Shutter: fast speeds shown as `1/N`; speeds ≥ ~0.3 s shown as **decimal seconds**
  (e.g. `0.5`, not `1/2`).
- **ISO (DX) vs ISO (M):** ISO(DX) = decode(`hd[18]`) when a DX code is present.
  `fr[8]` is always the taking ISO; show it under **ISO(M)** whenever there is no DX
  code OR the taking ISO differs from the DX code (manual override).

---

## 6. Missing / not-yet-confirmed (to make the tool complete)

Across 30 ground-truth rolls the decode is now field-exact. The only remaining gap:

- **Bulb exposure time:** the ES-E1 column exists but was blank even in the Windows
  export for the bulb frames captured (short bulb exposures), so the byte — if any —
  is unlocated. A long, timed bulb exposure would be needed to find it.
- A few rare enums have still never appeared (so the tool would print `?(0xNN)`):
  the continuous-with-booster vs low/high-speed continuous drive variants. The
  EOS-1V's only AF modes are One-Shot, AI Servo, and Manual focus (`AFMODE`) —
  there is **no** "AI Focus AF" (a consumer-body feature); an earlier note listing
  it was a wrong assumption and has been removed. *(RESOLVED: `fr[23]`/`0x4A` is
  not mystery padding — it's the Focusing-point-selection record item gated by
  `hd[14] & 0x08`; see the variable-layout note in §5. The other items-mask fields
  beyond the two confirmed knobs still need calibration — one all-items download.)*
- **Bulb exposure time — likely lead:** the 2-byte field that `hd[11] & 0x0c`
  inserts before the shot date is almost certainly this (the baseline mask had that
  item OFF, which is why the column was always blank). A bulb frame shot with the
  item enabled should reveal it.

Everything else — including exposure and flash compensation, all metering modes
(incl. Center Averaging), all drive modes (incl. ultra-high-speed), Bulb/DEP/Av/Tv/
Manual/Program modes, multiple exposure, lenses over 255 mm, slow shutter speeds,
and the set-clock and erase-all write paths — is confirmed on real hardware. On
Linux the device needs root or a udev rule for `04A9:3040`.
