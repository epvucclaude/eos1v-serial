Note: I didn't write any of this, it was all spewed out by claude code based on dissecting before/after usb bus traces while I painstakingly changed every goddamn setting on the camera using the old windows xp software. Nevertheless it appears to work on my camera and has not caused anything bad to happen despite heavily exercising all the functions. Its reverse engineering of the shooting data storage format also appears to correspond to reality.  I mostly just use it to download the shooting records to a CSV for conversion into EXIF tags, but all the camera settings are also supported for reading and writing, basically you just read the current settings out into a text file, edit the file as desired, and write it back to the camera. I should turn it into a web app or something, I guess. 

# wiring:
The camera side is the "Canon N3" connector, which has 3 pins: https://martybugs.net/blog/images/n3_connector.png  Ground is still ground, "Shutter" is data *from* the camera, and "Focus" is data *to* the camera. There are no modem control or other signals. 

I bought this shutter release cable on Amazon: https://www.amazon.com/dp/B071R7SC3D
It has a N3 to 2.5mm TRS cable, so I wired a 2.5mm socket to a USB-serial adapter (https://www.amazon.com/dp/B0FGX42GYB , though anything will work, it runs at only 9600 baud) 

The tip end is data *out* from the camera, the middle ring is data "into" the camera, and the bottom sleeve is ground.  There's no voltage or logic conversion needed, the usb-uarrt dongle is jumpered for 5v, tip to RX, ring to TX, sleeve to GND. 

The only downside to using a DIY usb-uart cable is that you should power up the USB side before connecting the cable to the camera, or else the camera sometimes sees plugging it in as a "button press" instead and fires the shutter. When powered up already this doesn't happen.  The Canon cable avoids this by having an electrically high impedance facing the camera when unpowered.  

# tagging
there's a sort of halfassed tool eos1v-tag.py included for reading the CSV this generates when downloading shooting data from the camera, and converting it into invocations of exiftool on image files that match a template, so you can apply the CSV to a folder full of files like "20260823-eos1v-canon35f1p4-delta100-d76-###.jpg" and it will write exif tags to those files with the correct data like date, time, shutter speed, aperture, focal length, ISO, flash, etc. 

# eos1v_tool

Talk to a **Canon EOS-1V** over the discontinued Canon **ES-E1 / "EOS USB Cable"**
(USB `04a9:3040`) with no Windows and no ES-E1 software. Download the in-camera
shooting data, set the clock, erase the data memory, back up/restore Custom
Functions, and **decode every Custom and Personal Function to its named setting** —
all reverse-engineered from `usbmon` captures and on-camera calibration, validated
field-for-field against the ES-E1 CSV exports (30 rolls, 0 mismatches) and the
ES-E1 settings UI.

See **docs/EOS-1V-protocol-notes.md** for the protocol reference.

## Install

```
pip install pyusb        # only needed for live camera operations; libusb must be installed
```

On Linux, live operations need root or a udev rule for `04a9:3040`. Put the camera
on a charged battery and connect it; the tool wakes it (it beeps, like ES-E1 did).

## Use

```
python eos1v_tool.py download out.csv raw.txt   # pull all shooting data -> CSV (+ raw dump)
python eos1v_tool.py decode raw.txt out.csv      # decode a saved dump offline (no camera)
python eos1v_tool.py probe                        # diagnostic: wake + read/decode first frame
python eos1v_tool.py set-clock now                # or an ISO 8601 time
python eos1v_tool.py erase-all                    # DESTRUCTIVE: prompts for typed confirmation
python eos1v_tool.py read-cfn cfn-backup.txt      # read Custom Functions (raw registers)
python eos1v_tool.py read-pfn pfn-backup.txt      # read Personal Functions (raw registers)
python eos1v_tool.py write-cfn cfn-backup.txt     # restore Custom Functions from a backup
python eos1v_tool.py write-pfn pfn-backup.txt     # restore Personal Functions from a backup
python eos1v_tool.py decode-cfn cfn-backup.txt    # decode C.Fn registers -> named settings
python eos1v_tool.py decode-pfn pfn-backup.txt    # decode P.Fn registers -> named settings
python eos1v_tool.py read-items [raw.txt]         # show the "data items to be recorded" mask
python eos1v_tool.py dump-fn  cfn-backup.txt      # annotated byte/nibble/bit layout (offline)
python eos1v_tool.py diff-fn  a.txt b.txt          # diff two register backups (offline)
```

The `decode-*`, `dump-fn`, and `diff-fn` commands work offline on a saved backup
(no camera). Add `-v` for a step trace (and to include the raw hex column in the
CSV). Custom and Personal Function encodings are documented in
`docs/EOS-1V-protocol-notes.md` §4.

## Test

```
python tests/run_all.py
```

No hardware needed — it runs the mock-camera simulations (protocol, writes,
functions) and a decode regression against captures and ES-E1 CSV exports.

> **Note:** the test fixtures live in a `data/` directory that is **not included
> in this repository** (it holds personal film-roll exposure data and
> logic-analyzer captures). Supply your own `data/` — captures plus the matching
> ES-E1 exports, named as the tests expect — to run the full suite.
