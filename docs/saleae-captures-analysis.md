# Saleae Logic Captures — Raw Serial Analysis

Analyzed June 2026. Supersedes `../saleae-decode.txt`.

## Captures

| File | Type | Channels | Rate | Duration |
|------|------|----------|------|----------|
| `eos1v-serial-comms.sal` | Analog | 2 (A0 TX, A1 RX) | 31.25 kSa/s | 7.58 s |
| `session-with-windows.sal` | Digital | 2 (Ch0 TX, Ch1 RX) | 1 MSa/s | ~full session |

Wiring: **A0/Ch0 = host→camera (TX), A1/Ch1 = camera→host (RX)**.

## Serial Parameters (Confirmed)

**9600 baud, 8 data bits, no parity, 1 stop bit (9600 8N1)**

Determined from `eos1v-serial-comms.sal` using pulse-width histogram: shortest pulses are ~96–104 µs, which is one bit period at 9600 baud (1/9600 = 104.17 µs). All pulse widths are multiples of ~104 µs. Manual Python decoder produced 0 framing errors on Ch0 at 9600 8N1.

## Critical Finding: MSB-First Bit Ordering

**The camera transmits and expects bytes MSB-first on the serial wire.** This is non-standard for RS-232 (which normally uses LSB-first), but is the bit order used throughout the Canon EOS-1V protocol.

Evidence: Saleae Logic 2's default async UART decoder uses LSB-first. Decoding both captures in Logic 2's **MSB** mode produces byte values that exactly match the protocol byte values documented in `EOS-1V-protocol-notes.md`:

- MSB decode of session 1 TX commands: `D3 DD C5 C6 C1 C3 C4 CB CC CA C7 C8 C0 CD CF CE D1` → exact match to the P.Fn read register sequence in the docs
- MSB decode of session 2 TX commands: `D5 D7 D9 D1` → exact match to C.Fn read registers

When analyzing raw serial captures in Logic 2:
- **Use MSB bit order** to get correct protocol byte values
- **Set threshold to 3.3 V** (works correctly for 5 V signals: HIGH >> 3.3 V, LOW << 3.3 V)
- **Signal inversion: OFF** (Logic 2 setting: `0`, not `false`)

Logic 2 exports 0x00 bytes as the two-character string `\0`, not as a framing error.

### Byte Mapping Reference

The table below maps LSB-decoded values (what Logic 2 shows by default) to the MSB-decoded values (actual protocol bytes). Use this when reading captures decoded with the wrong bit order.

| LSB decode | MSB decode (actual) | Role |
|------------|---------------------|------|
| `0xFF` | `0xFF` | Sync/reset (palindrome) |
| `0x2F` | `0xF4` | Sync/flow byte |
| `0x6F` | `0xF6` | Session-start / identify command |
| `0x4F` | `0xF2` | Session teardown |
| `0x2E` | `0xF1` | Continue/separator (appears very frequently) |
| `0xCB` | `0xD3` | P.Fn register 1 |
| `0xBB` | `0xDD` | P.Fn register 2 |
| `0xA3` | `0xC5` | P.Fn register 3 |
| `0x63` | `0xC6` | P.Fn register 4 |
| `0xAB` | `0xD5` | C.Fn register 1 |
| `0xEB` | `0xD7` | C.Fn register 2 |
| `0xE7` | `0xE7` | Write command (palindrome; same in both) |

---

## Capture 1: `eos1v-serial-comms.sal`

Short analog-only capture (analog, 31.25 kSa/s). Used only for baud rate confirmation.

- Ch0 decoded to **21 bytes**, 0 framing errors at 9600 8N1
- All 21 bytes have MSB=1 and LSB=0 (odd-parity-like pattern); consistent with the protocol's heavy use of bytes in the `0xC0–0xFF` range (all have MSB=1 in MSB-first notation)
- Too short (< 8 s) to contain a full P.Fn or C.Fn session

---

## Capture 2: `session-with-windows.sal`

Complete Windows software session. Digital capture at 1 MSa/s (104 samples/bit — clean decode).

**Summary:**
- Ch0 (host→camera): **69 bytes**
- Ch1 (camera→host): **311 bytes**
- 5 sub-sessions detected

### Session Structure

```
Session 1: P.Fn read (17 registers × ~18 RX bytes each)
Session 2: C.Fn read (4 registers × ~12 RX bytes each)
Session 3: Camera status / header block read
Session 4: Write operation (one setting changed)
Session 5: Disconnect
```

### Handshake Pattern (repeated before each session)

Each session opens with a wake/handshake:

```
TX: FF          ← sync/reset
RX: F4          ← camera: sync/flow byte (~70 ms later)
TX: F4          ← host: echo F4
TX: F6          ← host: session-start / identify command
RX: [16 bytes]  ← camera: ID block (see below)
TX: F1          ← host: continue
RX: F4          ← camera: sync/flow
TX: F4          ← host: echo F4
RX: [5 bytes]   ← camera: version/capability block (see below)
TX: F4          ← host: echo F4
```

