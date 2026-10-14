#!/usr/bin/env python3
"""Validate study metadata against a feature table before an analysis step.

Usage:
    validate_metadata.py METADATA.tsv --group-column lab [--groups labA labB labC]
                         [--feature-table FT.tsv [FT2.tsv ...]] [--id-column Sample_ID]

Checks: tab-separated layout, sample IDs, that the grouping column exists
(case-sensitive), that it has 2 or more levels, that requested group values
match the data exactly, and that metadata IDs match the feature-table sample
columns. Several --group-column values may be given for multi-factor runs.

Exit code: 0 = valid (warnings allowed), 1 = errors, 2 = cannot read file.
"""
import argparse
import difflib
import statistics
import sys
from collections import Counter
from pathlib import Path


def read_lines(path, label, errors):
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        print(f"Cannot read {label} {path}: {exc}", file=sys.stderr)
        sys.exit(2)
    if raw.startswith(b"\xef\xbb\xbf"):
        errors.append(f"{label}: starts with a byte-order mark, which corrupts the first column name. Re-save without BOM.")
        raw = raw[3:]
    if b"\r" in raw:
        errors.append(f"{label}: has Windows (CRLF) line endings. Fix with: sed -i 's/\\r$//' FILE")
        raw = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return [ln for ln in raw.decode("utf-8", errors="replace").split("\n") if ln.strip()]


def suggest(word, options):
    close = difflib.get_close_matches(word, options, n=1, cutoff=0.6)
    ci = [o for o in options if o.lower() == word.lower()]
    pick = ci[0] if ci else (close[0] if close else None)
    return f" Did you mean '{pick}'?" if pick else ""


MIN_TESTABLE, MIN_FDR, RECOMMENDED = 4, 8, 10  # group-size tiers from exact-test arithmetic (general practice), proposed by the project owner pending the lab's confirmation
SUBJECT_NAMES = {"subject", "subject_id", "subjectid", "patient", "patient_id", "patientid", "participant", "participant_id", "individual", "donor", "person", "pair", "pair_id", "household", "family", "family_id"}
COVARIATE_SD = 0.5      # this skill's heuristics for "differs between groups"
COVARIATE_SHARE = 0.25
NUMERIC_LEVELS_AS_CONTINUOUS = 5  # this skill's heuristic; only the continuous-Age failure was observed


