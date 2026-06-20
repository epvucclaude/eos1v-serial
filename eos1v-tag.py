#!/usr/bin/env python3
"""
eos1v-tag - Apply EXIF metadata from an eos1v_tool.py CSV log to scanned/
             developed image files, based on a filename pattern.

Usage:
    eos1v-tag.py [options] <csv-file> <pattern>

<pattern> is a filename glob/regex containing exactly one frame-number
placeholder, "%d", e.g.:

    eos1v-tag.py log.csv "20260612-eos1v-26-350-my-vacation-pictures-%d.jpg"

The %d is replaced internally by a regex group that matches one or more
digits, used to:
  1. find matching files in the target directory (default: current dir)
  2. determine which CSV row (by frame number) supplies the EXIF data

The CSV is expected to be in the format produced by eos1v_tool.py's
"download"/"decode" commands (header row with named columns: Film,
Film loaded date, Film loaded time, Frame, Focal length, Max aperture,
Tv, Av, ISO (DX), ISO (M), Exposure compensation, Flash exposure
compensation, Shooting mode, Metering mode, Flash mode, Film advance,
AF mode, Multiple exposure, Date, Time, Battery date, Battery time).

Options:
    --film FILM       Only use rows whose Film column equals FILM
                       (e.g. "26-350"). If omitted and the CSV contains
                       more than one roll, you must disambiguate this way
                       or frame numbers may be ambiguous.
    --dir DIR         Directory to search for matching files (default: .)
    --dry-run         Show what would be written, but don't modify files
    -v, --verbose     Print details about each tag set/omitted
    -n, --frame-offset N
                       Add N to the frame number parsed from the filename
                       before looking it up in the CSV (default 0). Useful
                       if your filenames don't start numbering at the
                       camera's frame 1.

Notes:
- It is better to omit a tag than to write an incorrect one. Any field
  that is missing, blank, "?(0xNN)" (unrecognized raw value), or that
  cannot be parsed is simply skipped.
- Lens make/model are NOT written by this tool (handled separately).
  Make is set to "Canon" and Model to "EOS-1V" unconditionally, per the
  camera body.
"""

import sys
import os
import re
import csv
import shutil
import argparse
import subprocess
from fractions import Fraction


# ----------------------------------------------------------------------
# Mappings from eos1v_tool.py's human-readable strings to EXIF numeric
# values.  Only modes with a real EXIF equivalent are mapped; anything
# else is left untagged.  These are written with exiftool's "#" raw-value
# suffix (e.g. -ExposureProgram#=2) so the exact EXIF integer lands in the
# file rather than relying on exiftool's human-readable spelling.
# ----------------------------------------------------------------------

# Exif.Photo.ExposureProgram
EXPOSURE_PROGRAM = {
    "Program AE": 2,
    "Shutter-speed-priority AE": 4,
    "Aperture-priority AE": 3,
    "Manual exposure": 1,
    # "Bulb" and "Depth-of-field AE" have no distinct EXIF ExposureProgram
    # value that matches the camera's actual behaviour closely enough;
    # omit rather than guess.
}

# Exif.Photo.MeteringMode
METERING_MODE = {
    "Evaluative": 5,         # Pattern
    "Partial": 6,
    "Spot": 3,
    "Center Averaging": 2,
    # "Center-weighted Average" = 2 also; Canon's "Center Averaging"
    # matches that best.
}

# Exif.Photo.Flash (bit 0 = flash fired)
# eos1v_tool.py only distinguishes OFF / TTL autoflash / E-TTL, and we
# can't tell if flash actually *fired* vs was just armed in some modes,
# so only map the unambiguous "OFF" case (flash did not fire, bit0=0).
FLASH_MODE = {
    "OFF": 0x00,
    # "TTL autoflash" / "E-TTL" -> ambiguous fired/not-fired + return-light
    # state from this data alone; omit.
}

