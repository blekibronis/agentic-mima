#!/usr/bin/env python3
"""Report the status of submitted PBS jobs (read-only).

Usage:
    check_job_status.py --record submission_record.json [--format text|json]
    check_job_status.py --jobs 1001.server 1002.server
    check_job_status.py --record R.json --wait --interval 120 --timeout 3600

Reads job state from `qstat -x -f -F json` (finished jobs need job history to be
enabled on the server; if it is not, finished jobs may show as UNKNOWN and the
PBS log files and check_outputs.py are the source of truth). Nothing is
submitted, changed or deleted.

Status values: QUEUED, HELD, RUNNING, SUCCEEDED, FAILED, UNKNOWN.
Exit code: 0 = all succeeded, 1 = at least one failed, 3 = still in progress
(something is queued, held or running), 4 = unresolved (nothing is active, but
the server does not know at least one job, so its outcome must come from the
PBS log and check_outputs.py), 2 = tool or usage error.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

JOB_ID = re.compile(r"^\d+(\[\])?(\.[A-Za-z0-9_.-]+)?$")
STATE = {"Q": "QUEUED", "W": "QUEUED", "H": "HELD", "T": "QUEUED", "R": "RUNNING", "B": "RUNNING",
         "E": "RUNNING", "S": "RUNNING", "U": "RUNNING"}


def to_seconds(hms):
    try:
        parts = [int(float(x)) for x in str(hms).split(":")]
    except ValueError:
        return None
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def to_gb(value):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgt]?)b?", str(value).strip().lower())
    if not m:
        return None
    return float(m.group(1)) * {"": 1 / 1024**3, "k": 1 / 1024**2, "m": 1 / 1024, "g": 1, "t": 1024}[m.group(2)]


def exit_meaning(code):
    if code is None:
        return ""
    if code == 0:
        return "finished normally"
    if code < 0:
        return "PBS could not start or run the job (see the server 'comment' and the job log)"
    if code >= 256:
        return f"terminated by signal {code - 256} (typically a walltime or memory limit kill, or qdel)"
    return f"the script exited with status {code} (see the job log)"


def classify(job):
    st = str(job.get("job_state", "?"))
    if st in ("F", "X"):
        code = job.get("Exit_status")
        if code is None:
            return "UNKNOWN", None
        code = int(code)
        return ("SUCCEEDED" if code == 0 else "FAILED"), code
    return STATE.get(st, "UNKNOWN"), None


def usage_note(job):
    """Flag resource use near or above the request; helps explain kills and size reruns."""
    used, req = job.get("resources_used") or {}, job.get("Resource_List") or {}
    notes = []
    w_u, w_r = to_seconds(used.get("walltime")), to_seconds(req.get("walltime"))
    if w_u and w_r and w_u / w_r >= 0.9:
        notes.append(f"walltime {used['walltime']} of {req['walltime']}")
    m_u, m_r = to_gb(used.get("mem", "")), to_gb(req.get("mem", ""))
    if m_u and m_r and m_u / m_r >= 0.9:
        notes.append(f"memory {used['mem']} of {req['mem']}")
    return "used 90% or more of the request: " + "; ".join(notes) if notes else ""


def query(ids, qstat):
    cmd = [qstat, "-x", "-f", "-F", "json"] + ids
    cp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
    if cp.returncode != 0 and "-x" in cmd and ("illegal option" in cp.stderr.lower() or "unknown option" in cp.stderr.lower()):
        cp = subprocess.run([qstat, "-f", "-F", "json"] + ids, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
    jobs = {}
    if cp.stdout.strip():
        try:
            jobs = json.loads(cp.stdout).get("Jobs", {}) or {}
        except json.JSONDecodeError:
            raise RuntimeError("qstat did not return JSON. This tool needs a PBS version that supports 'qstat -F json'.")
    return jobs, cp.stderr.strip()


def collect(entries, qstat):
    ids = [e["pbs_job_id"] for e in entries]
    jobs, err = query(ids, qstat)
    rows = []
    for e in entries:
        jid = e["pbs_job_id"]
        job = jobs.get(jid) or next((v for k, v in jobs.items() if k.split(".")[0] == jid.split(".")[0]), None)
        if job is None:
            rows.append({"id": e.get("id", jid), "pbs_job_id": jid, "status": "UNKNOWN", "detail": "not known to the server (finished and aged out of history, or wrong id)"})
            continue
        status, code = classify(job)
        row = {"id": e.get("id", jid), "pbs_job_id": jid, "name": job.get("Job_Name"), "status": status,
               "state_code": job.get("job_state"), "exit_status": code, "queue": job.get("queue"),
               "walltime_used": (job.get("resources_used") or {}).get("walltime"),
               "mem_used": (job.get("resources_used") or {}).get("mem"),
               "walltime_requested": (job.get("Resource_List") or {}).get("walltime"),
               "mem_requested": (job.get("Resource_List") or {}).get("mem"),
               "comment": job.get("comment"), "output_path": job.get("Output_Path")}
        if code is not None:
            row["detail"] = exit_meaning(code)
        note = usage_note(job)
        if note:
            row["resource_note"] = note
        rows.append(row)
    return rows


def overall(rows):
    st = {r["status"] for r in rows}
    if "FAILED" in st:
        return "FAILED"
    if st == {"SUCCEEDED"}:
        return "COMPLETE"
    if st & {"QUEUED", "HELD", "RUNNING"}:
        return "IN_PROGRESS"
    return "UNRESOLVED"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--record", help="submission_record.json written by submit_jobs.py")
    ap.add_argument("--jobs", nargs="+", help="PBS job ids")
    ap.add_argument("--wait", action="store_true", help="Poll until no job is queued or running")
    ap.add_argument("--interval", type=int, default=120, help="Seconds between polls with --wait")
    ap.add_argument("--timeout", type=int, default=3600, help="Give up waiting after this many seconds")
    ap.add_argument("--qstat", default="qstat")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    if not args.record and not args.jobs:
        ap.error("give --record or --jobs")
    entries = []
    if args.record:
        try:
            entries = [j for j in json.loads(Path(args.record).read_text()).get("jobs", []) if j.get("pbs_job_id")]
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Cannot read record: {exc}", file=sys.stderr)
            return 2
    for j in args.jobs or []:
        if not JOB_ID.match(j):
            print(f"'{j}' is not a PBS job id.", file=sys.stderr)
            return 2
        entries.append({"id": j, "pbs_job_id": j})
    if not entries:
        print("No submitted jobs found.", file=sys.stderr)
        return 2
    if shutil.which(args.qstat) is None:
        print(f"'{args.qstat}' not found on PATH. Run this on a machine with PBS, or ask the user to run: qstat -x -f {' '.join(e['pbs_job_id'] for e in entries)}", file=sys.stderr)
        return 2

    start = time.time()
    while True:
        try:
            rows = collect(entries, args.qstat)
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            print(f"qstat failed: {exc}", file=sys.stderr)
            return 2
        active = [r for r in rows if r["status"] in ("QUEUED", "HELD", "RUNNING")]
        if not args.wait or not active or time.time() - start >= args.timeout:
            break
        print(f"{len(active)} of {len(rows)} job(s) still active; next check in {args.interval}s", file=sys.stderr)
        time.sleep(args.interval)

    summary = overall(rows)
    if args.format == "json":
        print(json.dumps({"overall": summary, "jobs": rows}, indent=2))
    else:
        for r in rows:
            line = f"{r['id']:<14} {r['pbs_job_id']:<16} {r['status']:<10}"
            if r.get("walltime_used"):
                line += f" walltime {r['walltime_used']}/{r.get('walltime_requested', '?')}"
            if r.get("mem_used"):
                line += f" mem {r['mem_used']}/{r.get('mem_requested', '?')}"
            print(line)
            if r["status"] == "FAILED" or r["status"] == "UNKNOWN":
                if r.get("detail"):
                    prefix = f"exit {r['exit_status']}: " if r.get("exit_status") is not None else ""
                    print(f"    -> {prefix}{r['detail']}")
                if r.get("comment") and r["status"] == "FAILED":
                    print(f"    -> server comment: {r['comment']}")
            if r.get("resource_note"):
                print(f"    -> {r['resource_note']}")
        counts = {s: sum(1 for r in rows if r["status"] == s) for s in sorted({r["status"] for r in rows})}
        print(f"\nOVERALL: {summary}  {counts}")
    return {"COMPLETE": 0, "FAILED": 1, "UNRESOLVED": 4}.get(summary, 3)


if __name__ == "__main__":
    sys.exit(main())