def is_number(v):
    try:
        float(v)
        return True
    except ValueError:
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("metadata")
    ap.add_argument("--group-column", action="append", required=True, help="Metadata column with the groups (repeatable)")
    ap.add_argument("--groups", nargs="+", help="Group values expected in the (single) group column")
    ap.add_argument("--feature-table", nargs="+", help="One or more feature tables (Bracken or HUMAnN) whose sample columns must match the metadata IDs")
    ap.add_argument("--id-column", help="Sample ID column (default: first column)")
    ap.add_argument("--subject-column", help="Column identifying the person (or household) each sample came from. Repeated values are out of scope for mima-visualisation.")
    ap.add_argument("--covariate", action="append", help="Clinical or technical covariate to compare between groups (repeatable)")
    args = ap.parse_args()

    errors, warns = [], []
    lines = read_lines(args.metadata, "metadata", errors)
    if not lines:
        print("ERROR: metadata is empty")
        return 1
    header = lines[0].split("\t")
    if len(header) < 2:
        errors.append("Header has one column only. Metadata must be tab-separated (not comma- or space-separated).")
    rows = []
    for n, ln in enumerate(lines[1:], start=2):
        cells = ln.split("\t")
        if len(cells) != len(header):
            errors.append(f"Line {n}: expected {len(header)} columns, found {len(cells)}.")
        else:
            rows.append(cells)

    id_col = args.id_column or header[0]
    if id_col not in header:
        errors.append(f"ID column '{id_col}' not found. Columns: {', '.join(header)}.{suggest(id_col, header)}")
        ids = []
    else:
        idx = header.index(id_col)
        ids = [r[idx] for r in rows]
        dups = [k for k, v in Counter(ids).items() if v > 1]
        if dups:
            errors.append(f"Duplicate sample IDs: {', '.join(dups[:5])}{' ...' if len(dups) > 5 else ''}")
        if any(not i.strip() for i in ids):
            errors.append("Some sample IDs are empty.")
    print(f"Samples in metadata: {len(rows)}   ID column: {id_col}")

    for gc in args.group_column:
        if gc not in header:
            errors.append(f"Group column '{gc}' not found (names are case-sensitive). Columns: {', '.join(header)}.{suggest(gc, header)}")
            continue
        gi = header.index(gc)
        clash = [h for h in header if h != gc and gc in h]
        if clash:
            warns.append(f"Group column '{gc}' is part of other column names ({', '.join(clash)}). mima-classifier-RF finds its -c column by pattern matching (grep), so it would select all of them and break; rename the columns so '{gc}' is not a substring of another header, or use a copy of the metadata without the others.")
        values = [r[gi] for r in rows]
        empties = sum(1 for v in values if not v.strip())
        if empties:
            warns.append(f"Column '{gc}': {empties} sample(s) have an empty value and will be excluded or cause errors.")
        counts = Counter(v for v in values if v.strip())
        print(f"Column '{gc}' levels: " + ", ".join(f"{k} (n={v})" for k, v in counts.items()))
        if len(counts) < 2:
            errors.append(f"Column '{gc}' has {len(counts)} level(s). Group comparisons need at least 2.")
        if counts and all(is_number(v) for v in counts):
            if len(counts) > NUMERIC_LEVELS_AS_CONTINUOUS:
                errors.append(f"Column '{gc}' is numeric with {len(counts)} distinct values, so it is a continuous variable. mima-visualisation needs a categorical group column: a numeric study group (Age) crashed the container at the CCA/RDA step on Gadi. Bin it into labelled groups (for example young, middle, older) in a new column.")
            else:
                warns.append(f"Column '{gc}' is numeric ({len(counts)} distinct values). R may read it as a continuous variable rather than a group. Untested: use text labels (for example G0, G1) to be safe.")
        for level, n in counts.items():
            if n < MIN_TESTABLE:
                warns.append(f"Column '{gc}': group '{level}' has only {n} sample(s): descriptive only. Below {MIN_TESTABLE} per group a rank-based two-group test cannot reach p < 0.05 even before multiple-testing correction (smallest possible p is 0.10 at 3 v 3 and 0.029 at 4 v 4), so report what was seen and do not claim a difference.")
            elif n < MIN_FDR:
                warns.append(f"Column '{gc}': group '{level}' has {n} samples: exploratory. With fewer than {MIN_FDR} per group, FDR correction across hundreds of taxa is almost never met, even by a taxon that separates the groups perfectly. Report effect sizes and call results preliminary.")
            elif n < RECOMMENDED:
                warns.append(f"Column '{gc}': group '{level}' has {n} samples. {MIN_FDR} is the usable minimum for differential abundance with FDR; {RECOMMENDED} or more per group is recommended.")
        if args.groups and len(args.group_column) == 1:
            for g in args.groups:
                if g not in counts:
                    errors.append(f"Group '{g}' is not a value of '{gc}' (values are case-sensitive).{suggest(g, list(counts))}")
            extra = [k for k in counts if k not in args.groups]
            if extra:
                warns.append(f"Column '{gc}' also contains levels not requested: {', '.join(extra)}.")

    if args.subject_column:
        if args.subject_column not in header:
            errors.append(f"Subject column '{args.subject_column}' not found. Columns: {', '.join(header)}.{suggest(args.subject_column, header)}")
        else:
            si = header.index(args.subject_column)
            rep = [k for k, v in Counter(r[si] for r in rows if r[si].strip()).items() if v > 1]
            if rep:
                errors.append(f"{len(rep)} subject(s) in '{args.subject_column}' appear in more than one sample (for example {', '.join(rep[:3])}): repeated or paired samples are out of scope for mima-visualisation, which treats every sample as independent. Do not run it on this design. Use a method with a subject random effect (for example MaAsLin2 or ANCOM-BC2) or a PERMANOVA with restricted permutations; the skill names these but does not run them.")
    else:
        for col in header:
            if col.strip().lower() in SUBJECT_NAMES and col != id_col:
                ci = header.index(col)
                rep = [k for k, v in Counter(r[ci] for r in rows if r[ci].strip()).items() if v > 1]
                if rep:
                    warns.append(f"Column '{col}' looks like a subject identifier and {len(rep)} value(s) occur more than once. If any person was sampled more than once, the samples are not independent: ask the user, and re-run with --subject-column {col} (repeated samples are out of scope for mima-visualisation).")

    if args.covariate and args.group_column and args.group_column[0] in header:
        gi = header.index(args.group_column[0])
        by_group = {}
        for r in rows:
            if r[gi].strip():
                by_group.setdefault(r[gi], []).append(r)
        for cov in args.covariate:
            if cov not in header:
                errors.append(f"Covariate '{cov}' not found. Columns: {', '.join(header)}.{suggest(cov, header)}")
                continue
            ci = header.index(cov)
            vals = [r[ci] for r in rows if r[ci].strip()]
            if vals and all(is_number(v) for v in vals):
                means = {g: statistics.mean(float(r[ci]) for r in rs if r[ci].strip()) for g, rs in by_group.items() if any(r[ci].strip() for r in rs)}
                sd = statistics.pstdev(float(v) for v in vals) or 1.0
                print(f"Covariate '{cov}' mean by group: " + ", ".join(f"{g} {m:.1f}" for g, m in means.items()))
                if means and (max(means.values()) - min(means.values())) > COVARIATE_SD * sd:
                    warns.append(f"Covariate '{cov}' differs between groups (means {min(means.values()):.1f} to {max(means.values()):.1f}; heuristic: more than {COVARIATE_SD} SD apart). mima-visualisation does not adjust for covariates, so report the results as preliminary associations that need adjustment (for example with MaAsLin2 or ANCOM-BC2).")
            else:
                shares = {}
                for g, rs in by_group.items():
                    c = Counter(r[ci] for r in rs if r[ci].strip()); tot = sum(c.values()) or 1
                    shares[g] = {k: v / tot for k, v in c.items()}
                levels = sorted({k for sh in shares.values() for k in sh})
                print(f"Covariate '{cov}' share by group: " + "; ".join(f"{g}: " + ", ".join(f"{k} {sh.get(k, 0):.0%}" for k in levels) for g, sh in shares.items()))
                if any(max(sh.get(k, 0) for sh in shares.values()) - min(sh.get(k, 0) for sh in shares.values()) > COVARIATE_SHARE for k in levels):
                    warns.append(f"Covariate '{cov}' differs between groups (a level's share differs by more than {COVARIATE_SHARE:.0%}; heuristic). mima-visualisation does not adjust for covariates, so report the results as preliminary associations that need adjustment (for example with MaAsLin2 or ANCOM-BC2).")

    for ft_path in args.feature_table or []:
        ft_name = Path(ft_path).name
        ft_lines = read_lines(ft_path, f"feature table {ft_name}", errors)
        if not ft_lines:
            errors.append(f"{ft_name}: feature table is empty.")
            continue
        header_cells = ft_lines[0].split("\t")
        ft_samples = header_cells[1:]
        first = header_cells[0].strip()
        if first in ("# Pathway", "# Gene"):
            kind = "HUMAnN " + ("pathway" if first == "# Pathway" else "gene") + " table"
        elif first in ("# Taxonomy", "name"):
            kind = "taxonomy table"
        elif first == "# Gene Family":
            kind = "HUMAnN gene-family table"
            errors.append(f"{ft_name}: header '# Gene Family' is rejected by the container (models.py raises 'Could not detect data matrix format'; confirmed on Gadi). Make a copy whose first header cell is '# Gene' and pass the copy; see references/troubleshooting.md. Do not edit the original in place.")
        elif first.startswith("#"):
            kind = "table with an unrecognised '#' header"
            warns.append(f"{ft_name}: header '{first}' is not one of '# Taxonomy', 'name', '# Pathway', '# Gene', which are the headers the container's reader recognises (models.py); it may be rejected.")
        else:
            kind = "table"
            if first != "name":
                warns.append(f"{ft_name}: first header cell '{first}' is not 'name' or '# Taxonomy'; the container's reader may not recognise it as a taxonomy table.")
        if not ft_samples:
            errors.append(f"{ft_name}: header has no sample columns. It must be tab-separated with samples as columns.")
            continue
        a, b = set(ids), set(ft_samples)
        print(f"{ft_name} ({kind}): {len(b)} samples, {len(a & b)} shared with metadata")
        if ids and not (a & b):
            errors.append(f"{ft_name}: no sample IDs are shared with the metadata. Check the ID column and any prefix or suffix differences.")
        else:
            if b - a:
                warns.append(f"{ft_name}: {len(b - a)} sample(s) missing from metadata: {', '.join(sorted(b - a)[:5])}")
            if a - b:
                warns.append(f"{ft_name}: {len(a - b)} metadata sample(s) missing from this table (e.g. removed by the abundance threshold): {', '.join(sorted(a - b)[:5])}")
        if ft_name.startswith("bracken_FT_") and not (ft_name.endswith("_counts") or ft_name.endswith("_relAbund")):
            warns.append(f"{ft_name}: has neither _counts nor _relAbund suffix, so it holds counts and relative abundances together per sample. Use the _counts or _relAbund table: mima-visualisation accepts either (the tutorial uses _relAbund), and the base table has not been tested with these modules.")

    for e in errors:
        print(f"ERROR: {e}")
    for w in warns:
        print(f"WARN:  {w}")
    print("RESULT: " + ("INVALID" if errors else "VALID"))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
