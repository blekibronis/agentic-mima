#!/usr/bin/env python3
"""Check a finished MIMA analysis report against the report template (read-only).

Usage:
    check_report.py REPORT.md [--stages qc taxa func diversity classifier]
                    [--run-dir mima_run] [--format text|json]

Quality criteria (errors unless noted):
  * no [FILL: ...] placeholders and no leftover template instructions
  * all nine numbered sections present
  * the Methods section contains a fenced block with the commands that were run
  * with --stages: a Results subsection for every stage run, and none for stages not run
  * warning: a Results line with numbers that names no source file or "Source"
  * warning (with --run-dir): a file named in backticks that is not in the run folder

Use it in a refinement loop: fix what it reports, rerun, repeat. Stop after three
rounds that report the same error and ask the user for the missing information
instead of guessing it.

Exit code: 0 = no errors (warnings allowed), 1 = errors, 2 = cannot read the report.
"""
import argparse
import json
import re
import sys
from pathlib import Path

SECTIONS = [(1, "Summary"), (2, "Objective and design"), (3, "Data"), (4, "Methods"), (5, "Results"),
            (6, "Quality checks and deviations"), (7, "Limitations"), (8, "Reproducibility"), (9, "Files delivered")]
STAGE_SUBSECTION = {"qc": "5.1", "taxa": "5.2", "func": "5.3", "diversity": "5.4", "classifier": "5.5"}
FILE_TOKEN = re.compile(r"`([\w./-]+\.(?:csv|tsv|json|txt|pdf|png|log|pbs|md|sh|cfg))`")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report")
    ap.add_argument("--stages", nargs="*", choices=sorted(STAGE_SUBSECTION), help="Stages that were run")
    ap.add_argument("--run-dir", help="Run folder, to check that named files exist")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    try:
        text = Path(args.report).read_text()
    except OSError as exc:
        print(f"Cannot read report: {exc}", file=sys.stderr)
        return 2
    lines = text.split("\n")
    errors, warns = [], []

    if not text.strip():
        errors.append("Report is empty.")
    for n, ln in enumerate(lines, 1):
        if "[FILL" in ln:
            errors.append(f"Line {n}: unfilled placeholder: {ln.strip()[:100]}")
    if "How to use this template" in text:
        errors.append("The template's instruction block is still in the report; delete it.")

    heads = {}
    for n, ln in enumerate(lines, 1):
        m = re.match(r"^##\s+(\d+)\.\s+(.*)$", ln)
        if m:
            heads[int(m.group(1))] = (n, m.group(2).strip())
    for num, name in SECTIONS:
        if num not in heads:
            errors.append(f"Missing section '## {num}. {name}'.")

    def section_lines(num):
        if num not in heads:
            return []
        start = heads[num][0]
        later = [v[0] for k, v in heads.items() if k > num]
        end = min(later) - 1 if later else len(lines)
        return list(enumerate(lines[start:end], start + 1))

    methods = "\n".join(t for _, t in section_lines(4))
    if "```" not in methods:
        errors.append("Section 4 (Methods) has no fenced block with the commands that were run.")

    subs = {m.group(1) for ln in lines for m in [re.match(r"^###\s+(5\.\d)\b", ln)] if m}
    if args.stages is not None:
        for st in args.stages:
            if STAGE_SUBSECTION[st] not in subs:
                errors.append(f"Stage '{st}' was run but Results subsection {STAGE_SUBSECTION[st]} is missing.")
        for st, sub in STAGE_SUBSECTION.items():
            if st not in args.stages and sub in subs:
                errors.append(f"Results subsection {sub} is present but stage '{st}' was not run; delete it.")

    for n, ln in section_lines(5):
        s = ln.strip()
        if not s or s.startswith(("#", "|", "```", "-", "*")) or not re.search(r"\d", s):
            continue
        if "`" not in s and "source" not in s.lower():
            warns.append(f"Line {n}: numbers with no named source file: {s[:90]}")

    if args.run_dir:
        root = Path(args.run_dir)
        for n, ln in enumerate(lines, 1):
            for tok in FILE_TOKEN.findall(ln):
                if not ((root / tok).exists() or any(root.rglob(Path(tok).name))):
                    warns.append(f"Line {n}: '{tok}' is not in {root}")

    errors = list(dict.fromkeys(errors))
    warns = list(dict.fromkeys(warns))
    if args.format == "json":
        print(json.dumps({"ok": not errors, "errors": errors, "warnings": warns}, indent=2))
    else:
        for e in errors:
            print(f"ERROR: {e}")
        for w in warns:
            print(f"WARN:  {w}")
        print("RESULT: " + ("NOT READY" if errors else "READY") + f" ({len(errors)} error(s), {len(warns)} warning(s))")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
