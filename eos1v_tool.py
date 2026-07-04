#! /Users/eric/claude-code/.venv/bin/python3
"""
eos1v_tool.py  -  Talk to a Canon EOS-1V through the Canon EOS USB Cable
                  (ES-E1 cable, USB VID:PID 04a9:3040)

    download  out.csv [raw.bin]   # live: read camera -> CSV
    decode    raw.bin  out.csv     # offline: decode a saved dump to csv
    set-clock [now | 2026-06-12T13:51:53]
    erase-all
    read-cfn  [backup.txt]         # read Custom Functions (raw registers)
    read-pfn  [backup.txt]         # read Personal Functions (raw registers)
    write-cfn  backup.txt          # restore Custom Functions (writes only changed regs)
    write-pfn  backup.txt          # restore Personal Functions (writes only changed regs)
    dump-fn   backup.txt           # annotated byte/nibble/bit layout of a backup
    diff-fn   a.txt b.txt          # diff two backups (calibration helper)
    decode-cfn backup.txt          # decode C.Fn registers to named settings
    decode-pfn backup.txt          # decode P.Fn registers to named settings
    dump-settings [out.txt]        # whole-camera settings -> editable file (live)
                                   #   offline: dump-settings --from cfn.txt pfn.txt [out]
    check settings.txt             # validate an edited settings file + show the diff
                                   #   offline: check settings.txt --against cfn.txt pfn.txt
    apply settings.txt             # write the changed settings to the camera (gated)
                                   #   dry run: apply settings.txt --against cfn.txt pfn.txt
    probe

  Transport: default talks to the Canon ES-E1 USB cable (04a9:3040). To use a
  raw serial cable instead (e.g. an FTDI wired to the camera's inverted TTL
  levels), add  --serial /dev/ttyUSB0  (or /dev/cu.usbserial-XXXX on macOS) to
  any live command, or set EOS1V_SERIAL.  Needs pyserial; 9600 8N1.
"""
import sys, struct, csv, time

SETUP_SEQUENCE = [0xf6, 0xf1, 0xe8, 0xfc, 0xe1]   # post-wake status/setup queries
TEARDOWN      = [0xf2, 0xf2]                       # best-effort close

# Custom Functions (C.Fn): registers read by these commands, written by read+1.
CFN_READ = [0xd5, 0xd7, 0xd9, 0xd1]
# read-command -> (write-command, trailing bytes the write block adds vs the read)
CFN_WRITE = {0xd1: (0xd2, b''), 0xd5: (0xd6, b''),
             0xd7: (0xd8, b'\x41'), 0xd9: (0xda, b'\x41')}
CFN_WRITE_ORDER = [0xd1, 0xd5, 0xd7, 0xd9]         # order ES-E1 writes them
# Personal Functions (P.Fn): registers read by these commands.
PFN_READ = [0xd3, 0xdd, 0xc5, 0xc6, 0xc1, 0xc3, 0xc4, 0xcb, 0xcc, 0xca,
            0xc7, 0xc8, 0xc0, 0xcd, 0xcf, 0xce, 0xd1]
# P.Fn write commands (captured 2026-06-15, data/usbmon-read-write-pfn.pcapng):
# the D-registers are read+1 (d3->d4, dd->de), the C-registers are read-0x10
# (c5->b5 ... cf->bf). ES-E1 writes the whole block in read order; d1 (the C.Fn
# active bank, last in PFN_READ) is never written.
PFN_WRITE_ORDER = PFN_READ[:-1]                    # writable regs, ES-E1's order
PFN_WRITE = {rc: (rc + 1 if rc in (0xd3, 0xdd) else rc - 0x10)
             for rc in PFN_WRITE_ORDER}
# For a future selective `apply`: enabling/disabling P.Fn-N flips bit (N-1)%8 of
# byte 3-(N-1)//8 in D3 always, and in DD too EXCEPT for these "-0" functions.
# (DD byte 4 = 0x15 is a constant marker, not an enable bit.)
PFN_DD_SKIP = {16, 21}
CMD_FILM   = 0xe3
CMD_FRAME  = 0xe4

# ---- Custom Function decode table ----------------------------------------
# The BYTE ENCODING below is hardware-confirmed (validated 2026-06-14 against two
# known camera states): the active bank D1 holds all 20 functions as one-hot
# values -- C.Fn 1..18 in bytes 0..8 (low nibble first, slot = C.Fn-1), C.Fn 19
# in byte 9 (full 8-bit one-hot, 6 options), and C.Fn 0 in byte 10's low nibble
# (active bank only; the switchable banks D5/D7/D9 are 10 bytes and omit it).
#
# NAMES and OPTION LABELS are the *literal* strings from the ES-E1 Windows binary
# (Remote.exe), extracted 2026-06-14 -- the authoritative source. Exception: C.Fn-0
# is not shown by ES-E1 (it's hidden), so its name is the manual's and its option 1
# list is the manual's readable form (the binary stores the screen II/III glyphs
# oddly). {C.Fn number: (name, {value: label})}.
CFN_FUNCS = {
    0:  ("Focusing screen characteristics",            # name from manual (not in binary)
         {0: "Ec-N,Ec-R", 1: "Ec-A,B,C,CII,CIII,D,H,I,L"}),
    1:  ("Auto film rewind mode",
         {0: "High-speed auto rewind",
          1: "High-speed rewind with film rewind button",
          2: "Silent auto rewind",
          3: "Silent rewind with film rewind button"}),
    2:  ("Film leader position after film rewind",
         {0: "Rewind film leader into the cartridge",
          1: "Leave film leader outside the cartridge"}),
    3:  ("DX-coded film speed setting method",
         {0: "Automatic", 1: "Manual"}),
    4:  ("AF activation/AE lock",
         {0: "AE and AF with shutter button pressed halfway / AE lock with AE lock button",
          1: "AE and AF with AE lock button / AE lock with shutter button pressed halfway",
          2: "AF with shutter button / AF lock with AE lock button",
          3: "AE and AF with AE lock button / No AE lock"}),
    5:  ("Manual Tv/Av settings",
         {0: "Use the Main Dial to set the shutter speed and the Quick Control Dial to set the aperture",
          1: "Use the Main Dial to set the aperture and the Quick Control Dial to set the shutter speed",
          2: "Same as (0). The aperture can be set while the lens is detached.",
          3: "Same as (1). The aperture can be set while the lens is detached."}),
    6:  ("Exposure level increments",
         {0: "Shutter speed and aperture increments: 1/3 stop / Exposure compensation: 1/3 stop",
          1: "Shutter speed and aperture increments: 1 stop / Exposure compensation: 1/3 stop",
          2: "Shutter speed and aperture increments: 1/2 stop / Exposure compensation: 1/2 stop"}),
    7:  ("USM lens electronic manual focusing",
         {0: "Enabled after One-Shot AF achieved (With C.Fn-4-1/3, also enabled before achieving focus)",
          1: "Disabled after One-Shot AF achieved (With C.Fn-4-1/3, enabled before achieving focus)",
          2: "Disabled after One-Shot AF achieved (Disabled before achieving focus even with C.Fn-4-1/3)"}),
    8:  ("Frame counter sequence",
         {0: "Count up", 1: "Count down",
          2: "F or 9 - 0 displayed by frame counter in viewfinder (same as with EOS-1N)"}),
    9:  ("AEB sequence and auto cancellation",
         {0: "0 , -- , + /Cancel AEB", 1: "0 , -- , + /Continue AEB",
          2: "-- , 0 , + /Cancel AEB", 3: "-- , 0 , + /Continue AEB"}),
    10: ("Focusing point flashing mode",
         {0: "Enabled", 1: "Disabled",
          2: "Enabled (No dimmed flashing)", 3: "Bright flashing"}),
    11: ('Focusing point selection method ("H": Horizontal focusing point; "V": Vertical focusing point)',
         {0: "Focusing point selector + Main Dial (H), or + Quick Control Dial (V)",
          1: "Exposure compensation button + Main Dial (H), or + Quick Control Dial (V)",
          2: "Horizontal focusing point selection with Quick Control Dial only",
          3: "FE lock button + Main Dial (H), or +  Quick Control Dial for (V)"}),
    12: ("Mirror lockup",
         {0: "Disabled", 1: "Enabled"}),
    13: ("Focusing point selection limit and spot metering linkage",
         {0: "45 / Center focusing point-linked spot metering",
          1: "11 / Active focusing point-linked spot metering",
          2: "11 / Center focusing point-linked spot metering",
          3: "9 / Active focusing point-linked spot metering"}),
    14: ("Automatic reduction of fill flash output",
         {0: "Enabled", 1: "Disabled"}),
    15: ("Shutter curtain synchronization",
         {0: "1st-curtain synchronization", 1: "2nd-curtain synchronization"}),
    16: ("Safety shift",
         {0: "Disabled", 1: "Enabled"}),
    17: ("Focusing point activation area",
         {0: "Standard",
          1: "Expanded to 7 focusing points with adjacent focusing points",
          2: "Expanded automatically to 7 or 13 focusing points depending on the shooting situation"}),
    18: ("Switchover to registered focusing point",
         {0: "Use Assist button + Focusing point selector",
          1: "Use Assist button",
          2: "Switchover only while pressing Assist button"}),
    19: ("Lens AF stop button function switching",
         {0: "AF stop", 1: "AF start",
          2: "AE lock during metering",
          3: "Automatic selection among 45 points while pressed",
          4: "Toggle between One-Shot AF and AI Servo AF",
          5: "Image Stabilizer operation"}),
}

# ---- Personal Function scaffold ------------------------------------------
# UNLIKE CFN_FUNCS, NONE OF THIS IS CONFIRMED. The byte encoding for P.Fn has
# not been calibrated at all yet (no decoder exists), and the EOS-1V instruction
# manual only gives each function's name/purpose, not a clean per-value table the
# way it does for C.Fn -- the detailed option values came from the ES-E1 UI we're
# replacing. So both halves below are provisional:
#   * names are transcribed from the manual (P.Fn-0..30) and are reliable;
#   * the option dicts are best-effort: the clearly binary on/off functions are
#     filled with {0: "Off (default)", 1: "On"} as a *placeholder* (the real
#     byte values and even the on/off direction are unverified), and the
#     multi-value / range functions are left as {} with a comment, to be pinned
#     by calibration the same way C.Fn was. Edit freely.
# Option labels for P.Fn 1,2,3,12 (and the value ranges noted for 4,5,19,20,23)
# are transcribed from the ES-E1 software manual's "Description of Personal
# Functions" screenshots -- so the *labels and their UI order* are now solid, but
# the byte encoding and which value maps to which byte are still uncalibrated.
# Value keys here are placeholders (UI order) until calibration captures pin them.
# P.Fn-4 / P.Fn-5 are dual-field (two dropdowns each), so their option lists live
# here as ordered scales rather than in PFN_FUNCS's uniform {value: label} shape.
# Strings are as the ES-E1 software displays them. Shutter notation (confirmed by
# a P.Fn-4 screenshot showing "1/6400" and "25\""): a trailing " means seconds,
# and "1/N" is a reciprocal second. The MAX list is all fractions (1/250..1/8000);
# the MIN "1/4".."1/60" prefixes follow the same convention (the screenshot only
# pinned the second-valued "25\""). List order is the UI/dropdown order; the
# byte<->index mapping is still uncalibrated.
PFN4_SHUTTER_MAX = [            # fastest-shutter-speed limit
    "1/250", "1/320", "1/400", "1/500", "1/640", "1/750", "1/800", "1/1000",
    "1/1250", "1/1500", "1/1600", "1/2000", "1/2500", "1/3000", "1/3200",
    "1/4000", "1/5000", "1/6000", "1/6400", "1/8000"]
PFN4_SHUTTER_MIN = [           # slowest-shutter-speed limit
    '30"', '25"', '20"', '15"', '13"', '10"', '8.0"', '6.0"', '5.0"', '4.0"',
    '3.2"', '3.0"', '2.5"', '2.0"', '1.6"', '1.5"', '1.3"', '1.0"', '0.8"',
    '0.7"', '0.6"', '0.5"', '0.4"', '0.3"', "1/4", "1/5", "1/6", "1/8", "1/10",
    "1/13", "1/15", "1/20", "1/25", "1/30", "1/40", "1/45", "1/50", "1/60"]
PFN5_APERTURE_MIN = [          # smallest-aperture limit (largest f-number)
    "1.4", "1.6", "1.8", "2.0", "2.2", "2.5", "2.8", "3.2", "3.5", "4.0", "4.5",
    "5.0", "5.6", "6.3", "6.7", "7.1", "8.0", "9.0", "9.5", "10", "11", "13",
    "14", "16", "18", "19", "20", "22", "25", "27", "29", "32", "36", "38",
    "40", "45", "51", "54", "57", "64", "72", "76", "81", "91"]
PFN5_APERTURE_MAX = [          # largest-aperture limit (smallest f-number)
    "1.0", "1.1", "1.2", "1.4", "1.6", "1.8", "2.0", "2.2", "2.5", "2.8", "3.2",
    "3.5", "4.0", "4.5", "5.0", "5.6", "6.3", "6.7", "7.1", "8.0", "9.0",
    "9.5", "10", "11", "13", "14", "16", "18", "19", "20", "22", "25", "27",
    "29", "32", "36", "38", "40", "45", "51", "54", "57", "64", "72"]

PFN_FUNCS = {
    0:  ("Custom Function group registration", {}),     # the C.Fn bank mechanism
    # P.Fn-1: bitmask -- each checkbox DISABLES that shooting mode (>=1 must stay
    # enabled). Labels/order from ES-E1 manual; bit positions TBD by calibration.
    1:  ("Disable unwanted shooting mode(s)",
         {0: "Program AE", 1: "Shutter-speed-priority AE",
          2: "Aperture-priority AE", 3: "Depth-of-field AE",
          4: "Manual exposure", 5: "Bulb exposure"}),
    # P.Fn-2: bitmask -- each checkbox DISABLES that metering mode.
    2:  ("Disable unwanted metering mode(s)",
         {0: "Evaluative metering", 1: "Partial metering", 2: "Spot metering",
          3: "Centerweighted averaging metering"}),
    # P.Fn-3: radio (enum), UI order. Value keys provisional until calibration.
    3:  ("Metering mode for manual exposure",
         {0: "Evaluative metering", 1: "Partial metering", 2: "Spot metering",
          3: "Centerweighted averaging metering"}),
    4:  ("Max/min shutter speeds to be used", {}),      # PFN4_SHUTTER_MAX/_MIN
    5:  ("Max/min apertures to be used", {}),           # PFN5_APERTURE_MIN/_MAX
    6:  ("Register/switch shooting & metering mode", {}),  # preset, set on camera
    7:  ("Repeat AEB during continuous shooting",
         {0: "Off (default)", 1: "On"}),
    8:  ("AEB only for the first two frames",
         {0: "Off (default)", 1: "On"}),
    9:  ("AEB sequence over/correct/under (for C.Fn-9-2/3)",
         {0: "Off (default)", 1: "On"}),
    10: ("Maintain shift amount for program shift",
         {0: "Off (default)", 1: "On"}),
    11: ("Prevent cancellation of multiple exposures",
         {0: "Off (default)", 1: "On"}),
    # P.Fn-12: 5-level dropdown; all five labels confirmed in the ES-E1 binary
    # (Remote.exe). "Standard" == P.Fn-12 off.
    12: ("AI Servo AF subject-tracking sensitivity",
         {0: "Slow", 1: "Slightly slow", 2: "Standard",
          3: "Slightly fast", 4: "Fast"}),
    13: ("AI Servo continuous shooting per film-advance speed",
         {0: "Off (default)", 1: "On"}),
    14: ("Disable AF lens driving for focus search",
         {0: "Off (default)", 1: "On"}),
    15: ("Disable AF-assist beam",
         {0: "Off (default)", 1: "On"}),
    16: ("Auto-shoot at fixed focus point when achieved",
         {0: "Off (default)", 1: "On"}),
    17: ("Prevent automatic focusing point selection",
         {0: "Off (default)", 1: "On"}),
    18: ("Enable auto AF point selection when C.Fn-11-2 set",
         {0: "Off (default)", 1: "On"}),
    # P.Fn-19: three dropdowns (one per booster advance mode), each a small fps
    # range -- Ultra-high 8-10, High 4-7, Low 1-3 f/sec. Encoding TBD.
    19: ("Continuous speed with Power Drive Booster", {}),
    20: ("Limit number of frames in continuous shooting", {}),  # numeric 2-36
    21: ("Silent (low-speed) film advance after shot",
         {0: "Off (default)", 1: "On"}),
    22: ("Disable shutter release when no film loaded",
         {0: "Off (default)", 1: "On"}),
    # P.Fn-23: THREE numeric second-fields -- "6 sec." activation timer, "16 sec."
    # timer, and post-shutter-release (exposure-retain) timer; each 0-3600s.
    23: ("Function activation timer durations", {}),
    24: ("Keep LCD illumination on during bulb exposures",
         {0: "Off (default)", 1: "On"}),
    # P.Fn-25: composite of 5 dropdowns -- shooting mode, metering mode, film
    # advance mode, AF mode, focusing point selection (center / automatic).
    25: ("Change CLEAR-button default settings", {}),
    26: ("Shorten shutter release time lag",
         {0: "Off (default)", 1: "On"}),
    27: ("Reverse electronic dial direction",
         {0: "Off (default)", 1: "On"}),
    28: ("Prevent exposure compensation with Quick Control Dial",
         {0: "Off (default)", 1: "On"}),
    29: ("Warn when memory nearly full",
         {0: "Off (default)", 1: "On"}),
    # P.Fn-30: radio (UI order Dark, Light). Value keys provisional.
    30: ("Film ID imprinting density",
         {0: "Dark", 1: "Light"}),
}

