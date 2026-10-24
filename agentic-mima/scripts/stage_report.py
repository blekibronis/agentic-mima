#!/usr/bin/env python3
"""The follow-up job that reports on a finished stage, so nobody has to be watching.

Usage:
    stage_report.py write-job --run-dir FOLDER --batch NAME [--like STAGE_SCRIPT.pbs] [--out FILE]
                              [--walltime 0:10:00] [--mem 2GB]
    stage_report.py run --run-dir FOLDER --batch NAME          (this is what the job runs; read-only apart from its own report)

write-job writes a small PBS script, stage_report_<batch>.pbs, that submit_jobs.py queues together with a stage's jobs
with the dependency "afterany": PBS starts it when every job of the stage has ended, whatever the outcome (finished,
failed, killed at the walltime limit or deleted). It needs 1 CPU and a few minutes. It never submits anything.

run, inside that job: reads each stage job's final state from PBS while PBS still remembers it (qstat history is
short-lived), scans the job logs for known failures, checks the stage's expected outputs when the ledger names them,
then writes STAGE_REPORT_<batch>.json and .md in the run folder, records the result in the ledger
(mima_run_state.json) and emails a one-line notice to the address given at submission, if a mail command exists on the
node. It uses no model and changes no pipeline file.
"""
import argparse
import json
import os
import re
import signal
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_state  # noqa: E402

BATCH = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
WALLTIME = re.compile(r"^\d+:\d{2}(:\d{2})?$")
MEM = re.compile(r"^\d+(MB|GB|mb|gb)$")
MAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def header_lines_from(like):
    """The project (-P) and storage lines of a stage script: the report job needs the same file systems."""
    keep = []
    try:
        for ln in Path(like).read_text(errors="replace").splitlines()[:80]:
            s = ln.strip()
            if re.match(r"#PBS\s+-P\s+\S+", s) or re.match(r"#PBS\s+-l\s+storage=\S+", s):
                keep.append(re.sub(r"\s+#.*$", "", s))   # drop a trailing comment
    except OSError:
        pass
    return keep