# exiftool tag names used.  exiftool accepts human-friendly values for the
# rational tags (e.g. ExposureTime="1/320", FNumber="6.3",
# MaxApertureValue="2.5" as an f-number which it converts to APEX itself)
# and writes the correct EXIF field type, so no manual rational/APEX
# conversion is needed.


def parse_shutter_speed(tv):
    """Tv field like '1/320', '1/13', '1', '0.3' -> Fraction, or None."""
    tv = (tv or "").strip()
    if not tv:
        return None
    m = re.match(r'^(\d+)\s*/\s*(\d+)$', tv)
    if m:
        return Fraction(int(m.group(1)), int(m.group(2)))
    try:
        return Fraction(tv).limit_denominator(100000)
    except (ValueError, ZeroDivisionError):
        return None


def parse_decimal(value):
    """Generic 'x.y' field -> Fraction, or None if blank/unparseable."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return Fraction(value).limit_denominator(10000)
    except (ValueError, ZeroDivisionError):
        return None


def parse_focal_length(fl):
    """'50mm' -> Fraction(50), or None."""
    fl = (fl or "").strip()
    m = re.match(r'^(\d+(?:\.\d+)?)\s*mm$', fl, re.IGNORECASE)
    if not m:
        return None
    try:
        return Fraction(m.group(1)).limit_denominator(1000)
    except (ValueError, ZeroDivisionError):
        return None


def parse_iso(iso_m, iso_dx):
    """ISO (M) overrides ISO (DX); both may be blank. Returns int or
    None."""
    for val in (iso_m, iso_dx):
        val = (val or "").strip()
        if val:
            try:
                return int(float(val))
            except ValueError:
                continue
    return None


def parse_exif_date(date_str, time_str):
    """'YYYY-MM-DD' + 'HH:MM:SS' -> 'YYYY:MM:DD HH:MM:SS', or None."""
    date_str = (date_str or "").strip()
    time_str = (time_str or "").strip()
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', date_str)
    if not m:
        return None
    if not re.match(r'^\d{2}:\d{2}:\d{2}$', time_str):
        time_str = "00:00:00"
    y, mo, d = m.groups()
    return f"{y}:{mo}:{d} {time_str}"


def load_csv_rows(csv_path, film=None):
    """Load rows from an eos1v_tool.py CSV.

    Returns: dict mapping frame_number (int) -> row (dict).

    If `film` is given, only rows with that Film value are kept. If not
    given and the CSV contains more than one distinct Film value, raises
    ValueError (ambiguous).
    """
    rows_by_film = {}
    with open(csv_path, newline='') as f:
        reader = csv.DictReader(f)
        required = {'Film', 'Frame'}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(
                f"{csv_path}: doesn't look like an eos1v_tool.py CSV "
                f"(missing columns {required - set(reader.fieldnames or [])})"
            )
        for row in reader:
            f_id = row['Film'].strip()
            rows_by_film.setdefault(f_id, {})
            try:
                frame_no = int(row['Frame'].strip())
            except (ValueError, AttributeError):
                continue
            # If multiple rows share a frame number (multiple-exposure
            # continuations), keep the first one only.
            if frame_no not in rows_by_film[f_id]:
                rows_by_film[f_id][frame_no] = row

    if film:
        if film not in rows_by_film:
            raise ValueError(
                f"No rows for Film '{film}' in {csv_path}. "
                f"Available: {', '.join(sorted(rows_by_film))}"
            )
        return rows_by_film[film]

    if len(rows_by_film) == 1:
        return next(iter(rows_by_film.values()))

    raise ValueError(
        f"{csv_path} contains multiple rolls "
        f"({', '.join(sorted(rows_by_film))}); "
        f"use --film to select one."
    )


def _decimal_str(frac):
    """Format a Fraction as a short decimal string for exiftool, e.g.
    Fraction(63, 10) -> '6.3', Fraction(50) -> '50'."""
    return f"{float(frac):g}"


def build_exif_updates(row, verbose=False):
    """Given a CSV row dict, return a list of exiftool assignment args
    (e.g. ['-ExposureTime=1/320', '-ExposureProgram#=2', ...]), omitting
    anything blank/unparseable per the omit-over-guess policy.

    exiftool is handed human-friendly values and figures out the EXIF
    field type/encoding itself: 'ExposureTime=1/320' becomes a RATIONAL,
    'FNumber=6.3' a RATIONAL, 'MaxApertureValue=2.5' is taken as an
    f-number and converted to the APEX rational, and a negative
    'ExposureCompensation=-1' becomes an SRATIONAL.  The '#' suffix on the
    enum tags writes the raw EXIF integer rather than a print-conversion
    string."""
    args = []
    skipped = []

    # Make / Model: always Canon EOS-1V for this body.
    args.append("-Make=Canon")
    args.append("-Model=EOS-1V")

    # Shutter speed -> ExposureTime
    tv = parse_shutter_speed(row.get('Tv'))
    if tv is not None and tv > 0:
        args.append(f"-ExposureTime={tv.numerator}/{tv.denominator}")
    else:
        skipped.append(('ExposureTime', row.get('Tv')))

    # Aperture -> FNumber
    av = parse_decimal(row.get('Av'))
    if av is not None and av > 0:
        args.append(f"-FNumber={_decimal_str(av)}")
    else:
        skipped.append(('FNumber', row.get('Av')))

    # Max aperture -> MaxApertureValue (exiftool converts f-number -> APEX)
    maxav = parse_decimal(row.get('Max aperture'))
    if maxav is not None and maxav > 0:
        args.append(f"-MaxApertureValue={_decimal_str(maxav)}")
    else:
        skipped.append(('MaxApertureValue', row.get('Max aperture')))

    # ISO
    iso = parse_iso(row.get('ISO (M)'), row.get('ISO (DX)'))
    if iso is not None and iso > 0:
        args.append(f"-ISO={iso}")
    else:
        skipped.append(('ISO',
                         f"M={row.get('ISO (M)')!r} DX={row.get('ISO (DX)')!r}"))

    # Focal length
    fl = parse_focal_length(row.get('Focal length'))
    if fl is not None and fl > 0:
        args.append(f"-FocalLength={_decimal_str(fl)}")
    else:
        skipped.append(('FocalLength', row.get('Focal length')))

    # Exposure compensation
    comp = parse_decimal(row.get('Exposure compensation'))
    if comp is not None:
        args.append(f"-ExposureCompensation={_decimal_str(comp)}")
    else:
        skipped.append(('ExposureCompensation',
                         row.get('Exposure compensation')))

    # Shooting mode -> ExposureProgram (raw EXIF integer via '#')
    mode = (row.get('Shooting mode') or '').strip()
    if mode in EXPOSURE_PROGRAM:
        args.append(f"-ExposureProgram#={EXPOSURE_PROGRAM[mode]}")
    else:
        skipped.append(('ExposureProgram', mode))

    # Metering mode
    metering = (row.get('Metering mode') or '').strip()
    if metering in METERING_MODE:
        args.append(f"-MeteringMode#={METERING_MODE[metering]}")
    else:
        skipped.append(('MeteringMode', metering))

    # Flash mode
    flash = (row.get('Flash mode') or '').strip()
    if flash in FLASH_MODE:
        args.append(f"-Flash#={FLASH_MODE[flash]}")
    else:
        skipped.append(('Flash', flash))

    # Date/time -> DateTimeOriginal + ModifyDate (the EXIF DateTime tag)
    dt = parse_exif_date(row.get('Date'), row.get('Time'))
    if dt:
        args.append(f"-DateTimeOriginal={dt}")
        args.append(f"-ModifyDate={dt}")
    else:
        skipped.append(('DateTimeOriginal',
                         f"{row.get('Date')!r} {row.get('Time')!r}"))

    if verbose and skipped:
        for tag, val in skipped:
            print(f"    (omitting {tag}: unparseable/blank value {val!r})")

    return args


def apply_exif(image_path, exif_args, dry_run=False, verbose=False):
    """Write the given exiftool assignment args into image_path in place.

    Uses exiftool's metadata-only edit, which rewrites just the EXIF/APP1
    segment and leaves the JPEG image data byte-for-byte untouched (unlike
    a Pillow re-save, which re-encodes the pixels and causes generation
    loss).  -P preserves the file's modification timestamp."""
    cmd = ["exiftool", "-overwrite_original", "-P", *exif_args, image_path]

    if verbose:
        print(f"    exiftool args: {exif_args}")

    if dry_run:
        return

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"exiftool failed for {image_path}: {msg}")
    # exiftool reports warnings (e.g. minor tag issues) on stderr even on
    # success; surface them when verbose.
    if verbose and proc.stderr.strip():
        print(f"    exiftool: {proc.stderr.strip()}")


