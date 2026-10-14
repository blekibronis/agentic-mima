#!/usr/bin/env python3
"""Estimate the read length to give Bracken (--read-length) from clean FASTQ files (read-only).

Usage:
    estimate_read_length.py DIR_OR_FASTQ [DIR_OR_FASTQ ...] [--reads 4000] [--available 50,100,150]

Reads the first --reads records of each forward-read file (the first mate only, so
2x150 means 150), takes each sample's mean length, then the median across samples,
and maps it to the nearest available Bracken k-mer distribution. A tie goes to the
lower value, so 75 maps to 50 and 125 maps to 100.

This is the project's rule (this skill's heuristic, not a Bracken rule). The
available lengths must match the database: on the shared GTDB database they are 50,
100 and 150 (build_command.py --check-fs verifies the file exists).

Exit code: 0 = estimate printed, 2 = no FASTQ files found.
"""
import argparse
import gzip
import statistics
import sys
from pathlib import Path

FORWARD = ("*_clean_1.fq.gz", "*_1.fq.gz", "*_1.fastq.gz", "*_R1*.fq.gz", "*_R1*.fastq.gz", "*.fq.gz", "*.fastq.gz", "*.fq", "*.fastq")
SPREAD_BP = 25  # this skill's threshold for "lengths vary between samples"


def map_length(mean, available):
    """Nearest available length; a tie goes to the lower one."""
    best = None
    for a in sorted(available):
        if best is None or abs(a - mean) < abs(best - mean):
            best = a
    return best


def find_files(paths):
    out = []
    for p in map(Path, paths):
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            for pat in FORWARD:
                found = sorted(f for f in p.glob(pat) if f.is_file())
                if found:
                    # keep forward mates only when the pattern could also match reverse reads
                    found = [f for f in found if not any(t in f.name for t in ("_2.", "_R2"))]
                    out.extend(found)
                    break
    return out


def mean_length(path, n_reads):
    opener = gzip.open if path.suffix == ".gz" else open
    total = count = 0
    with opener(path, "rt", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i % 4 == 1:
                total += len(line.rstrip("\r\n"))
                count += 1
                if count >= n_reads:
                    break
    return total / count if count else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--reads", type=int, default=4000)
    ap.add_argument("--available", default="50,100,150")
    args = ap.parse_args()
    available = [int(x) for x in args.available.split(",")]

    files = find_files(args.paths)
    means = {}
    for f in files:
        try:
            m = mean_length(f, args.reads)
        except (OSError, EOFError):
            continue
        if m:
            means[f.name] = m
    if not means:
        print("No readable forward-read FASTQ files found.", file=sys.stderr)
        return 2

    values = list(means.values())
    med = statistics.median(values)
    chosen = map_length(med, available)
    print(f"Files read: {len(means)} (first {args.reads} reads each, first mate only)")
    print(f"Per-sample mean length: min {min(values):.1f}, median {med:.1f}, max {max(values):.1f}")
    print(f"Nearest available length ({', '.join(map(str, available))}; ties go lower): --read-length {chosen}")
    if med > max(available):
        print(f"WARN: reads are longer than the longest available distribution ({max(available)}). {chosen} is the best available approximation; the proper fix is a Bracken k-mer distribution built for this read length by the database owners.")
    if max(values) - min(values) > SPREAD_BP:
        print(f"WARN: sample means differ by more than {SPREAD_BP} bp. Use one value for the whole cohort, and check whether the libraries came from different runs.")
    print("This is the project's mapping rule (a heuristic, not a Bracken rule). Confirm the file databaseNmers.kmer_distrib exists with build_command.py --check-fs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
