#!/usr/bin/env python3
"""Validate a MIMA manifest.csv before the QC step.

Usage:
    validate_manifest.py MANIFEST.csv [--reads-dir /abs/raw_reads] [--check-fs]

Required format: header exactly 'Sample_ID,FileID_R1,FileID_R2' (case-sensitive),
one paired-end sample per row. File names are relative to the QC -i directory.
--check-fs (with --reads-dir) confirms the FASTQ files exist.

Exit code: 0 = valid (warnings allowed), 1 = errors, 2 = cannot read file.
"""
import argparse
import csv
import io
import re
import sys
from pathlib import Path

HEADER = ["Sample_ID", "FileID_R1", "FileID_R2"]
ID_BAD = re.compile(r"[^A-Za-z0-9_]+")  # keep in step with prepare_inputs.py (a test checks this)
FASTQ_EXT = (".fq.gz", ".fastq.gz", ".fq", ".fastq", ".fq.bz2", ".fastq.bz2")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest")
    ap.add_argument("--reads-dir", help="Directory the file names are relative to (the QC -i path)")
    ap.add_argument("--check-fs", action="store_true", help="Check that the FASTQ files exist in --reads-dir")
    args = ap.parse_args()

    try:
        raw = Path(args.manifest).read_bytes()
    except OSError as exc:
        print(f"Cannot read {args.manifest}: {exc}", file=sys.stderr)
        return 2

    errors, warns = [], []
    if raw.startswith(b"\xef\xbb\xbf"):
        errors.append("File starts with a UTF-8 byte-order mark, which corrupts the first header name. Re-save as plain UTF-8 without BOM.")
        raw = raw[3:]
    if b"\r" in raw:
        errors.append("File has Windows (CRLF) line endings, which corrupt the last column. Fix with: sed -i 's/\\r$//' manifest.csv")
        raw = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

    text = raw.decode("utf-8", errors="replace")
    rows = [r for r in csv.reader(io.StringIO(text)) if r and any(c.strip() for c in r)]
    if not rows:
        errors.append("Manifest is empty.")
    else:
        header, body = rows[0], rows[1:]
        if header != HEADER:
            near = [h for h in header if h.strip().lower() in [x.lower() for x in HEADER]]
            hint = f" Found: {header}." + (" Header names are case-sensitive and must not contain spaces." if near else "")
            errors.append(f"Header must be exactly {','.join(HEADER)}.{hint}")
        if not body:
            errors.append("Manifest has a header but no samples.")
        seen_ids, seen_files = {}, {}
        for n, row in enumerate(body, start=2):
            if len(row) != 3:
                errors.append(f"Line {n}: expected 3 columns, found {len(row)}.")
                continue
            sid, r1, r2 = row
            for col, val in zip(HEADER, row):
                if not val.strip():
                    errors.append(f"Line {n}: {col} is empty.")
                elif val != val.strip():
                    errors.append(f"Line {n}: {col} has leading or trailing spaces: '{val}'.")
            if ID_BAD.search(sid):
                warns.append(f"Line {n}: Sample_ID '{sid}' has characters other than letters, digits and underscores. Sample_ID names every "
                             "output file, so the project convention is letters, digits and underscores only (general practice, not proven to be "
                             "required). prepare_inputs.py renames safely and keeps a mapping.")
            if sid in seen_ids:
                errors.append(f"Line {n}: duplicate Sample_ID '{sid}' (first on line {seen_ids[sid]}).")
            seen_ids.setdefault(sid, n)
            if r1 and r1 == r2:
                errors.append(f"Line {n}: forward and reverse file are the same ('{r1}').")
            for f in (r1, r2):
                if f in seen_files and seen_files[f] != n:
                    errors.append(f"Line {n}: file '{f}' is already used on line {seen_files[f]}.")
                seen_files.setdefault(f, n)
                if f.startswith("/"):
                    warns.append(f"Line {n}: '{f}' is an absolute path. File names are joined to the QC -i directory, so use names only.")
                elif f and not f.endswith(FASTQ_EXT):
                    warns.append(f"Line {n}: '{f}' does not end in a common FASTQ extension.")
            if r1 and r2 and not any(t in r1 for t in ("_1", "_R1", ".1")):
                warns.append(f"Line {n}: forward file '{r1}' does not look like read 1 (_1 / _R1). Check R1 and R2 are not swapped.")
            if r1 and r2 and not any(t in r2 for t in ("_2", "_R2", ".2")):
                warns.append(f"Line {n}: reverse file '{r2}' does not look like read 2 (_2 / _R2). Check R1 and R2 are not swapped.")
            if args.check_fs and args.reads_dir:
                for f in (r1, r2):
                    if f and not (Path(args.reads_dir) / f).is_file():
                        errors.append(f"Line {n}: file not found: {Path(args.reads_dir) / f}")
        print(f"Samples: {len(body)}")

    if args.check_fs and not args.reads_dir:
        warns.append("--check-fs needs --reads-dir; file existence was not checked.")
    errors = list(dict.fromkeys(errors))
    warns = list(dict.fromkeys(warns))
    for e in errors:
        print(f"ERROR: {e}")
    for w in warns:
        print(f"WARN:  {w}")
    print("RESULT: " + ("INVALID" if errors else "VALID"))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
