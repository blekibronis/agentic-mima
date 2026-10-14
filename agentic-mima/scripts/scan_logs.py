#!/usr/bin/env python3
"""Scan PBS and MIMA log files for known failure signatures (read-only).

Usage:
    scan_logs.py PATH [PATH ...] [--format text|json] [--max-hits 3]

PATH is a log file or a directory. Directories are searched (two levels deep)
for PBS logs (*.o<jobid>, *.e<jobid>), *.log, *.out and *.err files.

Signatures live in error_patterns.json (regex, cause, fix, confidence). The
scanner reports the first hits per signature with file and line number, so the
cause can be tied to a specific sample or job.

Exit code: 0 = no errors found, 1 = at least one error signature matched,
2 = no readable logs.
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

PATTERNS = Path(__file__).with_name("error_patterns.json")
LOG_NAME = re.compile(r".*(\.o\d+|\.e\d+|\.log|\.out|\.err)(\.txt)?$")
MAX_BYTES = 50 * 1024 * 1024


def load_patterns():
    data = json.loads(PATTERNS.read_text())["patterns"]
    for p in data:
        p["_re"] = re.compile(p["regex"])
    return data


def find_logs(paths):
    files = []
    for p in map(Path, paths):
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and len(f.relative_to(p).parts) <= 2 and LOG_NAME.match(f.name):
                    files.append(f)
    return files


def scan_file(path, patterns, max_hits=3):
    """Return {pattern_id: [(line_no, text), ...]} for one file."""
    hits = defaultdict(list)
    if path.stat().st_size > MAX_BYTES:
        return hits
    with open(path, errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            line = line.rstrip("\n").replace("\r", "\\r")
            matched = [p for p in patterns if p["_re"].search(line)]
            hidden = set(s for p in matched for s in p.get("supersedes", []))
            for p in matched:
                if p["id"] not in hidden and len(hits[p["id"]]) < max_hits:
                    hits[p["id"]].append((n, line.strip()[:200]))
    return {k: v for k, v in hits.items() if v}


def scan(paths, max_hits=3):
    patterns = load_patterns()
    by_id = {p["id"]: p for p in patterns}
    findings = []
    for f in find_logs(paths):
        try:
            hits = scan_file(f, patterns, max_hits)
        except OSError:
            continue
        for pid, lines in hits.items():
            p = by_id[pid]
            findings.append({"file": str(f), "pattern": pid, "severity": p["severity"], "confidence": p["confidence"], "verified": p.get("verified", False),
                             "evidence": p.get("evidence"), "cause": p["cause"], "fix": p["fix"], "lines": [{"line": n, "text": t} for n, t in lines]})
    return findings, len(find_logs(paths))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--max-hits", type=int, default=3)
    args = ap.parse_args()

    findings, n_files = scan(args.paths, args.max_hits)
    if n_files == 0:
        print("No log files found. PBS logs are named JOBNAME.o<jobid> in the directory the job was submitted from.", file=sys.stderr)
        return 2
    order = {"error": 0, "warning": 1}
    findings.sort(key=lambda f: (order.get(f["severity"], 2), f["pattern"], f["file"]))
    if args.format == "json":
        print(json.dumps({"files_scanned": n_files, "findings": findings}, indent=2))
    else:
        print(f"Scanned {n_files} log file(s).")
        if not findings:
            print("No known failure signatures found. This does not prove the run succeeded: check job exit status and outputs.")
        for f in findings:
            tag = ("verified against a real log: " + f["evidence"]["source"]) if f["verified"] and f.get("evidence") else "UNVERIFIED pattern: matched on general knowledge, confirm in the log"
            print(f"\n[{f['severity'].upper()}] {f['pattern']} (match confidence: {f['confidence']}; {tag})  {f['file']}")
            for h in f["lines"]:
                print(f"    line {h['line']}: {h['text']}")
            print(f"  Cause: {f['cause']}")
            print(f"  Fix:   {f['fix']}")
    return 1 if any(f["severity"] == "error" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