All subsequent reads use: `TX: <register>` → `RX: <echo> <len> <data...> <checksum>` → `TX: F4` (echo camera's trailing F4).

### Camera ID Block (16 bytes, after F6, always identical)

Received after the `F6` session-start command, before the host sends `F1`.

MSB decode: `F1 ?? ?? A0 E9 22 ?? ?? 38 00 20 00 00 00 28 F1`

The leading and trailing `F1` bytes are framing. Bytes 4–5 (`E9 22`) and bytes 9–14 (`38 00 20 00 00 00 28`) are stable camera-identity fields. The exact semantics are not yet decoded, but these bytes are **identical across all five session-open events in the Windows capture**, confirming they are fixed camera hardware/firmware identifiers.

### Camera Version/Capability Block (5 bytes, after F1 continue)

Received after the host sends `F1`. Always: `F1 F1 F1 34 4F`

The leading `F1` bytes are framing. `34 4F` is likely a firmware version or capability word.

### Session 1: P.Fn Read

TX sends P.Fn read registers in documented sequence:

```
TX (commands, MSB): D3 DD C5 C6 C1 C3 C4 CB CC CA C7 C8 C0 CD CF CE D1
```

This matches the P.Fn read sequence in `EOS-1V-protocol-notes.md` exactly. Camera responds to each with echo + data block.

### Session 2: C.Fn Read

```
TX (commands, MSB): D5 D7 D9 D1
```

Matches the C.Fn read sequence.

### Session 3: Status / Header Read

```
TX (commands, MSB): E8 FC E1
```

Camera responds with data blocks for each. `E8` and `FC` and `E1` are not yet documented separately; this may be a camera-status header read that the Windows software does before writing.

### Session 4: Write Operation (NEW — not in prior docs)

**The Windows software changes a single camera setting** in this session.

```
TX: E7          ← write command
RX: E7          ← camera echoes command
TX: [8-byte write payload]
...
TX: E1          ← unknown command (possibly write-commit or CRC check)
RX: E1 F1 00 F1 F1
...
TX: E1          ← possibly repeated for confirmation
RX: E1 F1 00 F1 F1
```

**Write payload (8 bytes):** `FF FF 30 FC 00 00 00 FC`

This 8-byte block was read back identically in session 3, except byte[2] was `0x00` before the write and `0x30` after. The Windows software changed byte[2] from `0x00` to `0x30` — this is the only difference, indicating one setting was toggled.

The `E7` write command and the 8-byte payload structure are **not previously documented** in the protocol notes.

### Session 5: Disconnect

```
TX: F2          ← disconnect / exit data mode
RX: F4          ← camera sends sync/flow (acknowledging before closing?)
TX: F4          ← host echoes F4
TX: F2          ← host sends F2 again (confirms teardown)
TX: 00          ← null terminator
```

Note: the protocol notes say `0xF2` exits data mode and re-entry requires another button press. The capture shows `F2` sent **twice**, with a `F4` exchange in between — the second `F2` appears to be the authoritative teardown command.

---

## Raw Decoded Data

### Ch0 (host→camera), MSB decode, 69 bytes

```
FF F4 F6 F1 F4 F6
FF F4 F1
D3 DD C5 C6 C1 C3 C4 CB CC CA C7 C8 C0 CD CF CE D1
F4 F6
FF F4 F1
D5 D7 D9 D1
F4 F6
FF F4 F1
E8 FC E1
FF F4 F1
E7 [write-data: FF FF 30 FC 00 00 00 FC]
E1 E1
F4 F6
F2 F4 F2 00
```

### Ch0 (host→camera), LSB decode (Logic 2 default), 69 bytes

```
FF 2F 6F 2E 2F 6F FF 2F 2E CB BB A3 63 2E C3 23 D3 33 53 E3 2E 2E B3 F3 73
2E 2F 6F FF 2F 2E AB EB 2E 2E 2F 6F FF 2F 2E 2E 3F 2E FF 2F 2E E7 2E 2E 2E
2E 2E FF FF 30 FC 00 00 00 FC 2E 2E 2E 2F 6F 4F 2F 4F 00
```

### Ch1 (camera→host), MSB decode, 311 bytes

(Full sequence includes echo + length + data + checksum for each command; see the Windows session .sal capture for timing detail. Key structural bytes match EOS-1V-protocol-notes.md reply format: `[echo][len][data...][checksum]`.)

---

## Implications for Software Development

1. **No code changes needed for bit ordering** — the Python tool and all protocol documentation already use MSB-first byte values (0xF4, 0xF1, etc.). The MSB-first ordering is at the physical layer; software sends bytes normally.

2. **Write command `0xE7`** is newly confirmed. The write payload is 8 bytes. The pre-write read sequence (`E8 FC E1`) should be performed first to read the current state, then modify the relevant byte(s) before writing.

3. **Disconnect sequence**: send `0xF2`, wait for `0xF4` from camera, echo `0xF4`, send `0xF2` again, send `0x00`.

4. **Session open sequence** is: `0xFF` sync → wait for `0xF4` → echo `0xF4` → send `0xF6` → receive 16-byte ID block → send `0xF1` → receive 5-byte version block.