def write_job(args):
    if not BATCH.match(args.batch):
        print("--batch must use letters, digits and _ . - only.", file=sys.stderr)
        return 2
    if not WALLTIME.match(args.walltime) or not MEM.match(args.mem):
        print("--walltime looks like 0:10:00 and --mem like 2GB.", file=sys.stderr)
        return 2
    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        print("Run folder not found: %s" % run_dir, file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else run_dir / ("stage_report_%s.pbs" % args.batch)
    extra = header_lines_from(args.like) if args.like else []
    me = Path(__file__).resolve()
    text = "\n".join(["#!/bin/bash", "#PBS -N mima-stage-report"] + extra +
                     ["#PBS -l ncpus=1", "#PBS -l walltime=%s" % args.walltime, "#PBS -l mem=%s" % args.mem, "#PBS -j oe",
                      "# Reports on a finished stage. Starts only after every job of the stage has ended; reads and reports, changes nothing else.",
                      "cd %s" % shlex.quote(str(run_dir)),
                      "%s %s run --run-dir %s --batch %s" % (args.python, shlex.quote(str(me)), shlex.quote(str(run_dir)), shlex.quote(args.batch)),
                      ""])
    out.write_text(text)
    print(str(out))
    return 0


# ------------------------------------------------------------------ the job itself
def counts_of(rows):
    c = {}
    for r in rows:
        c[r["status"]] = c.get(r["status"], 0) + 1
    return c


def one_line(batch, stage, overall, counts, out_ok):
    word = {"COMPLETE": "finished normally", "FAILED": "had a failure", "IN_PROGRESS": "is not finished", "UNRESOLVED": "has an unclear outcome"}[overall]
    extra = ""
    if out_ok is False:
        extra = "; expected output files are missing or empty"
    return "MIMA stage '%s' (%s) %s: %s%s. Start a new session and ask to check this run." % (
        stage or "unknown", batch, word, ", ".join("%d %s" % (n, s.lower()) for s, n in sorted(counts.items())), extra)


MAIL_WAIT = 20   # seconds to wait for the mail command


def send_mail(address, subject, body, wait=None):
    """Hand the notice to the node's mail command, and never wait on it for long.

    On Gadi the mail command can keep running after it has taken the message (it leaves a helper behind). With
    pipes attached, Python 3.6's subprocess.run waits for that helper forever, even after its own timeout: the first
    run of this job on Gadi sat idle until PBS killed it at the walltime. So no pipes are attached to the output,
    the command runs in its own process group, and the group is killed if it has not returned in `wait` seconds."""
    wait = wait if wait is not None else int(os.environ.get("MIMA_MAIL_WAIT", MAIL_WAIT))
    exe = shutil.which("mail") or shutil.which("mailx")
    if not address:
        return {"attempted": False, "note": "no email address was given at submission"}
    if not MAIL.match(address):
        return {"attempted": False, "note": "the address on record is not valid"}
    if not exe:
        return {"attempted": False, "note": "no mail command on this node; PBS end-of-job mail still applies"}
    try:
        p = subprocess.Popen([exe, "-s", subject, address], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, universal_newlines=True, start_new_session=True)
    except OSError as exc:
        return {"attempted": True, "sent": False, "note": str(exc)[:200]}
    try:
        p.stdin.write(body)
        p.stdin.close()
    except (OSError, ValueError):
        pass
    try:
        rc = p.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return {"attempted": True, "sent": None, "note": "the mail command did not return within %d seconds; it was stopped and delivery is not confirmed" % wait}
    return {"attempted": True, "sent": rc == 0, "note": "" if rc == 0 else "mail command exited with status %d" % rc}


def run(args):
    import check_job_status as cjs
    run_dir = Path(args.run_dir)
    ledger = run_state.load(run_dir)
    stage = run_state.find_stage(ledger, args.batch) if ledger else None
    if stage:
        jobs = [j for j in stage.get("jobs", []) if j.get("pbs_job_id")]
    else:   # no ledger: fall back to the submission record
        jobs = [j for j in run_state._entries_from_record(run_dir) if j.get("batch") == args.batch]
    stage_jobs = [j for j in jobs if j.get("role", "stage") != "report"]
    if not stage_jobs:
        print("No jobs found for batch %s in %s." % (args.batch, run_dir), file=sys.stderr)
        return 1

    rows = cjs.collect(stage_jobs, args.qstat)
    overall = cjs.overall(rows)
    counts = counts_of(rows)
    findings = []
    dirs = sorted({j.get("cwd") for j in stage_jobs if j.get("cwd") and Path(j["cwd"]).is_dir()})
    if dirs:
        import scan_logs
        found, _n = scan_logs.scan(dirs)
        findings = run_state._compact_findings(found)
    outputs = None
    chk = (stage or {}).get("check")
    if chk and overall == "COMPLETE" and chk.get("stage") and chk.get("dir"):
        import check_outputs as co
        res = co.Result(chk["stage"])
        co.STAGES[chk["stage"]](res, Path(chk["dir"]), chk.get("manifest"))
        outputs = {"stage": chk["stage"], "dir": chk["dir"], "ok": res.ok, "summary": res.summary()}

    stage_name = (stage or {}).get("stage")
    line = one_line(args.batch, stage_name, overall, counts, outputs["ok"] if outputs else None)
    report = {"batch": args.batch, "stage": stage_name, "written_at": run_state.now(), "overall": overall, "counts": counts,
              "rows": rows, "log_findings": findings, "outputs": outputs, "notice": line}
    jf, mf = run_dir / ("STAGE_REPORT_%s.json" % args.batch), run_dir / ("STAGE_REPORT_%s.md" % args.batch)
    mail_to = (stage or {}).get("mail_to")
    # The files and the ledger come first; the email goes last, so a mail command that misbehaves cannot cost the report.
    report["mail"] = {"attempted": False, "note": "not sent yet"}
    jf.write_text(json.dumps(report, indent=2) + "\n")
    md = ["# Stage report: %s (%s)" % (stage_name or "unknown", args.batch), "", "Written %s." % report["written_at"], "", "**%s**" % line, ""]
    md += ["| job | PBS id | state | walltime | memory |", "|---|---|---|---|---|"]
    for r in rows:
        md.append("| %s | %s | %s | %s/%s | %s/%s |" % (r.get("id"), r.get("pbs_job_id"), r["status"], r.get("walltime_used", "?"),
                                                       r.get("walltime_requested", "?"), r.get("mem_used", "?"), r.get("mem_requested", "?")))
    for f in findings:
        md.append("\n- %s %s: %s" % (f["severity"], f["pattern"], f["line"][:140]))
    if outputs:
        md.append("\nOutput check: %s %s" % ("complete" if outputs["ok"] else "INCOMPLETE", json.dumps(outputs["summary"])))
    mf.write_text("\n".join(md) + "\n")

    # Record it in the ledger. Another session may be writing at the same moment, so wait for the lock briefly.
    def record(mail_only=False):
        if ledger is None or stage is None:
            return
        try:
            with run_state.lock(run_dir, wait=30):
                led = run_state.load(run_dir) or ledger
                st = run_state.find_stage(led, args.batch)
                if st is None:
                    return
                if mail_only:
                    st.setdefault("report", {})["mail"] = report["mail"]
                else:
                    st["report"] = {"written_at": report["written_at"], "overall": overall, "counts": counts, "rows": rows,
                                    "file": str(jf), "mail": report["mail"]}
                    run_state.add_event(led, "stage_report", line)
                run_state.save(run_dir, led)
        except run_state.LedgerBusy:
            print("Ledger was busy; the report files were written but the ledger was not updated.", file=sys.stderr)

    record()
    report["mail"] = send_mail(mail_to, "MIMA stage %s: %s" % (stage_name or args.batch, overall), line + "\n\nRun folder: %s\n" % run_dir)
    jf.write_text(json.dumps(report, indent=2) + "\n")
    record(mail_only=True)
    print(line)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    w = sub.add_parser("write-job")
    w.add_argument("--run-dir", required=True)
    w.add_argument("--batch", required=True)
    w.add_argument("--like", help="a stage PBS script whose -P and storage lines are copied")
    w.add_argument("--out")
    w.add_argument("--walltime", default="0:10:00")
    w.add_argument("--mem", default="2GB")
    w.add_argument("--python", default="python3")
    r = sub.add_parser("run")
    r.add_argument("--run-dir", required=True)
    r.add_argument("--batch", required=True)
    r.add_argument("--qstat", default="qstat")
    args = ap.parse_args()
    if args.cmd == "write-job":
        return write_job(args)
    if args.cmd == "run":
        if not BATCH.match(args.batch):
            print("--batch must use letters, digits and _ . - only.", file=sys.stderr)
            return 2
        try:
            return run(args)
        except Exception as exc:   # leave a trace the next session can read
            try:
                (Path(args.run_dir) / ("STAGE_REPORT_%s.error.txt" % args.batch)).write_text(
                    "%s: the stage report job failed: %s\nCheck the jobs by hand with check_job_status.\n" % (run_state.now(), exc))
            except OSError:
                pass
            print("stage report failed: %s" % exc, file=sys.stderr)
            return 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
