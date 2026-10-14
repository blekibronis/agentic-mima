#!/usr/bin/env python3
"""Validate a MIMA PBS header config (pbs_header_qc.cfg / _taxa.cfg / _func.cfg).

Usage:
    validate_pbs_config.py CONFIG.cfg [--module qc|taxa|func]

The --pbs-config file is copied into every generated PBS script, so leftover
placeholders or missing resource lines break every job. Checks: shebang,
#PBS name/cpu/walltime/memory lines, IMAGE_DIR, APPTAINER_BIND without
placeholders, line endings, and (for --module taxa) enough memory for Kraken2.

Exit code: 0 = valid (warnings allowed), 1 = errors, 2 = cannot read file.
"""
import argparse
import re
import sys
from pathlib import Path


# IMAGE_DIR may be assigned plainly or exported; both are valid shell and both are used in real configs.
IMAGE_ASSIGN = re.compile(r"\s*(?:export\s+|declare\s+-x\s+|readonly\s+)?IMAGE_DIR=(.*)$")

MIN_TAXA_MEM_GB = 300
MINUTES_PER_SAMPLE = 20   # project owner's figure for 15-20M read pairs on 28 cores; calibrate on the first real run
BUFFER_MIN = 60
CPU_RANGE = (16, 28)  # tutorial: Kraken2 with the GTDB r95 database needs ~300GB


def mem_gb(value):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgt]?b?)", value.strip().lower())
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2)
    return n * {"": 1 / 1024**3, "b": 1 / 1024**3, "kb": 1 / 1024**2, "k": 1 / 1024**2,
                "mb": 1 / 1024, "m": 1 / 1024, "gb": 1, "g": 1, "tb": 1024, "t": 1024}[unit]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config")
    ap.add_argument("--module", choices=["qc", "taxa", "func"])
    ap.add_argument("--samples", type=int, help="Number of samples; with --module taxa, checks the walltime against the sizing guideline")
    args = ap.parse_args()
    try:
        raw = Path(args.config).read_bytes()
    except OSError as exc:
        print(f"Cannot read {args.config}: {exc}", file=sys.stderr)
        return 2

    errors, warns = [], []
    if b"\r" in raw:
        errors.append("File has Windows (CRLF) line endings, which break bash and #PBS parsing. Fix with: sed -i 's/\\r$//' CONFIG")
        raw = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    lines = raw.decode("utf-8", errors="replace").split("\n")

    if not lines or not lines[0].startswith("#!"):
        errors.append("First line must be a shebang, e.g. #!/bin/bash")
    pbs = [ln for ln in lines if ln.startswith("#PBS")]
    joined = "\n".join(pbs)
    if not re.search(r"^#PBS\s+-N\s+\S+", joined, re.M):
        errors.append("Missing '#PBS -N <jobname>'.")
    if not re.search(r"ncpus=\d+", joined):
        errors.append("Missing CPU request (e.g. '#PBS -l ncpus=8' or 'select=1:ncpus=8').")
    wt = re.search(r"walltime=(\S+)", joined)
    if not wt:
        errors.append("Missing '#PBS -l walltime=H:MM:SS'.")
    elif not re.fullmatch(r"\d+:\d{2}(:\d{2})?", wt.group(1)):
        errors.append(f"walltime '{wt.group(1)}' should look like 2:00:00.")
    mem = re.search(r"\bmem=(\S+)", joined)
    if not mem:
        errors.append("Missing memory request (e.g. '#PBS -l mem=64GB').")
    elif args.module == "taxa":
        gb = mem_gb(mem.group(1).split(":")[0])
        if gb is None:
            warns.append(f"Could not parse memory '{mem.group(1)}'.")
        elif gb < MIN_TAXA_MEM_GB:
            warns.append(f"Memory {mem.group(1)} is below ~{MIN_TAXA_MEM_GB}GB, which the tutorial says Kraken2 needs. The job may be killed.")
    if args.module == "taxa":
        cpus = re.search(r"ncpus=(\d+)", joined)
        if cpus and not CPU_RANGE[0] <= int(cpus.group(1)) <= CPU_RANGE[1]:
            warns.append(f"ncpus={cpus.group(1)} is outside the project's guideline of {CPU_RANGE[0]} to {CPU_RANGE[1]} cores for Kraken2.")
        if args.samples and wt and re.fullmatch(r"\d+:\d{2}(:\d{2})?", wt.group(1)):
            parts = [int(x) for x in wt.group(1).split(":")]
            have = parts[0] * 60 + parts[1]
            est = MINUTES_PER_SAMPLE * args.samples
            floor_min, rec = est + BUFFER_MIN, max(int(1.5 * est), est + BUFFER_MIN)
            fmt = lambda m: f"{m // 60}:{m % 60:02d}:00"
            if have < floor_min:
                warns.append(f"Walltime {wt.group(1)} is below the guideline of {MINUTES_PER_SAMPLE} minutes per sample plus {BUFFER_MIN} minutes for {args.samples} samples ({fmt(floor_min)}). A job killed at the walltime wastes the queue wait; {fmt(rec)} (1.5 x the estimate) is recommended. The per-sample time is the project owner's figure for 15-20M read pairs on 28 cores: calibrate it on the first real run, and check the queue's walltime limit.")
            elif have < rec:
                print(f"NOTE:  Walltime {wt.group(1)} meets the guideline ({fmt(floor_min)}); {fmt(rec)} (1.5 x the estimate) leaves more margin.")
    if not re.search(r"^#PBS\s+-j\s+oe", joined, re.M):
        warns.append("No '#PBS -j oe': output and error logs will be separate files. The QC report step expects the combined log.")

    body = [ln for ln in lines if not ln.startswith("#")]
    image = [ln for ln in body if IMAGE_ASSIGN.match(ln)]
    if not image:
        errors.append("IMAGE_DIR is not set. It must point to the MIMA container image (a .sif file) or a built sandbox directory.")
    elif not IMAGE_ASSIGN.match(image[0]).group(1).strip().strip("\"'") or "<" in image[0]:
        errors.append(f"IMAGE_DIR has a placeholder value: {image[0].strip()}")
    bind = [ln for ln in body if "APPTAINER_BIND" in ln and "export" in ln]
    if not bind:
        errors.append("APPTAINER_BIND is not exported. Bind every directory holding data, references and outputs that is outside the home directory.")
    else:
        b = bind[0]
        if "<" in b or ">" in b or "/path/to" in b:
            errors.append(f"APPTAINER_BIND still contains placeholder paths: {b.strip()}")
        else:
            value = re.search(r'APPTAINER_BIND=["\']?([^"\']*)', b)
            pairs = [p for p in (value.group(1).split(",") if value else []) if p]
            if not pairs:
                errors.append("APPTAINER_BIND is empty.")
            for p in pairs:
                if p.count(":") != 1 or not all(x.startswith("/") for x in p.split(":")):
                    errors.append(f"Bind '{p}' must be /host/path:/container/path with absolute paths.")

    for e in errors:
        print(f"ERROR: {e}")
    for w in warns:
        print(f"WARN:  {w}")
    print("RESULT: " + ("INVALID" if errors else "VALID"))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
