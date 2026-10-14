#!/usr/bin/env python3
"""Pre-submission checks for generated MIMA PBS scripts.

Usage:
    validate_pbs_scripts.py PATH [PATH ...] [--check-fs] [--format text|json]

PATH is a .pbs file or a directory (searched recursively for *.pbs).

Run this after a MIMA generator (mima-qc, mima-taxa-profiling,
mima-function-profiling) has written its scripts and before anything is
submitted. It catches leftover placeholders, missing resources, CRLF line
endings, unsafe commands and (with --check-fs, on the cluster) missing paths.

Exit code: 0 = all scripts valid (warnings allowed), 1 = errors, 2 = no scripts found.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_pbs_config import MIN_TAXA_MEM_GB, mem_gb  # noqa: E402

# IMAGE_DIR may be assigned plainly or exported; both are valid shell and both are used in real configs.
IMAGE_ASSIGN = re.compile(r"\s*(?:export\s+|declare\s+-x\s+|readonly\s+)?IMAGE_DIR=(.*)$")
PLACEHOLDER = re.compile(r"/path/to|<[A-Za-z_/]*(?:path|PATH|PROJECT|source|destination)[A-Za-z0-9_/]*>")
ABS_TOKEN = re.compile(r"(?<![\w$}:/])(/[\w./+=@:,-]+)")
INPUT_EXT = (".fq.gz", ".fastq.gz", ".fq", ".fastq", ".sh", ".pbs")

# (pattern, severity, message)
UNSAFE = [
    (re.compile(r"\bsudo\b"), "error", "uses sudo"),
    (re.compile(r"\b(curl|wget)\b"), "error", "downloads from the network"),
    (re.compile(r"\|\s*(ba)?sh\b"), "error", "pipes into a shell"),
    (re.compile(r"\b(qsub|qdel|qalter)\b"), "error", "calls a PBS command from inside a job script"),
    (re.compile(r"\brm\s"), "warning", "deletes files with rm; confirm the target is a temporary file"),
    (re.compile(r"\beval\b"), "warning", "uses eval"),
]


def detect_module(path: Path):
    name, parent = path.name, path.parent.name
    if name.startswith("qcModule_"):
        return "qc"
    if name == "run_taxa_profiling.pbs":
        return "taxa"
    if name == "generate_func_feature_tables.pbs":
        return "func-tables"
    if parent.lower().startswith("function"):
        return "func"
    return None


def validate_file(path, check_fs=False, module=None):
    """Return a dict with errors and warnings for one PBS script."""
    path = Path(path)
    errors, warns = [], []
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8", errors="replace")
    module = module or detect_module(path)

    if "\r" in text:
        errors.append("CRLF line endings break bash and #PBS parsing. Fix with: sed -i 's/\\r$//' FILE")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if not lines[0].startswith("#!"):
        errors.append("First line must be a shebang, e.g. #!/bin/bash")

    pbs = "\n".join(ln for ln in lines if ln.startswith("#PBS"))
    if not re.search(r"^#PBS\s+-N\s+\S+", pbs, re.M):
        errors.append("Missing '#PBS -N <jobname>'.")
    if not re.search(r"ncpus=\d+", pbs):
        errors.append("Missing CPU request (ncpus).")
    wt = re.search(r"walltime=(\S+)", pbs)
    if not wt:
        errors.append("Missing walltime request.")
    elif not re.fullmatch(r"\d+:\d{2}(:\d{2})?", wt.group(1)):
        errors.append(f"walltime '{wt.group(1)}' should look like 2:00:00.")
    mem = re.search(r"\bmem=(\S+)", pbs)
    if not mem:
        errors.append("Missing memory request.")
    elif module == "taxa":
        gb = mem_gb(mem.group(1).split(":")[0])
        if gb is not None and gb < MIN_TAXA_MEM_GB:
            warns.append(f"Memory {mem.group(1)} is below ~{MIN_TAXA_MEM_GB}GB needed by Kraken2; the job may be killed.")
    if not re.search(r"^#PBS\s+-j\s+oe", pbs, re.M):
        warns.append("No '#PBS -j oe': logs will be split into separate output and error files.")

    body = [(n, ln) for n, ln in enumerate(lines, 1) if ln.strip() and not ln.lstrip().startswith("#")]
    for n, ln in body:
        if PLACEHOLDER.search(ln):
            errors.append(f"Line {n}: placeholder value still present: {ln.strip()[:120]}")
        for pat, sev, msg in UNSAFE:
            if pat.search(ln):
                (errors if sev == "error" else warns).append(f"Line {n}: {msg}: {ln.strip()[:120]}")

    image = [m for m in (IMAGE_ASSIGN.match(ln) for _, ln in body) if m]
    if not image:
        errors.append("IMAGE_DIR is not set.")
    elif not image[0].group(1).strip().strip("\"'"):
        errors.append("IMAGE_DIR is empty.")

    bind_lines = [ln for _, ln in body if "APPTAINER_BIND" in ln and "export" in ln]
    if not bind_lines:
        errors.append("APPTAINER_BIND is not exported; data outside the home directory will not be visible in the container.")
    else:
        m = re.search(r'APPTAINER_BIND=["\']?([^"\']*)', bind_lines[0])
        pairs = [p for p in (m.group(1).split(",") if m else []) if p]
        if not pairs:
            errors.append("APPTAINER_BIND is empty.")
        for p in pairs:
            parts = p.split(":")
            if len(parts) < 2 or not (parts[0].startswith("/") and parts[1].startswith("/")):
                errors.append(f"Bind '{p}' must be /host/path:/container/path with absolute paths.")
            elif check_fs and not Path(parts[0]).exists():
                errors.append(f"Bind source does not exist: {parts[0]}")

    if check_fs and module in ("qc", "taxa"):
        siblings = [f for f in path.parent.glob("*.sh") if f.is_file()]
        if not siblings:
            errors.append(f"no per-sample .sh scripts found next to {path.name}. The generator found no samples, which happens silently (exit code 0) when --fwd-suffix/--rev-suffix do not match the file names. Check the suffixes against the actual file names and regenerate.")

    if check_fs:
        for n, ln in body:
            m = re.match(r"\s*cd\s+(/\S+)", ln)
            if m and not Path(m.group(1)).is_dir():
                errors.append(f"Line {n}: directory does not exist: {m.group(1)}")
            if "APPTAINER_BIND" in ln:
                continue
            for tok in ABS_TOKEN.findall(ln):
                if tok.endswith(INPUT_EXT) and not Path(tok).is_file():
                    errors.append(f"Line {n}: file does not exist: {tok}")
    else:
        warns.append("Filesystem checks skipped; re-run with --check-fs on the cluster before submitting.")

    seen = set()
    errors = [e for e in errors if not (e in seen or seen.add(e))]
    return {"path": str(path), "module": module, "sha256": sha, "errors": errors, "warnings": warns, "ok": not errors}


def collect(paths):
    files = []
    for p in map(Path, paths):
        if p.is_dir():
            files += sorted(p.rglob("*.pbs"))
        elif p.is_file():
            files.append(p)
    return files


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--check-fs", action="store_true", help="Verify referenced paths exist (run on the cluster)")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    files = collect(args.paths)
    if not files:
        print("No .pbs files found.", file=sys.stderr)
        return 2
    results = [validate_file(f, args.check_fs) for f in files]
    if args.format == "json":
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            print(f"=== {r['path']} [{r['module'] or 'unknown module'}] {'OK' if r['ok'] else 'INVALID'}  sha256={r['sha256'][:12]}")
            for e in r["errors"]:
                print(f"  ERROR: {e}")
            for w in r["warnings"]:
                if not w.startswith("Filesystem checks skipped"):
                    print(f"  WARN:  {w}")
        if not args.check_fs:
            print("\nNOTE: filesystem checks skipped. Re-run with --check-fs on the cluster before submitting.")
        bad = sum(1 for r in results if not r["ok"])
        print(f"\n{len(results)} script(s) checked, {bad} invalid.")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
