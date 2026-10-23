#!/usr/bin/env python3
"""The run ledger: what a new session needs to pick up a run after the old chat has ended.

Usage:
    run_state.py resume [--run-dir FOLDER] [--format text|json]
    run_state.py list --root FOLDER [--limit 10] [--format text|json]

The ledger is the file mima_run_state.json in a run folder. submit_jobs.py and stage_report.py write it; the model never
does. It records, per submitted stage (a "batch"): the stage name, the choices the person made, the jobs and PBS ids, who is
to be emailed, where outputs should appear and the stage report once one exists. It is memory for a NEW session, never
the truth about the cluster: `resume` always asks PBS for the live state and trusts the ledger only for what PBS cannot
tell it (the person's choices, the batch, and a stage report written while PBS still remembered the jobs).

`resume` is read-only. It never submits, runs, changes or deletes anything. Without --run-dir it looks under
$MIMA_ALLOWED_ROOTS. With a folder that holds no run it looks up to 4 levels below it for runs, newest first.

Exit code: 0 = a summary was produced (read its "status"), 2 = no run found or usage problem.
Status words: in_progress, complete, failed, unresolved, no_jobs, choose_run.
"""
import argparse
import contextlib
import datetime as dt
import errno
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

LEDGER = "mima_run_state.json"
RECORD = "submission_record.json"
SCHEMA = 1
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".cache", "CleanReads", "QCReport"}
MAX_WALK = 30000   # directory entries looked at while searching for runs
MAX_DEPTH = 4


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class LedgerBusy(Exception):
    pass


def ledger_path(run_dir):
    return Path(run_dir) / LEDGER


def load(run_dir):
    """The ledger as a dict, or None when there is none. A damaged file raises ValueError (never silently replaced)."""
    p = ledger_path(run_dir)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
    except ValueError:
        raise ValueError("%s is not valid JSON; move it aside before continuing" % p)
    if not isinstance(data, dict):
        raise ValueError("%s does not hold a ledger" % p)
    return data


def save(run_dir, data):
    """Write the ledger atomically: a half-written file is never visible to another session or to a job."""
    p = ledger_path(run_dir)
    data["schema"] = SCHEMA
    data["run_dir"] = str(run_dir)
    data["updated_at"] = now()
    tmp = p.with_name(".%s.%d.tmp" % (LEDGER, os.getpid()))
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(str(tmp), str(p))


def new_ledger(run_dir):
    return {"schema": SCHEMA, "run_dir": str(run_dir), "created_at": now(), "stages": [], "events": []}


def add_event(data, kind, text):
    data.setdefault("events", []).append({"at": now(), "kind": kind, "text": text})
    del data["events"][:-200]


def find_stage(data, batch):
    for s in data.get("stages", []):
        if s.get("batch") == batch:
            return s
    return None


@contextlib.contextmanager
def lock(run_dir, wait=0.0):
    """Hold an exclusive lock on the run folder's ledger while it is changed.

    flock goes away by itself when the process ends, so a crash cannot leave a stale lock. If the file system does not
    support locks the work goes ahead unlocked (the writes are still atomic). Raises LedgerBusy if another process holds it
    for longer than `wait` seconds."""
    import time
    try:
        import fcntl
    except ImportError:  # not on a POSIX system
        yield False
        return
    fh = open(str(Path(run_dir) / (LEDGER + ".lock")), "a")
    got, deadline = False, time.time() + wait
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except (IOError, OSError) as exc:
                if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    if time.time() >= deadline:
                        raise LedgerBusy("another session is writing the ledger in %s" % run_dir)
                    time.sleep(0.2)
                else:   # locks not supported here
                    break
        yield got
    finally:
        if got:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except (IOError, OSError):
                pass
        fh.close()


# ------------------------------------------------------------------ finding runs
def find_runs(root, limit=10):
    """Folders under root (up to MAX_DEPTH levels) that hold a ledger or a submission record, newest first."""
    root = Path(root)
    found, seen = [], 0
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(str(root)):
        seen += 1 + len(dirnames)
        depth = len(Path(dirpath).parts) - base_depth
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")] if depth < MAX_DEPTH else []
        if seen > MAX_WALK:
            break
        hit = [n for n in (LEDGER, RECORD) if n in filenames]
        if hit:
            newest = max(os.path.getmtime(os.path.join(dirpath, n)) for n in hit)
            found.append((newest, dirpath, hit))
    found.sort(reverse=True)
    return [{"run_dir": d, "has_ledger": LEDGER in hit, "last_changed": dt.datetime.fromtimestamp(m, dt.timezone.utc).isoformat(timespec="seconds")}
            for m, d, hit in found[:limit]]


# ------------------------------------------------------------------ summary
def _entries_from_record(run_dir):
    p = Path(run_dir) / RECORD
    if not p.is_file():
        return []
    try:
        return [j for j in json.loads(p.read_text()).get("jobs", []) if j.get("pbs_job_id")]
    except ValueError:
        return []