def build_pattern_regex(pattern):
    """Turn a filename pattern containing one '%d' into a compiled regex
    with a named group 'frame', plus the literal prefix/suffix for
    building a glob to scan a directory cheaply."""
    if pattern.count('%d') != 1:
        raise ValueError("pattern must contain exactly one '%d'")
    prefix, suffix = pattern.split('%d')
    regex = re.escape(prefix) + r'(?P<frame>\d+)' + re.escape(suffix)
    return re.compile('^' + regex + '$')


def main():
    ap = argparse.ArgumentParser(
        description="Apply eos1v_tool.py CSV exposure data as EXIF tags "
                     "to image files matched by filename pattern.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument('csv_file', help="CSV file produced by eos1v_tool.py")
    ap.add_argument('pattern',
                     help="Filename pattern containing one '%%d' for the "
                          "frame number, e.g. "
                          "'20260612-eos1v-26-350-vacation-%%d.jpg'")
    ap.add_argument('--film', help="Film ID to use (e.g. '26-350'); "
                                    "required if the CSV has multiple rolls")
    ap.add_argument('--dir', default='.',
                     help="Directory to scan for matching files (default: .)")
    ap.add_argument('--dry-run', action='store_true',
                     help="Don't modify files, just report what would happen")
    ap.add_argument('-n', '--frame-offset', type=int, default=0,
                     help="Add this offset to the frame number parsed from "
                          "the filename before looking it up (default 0)")
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args()

    if not args.dry_run and shutil.which("exiftool") is None:
        print("error: exiftool not found on PATH; install it (e.g. "
              "'brew install exiftool') or use --dry-run.", file=sys.stderr)
        return 1

    try:
        rows_by_frame = load_csv_rows(args.csv_file, film=args.film)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        rx = build_pattern_regex(args.pattern)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    matched_any = False
    for fname in sorted(os.listdir(args.dir)):
        m = rx.match(fname)
        if not m:
            continue
        matched_any = True
        frame_no = int(m.group('frame')) + args.frame_offset
        path = os.path.join(args.dir, fname)

        row = rows_by_frame.get(frame_no)
        if row is None:
            print(f"{fname}: no CSV row for frame {frame_no}, skipping")
            continue

        # Show focal length / max aperture in the progress line as an aid
        # to the (manual) lens-association step. "-" when absent from the CSV.
        fl = parse_focal_length(row.get('Focal length'))
        maxav = parse_decimal(row.get('Max aperture'))
        fl_str = f"{float(fl):g}mm" if fl is not None and fl > 0 else "-"
        av_str = f"F{float(maxav):g}" if maxav is not None and maxav > 0 else "-"

        print(f"{fname}: frame {frame_no} : {fl_str} {av_str}"
              f"{' (dry run)' if args.dry_run else ''}")
        exif_args = build_exif_updates(row, verbose=args.verbose)
        try:
            apply_exif(path, exif_args,
                       dry_run=args.dry_run, verbose=args.verbose)
        except Exception as e:
            print(f"  error processing {fname}: {e}", file=sys.stderr)

    if not matched_any:
        print(f"No files in '{args.dir}' matched the pattern.", file=sys.stderr)
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
