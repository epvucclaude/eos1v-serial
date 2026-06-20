#!/usr/bin/env python3
"""Decode regression test: compare the tool's decode of saved raw dumps against
the Windows ES-E1 CSV exports, field by field.

Drop matching files into data/ (any subset works):
  * raw dumps the tool produced:  *.bin  or  raw*.txt   (lines "CMDHEX RESPHEX")
  * ES-E1 exports:                *.CSV / *.csv         (the Windows ground truth)

The test keys every frame by (Film ID, Frame No.) and compares the overlap, so
you only need to drop in the pairs you care about. It reports matched/total and
lists any mismatches. The whole project history reached 0 mismatches across 30
rolls; keep it that way.

Run:  python tests/test_decode.py
"""
import sys, pathlib, csv, tempfile, os, glob

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import eos1v_tool as tool
DATA = ROOT / 'data'

# ES-E1 column name -> (our column, transform)
def _date(s):
    s = s.strip()
    if '/' in s:
        m, d, y = s.split('/'); return f"{y}-{int(m):02d}-{int(d):02d}"
    return s
def _foc(s): return s if (not s or s.endswith('mm')) else s + 'mm'
def _tv(s):  return s.replace('="', '').replace('"', '')
FIELDS = [
    (('Focal length',), 'Focal length', _foc),
    (('Max. aperture', 'Max aperture'), 'Max aperture', str),
    (('Tv',), 'Tv', _tv),
    (('Av',), 'Av', str),
    (('ISO (M)',), 'ISO (M)', str),
    (('Exposure compensation',), 'Exposure compensation', str),
    (('Flash exposure compensation',), 'Flash exposure compensation', str),
    (('Flash mode',), 'Flash mode', str),
    (('Metering mode',), 'Metering mode', str),
    (('Shooting mode',), 'Shooting mode', str),
    (('Film advance mode', 'Film advance'), 'Film advance', str),
    (('AF mode',), 'AF mode', str),
    (('Multiple exposure',), 'Multiple exposure', str),
    (('Date',), 'Date', _date),
    (('Time',), 'Time', str),
]


def decode_mine():
    """Decode every raw dump in data/ -> {(film, frame): row}."""
    mine = {}
    files = sorted(glob.glob(str(DATA / '*.bin')) +
                   [p for p in glob.glob(str(DATA / 'raw*.txt'))])
    for path in files:
        blocks = tool.load_raw_blocks(path)
        films = tool.split_films_from_raw(blocks)
        fd, tmp = tempfile.mkstemp(suffix='.csv'); os.close(fd)
        tool.films_to_csv(films, tmp)
        for r in csv.DictReader(open(tmp)):
            mine[(r['Film'], r['Frame'])] = r
        os.unlink(tmp)
    return mine, files


def parse_es_e1(path):
    """Parse an ES-E1 export -> {(film, frame): {colname: value}}."""
    rows = list(csv.reader(open(path)))
    out = {}; hdr = None; film = None
    for r in rows:
        if len(r) > 2 and r[1] == 'Film ID':
            film = r[2].strip()
        elif len(r) > 2 and r[1].strip() in ('Frame No.', 'Frame'):
            hdr = [c.strip() for c in r]
        elif film and hdr and len(r) > 2 and r[1].strip().isdigit():
            d = {hdr[i]: (r[i].strip() if i < len(r) else '') for i in range(len(hdr))}
            frame = r[1].strip()
            out[(film, frame)] = d
    return out


def get(d, names):
    for n in names:
        if n in d:
            return d[n].replace('="', '').replace('"', '').strip()
    return None


def main():
    mine, raw_files = decode_mine()
    truth = {}
    for path in glob.glob(str(DATA / '*.CSV')) + glob.glob(str(DATA / '*.csv')):
        if pathlib.Path(path).name.lower().startswith('my'):
            continue                       # skip our own csv outputs if present
        try:
            truth.update(parse_es_e1(path))
        except Exception:
            pass
    if not mine or not truth:
        print("decode test: SKIPPED (need raw dumps AND ES-E1 .CSV exports in data/)")
        print(f"  raw dumps found: {len(raw_files)}; ground-truth frames: {len(truth)}")
        return
    tot = mis = 0; examples = []; compared = 0
    for key, wr in truth.items():
        m = mine.get(key)
        if not m:
            continue
        compared += 1
        for names, mc, fn in FIELDS:
            gv = get(wr, names)
            if gv is None:
                continue
            gv = fn(gv); dv = str(m.get(mc, '')).strip()
            tot += 1
            if gv != dv and not (gv == '' and dv == ''):
                mis += 1
                if len(examples) < 30:
                    examples.append(f"{key} {mc}: es-e1='{gv}' mine='{dv}'")
    print(f"decode regression: {tot - mis}/{tot} fields match "
          f"across {compared} frames ({mis} mismatches)")
    for e in examples:
        print("  ", e)
    assert mis == 0, "decode mismatches present"


if __name__ == '__main__':
    main()