def _compact_findings(findings, limit=10):
    out = []
    for f in findings[:limit]:
        out.append({"pattern": f["pattern"], "severity": f["severity"], "file": f["file"], "verified": f.get("verified", False),
                    "line": (f["lines"][0]["text"] if f.get("lines") else ""), "cause": f["cause"], "fix": f["fix"]})
    return out


def summarize(run_dir, qstat="qstat", check_outputs_too=True):
    """One read-only picture of a run folder: the ledger, live PBS state, log findings and an output check."""
    import shutil
    run_dir = Path(run_dir)
    out = {"run_dir": str(run_dir), "status": "no_jobs", "notes": []}
    try:
        ledger = load(run_dir)
    except ValueError as exc:
        ledger = None
        out["notes"].append(str(exc))
    out["ledger_found"] = ledger is not None
    stage = None
    if ledger and ledger.get("stages"):
        stage = ledger["stages"][-1]
        out["stage"] = stage.get("stage")
        out["batch"] = stage.get("batch")
        out["choices"] = stage.get("choices") or {}
        out["submitted_at"] = stage.get("submitted_at")
        out["mail_to"] = stage.get("mail_to")
        out["earlier_stages"] = [{"stage": s.get("stage"), "batch": s.get("batch"), "submitted_at": s.get("submitted_at")}
                                 for s in ledger["stages"][:-1]][-5:]
        entries = [{"id": j["id"], "pbs_job_id": j["pbs_job_id"], "cwd": j.get("cwd"), "role": j.get("role", "stage")}
                   for j in stage.get("jobs", []) if j.get("pbs_job_id")]
    else:
        entries = [dict(j, role=j.get("role", "stage")) for j in _entries_from_record(run_dir)]
        if entries:
            out["notes"].append("No ledger in this folder (a run made before ledgers existed). Using submission_record.json only: "
                                "the stage name and the choices made are not known.")
    stage_jobs = [e for e in entries if e["role"] != "report"]
    report_jobs = [e for e in entries if e["role"] == "report"]
    if not stage_jobs:
        out["next"] = "No submitted jobs were found in this folder. Nothing is running from here."
        return out

    rows, live_ok = [], False
    if shutil.which(qstat) is None:
        out["notes"].append("qstat is not available on this machine, so live job state could not be read.")
    else:
        try:
            import check_job_status as cjs
            rows = cjs.collect(stage_jobs, qstat)
            live_ok = True
        except Exception as exc:  # qstat failed or returned something unexpected
            out["notes"].append("qstat failed: %s" % str(exc)[:200])
    overall = None
    if live_ok:
        import check_job_status as cjs
        overall = cjs.overall(rows)
    report = None
    if stage and stage.get("report"):
        report = stage["report"]
        out["stage_report"] = {k: report.get(k) for k in ("written_at", "overall", "counts", "file", "mail")}
    # PBS forgets finished jobs after a while; a stage report written at the end of the batch kept what it said.
    if overall in (None, "UNRESOLVED") and report and report.get("rows") and report.get("overall") in ("COMPLETE", "FAILED"):
        rows, overall = report["rows"], report.get("overall")
        out["notes"].append("PBS no longer remembers these jobs. Their outcome below is from the stage report written at %s, "
                            "when PBS still did." % report.get("written_at"))
    if overall is None:
        overall = "UNRESOLVED"
    out["live_overall"] = overall
    out["jobs"] = rows
    out["status"] = {"COMPLETE": "complete", "FAILED": "failed", "IN_PROGRESS": "in_progress"}.get(overall, "unresolved")

    if report_jobs and live_ok:
        try:
            import check_job_status as cjs
            out["report_job"] = [{"pbs_job_id": r["pbs_job_id"], "status": r["status"]} for r in cjs.collect(report_jobs, qstat)]
        except Exception:
            pass

    if overall != "IN_PROGRESS":
        dirs = sorted({e.get("cwd") for e in stage_jobs if e.get("cwd") and Path(e["cwd"]).is_dir()})
        if dirs:
            try:
                import scan_logs
                findings, n = scan_logs.scan(dirs)
                out["logs_scanned"] = n
                out["log_findings"] = _compact_findings(findings)
            except Exception as exc:
                out["notes"].append("log scan failed: %s" % str(exc)[:200])
        chk = (stage or {}).get("check")
        if check_outputs_too and chk and overall == "COMPLETE" and chk.get("stage") and chk.get("dir"):
            try:
                import check_outputs as co
                res = co.Result(chk["stage"])
                co.STAGES[chk["stage"]](res, Path(chk["dir"]), chk.get("manifest"))
                out["outputs"] = {"stage": chk["stage"], "dir": chk["dir"], "ok": res.ok, "summary": res.summary()}
            except Exception as exc:
                out["notes"].append("output check failed: %s" % str(exc)[:200])
    out["next"] = next_hint(out)
    return out