# (bRequest, wValue, data_bytes)
SERIAL_INIT = [
    (0x01, 0x0000, bytes([0x05,0x04,0x08,0x00,0x00])),
    (0x01, 0x0000, bytes([0x05,0x06,0x08,0x00,0x00])),
    (0x03, 0x0003, b''),
    (0x01, 0x0000, bytes([0x05,0x06,0x08,0x00,0x00])),
    (0x03, 0x0003, b''),
    (0x03, 0x0003, b''),
    (0x01, 0x0000, bytes([0x05,0x06,0x08,0x00,0x00])),
]

#   host -> 0xff ; camera -> 0xf4 ; host -> 0xf4 (echo).  Thereafter the camera
#   periodically emits 0xf4 as a sync byte that the host must echo with 0xf4.
SYNC = 0xf4
ATTN = 0xff

# ============================== transports ==============================
# The camera speaks a framed serial protocol at 9600 8N1. Two ways to reach it,
# both exposing the same two primitives the protocol layer uses:
#   send(raw_bytes)            -- put serial bytes on the wire to the camera
#   recv(timeout_ms) -> bytes  -- whatever serial bytes arrived (b'' if none)
#
#   * UsbBridgeTransport -- the Canon ES-E1 cable (04a9:3040), a USB->serial
#     bridge that wraps every serial payload in its own [n][00][...] container
#     over bulk endpoints and needs 7 vendor control transfers to set up its UART.
#   * SerialTransport -- a plain serial port (e.g. an FTDI cable wired to the
#     camera's inverted-TTL levels). The bytes on the wire ARE the camera
#     protocol: no container, no setup beyond opening the port at 9600 8N1.

class UsbBridgeTransport:
    VID, PID = 0x04a9, 0x3040
    EP_OUT, EP_IN = 0x02, 0x81
    BM_VENDOR_OUT = 0x41          # host->device | vendor | recipient=interface

    def __init__(self, log=lambda m: None):
        import usb.core, usb.util
        self._usb = usb.util
        dev = usb.core.find(idVendor=self.VID, idProduct=self.PID)
        if dev is None:
            raise RuntimeError("Canon EOS USB cable (04a9:3040) not found. "
                               "Is the camera on (in PC-connect mode) and the "
                               "cable connected?  (For a raw serial cable use "
                               "--serial /dev/ttyUSB0.)")
        log("found device 04a9:3040")
        try:
            if dev.is_kernel_driver_active(0):
                log("detaching kernel driver on interface 0")
                dev.detach_kernel_driver(0)
        except Exception as e:
            log(f"  (kernel driver check: {e})")
        try:
            cfg = dev.get_active_configuration()
            already = (cfg.bConfigurationValue == 1)
            log(f"device already configured (#{cfg.bConfigurationValue})"
                if already else "device not yet configured")
        except Exception:
            already = False
        if not already:
            dev.set_configuration(); log("set_configuration done")
        else:
            log("skipping set_configuration (already active)")
        try:
            usb.util.claim_interface(dev, 0); log("claimed interface 0")
        except Exception as e:
            log(f"  (claim_interface: {e})")
        self.dev = dev
        self._serial_open(log)

    def _serial_open(self, log):
        """Replay the captured vendor init that opens the serial bridge."""
        for i, (bReq, wVal, data) in enumerate(SERIAL_INIT):
            n = self.dev.ctrl_transfer(self.BM_VENDOR_OUT, bReq, wVal, 0, data,
                                       timeout=2000)
            log(f"  serial-init {i+1}/{len(SERIAL_INIT)}: bReq=0x{bReq:02x} "
                f"wVal=0x{wVal:04x} data={data.hex()} -> wrote {n} bytes")
        time.sleep(0.05)          # let the UART settle before first command

    def send(self, data):
        """Wrap raw serial bytes in the bridge frame [n][00][...] padded to 64."""
        data = bytes(data)
        frame = bytes([len(data), 0]) + data
        self.dev.write(self.EP_OUT, frame + b'\x00' * (64 - len(frame)),
                       timeout=2000)

    def recv(self, timeout_ms):
        """Read one IN packet and unwrap it to its raw serial payload."""
        try:
            buf = self.dev.read(self.EP_IN, 64, timeout=timeout_ms)
        except Exception:
            return b''
        if buf and buf[0] > 0:
            return bytes(buf[2:2 + buf[0]])
        return b''

    def close(self):
        try: self._usb.release_interface(self.dev, 0)
        except Exception: pass


class SerialTransport:
    """Talk to the camera directly over a raw serial port. The signal-level
    inversion and 3.3V the camera needs are handled in HARDWARE (e.g. an FT232R
    with TXD/RXD inverted) -- UART inversion affects start/stop bits and can't be
    undone per-byte in software, so the bytes here are already clean. The camera
    port is 3-wire (TXD/RXD/GND): there are no modem-control lines, hence no
    hardware flow control to negotiate (DTR/RTS are left at pyserial's defaults on
    unconnected pins)."""
    def __init__(self, port, log=lambda m: None):
        try:
            import serial
        except ImportError:
            raise RuntimeError("pyserial is required for --serial "
                               "(pip install pyserial).")
        self.ser = serial.Serial(port, baudrate=9600, bytesize=8, parity='N',
                                 stopbits=1, timeout=0,
                                 rtscts=False, dsrdtr=False, xonxoff=False)
        log(f"opened serial port {port} at 9600 8N1, no flow control")

    def send(self, data):
        self.ser.write(bytes(data))

    def recv(self, timeout_ms):
        self.ser.timeout = max(timeout_ms / 1000.0, 0.001)
        first = self.ser.read(1)            # block up to timeout for one byte
        if not first:
            return b''
        n = self.ser.in_waiting             # then drain whatever else is ready
        return first + (self.ser.read(n) if n else b'')

    def close(self):
        try: self.ser.close()
        except Exception: pass


