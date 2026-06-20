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