def next_hint(s):
    st = s["status"]
    if st == "in_progress":
        return ("Jobs are still queued or running. Nothing to do yet; check again later. "
                "Do not start the next stage until these have finished.")
    if st == "failed":
        return ("At least one job failed. Read the job rows and log findings, explain them to the person, and fix the cause. "
                "An identical script cannot be submitted again unless the person asks for a rerun.")
    if st == "complete":
        if s.get("outputs"):
            if s["outputs"]["ok"]:
                return "The jobs finished normally and the expected output files are present. The next stage can be planned (see the skill's stage gates)."
            return "The jobs finished normally but expected output files are missing or empty. Report the output check before anything else."
        return ("The jobs finished normally. Check the stage's outputs (mima_check_outputs) before starting the next stage; "
                "no output check was run because the ledger does not say which folder to check.")
    return ("PBS no longer knows at least one job, and no stage report recorded its outcome. Decide from the job logs and the output check, "
            "not from time passed.")


# ------------------------------------------------------------------ text output
def render(s):
    lines = ["Run folder: %s" % s["run_dir"]]
    if s.get("ledger_found") or s.get("stage"):
        lines.append("Stage: %s   batch: %s   submitted: %s" % (s.get("stage") or "unknown", s.get("batch") or "-", s.get("submitted_at") or "-"))
    if s.get("choices"):
        lines.append("Choices recorded: " + "; ".join("%s=%s" % kv for kv in sorted(s["choices"].items())))
    lines.append("STATUS: %s" % s["status"].upper())
    for r in s.get("jobs", []):
        line = "  %-14s %-18s %s" % (r.get("id"), r.get("pbs_job_id"), r.get("status"))
        if r.get("walltime_used"):
            line += "  walltime %s/%s  mem %s/%s" % (r.get("walltime_used"), r.get("walltime_requested", "?"), r.get("mem_used", "?"), r.get("mem_requested", "?"))
        lines.append(line)
        if r.get("resource_note"):
            lines.append("      -> " + r["resource_note"])
    for f in s.get("log_findings", []):
        lines.append("  LOG %s %s (%s): %s" % (f["severity"].upper(), f["pattern"], "verified" if f["verified"] else "unverified", f["line"][:120]))
    if s.get("outputs"):
        o = s["outputs"]
        lines.append("  OUTPUTS: %s (%s)" % ("complete" if o["ok"] else "INCOMPLETE", json.dumps(o["summary"])))
    for n in s.get("notes", []):
        lines.append("NOTE: " + n)
    lines.append("NEXT: " + s["next"] if s.get("next") else "")
    return "\n".join(x for x in lines if x)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("resume")
    r.add_argument("--run-dir")
    r.add_argument("--qstat", default="qstat")
    r.add_argument("--format", choices=["text", "json"], default="text")
    ls = sub.add_parser("list")
    ls.add_argument("--root", required=True)
    ls.add_argument("--limit", type=int, default=10)
    ls.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return 2

    if args.cmd == "list":
        runs = find_runs(args.root, args.limit)
        if args.format == "json":
            print(json.dumps({"status": "choose_run" if runs else "not_found", "runs": runs}, indent=2))
        else:
            for x in runs:
                print("%s   last changed %s%s" % (x["run_dir"], x["last_changed"], "" if x["has_ledger"] else "   (no ledger)"))
            if not runs:
                print("No runs found under %s" % args.root)
        return 0 if runs else 2

    roots = [args.run_dir] if args.run_dir else [r_ for r_ in os.environ.get("MIMA_ALLOWED_ROOTS", "").split(os.pathsep) if r_]
    if not roots:
        print("Give --run-dir (the folder the stage was run in), or set MIMA_ALLOWED_ROOTS.", file=sys.stderr)
        return 2
    run_dir = None
    if args.run_dir and ((Path(args.run_dir) / LEDGER).is_file() or (Path(args.run_dir) / RECORD).is_file()):
        run_dir = args.run_dir
    else:
        runs = []
        for root in roots:
            if Path(root).is_dir():
                runs += find_runs(root, 10)
        runs.sort(key=lambda x: x["last_changed"], reverse=True)
        if len(runs) == 1:
            run_dir = runs[0]["run_dir"]
        elif runs:
            if args.format == "json":
                print(json.dumps({"status": "choose_run", "runs": runs[:10], "next": "Ask the person which run folder they mean."}, indent=2))
            else:
                print("More than one run was found. Which one?")
                for x in runs[:10]:
                    print("  %s   last changed %s" % (x["run_dir"], x["last_changed"]))
            return 0
    if run_dir is None:
        print("No run found (no %s or %s) in or under: %s" % (LEDGER, RECORD, ", ".join(roots)), file=sys.stderr)
        return 2
    s = summarize(run_dir, args.qstat)
    print(json.dumps(s, indent=2) if args.format == "json" else render(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