# ===================== protocol layer (transport-agnostic) =====================
class EOS1V:
    def __init__(self, port=None, transport=None):
        import os
        self.verbose = os.environ.get('EOS1V_VERBOSE', '') not in ('', '0')
        if transport is not None:                       # injected (e.g. tests)
            self.transport = transport
            return
        if port is None:
            port = os.environ.get('EOS1V_SERIAL') or None
        self.transport = (SerialTransport(port, self._log) if port
                          else UsbBridgeTransport(self._log))

    # --- background reader -------------------------------------------------
    # The Windows driver keeps a read armed on the bulk IN endpoint BEFORE it
    # writes a command; the cable only ACKs the OUT when an IN URB is pending,
    # and the camera's reply arrives tens of ms later. We reproduce that by
    # running a reader thread that continuously drains EP_IN into self._rx.
    def _start_reader(self):
        import threading
        self._rx = bytearray()
    def _start_reader(self):
        import threading
        self._rx = bytearray()        # raw serial bytes from the camera
        self._replies = []            # queue of complete framed replies
        self._syncs = 0               # count of 0xf4 sync bytes seen (for wake)
        self._rxlock = threading.Lock()
        self._reader_run = True
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()

    def _reader_loop(self):
        while self._reader_run:
            data = self.transport.recv(150)   # blocks up to 150ms; b'' if idle
            if data:
                with self._rxlock:
                    self._rx += data
            self._parse_rx()

    def _parse_rx(self):
        """Stream parser. A lone 0xF4 is a sync byte: echo it and drop it.
        Otherwise the stream is framed replies [echo][len][data*len][cksum];
        read each by length (so 0xF4 *inside* data is never mistaken for sync)
        and queue it."""
        to_echo = 0
        with self._rxlock:
            while self._rx:
                if self._rx[0] == SYNC:
                    del self._rx[0]
                    self._syncs += 1
                    to_echo += 1
                    continue
                if len(self._rx) < 2:
                    break                      # need the length byte
                need = self._rx[1] + 3
                if len(self._rx) < need:
                    break                      # wait for the rest of the reply
                self._replies.append(bytes(self._rx[:need]))
                del self._rx[:need]
        for _ in range(to_echo):               # echo each sync (0xf4) back
            try: self._send_cmd(SYNC)
            except Exception: pass

    def _wake(self, attempts=12):
        """Cold-start: send 0xFF; the camera answers with 0xF4 (which the reader
        auto-echoes). Returns True once a sync is seen."""
        for i in range(1, attempts+1):
            with self._rxlock:
                base = self._syncs
            self._send_cmd(ATTN)                 # 0xff "are you there?"
            deadline = time.time() + 1.0
            while time.time() < deadline:
                with self._rxlock:
                    seen = self._syncs > base
                if seen:
                    self._log(f"  wake: camera answered 0xf4 (attempt {i})")
                    return True
                time.sleep(0.01)
            self._log(f"  wake attempt {i}: no 0xf4")
        return False

    def _send_cmd(self, cmd):
        self.transport.send(bytes([cmd]))

    def command(self, cmd, timeout=3.0):
        """Send one command and return its framed reply [echo][len][data][cksum].
        The camera answers a not-ready command with a 0xF4 sync (auto-echoed by
        the reader) and expects a re-send. Resend ONLY when a new sync is seen
        after our send -- never on a plain timeout -- so a merely-slow reply is
        never mistaken for not-ready (which would advance the frame pointer)."""
        with self._rxlock:
            self._replies.clear()
            last_sync = self._syncs
        self._send_cmd(cmd)
        sent_at = time.time()
        deadline = sent_at + timeout
        MIN_RESEND = 0.04                       # genuine not-ready arrives ~70ms;
                                                # a trailing-sync race is <10ms
        while time.time() < deadline:
            with self._rxlock:
                reply = None
                for k, r in enumerate(self._replies):
                    if r and r[0] == cmd:
                        reply = r; del self._replies[k]; break
                if reply is None and self._replies:
                    self._replies.clear()      # drop non-matching/stale
                cur_sync = self._syncs
            if reply is not None:
                return reply
            if cur_sync > last_sync and (time.time() - sent_at) >= MIN_RESEND:
                self._send_cmd(cmd)            # not ready: resend
                last_sync = cur_sync
                sent_at = time.time()
            time.sleep(0.004)
        return b''

    # ===================== synchronous write-op layer =====================
    # The write operations (set-clock, erase) modify the camera, so they use
    # their own blocking, single-threaded I/O rather than the background
    # reader -- this gives exact, step-by-step control and reproduces the
    # captured ES-E1 exchanges byte for byte. Some of their acknowledgements
    # are *bare* bytes (0xf9, 0xf8, 0x01) that don't fit the framed-reply
    # model the reader uses, which is the other reason for a separate path.

    def _send_serial(self, data):
        """Send raw serial bytes to the camera (the transport frames them)."""
        self.transport.send(data)

    def _sync_open(self):
        self._sbuf = bytearray()

    def _sync_pump(self, timeout_ms=120):
        """Pull whatever serial bytes are waiting; echo any 0xF4 sync, buffer the
        rest. Returns the number of sync bytes seen (used for wake detection)."""
        syncs = 0
        for b in self.transport.recv(timeout_ms):
            if b == SYNC:
                syncs += 1
                try: self._send_serial(bytes([SYNC]))
                except Exception: pass
            else:
                self._sbuf.append(b)
        return syncs

    def _sync_wake(self, attempts=12):
        for i in range(1, attempts + 1):
            self._sbuf = bytearray()
            self._send_serial(bytes([ATTN]))            # 0xff
            deadline = time.time() + 1.0
            while time.time() < deadline:
                if self._sync_pump(80) > 0:             # camera answered 0xf4
                    self._log(f"  wake: camera answered 0xf4 (attempt {i})")
                    return True
            self._log(f"  wake attempt {i}: no 0xf4")
        return False

    def _sync_extract(self, cmd):
        """Pull a complete framed reply [cmd][len][data*len][cksum] out of the RAW
        byte buffer self._sbuf. A 0xF4 is a sync byte ONLY at a frame boundary
        (buffer head) -- it is echoed and dropped there; but once a frame has
        started it is read BY LENGTH, so a 0xF4 *inside* the data or as the
        checksum is kept (this is what the background reader does, and what the
        old per-byte sync-strip got wrong -- cd's 0xF4 checksum read as "no data").
        Returns the frame, or None if not yet complete."""
        buf = self._sbuf
        while buf:
            if buf[0] == SYNC:                         # idle sync -> echo and drop
                del buf[0]
                try: self._send_serial(bytes([SYNC]))
                except Exception: pass
                continue
            if buf[0] != cmd:                          # stray/desynced byte -> drop
                del buf[0]
                continue
            if len(buf) < 2 or len(buf) < buf[1] + 3:
                return None                            # frame not fully arrived yet
            n = buf[1] + 3
            frame = bytes(buf[:n]); del buf[:n]
            return frame
        return None

    def _sync_cmd(self, cmd, timeout=3.0):
        """Send a one-byte command and return its framed reply
        [echo][len][data][cksum], read by length. Resends on a not-ready sync.
        Only used for read/idempotent commands (status, read-clock, e1, and erase
        which is harmless to repeat)."""
        self._sbuf = bytearray()
        self._send_serial(bytes([cmd]))
        sent = time.time(); deadline = sent + timeout
        while time.time() < deadline:
            self._sbuf += self.transport.recv(80)      # RAW bytes; frame-aware below
            frame = self._sync_extract(cmd)
            if frame is not None:
                return frame
            if not self._sbuf and (time.time() - sent) > 0.2:
                self._send_serial(bytes([cmd])); sent = time.time()
        return b''

    def _sync_send_expect(self, serial_out, ack, timeout=3.0, label=''):
        """Send raw serial bytes and wait for a specific bare ack byte. Does NOT
        echo 0xF4 syncs while waiting -- at ES-E1's write pace the camera never goes
        idle, so no sync should arrive here; and a 0xF4 echoed between a write's
        command and its data would be read as the data-length byte and wedge it."""
        self._sbuf = bytearray()
        self._send_serial(serial_out)
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._sbuf += self.transport.recv(80)      # raw; do NOT echo mid-write
            if ack in self._sbuf:
                return True
        self._log(f"  step {label!r}: expected ack 0x{ack:02x}, got "
                  f"{bytes(self._sbuf).hex() or '(nothing)'}")
        return False

    def read_clock(self):
        """Read the camera clock (0xf3). Returns 6 BCD bytes or None."""
        r = self._sync_cmd(0xf3)
        p = self.parse(r)
        if not p or p[0] != 0xf3 or p[1] < 6:
            return None
        return bytes(p[2][:6])

    def set_clock(self, dt):
        """Set the camera clock to datetime dt. verifies by reading the clock back. 
        Returns (old_bcd, written_bcd, new_bcd). Modifies the camera."""
        self._sync_open()
        if not self._sync_wake():
            raise RuntimeError("camera did not answer the wake -- is it on and "
                               "in data-transfer mode?")
        self._sync_cmd(0xf6); self._sync_cmd(0xf1)      # status (as ES-E1 does)
        old = self.read_clock()
        self._sync_cmd(0xa1); self._sync_cmd(0xd1)      # pre-write state reads
        clk = clock_bcd(dt)
        # 4-step write transaction (each step must be acknowledged):
        if not self._sync_send_expect(bytes([0xf9]), 0xf9, label='f9'):
            raise RuntimeError("clock write: camera did not ack 0xf9")
        if not self._sync_send_expect(frame_serial([0x1a]), 0x01, label='select'):
            raise RuntimeError("clock write: camera did not ack the register select")
        if not self._sync_send_expect(bytes([0xf8]), 0xf8, label='f8'):
            raise RuntimeError("clock write: camera did not ack 0xf8")
        if not self._sync_send_expect(frame_serial(clk), 0x01, label='value'):
            raise RuntimeError("clock write: camera did not ack the clock value")
        new = self.read_clock()
        self._sync_send_expect(bytes([0xf2]), 0xf2, timeout=0.6, label='teardown')
        return old, clk, new

    def erase_all(self, confirm):
        """Erase ALL exposure data on the camera (0xe2). DESTRUCTIVE and
        irreversible. `confirm(count)` is called with the current stored-roll
        count and must return True for the erase to proceed. Returns
        (status, count_before, count_after) where status is 'ok' | 'failed' |
        'aborted'."""
        self._sync_open()
        if not self._sync_wake():
            raise RuntimeError("camera did not answer the wake -- is it on and "
                               "in data-transfer mode?")
        for c in (0xf6, 0xf1, 0xe8, 0xfc):
            self._sync_cmd(c)
        before = self._sync_cmd(0xe1)                   # e1 02 00 NN -> NN rolls
        cnt_before = before[3] if len(before) >= 4 else -1
        if not confirm(cnt_before):
            return ('aborted', cnt_before, None)
        r = self._sync_cmd(0xe2)                        # the erase
        ok = (len(r) >= 3 and r[0] == 0xe2 and r[2] == 0x01)   # expect e2 01 01
        after = self._sync_cmd(0xe1)
        cnt_after = after[3] if len(after) >= 4 else -1
        self._sync_send_expect(bytes([0xf2]), 0xf2, timeout=0.6, label='teardown')
        return (('ok' if (ok and cnt_after == 0) else 'failed'),
                cnt_before, cnt_after)

    # ---- Custom Functions (C.Fn) and Personal Functions (P.Fn) ----
    # Each setting block lives in a camera register read by a single command
    # byte and returned framed [echo][len][data][cksum]. C.Fn registers are also
    # writable (the write command = read command + 1, same send->ack->frame->01
    # transaction as the clock). The C.Fn write block for d8/da carries one extra
    # 0x41 byte the read doesn't return -- reproduced exactly here so a
    # backup->restore round-trips byte-for-byte.
    # ES-E1 wakes (0xFF->0xF4) and re-issues 0xF1 status before *each* block but
    # tears down (0xF2) only ONCE, at the very end of the session -- so the camera
    # stays in data-transfer mode across successive reads/writes without another
    # button press. Operations therefore take `close`: pass False to leave the
    # session open for the next block, True (default) to tear down after.
    def _teardown(self):
        for _ in range(4):                          # camera may still be committing
            if self._sync_send_expect(bytes([0xf2]), 0xf2, timeout=1.0,
                                      label='teardown'):
                return
            time.sleep(0.2)

    def _read_registers(self, cmds, close=True):
        """Read a list of register commands. Returns ordered list of
        (cmd, data_bytes, checksum_ok)."""
        self._sync_open()
        if not self._sync_wake():
            raise RuntimeError("camera did not answer the wake -- is it on and "
                               "in data-transfer mode?")
        self._sync_cmd(0xf1)                         # status, as ES-E1 does first
        out = []
        for c in cmds:
            # A read is idempotent (no EEPROM cost), so retry through a transient
            # garble: take the first checksum-valid reply, else the last attempt.
            # (Without this a glitched read returned empty data, which the settings
            # dump then rendered as a bogus "?(0x)" -- looking like a bad value.)
            last = (c, b'', False)
            for _ in range(3):
                p = self.parse(self._sync_cmd(c))
                if p and p[0] == c:
                    last = (c, bytes(p[2]), bool(p[4]))
                    if p[4]:                         # checksum ok -> trust it
                        break
            out.append(last)
        if close:
            self._teardown()
        return out

    def read_custom_functions(self, close=True):
        return self._read_registers(CFN_READ, close=close)

    def read_personal_functions(self, close=True):
        return self._read_registers(PFN_READ, close=close)

    def read_all_functions(self):
        """Read C.Fn then P.Fn in a SINGLE session (no teardown between blocks),
        matching ES-E1. Returns (cfn_list, pfn_list)."""
        cfn = self._read_registers(CFN_READ, close=False)
        pfn = self._read_registers(PFN_READ, close=True)
        return cfn, pfn

    def read_items(self):
        """Read the 8-byte 'shooting data items to be recorded' mask (E8).
        Same block the setup sequence already fetches; returns the 8 data bytes
        (b'' if unreadable). Handy for confirming a config change before shooting
        a calibration roll -- the mask is stamped into each film header (hd[9:17])
        and drives the per-frame record layout."""
        self._sync_open()
        if not self._sync_wake():
            raise RuntimeError("camera did not answer the wake -- is it on and "
                               "in data-transfer mode?")
        for c in (0xf6, 0xf1):
            self._sync_cmd(c)
        p = self.parse(self._sync_cmd(0xe8))
        self._teardown()
        return bytes(p[2]) if p and p[0] == 0xe8 and len(p[2]) >= 8 else b''

    # ES-E1's MEASURED write cadence (data/saleae-pfn25-writes.csv, two captures):
    # ~12 ms between registers, but ~75 ms after every 4th register, where the camera
    # commits the block to EEPROM and needs the time. At this pace the camera NEVER
    # goes idle. Getting it wrong was the whole write saga: a flat 15 ms gap overran
    # the commit (camera went silent ~reg 9); a flat 90 ms gap went too far the OTHER
    # way -- the camera idled between writes, emitted 0xF4 syncs, and dropped out of
    # write mode (~reg 5-8). The real fix is just to match ES-E1's gaps, not to add
    # machinery for the syncs that too-slow a pace causes.
    _WRITE_GAP = 0.012          # between registers
    _WRITE_GAP_LONG = 0.075     # after every 4th register (EEPROM commit boundary)
    _CMD_GAP = 0.010            # between a command echo and its data block

    def _pace_write(self, n):
        """Sleep ES-E1's inter-register gap after writing the n-th register (1-based):
        a long commit pause after reg 1, 5, 9, 13, short otherwise. The key fact from
        ES-E1's capture (data/saleae-es-e1-many-ops.csv): across EVERY 16-register
        block the LAST register gets a SHORT (~9ms) gap and echoes immediately --
        ES-E1 never long-gaps right before it. We were long-gapping before reg 16, so
        the camera was mid-commit when it arrived and withheld the echo. After reg
        1,5,9,13 means reg 14,15,16 all get short gaps, so the final register never
        lands in a commit window. (ES-E1's exact long-gap positions vary block to
        block; this matches its clean block and respects the always-short-last rule.)"""
        time.sleep(self._WRITE_GAP_LONG if n % 4 == 1 else self._WRITE_GAP)

    def _write_register(self, wc, data, timeout=3.0):
        """Write one register: `cmd -> echo`, then `[len][data][cksum] -> 01`. Plain
        handshake -- send the command, wait for its echo (a bare 0xF4 from the camera
        is ignored, not echoed: hardware testing showed echoing it just produces an
        F4 storm and does not unblock a camera withholding the echo). Data is sent
        only after the command echoes, so each register commits at most once."""
        if not self._sync_send_expect(bytes([wc]), wc, timeout=timeout,
                                      label=f'cmd {wc:02x}'):
            return False
        time.sleep(self._CMD_GAP)
        return self._sync_send_expect(frame_serial(data), 0x01, timeout=timeout,
                                      label=f'data {wc:02x}')

    def write_custom_functions(self, regs, close=True, only=None):
        """Write C.Fn registers. `regs` maps read-command -> data bytes. `only`,
        if given, restricts the write to those read-commands (the rest are left
        untouched -- used by `apply` to write just the changed registers). Returns
        list of (write_cmd, ok)."""
        self._sync_open()
        if not self._sync_wake():
            raise RuntimeError("camera did not answer the wake.")
        self._sync_cmd(0xf1)
        results = []; n = 0
        for rc in CFN_WRITE_ORDER:                  # capture order: d1,d5,d7,d9
            if only is not None and rc not in only:
                continue
            wc, trailer = CFN_WRITE[rc]
            results.append((wc, self._write_register(wc, bytes(regs[rc]) + trailer)))
            n += 1; self._pace_write(n)
        if close:
            self._teardown()
        return results

    def write_personal_functions(self, regs, close=True, only=None):
        """Write P.Fn registers, each `wcmd -> echo -> [len][data][cksum] -> 01`,
        paced like ES-E1. `only` restricts to those read-commands. d1 (the C.Fn
        bank) is never written. Returns list of (write_cmd, ok)."""
        self._sync_open()
        if not self._sync_wake():
            raise RuntimeError("camera did not answer the wake.")
        self._sync_cmd(0xf1)
        results = []; n = 0
        for rc in PFN_WRITE_ORDER:
            if only is not None and rc not in only:
                continue
            results.append((PFN_WRITE[rc], self._write_register(PFN_WRITE[rc],
                                                                bytes(regs[rc]))))
            n += 1; self._pace_write(n)
        if close:
            self._teardown()
        return results

    def verify_written(self, res):
        """Re-read (in the SAME session) the registers an apply just wrote and
        confirm each reads back EQUAL to what was intended. Catches a write that
        didn't take, or a register that won't read at all -- the cd-corruption
        symptom. Returns a list of (read_cmd, reason); empty means all good.
        Call with the session still open (writes done with close=False)."""
        checks = []                                       # (read_cmd, intended_bytes)
        if res['new_cfn'] is not None:
            rcs = res['cfn_only'] if res['cfn_only'] is not None else set(CFN_WRITE_ORDER)
            checks += [(rc, bytes(res['new_cfn'][rc]))
                       for rc in CFN_WRITE_ORDER if rc in rcs]
        if res['new_pfn'] is not None:
            rcs = res['pfn_only'] if res['pfn_only'] is not None else set(PFN_WRITE_ORDER)
            checks += [(rc, bytes(res['new_pfn'][rc]))
                       for rc in PFN_WRITE_ORDER if rc in rcs]
        if not checks:
            return []
        got = {c: (bytes(d), ok) for c, d, ok in
               self._read_registers([c for c, _ in checks], close=False)}
        bad = []
        for rc, intended in checks:
            d, ok = got.get(rc, (b'', False))
            if not ok or not d:
                bad.append((rc, "did not read back (no data)"))
            elif d != intended:
                bad.append((rc, f"reads {d.hex()}, expected {intended.hex()}"))
        return bad

    def close(self):
        self._reader_run = False
        try: self._reader.join(timeout=1.0)
        except Exception: pass
        self.transport.close()

    def _log(self, msg):
        if getattr(self, 'verbose', False):
            print(msg, flush=True)

    def probe(self):
        """One-shot diagnostic: dump descriptors, test the OUT and IN paths in
        isolation, and report exactly what the hardware does.
        Run as:  python3 eos1v_tool.py probe"""
        import usb.util, usb.core, threading
        self.verbose = True
        if not isinstance(self.transport, UsbBridgeTransport):
            print("probe is a USB-bridge diagnostic; it does not apply to a raw "
                  "serial connection. Try 'set-clock' or 'read-pfn' over --serial.")
            return
        dev = self.transport.dev
        print("\n=== descriptors as seen on this machine ===")
        cfg = dev.get_active_configuration()
        print(f"active config #{cfg.bConfigurationValue}, "
              f"{cfg.bNumInterfaces} interface(s)")
        for intf in cfg:
            print(f" interface {intf.bInterfaceNumber} alt {intf.bAlternateSetting} "
                  f"class 0x{intf.bInterfaceClass:02x}")
            for ep in intf:
                tt = {0:'control',1:'iso',2:'bulk',3:'interrupt'}[ep.bmAttributes & 3]
                d = 'IN' if ep.bEndpointAddress & 0x80 else 'OUT'
                print(f"   ep 0x{ep.bEndpointAddress:02x} {tt:9} {d} "
                      f"maxpkt {ep.wMaxPacketSize}")

        print("\n=== re-sending the 8 vendor init transfers (with results) ===")
        ok = True
        for i,(bReq,wVal,data) in enumerate(SERIAL_INIT):
            try:
                n = dev.ctrl_transfer(self.transport.BM_VENDOR_OUT, bReq, wVal, 0, data, timeout=2000)
                print(f"   {i+1}/8 bReq=0x{bReq:02x} wVal=0x{wVal:04x} "
                      f"data={data.hex() or '-':10} -> wrote {n}")
            except Exception as e:
                ok = False
                print(f"   {i+1}/8 bReq=0x{bReq:02x} FAILED: {type(e).__name__}: {e}")
        print(f"   init transfers all succeeded: {ok}")

        def try_read(ep, n=64, timeout=400, tag=''):
            try:
                b = bytes(dev.read(ep, n, timeout=timeout))
                print(f"   read {tag} 0x{ep:02x}: {len(b)} bytes: {b[:12].hex(' ')}")
                return b
            except usb.core.USBError as e:
                print(f"   read {tag} 0x{ep:02x}: {type(e).__name__}: {e}")
                return None

        print("\n=== Phase A: any unsolicited data on IN endpoints (no write) ===")
        for _ in range(3):
            try_read(self.transport.EP_IN, tag='bulk-IN')
        try_read(0x83, n=8, tag='intr-IN')

        print("\n=== Phase B: cold-wake handshake (send 0xff, expect 0xf4) ===")
        self._start_reader()
        woke = self._wake(attempts=6)
        print(f"   camera answered 0xf4: {woke}")
        if woke:
            print("   running post-wake setup queries:")
            for c in SETUP_SEQUENCE:
                r = self.command(c)
                print(f"      0x{c:02x} -> {r.hex() or '(no reply)'}")
            print("   reading first film header (0xe3) and first frame (0xe4):")
            rh = self.command(CMD_FILM)
            print(f"      e3 -> {rh.hex() or '(no reply)'}")
            ph = self.parse(rh)
            if ph and ph[0] == CMD_FILM and ph[1] > 1:
                hd = ph[2]
                fid = f"{hd[7]:02x}-{(f'{hd[5]:02x}{hd[6]:02x}').lstrip('0')}" if len(hd)>7 else '?'
                rf = self.command(CMD_FRAME)
                print(f"      e4 -> {rf.hex() or '(no reply)'}")
                pf = self.parse(rf)
                if pf and pf[0] == CMD_FRAME and pf[1] > 1:
                    d = decode_frame(1, pf[2], hd[18] if len(hd)>18 else 0xf0)
                    print(f"   -> DECODED film {fid} frame 1: {d['Focal length']} "
                          f"{d['Tv']} f/{d['Av']} ISO {d['ISO (DX)'] or d['ISO (M)']} "
                          f"{d['Shooting mode']}")
                    print("   *** full read path works -- run 'download' to get everything ***")
        else:
            print("   -> no 0xf4. The bridge is open and writes succeed, but the camera isn't answering on the serial line.")
        self._reader_run = False

        print("\n=== probe done ===")

    @staticmethod
    def parse(resp):
        if len(resp) < 2: return None
        echo, ln = resp[0], resp[1]
        data = resp[2:2+ln]
        cks  = resp[2+ln] if len(resp) > 2+ln else None
        ok   = (cks == (sum(data) & 0xff)) if cks is not None else False
        return echo, ln, data, cks, ok

    def download(self):
        """Run handshake, then pull every film + frame. Returns list of films,
        each {'hdr': bytes, 'frames': [bytes,...]} plus a flat raw log."""
        self._start_reader()
        rawlog = self.rawlog = []
        # Cold-start: get the camera's attention with the 0xff/0xf4 handshake
        self._log("waking camera (0xff -> expect 0xf4)...")
        if not self._wake():
            try: self._serial_open()           # re-open bridge and retry once
            except Exception: pass
            if not self._wake():
                raise RuntimeError(
                    "Camera did not answer the 0xff wake with 0xf4. The cable "
                    "is open and configured (9600 8N1) but the camera isn't "
                    "responding on the serial line. Confirm the EOS-1V is in "
                    "data-transfer mode with a charged battery. Run with -v to "
                    "see each attempt.")
        self._log("camera awake.")
        # Post-wake setup queries (camera/film status), mirroring ES-E1.
        rawlog.append(f"ff {('f4')}")
        for c in SETUP_SEQUENCE:
            r = self.command(c); rawlog.append(f"{c:02x} {r.hex()}")
            self._log(f"  setup 0x{c:02x} -> {r.hex() or '(no reply)'}")
        films = []
        while True:
            r = self.command(CMD_FILM)
            p = self.parse(r)
            # End of films: e3 returns the 1-byte 0x00 marker (e3 01 00 00),
            # same shape as the per-film end marker, OR an empty/zero-len reply.
            if (not p or p[0] != CMD_FILM or p[1] == 0
                    or (p[1] == 1 and p[2][:1] == b'\x00')):
                break
            hdr = p[2]; rawlog.append(f"{CMD_FILM:02x} {r.hex()}")
            frames = []
            while True:
                rf = self.command(CMD_FRAME)
                pf = self.parse(rf)
                rawlog.append(f"{CMD_FRAME:02x} {rf.hex()}")
                if not pf or (pf[1] == 1 and pf[2][:1] == b'\x00'):
                    break
                frames.append(pf[2])
            films.append({'hdr': hdr, 'frames': frames})
            if len(hdr) > 7:
                fid = f"{hdr[7]:02x}-{(f'{hdr[5]:02x}{hdr[6]:02x}').lstrip('0')}"
            else:
                fid = "?"
            self._log(f"  film {fid}: {len(frames)} frames")
        for c in TEARDOWN:
            try: self.command(c, timeout=0.3)
            except Exception: pass
        return films, ('\n'.join(rawlog) + '\n').encode()

# ============================ Decoding ============================
def bcd6(b):
    """6 BCD bytes YY MM DD HH MM SS -> ('YYYY-MM-DD','HH:MM:SS') or (None,None)."""
    if len(b) < 6 or all(x == 0xff for x in b): return (None, None)
    s = ''.join(f'{x:02x}' for x in b)
    return (f"20{s[0:2]}-{s[2:4]}-{s[4:6]}", f"{s[6:8]}:{s[8:10]}:{s[10:12]}")

def bcd(x):
    """Encode a 0..99 integer as a single packed-BCD byte."""
    return ((x // 10) << 4) | (x % 10)

def clock_bcd(dt):
    """datetime -> 6 BCD bytes YY MM DD HH MM SS (2-digit year)."""
    return bytes([bcd(dt.year % 100), bcd(dt.month), bcd(dt.day),
                  bcd(dt.hour), bcd(dt.minute), bcd(dt.second)])

def frame_serial(data):
    """Wrap data in the camera's serial framing: [len][data...][checksum]."""
    data = bytes(data)
    return bytes([len(data)]) + data + bytes([sum(data) & 0xff])

import math

# --- APEX scaling 
# Tv and Av bytes both advance 4 counts per full stop.
#   Av:  Av_apex = byte/4 ;  f-number = 2**(byte/8)         (byte 0x18 -> f/8.0)
#   Tv:  Tv_apex = (byte-20)/4 ; shutter = 2**-Tv_apex      (byte 0x14 -> 1 s)
# ISO advances 8 counts per stop, anchored ISO 50 at 0x40.
# Displayed values are snapped to Canon's standard 1/3-stop ladders so the
# output matches the camera's own labels (e.g. 0x19 -> "9.0", not "8.7").

_STD_AP = [1.0,1.1,1.2,1.4,1.6,1.8,2.0,2.2,2.5,2.8,3.2,3.5,4.0,4.5,5.0,5.6,6.3,
           7.1,8.0,9.0,10,11,13,14,16,18,20,22,25,29,32,36,40,45,51,57,64]
def _snap_ap(f):
    n = min(_STD_AP, key=lambda s: abs(math.log2(s)-math.log2(f)))
    return f"{n:.1f}" if n < 10 else f"{int(round(n))}"

_FAST = [4,5,6,8,10,13,15,20,25,30,40,50,60,80,100,125,160,200,250,320,400,
         500,640,800,1000,1250,1600,2000,2500,3200,4000,5000,6400,8000]
# slow speeds (>= ~0.3s) are shown by ES-E1 as decimal seconds, not 1/N:
_SLOW = [0.3,0.4,0.5,0.6,0.8,1,1.3,1.6,2,2.5,3,4,5,6,8,10,13,15,20,25,30]
def _tvlabel_slow(s):
    return str(int(s)) if s == int(s) else f"{s:g}"
_STD_TV = ([(math.log2(d), f"1/{d}") for d in _FAST] +
           [(-math.log2(s), _tvlabel_slow(s)) for s in _SLOW])

def apex_tv(b):                 # shutter speed
    if b in (0, 0xff): return ''
    tv = (b - 20) / 4.0
    return min(_STD_TV, key=lambda t: abs(t[0]-tv))[1]

def apex_av(b):                 # f-number (also used for max aperture)
    if b in (0, 0xff): return ''
    return _snap_ap(2 ** (b/8.0))

def iso_from_sv(b):             # ISO from Sv (8 counts/stop, 50 at 0x40)
    if b in (0, 0xf0, 0xff): return ''   # 0xf0 = no DX code present
    iso = 50 * 2 ** ((b-0x40)/8.0)
    # snap to nearest standard 1/3-stop ISO
    std = [25,32,40,50,64,80,100,125,160,200,250,320,400,500,640,800,1000,1250,
           1600,2000,2500,3200]
    return str(min(std, key=lambda s: abs(math.log2(s)-math.log2(iso))))

# Byte values not yet reverse-engineered  are shown as "?(0xNN)" 
EXPOSURE = {0x10:'Program AE', 
            0x20:'Shutter-speed-priority AE',
            0x40:'Aperture-priority AE', 
            0x80:'Manual exposure',
            0x08:'Depth-of-field AE', 
            0x04:'Bulb'}   # p[9] & 0xfc (one-hot, bits 2-7; low 2 bits = burst counter)
METERING = {0x10:'Center Averaging', 
            0x20:'Evaluative',
            0x40:'Partial', 
            0x80:'Spot'}                # p[8] & 0xf0
DRIVE    = {0x08:'Single-frame', 
            0x04:'Ultra-high-speed continuous',
            0x40:'Continuous (body only)', 
            0x10:'2-sec. self-timer',
            0x20:'10-sec. self-timer'}                            # p[10] (one-hot)
AFMODE   = {0x02:'One-Shot AF', 
            0x04:'AI Servo AF', 
            0x12:'Manual focus'}  # p[11] & 0xbf
_NO_EXPCOMP = ('Manual exposure', 'Bulb')   # modes where exp-comp isn't shown
def _flash(b):                              # p[7]
    # bit 0x08 = flash fired; the 0xc0 bits distinguish E-TTL (EX flash,
    # evaluative) from plain TTL autoflash. 0x02/0xc1 = OFF, 0x0a = TTL, 0xc9 = E-TTL.
    if not (b & 0x08):  return 'OFF'
    return 'E-TTL' if (b & 0xc0) else 'TTL autoflash'
def _comp(b):
    """Signed exposure/flash compensation byte -> Canon 1/3-stop label
    (eighths-of-a-stop encoding; e.g. 0x05 -> +0.7, 0xfb -> -0.7)."""
    v = b if b < 128 else b - 256
    thirds = round(v / 8 * 3) / 3
    return '0.0' if abs(thirds) < 1e-6 else f"{thirds:+.1f}"
def _lab(table, b):
    return table.get(b, f"?(0x{b:02x})")

# --- Per-frame record layout is compositional --------------------------------
# The per-frame record is the enabled "shooting data items to be recorded"
# concatenated in a FIXED canonical order (fields are never reordered), each a
# fixed size, then 0xff padding. The film's 8-byte items mask (hd[9:17], same
# format as the global E8 block) says which items are present.
#
# The bit<->field map below was DERIVED from the masks we hold and reproduces
# every one of them with zero unexplained bits -- all-off (c009...: only the
# mandatory Shooting mode + a pad byte survive), baseline, all-on, and both
# knob variants -- AND both hardware anchors (bulb = hd[11] bits 2,3; focus =
# hd[14] bit 3). The rule: the mask reads MSB-first within each byte, bytes
# hd[9]..hd[16] in record order, and each field occupies exactly (its byte-size)
# consecutive bits. So a field is present iff its bit is set, and its offset in
# fr[] = 3 (the 01 81 seq header) + the sizes of all present fields before it.
# The two mandatory header bits (hd[9] bits 7,6) are film-header items, not part
# of the per-frame record. INTERNAL prefix order (which prefix field owns which
# hd9/hd10 bit) is the record order confirmed on the baseline rolls; a mixed-item
# roll is the one remaining ground-truth check of that ordering.
FRAME_MASK_BASELINE = bytes.fromhex("ffff003f0008003f")

# (name, size_bytes, (mask_byte_index 0=hd9.., presence_bit)), in record order.
# mandatory=True items are always recorded (their bit is always set).
FRAME_FIELDS = [
    ('focal',     2, (0, 5), False),
    ('maxap',     1, (0, 3), False),
    ('Tv',        1, (0, 2), False),
    ('Av',        1, (0, 1), False),
    ('ISO',       1, (0, 0), False),
    ('expcomp',   1, (1, 7), False),
    ('flashcomp', 1, (1, 6), False),
    ('flashmode', 1, (1, 5), False),
    ('metering',  1, (1, 4), False),
    ('mode',      1, (1, 3), True),    # mandatory: the only field an all-off roll keeps
    ('drive',     1, (1, 2), False),
    ('afmode',    1, (1, 1), False),
    ('pad16',     1, (1, 0), True),    # mandatory 1-byte field, 0x00 in every capture
    ('bulb',      2, (2, 3), False),
    ('shotdate',  3, (3, 5), False),   # YY MM DD  -- date and time are SEPARATE
    ('shottime',  3, (3, 2), False),   # HH MM SS     items (26-357: time on, date off)
    ('cfn',      11, (4, 6), False),   # "Custom Function settings" (spans hd13->hd14);
                                       #  11 raw bytes, not decoded to named C.Fn (no
                                       #  ground truth: ES-E1's CSV omits it). 26-358.
    ('focus',     1, (5, 3), False),   # "Focusing point achieving focus" (the 0x4a
                                       #  byte); an AF-point code, printed raw (26-359)
    ('selection', 7, (6, 6), False),   # "Focusing point selection" (hd15 bits 6-0, 7
                                       #  bytes: a point bitmap); printed raw (26-360)
    ('batdate',   3, (7, 5), False),   # "Battery-loaded date and time" is one ES-E1
    ('battime',   3, (7, 2), False),   #  checkbox -> these two always toggle together
]
# Bit bookkeeping, all in the same MSB-first-within-byte, bytes-in-order traversal.
# hd9 bits 7,6 are two mandatory HEADER items (not part of the per-frame record).
def _field_bits(byte, bit, size):
    """The (byte,bit) positions of a `size`-byte field, MSB-first: descend within
    the byte, wrapping into the next byte(s) -- so a field can span mask bytes
    (Custom Function settings runs hd13 bit6 .. hd14 bit4)."""
    out = []
    for _ in range(size):
        out.append((byte, bit))
        bit -= 1
        if bit < 0: bit, byte = 7, byte + 1
    return out

_HEADER_BITS = {(0, 7), (0, 6)}
_FIELD_START = {(bi, bit): name for name, _s, (bi, bit), _m in FRAME_FIELDS}
# Every (byte,bit) a mapped field occupies. Anything set outside this ∪ header is
# an item we haven't identified.
_KNOWN_BITS = set(_HEADER_BITS)
for _n, _sz, (_bi, _bit), _m in FRAME_FIELDS:
    _KNOWN_BITS.update(_field_bits(_bi, _bit, _sz))

def frame_layout(mask):
    """items mask (hd[9:17]) -> (layout, total, unknown).
    Every SET bit is one recorded byte (bits==bytes, an invariant confirmed on every
    mask we hold). Walking the mask MSB-first, bytes in order, gives each field's
    offset by COUNTING set bits before it -- so mapped fields decode correctly even
    when unidentified items sit among them. `layout` maps present mapped field ->
    byte offset in fr[]; `total` = 3 (01 81 seq header) + count of all recorded
    bytes; `unknown` is True if any set bit isn't part of a mapped field."""
    if not mask or len(mask) < 8:
        mask = FRAME_MASK_BASELINE
    m = bytes(mask[:8])
    layout = {}; off = 3; unknown = False
    for bi in range(8):
        for bit in range(7, -1, -1):
            if (bi, bit) in _HEADER_BITS: continue      # header item, not in fr[]
            if not (m[bi] & (1 << bit)): continue
            if (bi, bit) in _FIELD_START:
                layout[_FIELD_START[(bi, bit)]] = off
            elif (bi, bit) not in _KNOWN_BITS:
                unknown = True                          # an item we can't identify
            off += 1                                    # every set bit = one byte
    return layout, off, unknown

# Names as they appear in the ES-E1 "Shooting Data Items to be Recorded" dialog
# (so read-items reads like the checkbox list). 'mode'/'pad16' aren't dialog items:
# Shooting mode is always recorded, pad16 is a mandatory reserved byte.
_ITEM_LABELS = {
    'focal': 'Focal length', 'maxap': 'Maximum aperture', 'Tv': 'Shutter speed',
    'Av': 'Aperture', 'ISO': 'Manually-set ISO film speed',
    'expcomp': 'Exposure compensation amount',
    'flashcomp': 'Flash exposure compensation amount', 'flashmode': 'Flash mode',
    'metering': 'Metering mode', 'mode': 'Shooting mode (always)',
    'drive': 'Film advance mode', 'afmode': 'AF mode', 'pad16': '(reserved)',
    'bulb': 'Bulb exposure time', 'shotdate': 'Date', 'shottime': 'Time',
    'cfn': 'Custom Function settings', 'focus': 'Focusing point achieving focus',
    'selection': 'Focusing point selection',
    'batdate': 'Battery-loaded date', 'battime': 'Battery-loaded time',
}

def decode_items(mask):
    """Human-readable summary of an 8-byte items-to-be-recorded mask (hd[9:17] or
    the E8 block): which recorded fields are ON/off and the resulting record
    length, flagging any bit our layout table doesn't yet map."""
    if not mask or len(mask) < 8:
        return "items mask: (unreadable)"
    m = bytes(mask[:8])
    layout, total, uncal = frame_layout(m)
    lines = [f"items mask: {m.hex()}    (record: {total} bytes + padding)"]
    for name, size, (bi, bit), mand in FRAME_FIELDS:
        on = bool(m[bi] & (1 << bit))
        tag = 'ON ' if on else 'off'
        note = ' [mandatory]' if mand else ''
        lines.append(f"  {_ITEM_LABELS[name]:<20}: {tag}"
                     f"   (hd[{9+bi}] bit {bit}, {size}B){note}")
    if uncal:
        where = ", ".join(f"hd[{9+b}] bit {k}"
                          for b in range(8) for k in range(8)
                          if (m[b] & (1 << k)) and (b, k) not in _KNOWN_BITS)
        lines.append(f"  ** UN-MAPPED item bit(s) set: {where} -- please report;"
                     " those fields are left blank rather than guessed **")
    return "\n".join(lines)

def _valid_bcd(b):
    return all((x >> 4) <= 9 and (x & 0xf) <= 9 for x in b)

def _bcd_date3(b):
    """3 BCD bytes YY MM DD -> '20YY-MM-DD', or None if absent/implausible."""
    if len(b) < 3 or all(x == 0xff for x in b) or not _valid_bcd(b[:3]): return None
    mo, dd = int(f'{b[1]:02x}'), int(f'{b[2]:02x}')
    if not (1 <= mo <= 12 and 1 <= dd <= 31): return None
    return f"20{b[0]:02x}-{b[1]:02x}-{b[2]:02x}"

def _bcd_time3(b):
    """3 BCD bytes HH MM SS -> 'HH:MM:SS', or None if absent/implausible."""
    if len(b) < 3 or all(x == 0xff for x in b) or not _valid_bcd(b[:3]): return None
    hh, mi, ss = (int(f'{b[i]:02x}') for i in range(3))
    if not (hh <= 23 and mi <= 59 and ss <= 59): return None
    return f"{b[0]:02x}:{b[1]:02x}:{b[2]:02x}"

FIELD_SIZE = {n: s for n, s, _b, _m in FRAME_FIELDS}

def decode_frame(seq, fr, film_dx=0xf0, mask=None):
    # fr = the e4 data field: [01][81][seq] + concatenated recorded fields + 0xff
    # padding. Which fields are present, and thus every offset, comes from the
    # film's items mask via frame_layout() -- there is no fixed prefix.
    p = fr[4:]
    layout, total, unknown = frame_layout(mask)
    # Structural integrity check that works for ANY mask, even one we've never seen:
    # bits==bytes means the recorded content is exactly fr[0:total] and everything
    # past it must be 0xff padding. If that holds, offsets are trustworthy even when
    # unidentified items are present (they just occupy counted bytes we don't label);
    # if it doesn't, we can't trust the layout -> flag, never guess (rule #1).
    trust = (total <= len(fr)) and all(b == 0xff for b in fr[total:])

    def fld(name):
        """The raw bytes of a recorded field, or None if absent/truncated."""
        o = layout.get(name)
        if o is None: return None
        b = fr[o:o + FIELD_SIZE[name]]
        return b if len(b) == FIELD_SIZE[name] else None
    def b1(name):
        b = fld(name); return b[0] if b else None
    def dt(name, conv):
        """A 3-byte date or time field -> (string, ok). Absent -> ('', True);
        present but not plausible BCD -> ('?(layout)', False), never silently wrong."""
        b = fld(name)
        if b is None: return '', True
        s = conv(b)
        return (s, True) if s is not None else ('?(layout)', False)

    shot_d, _d1 = dt('shotdate', _bcd_date3)
    shot_t, _d2 = dt('shottime', _bcd_time3)
    bat_d,  _d3 = dt('batdate',  _bcd_date3)
    bat_t,  _d4 = dt('battime',  _bcd_time3)
    _sok = _d1 and _d2; _bok = _d3 and _d4
    row = {'Frame': seq, 'Date': shot_d, 'Time': shot_t,
           'Battery date': bat_d, 'Battery time': bat_t,
           '_date_ok': _sok and _bok,
           '_untrusted': not trust,       # layout couldn't be trusted -> flagged
           '_unknown': unknown,           # some recorded items are unidentified
           'raw': p.hex(' ')}

    # Layout not trustworthy (record shorter than the mask needs, or non-0xff data
    # where padding should be): don't read fields from positions that may be shifted.
    if not trust:
        row.update({'Date': '?(layout)', 'Time': '?(layout)',
                    'Battery date': '?(layout)', 'Battery time': '?(layout)',
                    '_date_ok': False})
        return row

    # ISO: the film header records the DX-read speed (film_dx). The ISO field is
    # the actual taking speed. The camera shows a manual "(M)" value whenever the
    # film had no DX code, OR the taking speed was overridden away from the DX code.
    has_dx     = film_dx not in (0x00, 0xf0, 0xff)
    iso_b      = b1('ISO')
    iso_taking = iso_from_sv(iso_b) if iso_b is not None else ''
    iso_dx     = iso_from_sv(film_dx) if has_dx else ''
    manual     = (not has_dx) or (iso_b is not None and iso_b != film_dx)
    mode_b     = b1('mode')
    mode       = _lab(EXPOSURE, mode_b & 0xfc) if mode_b is not None else ''
    fb         = fld('focal')
    focal      = (fb[0] << 8) | fb[1] if fb else 0
    maxap, tv, av = b1('maxap'), b1('Tv'), b1('Av')
    ec, fc, fmb   = b1('expcomp'), b1('flashcomp'), b1('flashmode')
    met, dr, afb  = b1('metering'), b1('drive'), b1('afmode')
    # Focus-point items are recorded as opaque AF-point codes ES-E1 doesn't export
    # or interpret; we print them raw (hex) and don't guess the point mapping.
    focus_b = fld('focus'); sel_b = fld('selection')
    row.update({
        'Focal length': f"{focal}mm" if focal != 0 else '',
        'Max aperture': apex_av(maxap) if maxap is not None else '',
        'Tv': '' if (tv is None or tv == 0xf0) else apex_tv(tv),  # 0xf0 = Bulb
        'Av': apex_av(av) if av is not None else '',
        'ISO (DX)': iso_dx,
        'ISO (M)':  iso_taking if manual else '',
        # Exposure comp / flash exposure comp: signed eighths of a stop (0x08=+1.0,
        # 0xf8=-1.0, 0x05=+0.7). ES-E1 blanks exposure comp in Manual/Bulb.
        'Exposure compensation':       '' if (ec is None or mode in _NO_EXPCOMP) else _comp(ec),
        'Flash exposure compensation': _comp(fc) if fc is not None else '',
        'Shooting mode': mode,
        'Metering mode': _lab(METERING, met & 0xf0) if met is not None else '',
        'Flash mode':    _flash(fmb) if fmb is not None else '',
        'Film advance':  _lab(DRIVE, dr & 0x7f) if dr is not None else '',  # 0x80 = ME cont.
        # AF mode enum is the low 6 bits; bits 0x40/0x80 are flags (seen 0x42, 0xc2)
        # that ES-E1 ignores -- mask them so e.g. 0xc2 reads One-Shot, not ?(0x82).
        'AF mode':       _lab(AFMODE, afb & 0x3f) if afb is not None else '',
        'AF point achieving focus': f"{focus_b[0]:02x}" if focus_b else '',
        'AF point selection':       sel_b.hex() if sel_b else '',
    })
    return row

def split_films_from_raw(raw_blocks):
    """raw_blocks: list of (cmd, data) tuples in order -> films structure."""
    films=[]; cur=None
    for cmd, data in raw_blocks:
        if cmd == CMD_FILM:
            if cur: films.append(cur)
            cur={'hdr':data,'frames':[]}
        elif cmd == CMD_FRAME and cur is not None:
            if len(data)==1 and data[0]==0: continue
            cur['frames'].append(data)
    if cur: films.append(cur)
    return films

def films_to_csv(films, path, verbose=False):
    cols=['Film','Film loaded date','Film loaded time','Frame','Focal length',
          'Max aperture','Tv','Av','ISO (DX)','ISO (M)',
          'Exposure compensation','Flash exposure compensation',
          'Shooting mode','Metering mode','Flash mode','Film advance','AF mode',
          'AF point achieving focus','AF point selection',
          'Multiple exposure','Date','Time','Battery date','Battery time']
    if verbose: cols.append('raw')      # hex dump only when run verbose
    flagged=[]; untrusted_films=[]; unknown_films=[]   # post-run warnings (never silent)
    with open(path,'w',newline='') as f:
        w=csv.DictWriter(f, fieldnames=cols, extrasaction='ignore'); w.writeheader()
        for fi,film in enumerate(films,1):
            hd=film['hdr']
            # Per-film items bitmask (hd[9:17]) drives the whole record layout.
            mask = bytes(hd[9:17]) if len(hd)>=17 else b''
            layout, _total, _uncal = frame_layout(mask)
            drive_off = layout.get('drive')      # for the multiple-exposure flag
            # Film ID (matches the camera's frame-imprint, e.g. "26-218"):
            #   hd[5:7] = 3-digit running film number in BCD (02 18 -> "218")
            #   hd[7]   = 2-digit prefix in BCD          (26    -> "26")
            prefix = f"{hd[7]:02x}" if len(hd)>7 else '??'
            num    = (f"{hd[5]:02x}{hd[6]:02x}").lstrip('0') if len(hd)>6 else '??'
            num    = num or '0'
            film_dx = hd[18] if len(hd)>18 else 0xf0
            ld,lt = bcd6(hd[19:25]) if len(hd)>=25 else (None,None)
            # A multiple exposure is stored as several records sharing one frame
            # number (and a 0x80 flag on the drive byte of the continuation
            # records). Flag every record whose frame number occurs more than once.
            seqs = [fr[2] if len(fr)>2 else i for i,fr in enumerate(film['frames'],1)]
            dup  = {s for s in seqs if seqs.count(s) > 1}
            for j,fr in enumerate(film['frames'],1):
                frame_no = fr[2] if len(fr)>2 else j     # the camera's frame number
                me = (frame_no in dup) or (drive_off is not None
                     and len(fr) > drive_off and (fr[drive_off] & 0x80))
                row={'Film':f"{prefix}-{num}",
                     'Film loaded date':ld,'Film loaded time':lt,
                     'Multiple exposure':'ON' if me else 'OFF'}
                row.update(decode_frame(frame_no, fr, film_dx, mask))
                fid = f"{prefix}-{num}"
                if row.pop('_untrusted', False) and fid not in [u[0] for u in untrusted_films]:
                    untrusted_films.append((fid, mask.hex()))
                if row.pop('_unknown', False) and fid not in [u[0] for u in unknown_films]:
                    unknown_films.append((fid, mask.hex()))
                if not row.pop('_date_ok', True):
                    flagged.append(f"{fid} frame {frame_no}")
                w.writerow(row)
    if untrusted_films:
        sys.stderr.write(
            "WARNING: could not trust the record layout on: "
            + ", ".join(f"{fid} (items={m})" for fid,m in untrusted_films)
            + "\n  The record doesn't match the mask (bits!=bytes or truncated); "
              "those rolls' fields are left blank/flagged rather than guessed.\n")
    if unknown_films:
        sys.stderr.write(
            "NOTE: unidentified recorded item(s) on: "
            + ", ".join(f"{fid} (items={m})" for fid,m in unknown_films)
            + "\n  The known fields decoded correctly; some items this camera records "
              "aren't in our table yet and are omitted (not guessed). Please report "
              "the mask so they can be identified.\n")
    if flagged:
        sys.stderr.write("WARNING: implausible date bytes flagged '?(layout)' on "
                         f"{len(flagged)} frame(s): {', '.join(flagged[:8])}"
                         + (" ..." if len(flagged)>8 else "") + "\n")
    return path

# ============================ CLI ============================
def main():
    import os
    if '-v' in sys.argv or '--verbose' in sys.argv:
        os.environ['EOS1V_VERBOSE'] = '1'
        sys.argv = [a for a in sys.argv if a not in ('-v','--verbose')]
    _VERBOSE = os.environ.get('EOS1V_VERBOSE','') not in ('','0')
    serial_port = None                       # --serial PORT -> raw serial cable
    if '--serial' in sys.argv:
        i = sys.argv.index('--serial')
        if i + 1 >= len(sys.argv):
            print("--serial needs a port (e.g. --serial /dev/ttyUSB0)"); return
        serial_port = sys.argv[i + 1]
        del sys.argv[i:i + 2]
    if len(sys.argv)>=2 and sys.argv[1]=='probe':
        cam=EOS1V(port=serial_port)
        try: cam.probe()
        finally: cam.close()
        return
    if len(sys.argv)>=3 and sys.argv[1]=='download':
        cam=EOS1V(port=serial_port)
        raw=b''
        try:
            films,raw=cam.download()
            films_to_csv(films, sys.argv[2], verbose=_VERBOSE)
            n=sum(len(f['frames']) for f in films)
            print(f"Downloaded {len(films)} films / {n} frames -> {sys.argv[2]}")
        finally:
            # Always save whatever raw bytes we collected so a partial/failed
            # run is still useful for offline decoding and debugging.
            if len(sys.argv)>=4:
                if not raw and getattr(cam,'rawlog',None):
                    raw=('\n'.join(cam.rawlog)+'\n').encode()
                if raw:
                    open(sys.argv[3],'wb').write(raw)
                    print(f"raw dump -> {sys.argv[3]}")
            cam.close()
    elif len(sys.argv)>=4 and sys.argv[1]=='decode':
        blocks=load_raw_blocks(sys.argv[2])
        films=split_films_from_raw(blocks)
        films_to_csv(films, sys.argv[3], verbose=_VERBOSE)
        n=sum(len(f['frames']) for f in films)
        print(f"Decoded {len(films)} films / {n} frames -> {sys.argv[3]}")
    elif len(sys.argv)>=2 and sys.argv[1]=='read-items':
        # Show the 'shooting data items to be recorded' mask. With a raw-dump
        # argument, read it offline (E8 block, or the first film's hd[9:17]);
        # otherwise read it live from the camera. Useful to confirm a config
        # change took before shooting a calibration roll.
        if len(sys.argv)>=3:
            mask=b''
            for c,d in load_raw_blocks(sys.argv[2]):
                if c==0xe8 and len(d)>=8: mask=bytes(d[:8]); break
                if c==0xe3 and len(d)>=17 and not (d[0]==1 and d[1]==0):
                    mask=bytes(d[9:17]); break
            print(decode_items(mask))
        else:
            cam=EOS1V(port=serial_port)
            try: print(decode_items(cam.read_items()))
            finally: cam.close()
    elif len(sys.argv)>=2 and sys.argv[1]=='set-clock':
        from datetime import datetime
        when = sys.argv[2] if len(sys.argv)>=3 else 'now'
        try:
            dt = datetime.now() if when=='now' else datetime.fromisoformat(when)
        except ValueError:
            print("Could not parse time. Use 'now' or ISO 8601, e.g. "
                  "set-clock 2026-06-12T13:51:53")
            return
        print(f"Setting camera clock to {dt:%Y-%m-%d %H:%M:%S} "
              f"(host {'now' if when=='now' else 'value'}).")
        cam=EOS1V(port=serial_port)
        try:
            old,clk,new = cam.set_clock(dt)
            def fmt(b):
                d,t = bcd6(b) if b else (None,None)
                return f"{d} {t}" if d else "(unreadable)"
            print(f"  clock before: {fmt(old)}")
            print(f"  clock written: {fmt(clk)}")
            print(f"  clock after:  {fmt(new)}")
            if new and new[:5]==clk[:5]:
                print("  OK -- camera clock updated (seconds may differ slightly).")
            else:
                print("  WARNING -- read-back does not match; verify on the camera.")
        finally:
            cam.close()
    elif len(sys.argv)>=2 and sys.argv[1]=='erase-all':
        # Destructive 
        cam=EOS1V(port=serial_port)
        def confirm(cnt):
            print("  *** ERASE ALL EXPOSURE DATA ON THE CAMERA ***")
            if cnt < 0:
                print("  (could not read the current roll count -- proceed only")
                print("   if you are sure the camera is connected and awake)")
            else:
                print(f"  The camera currently holds {cnt} roll record(s).")
            print("  This PERMANENTLY erases all of it and CANNOT be undone.")
            print("  If you want to keep the data, run 'download' first.")
            try:
                ans = input("  Type YES to proceed (anything else aborts): ")
            except EOFError:
                return False
            return ans.strip() == 'YES'
        try:
            status, before, after = cam.erase_all(confirm)
            if status == 'aborted':
                print("Aborted -- nothing was erased.")
            elif status == 'ok':
                print(f"Erase confirmed: roll count {before} -> {after}. "
                      f"Camera data memory is now empty.")
            else:
                print(f"Erase may have FAILED: roll count {before} -> {after}. "
                      f"Re-check on the camera before relying on this.")
        finally:
            cam.close()
    elif len(sys.argv)>=2 and sys.argv[1] in ('read-cfn','read-pfn'):
        kind = 'Custom' if sys.argv[1]=='read-cfn' else 'Personal'
        cam=EOS1V(port=serial_port)
        try:
            regs = (cam.read_custom_functions() if kind=='Custom'
                    else cam.read_personal_functions())
        finally:
            cam.close()
        bad = [f"{c:02x}" for c,d,ok in regs if not ok or not d]
        print(f"{kind} Function registers (raw):")
        for c,d,ok in regs:
            flag = '' if ok else '  <-- checksum/!read FAILED'
            print(f"  reg {c:02x}: {d.hex(' ') or '(no data)'}{flag}")
        if bad:
            print(f"WARNING: {len(bad)} register(s) did not read cleanly: "
                  f"{', '.join(bad)}")
        print("\nNote: these are the raw register values. Mapping them to named "
              "C.Fn/P.Fn settings needs calibration captures (see notes).")
        if len(sys.argv)>=3:
            save_functions(sys.argv[2], kind, regs)
            print(f"backup saved -> {sys.argv[2]}")
    elif len(sys.argv)>=3 and sys.argv[1] in ('write-cfn','write-pfn'):
        pfn = sys.argv[1]=='write-pfn'
        regs, kind = load_functions(sys.argv[2])
        want = 'Personal' if pfn else 'Custom'
        if kind != want:
            print(f"{sys.argv[2]} is a {kind}-Function backup; {sys.argv[1]} needs "
                  f"a {want}-Function backup. Aborting."); return
        order = PFN_WRITE_ORDER if pfn else CFN_WRITE_ORDER
        wcmd = (lambda c: PFN_WRITE[c]) if pfn else (lambda c: CFN_WRITE[c][0])
        missing=[f"{c:02x}" for c in order if c not in regs]
        if missing:
            print(f"Backup is missing register(s) {', '.join(missing)}; "
                  f"refusing to write a partial set."); return
        cam=EOS1V(port=serial_port)
        try:
            readf = cam.read_personal_functions if pfn else cam.read_custom_functions
            writef = cam.write_personal_functions if pfn else cam.write_custom_functions
            cur = {c: bytes(d) for c, d, ok in readf(close=False)}
            diff = restore_diff(regs, cur, order)
            if not diff:
                cam._teardown()
                print(f"Camera's {want} Functions already match the backup -- "
                      f"nothing to write."); return
            print(f"{len(diff)} {want}-Function register(s) differ and will be written:")
            for c in diff:
                old = cur.get(c, b'').hex(' ') or '(unreadable)'
                print(f"  reg {c:02x} (write {wcmd(c):02x}): {old}  ->  {regs[c].hex(' ')}")
            print("This changes the camera's settings. (Restoring a backup you made "
                  "on this camera is safe; hand-edited values are validated only "
                  "against the captured block format.)")
            try: ans = input("\nType YES to write (anything else aborts): ")
            except EOFError: ans = ''
            if ans.strip() != 'YES':
                cam._teardown(); print("Aborted -- nothing was written."); return
            res = writef(regs, only=set(diff), close=False)
            chk = {c: (bytes(d), ok) for c, d, ok in cam._read_registers(diff, close=False)}
            cam._teardown()
            okn = sum(1 for _, ok in res if ok)
            bad = [f"{c:02x}" for c in diff
                   if not chk[c][1] or chk[c][0] != bytes(regs[c])]
            if okn == len(res) and not bad:
                print(f"Wrote and verified {len(res)} register(s) -- the camera now "
                      f"matches the backup.")
            else:
                print(f"Wrote {okn}/{len(res)}; {len(bad)} register(s) did NOT read "
                      f"back correctly ({', '.join(bad)}). Re-run to retry.")
        finally:
            cam.close()
    elif len(sys.argv)>=3 and sys.argv[1]=='dump-fn':
        regs, kind = load_functions(sys.argv[2])
        print(dump_functions(regs, kind))
    elif len(sys.argv)>=3 and sys.argv[1]=='decode-cfn':
        regs, kind = load_functions(sys.argv[2])
        if kind != 'Custom':
            print(f"{sys.argv[2]} is a {kind}-Function backup; decode-cfn needs "
                  f"a Custom-Function backup (read-cfn)."); return
        print(decode_custom_functions(regs))
    elif len(sys.argv)>=3 and sys.argv[1]=='decode-pfn':
        regs, kind = load_functions(sys.argv[2])
        if kind != 'Personal':
            print(f"{sys.argv[2]} is a {kind}-Function backup; decode-pfn needs "
                  f"a Personal-Function backup (read-pfn)."); return
        print(decode_personal_functions(regs))
    elif len(sys.argv)>=2 and sys.argv[1]=='dump-settings':
        args = sys.argv[2:]
        if args and args[0] == '--from':            # offline from two backups
            cregs, _ = load_functions(args[1])
            pregs, _ = load_functions(args[2])
            out = args[3] if len(args) > 3 else None
        else:                                       # live read from the camera
            cam = EOS1V(port=serial_port)
            try:
                cfn, pfn = cam.read_all_functions()  # one session, no re-wake needed
                cregs = {c: d for c, d, _ in cfn}
                pregs = {c: d for c, d, _ in pfn}
                bad = [f"{c:02x}" for c, _, ok in cfn + pfn if not ok]
                if bad:                              # a read glitched even after retries
                    print(f"WARNING: {len(bad)} register(s) did not read cleanly "
                          f"({', '.join(bad)}); their settings may show as '?(0x)'. "
                          f"Re-run dump-settings.")
            finally:
                cam.close()
            out = args[0] if args else None
        text = settings_dump(cregs, pregs)
        if out:
            open(out, 'w').write(text + "\n"); print(f"settings -> {out}")
        else:
            print(text)
    elif len(sys.argv)>=3 and sys.argv[1]=='check':
        file_text = open(sys.argv[2]).read()
        args = sys.argv[3:]
        if args and args[0] == '--against':            # offline vs two backups
            cregs, _ = load_functions(args[1])
            pregs, _ = load_functions(args[2])
        else:                                           # vs live camera state
            cam = EOS1V(port=serial_port)
            try:
                cfn, pfn = cam.read_all_functions()      # one session
                cregs = {c: d for c, d, _ in cfn}
                pregs = {c: d for c, d, _ in pfn}
            finally:
                cam.close()
        print(settings_check(file_text, cregs, pregs))
    elif len(sys.argv)>=3 and sys.argv[1]=='apply':
        file_text = open(sys.argv[2]).read()
        args = sys.argv[3:]
        if args and args[0] == '--against':              # offline dry run
            cregs, _ = load_functions(args[1]); pregs, _ = load_functions(args[2])
            res = apply_compute(file_text, cregs, pregs)
            if res['errors']:
                print("INVALID VALUES -- nothing applied:")
                for e in res['errors']:
                    print(f"  - {e}")
                return
            print(apply_summary(res))
            print("\n(dry run -- '--against' shows what apply would do; "
                  "no camera was touched.)")
            return
        cam = EOS1V(port=serial_port)
        try:
            cregs = {c: d for c, d, _ in cam.read_custom_functions(close=False)}
            pregs = {c: d for c, d, _ in cam.read_personal_functions(close=False)}
            res = apply_compute(file_text, cregs, pregs)
            if res['errors']:
                cam._teardown()
                print("INVALID VALUES -- nothing applied:")
                for e in res['errors']:
                    print(f"  - {e}")
                return
            print(apply_summary(res))
            if not res['changes']:
                cam._teardown(); return
            try:
                ans = input("\nType YES to write these to the camera: ")
            except EOFError:
                ans = ''
            if ans.strip() != 'YES':
                cam._teardown(); print("Aborted -- nothing was written."); return
            wrote, results, pfn_changed = [], [], res['new_pfn'] is not None
            if res['new_cfn'] is not None:                # keep the session open ...
                results += cam.write_custom_functions(res['new_cfn'], close=False,
                                                      only=res['cfn_only'])
                wrote.append('C.Fn')
            if pfn_changed:
                results += cam.write_personal_functions(res['new_pfn'], close=False,
                                                        only=res['pfn_only'])
                wrote.append('P.Fn')
            verify_bad = cam.verify_written(res)          # ... read it back, then close
            cam._teardown()
            okn = sum(1 for _, ok in results if ok)
            if okn == len(results):
                print(f"Wrote {', '.join(wrote)} ({len(results)} registers, all "
                      f"acknowledged).")
            else:
                print(f"Wrote {', '.join(wrote)} but {len(results) - okn} of "
                      f"{len(results)} register(s) did NOT acknowledge (the camera "
                      f"may have been slow) -- re-run 'check' to see what's left.")
            if verify_bad:
                print("VERIFY FAILED -- these registers did not read back as written:")
                for rc, why in verify_bad:
                    print(f"  reg {rc:02x}: {why}")
                print("  A register reading '(no data)' may be corrupted; restore it "
                      "from a backup with 'write-pfn'/'write-cfn'.")
            else:
                print("Verified: every written register reads back exactly as written.")
        finally:
            cam.close()
    elif len(sys.argv)>=4 and sys.argv[1]=='diff-fn':
        a, ka = load_functions(sys.argv[2])
        b, kb = load_functions(sys.argv[3])
        if ka != kb:
            print(f"Refusing to diff: {sys.argv[2]} is {ka}, "
                  f"{sys.argv[3]} is {kb}."); return
        print(f"Diff ({ka} Functions): {sys.argv[2]} -> {sys.argv[3]}")
        print(diff_functions(a, b))
    else:
        print(__doc__)

def save_functions(path, kind, regs):
    """Write a register backup: one 'XX: hex bytes' line per register."""
    with open(path, 'w') as f:
        f.write(f"# EOS-1V {kind} Function register backup\n")
        for c, d, ok in regs:
            f.write(f"{c:02x}: {d.hex(' ')}"
                    f"{'' if ok else '   # CHECKSUM FAILED ON READ'}\n")
    return path

def load_functions(path):
    """Load a register backup -> ({cmd: data_bytes}, kind)."""
    kind = 'Custom'; regs = {}
    for line in open(path):
        s = line.strip()
        if s.startswith('#'):
            if 'Personal' in s: kind = 'Personal'
            continue
        if ':' not in s: continue
        cmd, hexd = s.split(':', 1)
        regs[int(cmd, 16)] = bytes.fromhex(hexd.split('#')[0].strip())
    return regs, kind

def _reg_order(kind):
    """The register read-order for a kind, so dumps/diffs print consistently."""
    return CFN_READ if kind == 'Custom' else PFN_READ

def dump_functions(regs, kind):
    """Return an annotated, human-readable layout of register data: every byte
    broken out into its two nibbles and its 8 bits. No interpretation is applied
    -- this is scaffolding for mapping bytes to named C.Fn/P.Fn settings, so it
    deliberately shows the raw structure and nothing it can't prove. `regs` is
    {cmd: data_bytes} (as load_functions returns)."""
    lines = [f"EOS-1V {kind} Function registers -- annotated layout",
             "(nibbles are hi/lo; bits are MSB..LSB. Values are raw, unmapped.)",
             ""]
    order = [c for c in _reg_order(kind) if c in regs]
    order += [c for c in regs if c not in order]      # any extras, after
    for c in order:
        d = regs[c]
        lines.append(f"reg {c:02x}  [{len(d)} byte(s)]   {d.hex(' ') or '(empty)'}")
        if d:
            lines.append("    byte  hex   nib    bits")
            for i, b in enumerate(d):
                lines.append(f"    {i:>3}   {b:02x}   {b>>4:x} {b&0xf:x}    "
                             f"{b>>4:04b} {b&0xf:04b}")
        lines.append("")
    return "\n".join(lines)

def _onehot(n):
    """Decode a one-hot field to its value (the index of the single set bit).
    Returns None if n isn't a clean single-bit value (0, or multiple bits)."""
    if n and (n & (n - 1)) == 0:
        return n.bit_length() - 1
    return None

def decode_cfn_bank(data):
    """Decode one C.Fn bank's data field to {C.Fn number: value}. Encoding is
    hardware-confirmed: C.Fn 1..18 = bytes 0..8 low-nibble-first (slot=C.Fn-1),
    C.Fn 19 = byte 9 (full-byte one-hot), C.Fn 0 = byte 10 low nibble (only the
    active bank D1 has byte 10). Value is None where the field isn't a clean
    one-hot. The high nibble of byte 10 is reserved/unknown and ignored."""
    out = {}
    for i in range(min(9, len(data))):                 # C.Fn 1..18, two per byte
        out[2*i + 1] = _onehot(data[i] & 0xf)          # low nibble first
        out[2*i + 2] = _onehot(data[i] >> 4)
    if len(data) >= 10:
        out[19] = _onehot(data[9])                     # full-byte one-hot
    if len(data) >= 11:
        out[0] = _onehot(data[10] & 0xf)               # active bank only
    return out

def decode_custom_functions(regs):
    """Render a human-readable decode of Custom Functions from a register set
    ({cmd: data_bytes}). The active bank D1 is decoded in full (incl. C.Fn 0);
    the three switchable banks D5/D7/D9 are summarised by their non-default
    settings. Names/labels come from CFN_FUNCS (manual-derived, verify)."""
    lines = ["EOS-1V Custom Functions  (decoded from active bank D1)"]
    active = regs.get(0xd1, b'')
    if not active:
        return "No active C.Fn bank (D1) in this data."
    vals = decode_cfn_bank(active)
    for n in range(0, 20):
        if n not in vals:
            continue
        name, opts = CFN_FUNCS.get(n, (f"C.Fn-{n}", {}))
        v = vals[n]
        if v is None:
            shown = f"?(not one-hot: {active[10 if n==0 else 9 if n==19 else (n-1)//2]:#04x})"
        else:
            shown = f"{v}  {opts.get(v, '?(undocumented value)')}"
        lines.append(f"  C.Fn-{n:<2} {name:<38} = {shown}")
    hi = active[10] >> 4 if len(active) >= 11 else 0
    if hi:
        lines.append(f"  (note: byte 10 high nibble = {hi:#x}, reserved/unknown)")
    # Summarise the three alternate banks against an all-default decode.
    alt = []
    for c in (0xd5, 0xd7, 0xd9):
        d = regs.get(c)
        if not d:
            continue
        bv = decode_cfn_bank(d)
        diffs = [f"C.Fn-{n}={v}" for n, v in sorted(bv.items())
                 if v not in (0, None)]
        alt.append(f"  bank {c:02x}: " +
                   ("all default" if not diffs else ", ".join(diffs)))
    if alt:
        lines.append("")
        lines.append("Switchable banks:")
        lines.extend(alt)
    lines.append("")
    lines.append("Names/option labels are from the EOS-1V manual and pending "
                 "per-camera verification;")
    lines.append("the byte->value decoding is hardware-confirmed.")
    return "\n".join(lines)

# ---- Personal Function decode -------------------------------------------
# All P.Fn are now decoded (hardware-confirmed by calibration captures cross-
# checked against the ES-E1 "Combination" tab); see docs/EOS-1V-protocol-notes.md
# §4. The confidence tags remain in the output as a provenance aid:
#   ok   = hardware-confirmed by a calibration capture
#   ~    = predicted/provisional (currently none)
#   ?    = unknown -- e.g. an enum value never exercised (falls through to ?(0x##))
# Confirmed (2026-06-14): d3 enable bitmask is byte-3-first and fully confirmed
# (ES-E1 "Combination" tab matched the prediction exactly); on/off P.Fn carry no
# value byte (P.Fn-7 moved only its enable bit); P.Fn-3 = reg c1 metering one-hot
# (CWA 0x10 -> Spot 0x80); P.Fn-4 = c3 (Tv max/min); P.Fn-5 = c4 (Av max/min);
# P.Fn-1 = c5 shoot-mask; P.Fn-2 = c6 meter-mask; P.Fn-12 = cb sensitivity;
# P.Fn-20 = ca frames; P.Fn-23 = c7/c8/c0 timers (sec x16, 16-bit BE); P.Fn-25 =
# cd CLEAR defaults; P.Fn-30 = cf density; P.Fn-19 = cc booster fps. P.Fn-6 is
# enable-only here (its preset modes are stored on the camera body, not in these
# registers), and the 18 on/off P.Fn carry no value byte (state == enable bit).

def _shutter_to_tv(lbl):                 # dropdown label -> Tv (stops)
    return (-math.log2(float(lbl[:-1])) if lbl.endswith('"')
            else math.log2(float(lbl.split('/')[1])))

def _pfn_shutter(b, scale):              # P.Fn Tv code (8/stop, ref 56) -> rung
    if b in (0, 0xff): return '?'
    tv = (b - 56) / 8.0
    return min(scale, key=lambda s: abs(_shutter_to_tv(s) - tv))

def _pfn_aperture(b, scale):             # P.Fn Av code (8/stop, f/1.0=0x70) -> rung
    if b in (0, 0xff): return '?'
    av = (112 - b) / 8.0
    return min(scale, key=lambda s: abs(2*math.log2(float(s)) - av))

# Enable bitmask (CONFIRMED): byte-3-first; P.Fn-N enable = byte 3-(N-1)//8, bit
# (N-1)%8. Verified against the ES-E1 Combination tab (P.Fn-3,4,16,21,28).
def pfn_enabled(d3, n):
    if not d3: return None
    off = 3 - (n - 1) // 8
    if off < 0 or off >= len(d3): return None
    return bool(d3[off] & (1 << ((n - 1) % 8)))

# Value-bearing functions: {P.Fn: (read-cmd reg, kind, confidence)}. Functions not
# listed are treated as on/off (state == enable bit). 'todo' = register unknown.
PFN_VALUE = {
    3:  (0xc1, 'metering',   'ok'),  # confirmed: exposure METERING one-hot in c1[0]
    4:  (0xc3, 'shutter',    'ok'),  # confirmed
    5:  (0xc4, 'aperture',   'ok'),  # confirmed
    12: (0xcb, 'sensitivity','ok'),  # cb: Fast=0x00, Standard=0x40, Slow=0x80 (±0x20)
    1:  (0xc5, 'shoot_mask', 'ok'),  # c5 mask, mode i -> bit (5-i); confirmed
    2:  (0xc6, 'meter_mask', 'ok'),  # c6 mask, Spot=0x80 etc.; confirmed
    20: (0xca, 'framecount', 'ok'),  # ca = max frames as a plain byte; confirmed
    30: (0xcf, 'density', 'ok'),     # cf bit7: Light=0x80, Dark=clear (both confirmed)
    25: (0xcd, 'clear_defaults','ok'),# cd: [0]=shooting [2]=metering [3]=advance
                                     # [1]=AF(low bits)+focus(bit7); all confirmed
    23: (0xc7, 'timers', 'ok'),      # c7/c8/c0 = 6s/16s/post timers, each sec x16 16-bit BE
    19: (0xcc, 'booster_fps','ok'),  # cc: fps=10-byte/2; Low=cc[0](=cc[1]), High=cc[2], Ultra=cc[3]
}
# P.Fn-25 CLEAR-default sub-fields (reg cd). Film advance (byte3) and AF mode
# (byte1 low bits) are one-hot; focus point is byte1 bit7 (set=Auto, clear=Center).
# Confirmed 2026-06-16 (data/pfn-pfn25-aiservo.txt: cd byte1=0x84 = AI Servo+Auto).
# AF mode has only TWO options in P.Fn-25 (no AI Focus -- ES-E1 dropdown shows
# only One-Shot/AI Servo). Film advance has SEVEN dropdown options; bytes known
# for 3, the other 4 are not yet mapped.
# Film advance (cd byte3) one-hot. Same encoding as the validated exposure-data
# DRIVE field: Single 0x08 and Continuous 0x40 match both sources; Ultra-high
# 0x04 and the self-timers 0x10/0x20 come from DRIVE; Low-speed 0x01 from a P.Fn-25
# capture. High-speed 0x02 is INFERRED (fills the Low/High/Ultra = bit 0/1/2
# sequence) -- the one unconfirmed byte here; confirm with a capture when handy.
_PFN25_FILMADV = {0x08: "Single-frame", 0x40: "Continuous",
                  0x01: "Low-speed continuous", 0x02: "High-speed continuous",
                  0x04: "Ultra-high-speed continuous",
                  0x20: "10 sec. self timer", 0x10: "2 sec. self timer"}
_PFN25_ADV_ALL = list(_PFN25_FILMADV.values())
_PFN25_AF = {0x02: "One-Shot AF", 0x04: "AI Servo AF"}   # only two options
PFN_VALUE_TODO = set()                                # all value registers located
# PREDICTED bit layouts for the disable-masks:
_PFN2_METER_BITS = {0x20: "Evaluative metering", 0x40: "Partial metering",
                    0x80: "Spot metering", 0x10: "Centerweighted averaging metering"}
# PREDICTED P.Fn-3 metering: exposure one-hot byte -> P.Fn-3 option value.
_PFN3_METER = {0x20: 0, 0x40: 1, 0x80: 2, 0x10: 3}
# P.Fn-12 sensitivity (reg cb): Standard-centred, +/-0x20 per level. Fast=0x00 and
# Slow=0x80 confirmed; Standard=0x40 (= disabled default); the two "Slightly"
# levels are interpolated.
_PFN12_SENS = {0x00: "Fast", 0x20: "Slightly fast", 0x40: "Standard",
               0x60: "Slightly slow", 0x80: "Slow"}

# Numbered option tables for the settings file -- numbers are the manual/camera
# index. Used by both the dump (value/choices) and check (resolve).
_SENS_OPTS = {0: 'Slow', 1: 'Slightly slow', 2: 'Standard',
              3: 'Slightly fast', 4: 'Fast'}
_SENS_LABEL_NUM = {v: k for k, v in _SENS_OPTS.items()}
_PFN_ENUM = {'metering': PFN_FUNCS[3][1], 'sensitivity': _SENS_OPTS,
             'density': {0: 'Dark', 1: 'Light'}}
# disable-masks: ordered (bit, name); list index = the number shown to the user
_SHOOT_MASK = list(PFN_FUNCS[1][1].items())          # [(0,'Program AE'), ...]
_METER_MASK = [(0x20, 'Evaluative metering'), (0x40, 'Partial metering'),
               (0x80, 'Spot metering'), (0x10, 'Centerweighted averaging metering')]

def decode_personal_functions(regs):
    """Decode what we can of the Personal Functions and flag predictions.
    Output is a framework: [ok] confirmed, [~] predicted, [?] unknown."""
    d3 = regs.get(0xd3, b'')
    L = ["EOS-1V Personal Functions  (FRAMEWORK -- [ok] confirmed, "
         "[~] PREDICTED, [?] unknown)",
         f"  enable bitmask d3 = {d3.hex(' ') or '(missing)'}", ""]
    for n in range(1, 31):
        name, opts = PFN_FUNCS.get(n, (f"P.Fn-{n}", {}))
        en = pfn_enabled(d3, n)
        en_tag = 'ok'                    # enable bitmask layout confirmed
        en_s = '?' if en is None else ('ON ' if en else 'off')
        # decode value (or note booleans / todo)
        if n in PFN_VALUE:
            reg, kind, conf = PFN_VALUE[n]
            d = regs.get(reg, b'')
            if kind == 'shutter' and len(d) >= 2:
                val = f"Max {_pfn_shutter(d[0], PFN4_SHUTTER_MAX)}, " \
                      f"Min {_pfn_shutter(d[1], PFN4_SHUTTER_MIN)}"
            elif kind == 'aperture' and len(d) >= 2:
                val = f"Max f/{_pfn_aperture(d[0], PFN5_APERTURE_MAX)}, " \
                      f"Min f/{_pfn_aperture(d[1], PFN5_APERTURE_MIN)}"
            elif kind == 'metering' and d:
                val = opts.get(_PFN3_METER.get(d[0]), f"?(0x{d[0]:02x})")
            elif kind == 'sensitivity' and d:
                val = _PFN12_SENS.get(d[0], f"?(0x{d[0]:02x})")
            elif kind == 'shoot_mask' and d:
                # bit order is reversed vs dialog: mode i -> bit (5-i) (Program=bit5)
                dis = [opts[i] for i in range(6)
                       if i in opts and not (d[0] & (1 << (5 - i)))]
                val = "all modes allowed" if not dis else "disables: " + ", ".join(dis)
            elif kind == 'meter_mask' and d:
                dis = [nm for bit, nm in _PFN2_METER_BITS.items() if not (d[0] & bit)]
                val = "all metering allowed" if not dis else "disables: " + ", ".join(dis)
            elif kind == 'framecount' and d:
                val = f"{d[0]} frame(s)"
            elif kind == 'density' and d:
                val = "Light" if (d[0] & 0x80) else "Dark"
            elif kind == 'clear_defaults' and len(d) >= 5:
                sm = EXPOSURE.get(d[0], f"?(0x{d[0]:02x})")
                me = METERING.get(d[2], f"?(0x{d[2]:02x})")
                fa = _PFN25_FILMADV.get(d[3], f"?(0x{d[3]:02x})")
                af = _PFN25_AF.get(d[1] & 0x7f, f"?(0x{d[1]&0x7f:02x})")
                fp = "Automatic" if (d[1] & 0x80) else "Center"
                val = (f"Shooting:{sm}, Metering:{me}, Advance:{fa}, "
                       f"AF:{af}, Point:{fp}")
            elif kind == 'timers':
                def _tsec(b):
                    return f"{((b[0]<<8)|b[1])/16:g}s" if len(b) >= 2 else "?"
                val = (f"6-sec={_tsec(regs.get(0xc7,b''))}, "
                       f"16-sec={_tsec(regs.get(0xc8,b''))}, "
                       f"post-release={_tsec(regs.get(0xc0,b''))}  (sec x16, 16-bit BE)")
            elif kind == 'booster_fps' and len(d) >= 4:
                # fps = 10 - byte/2; Low=cc[0] (mirrored in cc[1]), High=cc[2], Ultra=cc[3]
                val = (f"Ultra-high={10 - d[3]//2}, High={10 - d[2]//2}, "
                       f"Low={10 - d[0]//2} fps")
            else:
                val = f"?(reg {reg:02x} = {d.hex(' ') or 'empty'})"
            vtag = conf
        elif n == 6:
            val, vtag = "preset modes set on-camera (no P.Fn register)", 'ok'
        elif n in PFN_VALUE_TODO:
            val, vtag = "value register not yet identified", '?'
        else:
            val, vtag = "(on/off; state = enable bit)", 'ok'
        L.append(f"  [{en_tag:>2}/{vtag:>2}] P.Fn-{n:<2} {en_s}  "
                 f"{name[:34]:<34} {val}")
    L += ["",
          "Legend: first tag = enable-bit confidence, second = value confidence.",
          "All P.Fn decoded: 1,2,3,4,5,12,19,20,23,25,30 + enables + booleans;",
          "P.Fn-6 is enable-only (preset modes set on the camera body)."]
    return "\n".join(L)

# ---- Human-readable settings file (dump side) ----------------------------
# Emits one forgiving `name = value` file for the whole camera (active C.Fn +
# all P.Fn), each setting an annotated stanza:  # <ID>: <name> / # choices:.. /
# <ID> = <value>.  Values are compact and edit-friendly.  (check/apply parse this
# back; see docs.)  P.Fn are OFF unless they carry a value.

def _tsec(b):                            # timer reg bytes -> "Ns"
    return f"{((b[0]<<8)|b[1])//16}s" if len(b) >= 2 else "?"

def _pfn_dump_value(n, regs, en):
    """Compact value string for P.Fn n: 'off' when disabled, 'on' for pure
    on/off functions, else the decoded value."""
    if not en:
        return "off"
    if n == 6 or n not in PFN_VALUE:
        return "on"
    reg, kind, _ = PFN_VALUE[n]
    name, opts = PFN_FUNCS.get(n, (f"P.Fn-{n}", {}))
    d = regs.get(reg, b'')
    if kind == 'metering' and d:
        i = _PFN3_METER.get(d[0])
        return f"{i}: {_PFN_ENUM['metering'][i]}" if i is not None else f"?(0x{d[0]:02x})"
    if kind == 'sensitivity' and d:
        lbl = _PFN12_SENS.get(d[0])
        return f"{_SENS_LABEL_NUM[lbl]}: {lbl}" if lbl else f"?(0x{d[0]:02x})"
    if kind == 'shutter' and len(d) >= 2:
        return f"{_pfn_shutter(d[0], PFN4_SHUTTER_MAX)} .. {_pfn_shutter(d[1], PFN4_SHUTTER_MIN)}"
    if kind == 'aperture' and len(d) >= 2:
        return f"f/{_pfn_aperture(d[0], PFN5_APERTURE_MAX)} .. f/{_pfn_aperture(d[1], PFN5_APERTURE_MIN)}"
    if kind == 'shoot_mask' and d:
        dis = [f"{i}: {nm}" for i, (_, nm) in enumerate(_SHOOT_MASK)
               if not (d[0] & (1 << (5 - i)))]
        return ", ".join(dis) if dis else "none"
    if kind == 'meter_mask' and d:
        dis = [f"{i}: {nm}" for i, (bit, nm) in enumerate(_METER_MASK)
               if not (d[0] & bit)]
        return ", ".join(dis) if dis else "none"
    if kind == 'framecount' and d:
        return str(d[0])
    if kind == 'density' and d:
        num = 1 if (d[0] & 0x80) else 0
        return f"{num}: {_PFN_ENUM['density'][num]}"
    if kind == 'booster_fps' and len(d) >= 4:
        return f"{10 - d[3]//2} / {10 - d[2]//2} / {10 - d[0]//2}"
    if kind == 'timers':
        return (f"{_tsec(regs.get(0xc7, b''))} / {_tsec(regs.get(0xc8, b''))} / "
                f"{_tsec(regs.get(0xc0, b''))}")
    if kind == 'clear_defaults' and len(d) >= 5:
        af = _PFN25_AF.get(d[1] & 0x7f, f"?(0x{d[1]&0x7f:02x})")
        fp = "Automatic" if (d[1] & 0x80) else "Center"
        return (f"{EXPOSURE.get(d[0], '?')} / {METERING.get(d[2], '?')} / "
                f"{_PFN25_FILMADV.get(d[3], '?')} / {af} / {fp}")
    return f"?(0x{d.hex()})"

# choices/range hint per P.Fn value-kind (off is always allowed too)
_PFN_CHOICES = {
    'metering':    "Evaluative metering | Partial metering | Spot metering | "
                   "Centerweighted averaging metering",
    'sensitivity': "Slow | Slightly slow | Standard | Slightly fast | Fast",
    'shutter':     "fastest .. slowest   (range 1/8000 .. 30\")",
    'aperture':    "largest .. smallest  (range f/1.0 .. f/91)",
    'shoot_mask':  "comma-list of modes to disable, or 'none'",
    'meter_mask':  "comma-list of metering modes to disable, or 'none'",
    'framecount':  "2..36",
    'density':     "Dark | Light",
    'booster_fps': "Ultra / High / Low fps   (8-10 / 4-7 / 1-3)",
    'timers':      "6-sec / 16-sec / post-release   (seconds, 0..3600 each)",
    'clear_defaults': "shooting / metering / advance / AF / point",
}

def _choices_with_off(items):
    """Numbered enum choices (inline if short, else listed) + an 'off' option."""
    lines = _fmt_choices(items)
    if len(lines) == 1:
        return [lines[0] + " | off"]
    return lines + ["#    (or 'off' to disable)"]

def _pfn_choices(n):
    """Return the '# choices' comment line(s) for P.Fn n (a list of lines)."""
    if n == 6 or n not in PFN_VALUE:
        return ["# choices: on | off"]
    kind = PFN_VALUE[n][1]
    if kind in _PFN_ENUM:
        return _choices_with_off([f"{k}: {l}" for k, l in _PFN_ENUM[kind].items()])
    if kind in ('shoot_mask', 'meter_mask'):
        modes = ([nm for _, nm in _SHOOT_MASK] if kind == 'shoot_mask'
                 else [nm for _, nm in _METER_MASK])
        return ["# choices: comma-separated modes to disable (or 'none', or 'off'):"] \
            + [f"#    {i}: {nm}" for i, nm in enumerate(modes)]
    if kind == 'shutter':
        return ["# choices: fastest .. slowest  (any value snaps to nearest; or 'off')",
                "#    fastest: " + ", ".join(PFN4_SHUTTER_MAX),
                "#    slowest: " + ", ".join(PFN4_SHUTTER_MIN)]
    if kind == 'aperture':
        return ["# choices: largest .. smallest f-number  (snaps to nearest; or 'off')",
                "#    largest:  f/" + ", f/".join(PFN5_APERTURE_MAX),
                "#    smallest: f/" + ", f/".join(PFN5_APERTURE_MIN)]
    if kind == 'clear_defaults':
        return ["# choices: shooting / metering / advance / AF / point  (or 'off'):",
                "#    shooting: " + " | ".join(EXPOSURE.values()),
                "#    metering: " + " | ".join(METERING.values()),
                "#    advance:  " + " | ".join(_PFN25_ADV_ALL),
                "#    AF:       One-Shot AF | AI Servo AF",
                "#    point:    Automatic | Center"]
    return [f"# choices: {_PFN_CHOICES.get(kind, '?')} | off"]

_SETTINGS_HEADER = """\
# ============================================================================
#  Canon EOS-1V  --  camera settings   (eos1v_tool dump-settings)
#
#  Edit values below, then:
#     eos1v_tool.py check  this-file.txt    # validate + show what would change
#     eos1v_tool.py apply  this-file.txt    # write the changes to the camera
#
#  Forgiving format: spacing/caps don't matter; give a value by name OR number;
#  '#' starts a note; the 'choices:' lines list what's allowed; lines you don't
#  change are left exactly as they are on the camera.  A Personal Function is OFF
#  unless you give it a value (write 'off' to disable one).
# ============================================================================
"""

def current_settings(cfn_regs, pfn_regs):
    """{setting-id: canonical value string} for the current register state --
    the single source of truth shared by dump and check."""
    vals = {}
    cvals = decode_cfn_bank(cfn_regs.get(0xd1, b''))
    for n in range(0, 20):
        opts = CFN_FUNCS.get(n, ('', {}))[1]
        v = cvals.get(n)
        vals[f"C.Fn-{n}"] = (f"{v}: {opts[v]}" if v in opts
                             else (f"?(0x{v:x})" if v is not None else "?"))
    d3 = pfn_regs.get(0xd3, b'')
    for n in range(1, 31):
        vals[f"P.Fn-{n}"] = _pfn_dump_value(n, pfn_regs, pfn_enabled(d3, n))
    return vals

def _fmt_choices(items):
    """One '# choices: a | b' line, or a numbered list if that would be too long."""
    joined = " | ".join(items)
    if len(joined) <= 66:
        return [f"# choices: {joined}"]
    return ["# choices:"] + [f"#    {c}" for c in items]

def settings_dump(cfn_regs, pfn_regs):
    """Render the whole-camera settings file from C.Fn and P.Fn register sets."""
    cur = current_settings(cfn_regs, pfn_regs)
    out = [_SETTINGS_HEADER, "# === Custom Functions (active set) ==="]
    for n in range(0, 20):
        name, opts = CFN_FUNCS.get(n, (f"C.Fn-{n}", {}))
        sid = f"C.Fn-{n}"
        if n == 0:        # body-level focusing-screen setting; ES-E1 hides it and
            out += ["", f"# {sid}: {name}",   # never writes it. Show read-only only.
                    f"#   READ-ONLY -- set on the camera body only.  Currently: "
                    f"{cur[sid]}"]
            continue
        items = [f"{k}: {lbl}" for k, lbl in opts.items()]    # numbered choices
        out += ["", f"# {sid}: {name}"] + _fmt_choices(items) \
            + [f"{sid} = {cur[sid]}"]
    out += ["", "", "# === Personal Functions ==="]
    for n in range(1, 31):
        name = PFN_FUNCS.get(n, (f"P.Fn-{n}", {}))[0]
        sid = f"P.Fn-{n}"
        out += ["", f"# {sid}: {name}"] + _pfn_choices(n) + [f"{sid} = {cur[sid]}"]
    out += ["", "# (Note: the 3 switchable C.Fn banks are not yet emitted here.)"]
    return "\n".join(out)

# ---- settings file: parse + validate + diff (check) ----------------------
import re

def parse_settings(text):
    """Forgiving parse of the settings file -> ({id: value}, [notes]). Ignores
    comments/blanks; tolerates spacing, case, and an optional name before '='."""
    out, notes = {}, []
    for ln, raw in enumerate(text.splitlines(), 1):
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        if '=' not in line:
            notes.append(f"line {ln}: no '=', ignored ({raw.strip()!r})"); continue
        key, val = line.split('=', 1)
        m = re.search(r'([CP])\.?Fn-?(\d+)', key, re.I)
        if not m:
            notes.append(f"line {ln}: no C.Fn/P.Fn id, ignored ({raw.strip()!r})")
            continue
        fam = 'C.Fn' if m.group(1).upper() == 'C' else 'P.Fn'
        out[f"{fam}-{int(m.group(2))}"] = val.strip()
    return out, notes

def _match_choice(value, opts):
    """Match value to opts ({intkey: label}) by 0-based number or case-insensitive
    label. Returns the canonical label or None."""
    v = value.strip()
    if v.isdigit() and int(v) in opts:
        return opts[int(v)]
    for lbl in opts.values():
        if lbl.lower() == v.lower():
            return lbl
    return None

def _norm(s):
    return re.sub(r'\s+', ' ', s).strip().casefold()

def _resolve_numbered(v, opts):
    """Resolve v against {number: label} by 'N: label', plain number, or name.
    Returns canonical 'N: label' or None."""
    m = re.match(r'^(\d+)\s*:', v)
    if m and int(m.group(1)) in opts:
        k = int(m.group(1)); return f"{k}: {opts[k]}"
    c = _match_choice(v, opts)
    if c is not None:
        k = next(kk for kk, ll in opts.items() if ll == c)
        return f"{k}: {c}"
    return None

def _parse_tv(s):
    s = s.strip()
    if s.endswith('"'):
        try: return -math.log2(float(s[:-1]))
        except ValueError: return None
    if '/' in s:
        try: return math.log2(float(s.split('/')[1]))
        except ValueError: return None
    return None

def _parse_av(s):
    try: return 2 * math.log2(float(s.strip().lstrip('fF/')))
    except ValueError: return None

def _resolve_range(sid, kind, v):
    parts = [p.strip() for p in re.split(r'\.\.', v)]
    if len(parts) != 2:
        return None, f"{sid}: needs 'fastest .. slowest' (or 'off')", None
    a, b = parts
    if kind == 'shutter':
        pa, pb = _parse_tv(a), _parse_tv(b)
        if pa is None or pb is None:
            return None, f"{sid}: use shutter speeds like 1/1000 .. 30\"", None
        sa = min(PFN4_SHUTTER_MAX, key=lambda s: abs(_shutter_to_tv(s) - pa))
        sb = min(PFN4_SHUTTER_MIN, key=lambda s: abs(_shutter_to_tv(s) - pb))
        canon, ina, inb = f"{sa} .. {sb}", a, b
    else:
        pa, pb = _parse_av(a), _parse_av(b)
        if pa is None or pb is None:
            return None, f"{sid}: use apertures like f/1.4 .. f/22", None
        sa = min(PFN5_APERTURE_MAX, key=lambda s: abs(2*math.log2(float(s)) - pa))
        sb = min(PFN5_APERTURE_MIN, key=lambda s: abs(2*math.log2(float(s)) - pb))
        canon, ina, inb = f"f/{sa} .. f/{sb}", a.lstrip('fF/'), b.lstrip('fF/')
    note = (f"{sid}: '{v}' -> snapped to nearest, {canon}"
            if _norm(ina) != _norm(sa) or _norm(inb) != _norm(sb) else None)
    return canon, None, note

def _resolve_mask(sid, kind, v):
    if v.lower() == 'none':
        return 'none', None, None
    pool = (dict(_SHOOT_MASK) if kind == 'shoot_mask'
            else {i: nm for i, (_, nm) in enumerate(_METER_MASK)})
    canon = []
    for part in v.split(','):
        p = part.strip()
        if not p:
            continue
        c = _resolve_numbered(p, pool)
        if c is None:
            return None, f"{sid}: '{p}' is not one of the listed modes", None
        canon.append(c)
    return (", ".join(canon) if canon else "none"), None, None

def _resolve_value(sid, value):
    """Validate/canonicalize a settings value. Returns (canonical, error, note)."""
    fam, n = sid.split('-'); n = int(n); v = value.strip()
    if fam == 'C.Fn':
        if n == 0:
            return None, (f"{sid} is read-only (focusing screen -- set on the "
                          f"camera body only)"), None
        c = _resolve_numbered(v, CFN_FUNCS.get(n, ('', {}))[1])
        return (c, None, None) if c else \
            (None, f"{sid}: '{v}' is not a valid choice (use a number or name)", None)
    if v.lower() == 'off':                                # any P.Fn may be off
        return 'off', None, None
    if n == 6 or n not in PFN_VALUE:                      # on/off
        if v.lower() in ('on', 'enabled', 'yes', 'true', '1'):
            return 'on', None, None
        return None, f"{sid}: on/off only (got '{v}')", None
    kind = PFN_VALUE[n][1]
    if kind in _PFN_ENUM:
        c = _resolve_numbered(v, _PFN_ENUM[kind])
        return (c, None, None) if c else (None, f"{sid}: '{v}' is not a valid choice", None)
    if kind == 'framecount':
        if v.isdigit() and 2 <= int(v) <= 36:
            return str(int(v)), None, None
        return None, f"{sid}: frame limit must be 2..36", None
    if kind in ('shutter', 'aperture'):
        return _resolve_range(sid, kind, v)
    if kind in ('shoot_mask', 'meter_mask'):
        return _resolve_mask(sid, kind, v)
    if kind == 'clear_defaults':
        return _resolve_clear_defaults(sid, v)
    return v, None, None   # timers / booster_fps: structural compare

def _resolve_clear_defaults(sid, v):
    parts = [p.strip() for p in v.split('/')]
    if len(parts) != 5:
        return None, f"{sid}: needs shooting / metering / advance / AF / point", None
    sm = _match_name(parts[0], EXPOSURE.values())
    me = _match_name(parts[1], METERING.values())
    fa = _match_name(parts[2], _PFN25_ADV_ALL)
    af = _match_name(parts[3], _PFN25_AF.values())
    p4 = parts[4].lower()
    fp = ('Automatic' if p4.startswith('auto')
          else 'Center' if p4.startswith('cent') else None)
    bad = [lab for lab, val in (('shooting', sm), ('metering', me),
           ('advance', fa), ('AF', af), ('point', fp)) if val is None]
    if bad:
        return None, f"{sid}: unrecognized {', '.join(bad)} value", None
    if fa not in _PFN25_FILMADV.values():
        return None, f"{sid}: film advance '{fa}' not yet writable (byte unmapped)", None
    return f"{sm} / {me} / {fa} / {af} / {fp}", None, None

def settings_check(file_text, cfn_regs, pfn_regs):
    """Validate a settings file and diff it against the current register state."""
    parsed, notes = parse_settings(file_text)
    cur = current_settings(cfn_regs, pfn_regs)
    errors, changes, unknown, snaps = [], [], [], []
    for sid, rawval in parsed.items():
        if sid not in cur:
            unknown.append(sid); continue
        if _norm(rawval) == _norm(cur[sid]):      # exactly unchanged -> skip
            continue
        canon, err, note = _resolve_value(sid, rawval)
        if err:
            errors.append(err); continue
        if note:
            snaps.append(note)
        if _norm(canon) != _norm(cur[sid]):
            changes.append((sid, cur[sid], canon))
    L = []
    if notes:
        L += ["Parse notes:"] + [f"  {x}" for x in notes] + [""]
    if unknown:
        L += [f"Unknown settings ignored: {', '.join(unknown)}", ""]
    if snaps:
        L += ["Snapped to nearest valid value:"] + [f"  {x}" for x in snaps] + [""]
    if errors:
        L += [f"INVALID VALUES ({len(errors)}) -- fix before apply:"]
        L += [f"  - {e}" for e in errors] + [""]
    if changes:
        L += [f"Changes vs current camera state ({len(changes)}):"]
        L += [f"  {sid:<8} {old}  ->  {new}" for sid, old, new in changes]
    else:
        L.append("No changes vs current state.")
    L += ["", ("Fix the invalid value(s), then apply." if errors else
               (f"{len(changes)} change(s) ready to apply." if changes
                else "Nothing to apply."))]
    return "\n".join(L)

# ---- settings file: encode + apply ---------------------------------------
# Encoders are the inverse of the decoders -- value -> register bytes, working
# from the CURRENT registers and overwriting only the known fields (so reserved
# bits, C.Fn-0/byte10, and untouched registers are preserved exactly). All encode
# paths below are exact and verified against captures; the one we can't yet
# byte-exactly encode (P.Fn-25's cd metering/AF bytes) is refused, not guessed.
_PFN3_NUM_BIT = {v: k for k, v in _PFN3_METER.items()}   # option# -> metering bit
# P.Fn-25 (clear_defaults) WRITE re-enabled 2026-06-17. A logic-analyzer capture of
# ES-E1 toggling P.Fn-25 metering (data/saleae-pfn25-writes.csv) writes cd =
# 10 82 10 08 4A (Center Averaging) and 10 82 20 08 4A (Evaluative) -- the FIRST is
# byte-for-byte what our encoder produces, so the value was right all along. The
# 2026-06-16 cd "corruption" was NEITHER the value NOR the write: it was a READ bug
# -- the sync read path stripped every 0xF4 as a sync byte, and cd's checksum is
# 0xF4, so the reply came up a byte short and read as "(no data)" (fixed: see
# _sync_extract, read by length). cd is still written as ES-E1's FULL 16-register
# block (apply_compute) -- capture-faithful -- and apply verifies by reading back.
PFN_NOWRITE = set()
# P.Fn-25 reverse maps (label -> register bit), from the decode tables.
_CD_SHOOT = {v: k for k, v in EXPOSURE.items()}
_CD_METER = {v: k for k, v in METERING.items()}
_CD_ADV = {v: k for k, v in _PFN25_FILMADV.items()}
_CD_AF = {v: k for k, v in _PFN25_AF.items()}

def _match_name(s, names):
    s = s.strip().lower()
    return next((nm for nm in names if nm.lower() == s), None)

# Shutter/aperture rung -> byte: the 1/8-EV formula, then nudged so it decodes
# back to exactly that rung (handles rounded labels like 30"=32"). The result is
# guaranteed to read back as the chosen rung; it may differ <=1 LSB (1/8 EV) from
# ES-E1's exact byte, which is within the camera's own resolution.
def _byte_from_tv(label):
    return round(8 * _shutter_to_tv(label)) + 56
def _byte_from_av(label):
    return 112 - round(16 * math.log2(float(label.lstrip('fF/'))))

def _encode_rung(label, scale, formula, decode):
    b0 = max(0, min(255, formula(label)))
    for db in (0, 1, -1, 2, -2, 3, -3, 4, -4):
        b = b0 + db
        if 0 <= b <= 255 and decode(b, scale) == label:
            return b
    return b0

def _set_enable(reg, n, on):
    if reg is None:
        return
    off = 3 - (n - 1) // 8
    if 0 <= off < len(reg):
        bit = 1 << ((n - 1) % 8)
        reg[off] = (reg[off] | bit) if on else (reg[off] & ~bit & 0xff)

def encode_cfn_bank(cur, values, active):
    """cur = current bank bytes; values = {C.Fn: option#} for 1..19. Returns new
    bank bytes (one-hot), preserving C.Fn-0/byte10 and anything not listed."""
    b = bytearray(cur); b += bytes(max(0, (11 if active else 10) - len(b)))
    for n, v in values.items():
        if n == 19:
            b[9] = 1 << v
        elif 1 <= n <= 18:
            i = (n - 1) // 2
            if (n - 1) % 2 == 0:
                b[i] = (b[i] & 0xf0) | (1 << v)
            else:
                b[i] = (b[i] & 0x0f) | ((1 << v) << 4)
    return bytes(b)

def encode_pfn(cur_regs, pfn_vals):
    """pfn_vals = {P.Fn: canonical value string}. Returns new pfn register dict
    (value registers + d3/dd enable bits), preserving everything else."""
    regs = {k: bytearray(v) for k, v in cur_regs.items()}
    for n, canon in pfn_vals.items():
        on = canon.strip().lower() != 'off'
        _set_enable(regs.get(0xd3), n, on)
        if n not in (16, 21):
            _set_enable(regs.get(0xdd), n, on)
        if not on or n == 6 or n not in PFN_VALUE:
            continue
        reg, kind, _ = PFN_VALUE[n]; d = regs[reg]
        head = lambda: int(canon.split(':')[0])           # leading option number
        if kind == 'metering':
            d[0] = _PFN3_NUM_BIT[head()]
        elif kind == 'sensitivity':
            d[0] = 0x80 - head() * 0x20
        elif kind == 'density':
            d[0] = (d[0] & 0x7f) | (0x80 if head() else 0)
        elif kind == 'framecount':
            d[0] = int(canon)
        elif kind == 'shoot_mask':
            a = 0x3f
            if canon.lower() != 'none':
                for p in canon.split(','):
                    a &= ~(1 << (5 - int(p.split(':')[0])))
            d[0] = a & 0xff
        elif kind == 'meter_mask':
            a = 0xf0
            if canon.lower() != 'none':
                for p in canon.split(','):
                    a &= ~_METER_MASK[int(p.split(':')[0])][0]
            d[0] = a & 0xff
        elif kind == 'booster_fps':
            u, h, l = (int(x) for x in canon.replace(' ', '').split('/'))
            d[0] = d[1] = 2 * (10 - l); d[2] = 2 * (10 - h); d[3] = 2 * (10 - u)
        elif kind == 'timers':
            secs = [int(re.sub(r'\D', '', x)) for x in canon.split('/')]
            for r2, s in zip((0xc7, 0xc8, 0xc0), secs):
                v16 = s * 16
                regs[r2][0], regs[r2][1] = (v16 >> 8) & 0xff, v16 & 0xff
        elif kind == 'shutter':
            mx, mn = [x.strip() for x in canon.split('..')]
            d[0] = _encode_rung(mx, PFN4_SHUTTER_MAX, _byte_from_tv, _pfn_shutter)
            d[1] = _encode_rung(mn, PFN4_SHUTTER_MIN, _byte_from_tv, _pfn_shutter)
        elif kind == 'aperture':
            mx, mn = [x.strip().lstrip('fF/') for x in canon.split('..')]
            d[0] = _encode_rung(mx, PFN5_APERTURE_MAX, _byte_from_av, _pfn_aperture)
            d[1] = _encode_rung(mn, PFN5_APERTURE_MIN, _byte_from_av, _pfn_aperture)
        elif kind == 'clear_defaults':
            sm, me, fa, af, fp = [x.strip() for x in canon.split('/')]
            d[0] = _CD_SHOOT[sm]; d[2] = _CD_METER[me]; d[3] = _CD_ADV[fa]
            d[1] = _CD_AF[af] | (0x80 if fp.lower().startswith('auto') else 0)
    return {k: bytes(v) for k, v in regs.items()}

def _setting_name(sid):
    fam, n = sid.split('-'); n = int(n)
    table = CFN_FUNCS if fam == 'C.Fn' else PFN_FUNCS
    return table.get(n, ('', {}))[0]

def restore_diff(backup, current, order):
    """Read-commands in `order` whose backup bytes differ from the camera's current
    bytes. An unreadable/missing current register (b'') counts as differing, so a
    corrupt register still gets restored. Used by write-pfn/write-cfn to write only
    what changed -- fewer EEPROM writes, and it sidesteps the full-block write that
    the camera rejects on its final register."""
    return [rc for rc in order
            if bytes(backup.get(rc, b'')) != bytes(current.get(rc, b''))]

def apply_compute(file_text, cfn_regs, pfn_regs):
    """Validate + diff a settings file and build the registers to write. Returns
    dict(errors, changes, skipped, new_cfn, new_pfn)."""
    parsed, _ = parse_settings(file_text)
    cur = current_settings(cfn_regs, pfn_regs)
    errors, changes, skipped, cfn_vals, pfn_vals = [], [], [], {}, {}
    for sid, raw in parsed.items():
        if sid not in cur:
            continue
        if _norm(raw) == _norm(cur[sid]):         # exactly unchanged -> skip
            continue
        canon, err, _note = _resolve_value(sid, raw)
        if err:
            errors.append(err); continue
        if _norm(canon) == _norm(cur[sid]):
            continue
        fam, n = sid.split('-'); n = int(n)
        if fam == 'P.Fn' and n in PFN_VALUE and PFN_VALUE[n][1] in PFN_NOWRITE:
            skipped.append((sid, cur[sid], canon)); continue
        changes.append((sid, cur[sid], canon))
        if fam == 'C.Fn':
            cfn_vals[n] = int(canon.split(':')[0])
        else:
            pfn_vals[n] = canon
    new_cfn = new_pfn = None
    cfn_only = pfn_only = None
    if not errors:
        if cfn_vals:
            new_cfn = dict(cfn_regs)
            new_cfn[0xd1] = encode_cfn_bank(cfn_regs.get(0xd1, b''), cfn_vals, True)
            # write ONLY the registers that actually moved (spare EEPROM wear --
            # an active-bank C.Fn change is just d1; the 3 banks are untouched)
            cfn_only = {rc for rc in CFN_WRITE_ORDER
                        if new_cfn.get(rc) != cfn_regs.get(rc)}
        if pfn_vals:
            new_pfn = encode_pfn(pfn_regs, pfn_vals)
            pfn_only = {rc for rc in PFN_WRITE_ORDER
                        if new_pfn.get(rc) != pfn_regs.get(rc)}
            # P.Fn-25 (cd) writes selectively like everything else. (We briefly
            # forced a full-block write for it, suspecting selective writes caused
            # the cd "corruption" -- but that was a READ bug, since fixed. A cd
            # change is just the cd register, well under the EEPROM commit stall.)
    return dict(errors=errors, changes=changes, skipped=skipped,
                new_cfn=new_cfn, new_pfn=new_pfn,
                cfn_only=cfn_only, pfn_only=pfn_only)

def apply_summary(res):
    L = []
    if res['skipped']:
        L.append("Skipped (not yet writable via apply -- change on the camera):")
        L += [f"  {s}: {o}  ->  {n}" for s, o, n in res['skipped']] + [""]
    if not res['changes']:
        return "\n".join(L + ["No changes to apply."])
    L.append(f"The following {len(res['changes'])} setting(s) will change:")
    for sid, old, new in res['changes']:
        L.append(f"  {sid}  {_setting_name(sid)}")
        L.append(f"      {old}   ->   {new}")
    # only=None means a full-block write (every register in that category)
    def _n(only, order, present):
        return (len(order) if only is None else len(only)) if present else 0
    nreg = (_n(res.get('cfn_only'), CFN_WRITE_ORDER, res['new_cfn'] is not None)
            + _n(res.get('pfn_only'), PFN_WRITE_ORDER, res['new_pfn'] is not None))
    cats = [c for c, on in (("C.Fn", res['new_cfn']), ("P.Fn", res['new_pfn'])) if on]
    L += ["", f"Writing {nreg} changed register(s) in: {', '.join(cats)}"]
    return "\n".join(L)

def diff_functions(a, b):
    """Compare two register sets (each {cmd: data_bytes}) and report exactly what
    moved -- per byte, per nibble, per bit. This is the calibration workhorse:
    read at a known state, change ONE function on the camera, read again, diff,
    and the single field that moved pins that function's byte/nibble/bit."""
    lines = []
    cmds = list(a.keys()) + [c for c in b if c not in a]
    for c in cmds:
        da, db = a.get(c), b.get(c)
        if da is None:
            lines.append(f"reg {c:02x}: only in second  ({db.hex(' ')})"); continue
        if db is None:
            lines.append(f"reg {c:02x}: only in first   ({da.hex(' ')})"); continue
        if da == db:
            continue
        lines.append(f"reg {c:02x}: CHANGED")
        if len(da) != len(db):
            lines.append(f"    length {len(da)} -> {len(db)}")
        for i in range(max(len(da), len(db))):
            ba = da[i] if i < len(da) else None
            bb = db[i] if i < len(db) else None
            if ba == bb:
                continue
            sa = '--' if ba is None else f"{ba:02x}"
            sb = '--' if bb is None else f"{bb:02x}"
            note = ""
            if ba is not None and bb is not None:
                x = ba ^ bb
                bits = ' '.join(f"bit{7-k}" for k in range(8) if (x >> (7-k)) & 1)
                nib = []
                if (ba >> 4) != (bb >> 4): nib.append(f"hi {ba>>4:x}->{bb>>4:x}")
                if (ba & 0xf) != (bb & 0xf): nib.append(f"lo {ba&0xf:x}->{bb&0xf:x}")
                note = f"   ({', '.join(nib)}; {bits})"
            lines.append(f"    byte {i:>2}: {sa} -> {sb}{note}")
    if not lines:
        return "No differences -- the two register sets are identical."
    return "\n".join(lines)

def load_raw_blocks(path):
    """Parse the raw dump written during download: lines 'CMDHEX RESPHEX'."""
    blocks=[]
    for line in open(path,'r').read().splitlines():
        line=line.strip()
        if not line: continue
        parts=line.split()
        cmd=int(parts[0],16)
        resp=bytes.fromhex(parts[1]) if len(parts)>1 else b''
        p=EOS1V.parse(resp)
        if p: blocks.append((cmd, p[2]))
    return blocks

if __name__=='__main__':
    main()
