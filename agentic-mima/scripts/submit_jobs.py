#!/usr/bin/env python3
"""Submit validated MIMA PBS scripts with qsub, only after explicit confirmation.

Usage:
    submit_jobs.py PLAN.json [--no-fs-check] [--record FILE]                     # dry run (default)
    submit_jobs.py PLAN.json --execute --confirm TOKEN [--record FILE]           # real submission,
                                                                                 # a person types a confirmation
    submit_jobs.py PLAN.json --execute --confirm TOKEN --approval harness        # see below

PLAN.json:
    {"jobs": [
       {"id": "qc0", "script": "/abs/QC_module/qcModule_0.pbs"},
       {"id": "ft",  "script": "/abs/Function_profiling/featureTables/generate_func_feature_tables.pbs",
        "after": ["qc0"], "after_job_ids": ["1234.server"]}
    ]}
  id              short unique name used in dependencies and the record
  script          absolute path to a .pbs file
  after           ids of jobs earlier in this plan or in the record; adds -W depend=afterok
  after_job_ids   literal PBS job ids that are still queued or running
  cwd             optional; defaults to the script's directory (PBS writes its log there,
                  and the QC and Bracken report steps read those logs)

Safety model (what each control does and does not do)
  * The default is a dry run. It validates every script (validate_pbs_scripts.py) and prints
    the exact qsub commands plus a confirmation TOKEN.
  * The TOKEN binds the execution to what was reviewed: it is a SHA-256 prefix of the plan and
    of every script's content, so anything changed since the dry run invalidates it. It does
    NOT prove that a person approved: anyone who ran the dry run can read it.
  * Human approval is enforced separately by --approval:
      tty      (default) after the token check, the script asks for a typed confirmation
               ("submit N") and refuses unless stdin and stdout are an interactive terminal.
               A tool that runs commands without a terminal cannot satisfy this, so a person
               has to run the command themselves.
      harness  no typed confirmation. Use only when the tool that runs this command shows a
               person the exact command, including this flag and the token, and waits for
               their approval. The approval is then enforced by that tool, not by this script.
  * Optional plan fields: "mail_to" (email for PBS notices), "batch", "stage", "choices", "check", "expected_minutes" are kept in
    the run ledger (mima_run_state.json, next to the record). A job may have "after_any" (start when those jobs have ended
    in any way), "queue" (its own queue) and "role": "report" (the stage-report job: it only reports, never submits).
  * qsub is called without a shell, with a fixed argument list. Nothing outside qsub runs.
  * A script already recorded as submitted is refused unless --allow-resubmit is given.
  * Deleting or altering jobs is never done here.

Exit code: 0 = ok, 1 = validation or submission failure, 2 = bad plan or usage.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_state  # noqa: E402
from validate_pbs_scripts import validate_file  # noqa: E402

JOB_ID = re.compile(r"^\d+(\[\])?(\.[A-Za-z0-9_.-]+)?$")
ID_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SAFE_OPT = re.compile(r"^[A-Za-z0-9_.@:-]{1,64}$")
MAIL = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,120}\.[A-Za-z]{2,}$")
BATCH = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
ROLES = ("stage", "report")


def load_record(path):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            sys.exit(f"Record file is not valid JSON: {path}")
    return {"jobs": []}


def prepare(plan, record, check_fs):
    """Validate the plan; return (jobs, errors, warnings, validation_results)."""
    errors, warns, results = [], [], {}
    jobs = plan.get("jobs") if isinstance(plan, dict) else None
    if not jobs or not isinstance(jobs, list):
        return [], ["Plan must contain a non-empty 'jobs' list."], warns, results
    known_plan = {j.get("id") for j in jobs if isinstance(j, dict)}
    known_record = {j["id"]: j for j in record.get("jobs", []) if j.get("pbs_job_id")}
    seen = set()
    for j in jobs:
        jid = j.get("id")
        if not isinstance(jid, str) or not ID_NAME.match(jid):
            errors.append(f"Job id {jid!r} must use letters, digits and _ . - only.")
            continue
        if jid in seen:
            errors.append(f"Duplicate job id '{jid}'.")
        seen.add(jid)
        script = j.get("script")
        if not isinstance(script, str) or not script.startswith("/") or not script.endswith(".pbs"):
            errors.append(f"{jid}: script must be an absolute path ending in .pbs.")
            continue
        p = Path(script)
        if not p.is_file():
            errors.append(f"{jid}: script not found: {script}")
            continue
        j["cwd"] = j.get("cwd") or str(p.parent)
        if not Path(j["cwd"]).is_dir():
            errors.append(f"{jid}: working directory not found: {j['cwd']}")
        for dep in list(j.get("after", [])) + list(j.get("after_any", [])):
            if dep not in known_plan and dep not in known_record:
                errors.append(f"{jid}: 'after'/'after_any' refers to unknown job '{dep}'.")
        if j.get("role", "stage") not in ROLES:
            errors.append(f"{jid}: role must be one of {', '.join(ROLES)}.")
        if j.get("queue") is not None and not SAFE_OPT.match(str(j["queue"])):
            errors.append(f"{jid}: queue {j['queue']!r} has unsupported characters.")
        for ext in j.get("after_job_ids", []):
            if not JOB_ID.match(str(ext)):
                errors.append(f"{jid}: '{ext}' is not a PBS job id.")
        res = validate_file(p, check_fs, module="report" if j.get("role") == "report" else None)
        results[jid] = res
        j["sha256"] = res["sha256"]
        errors += [f"{jid}: {e}" for e in res["errors"]]
        warns += [f"{jid}: {w}" for w in res["warnings"] if not w.startswith("Filesystem checks skipped")]
    # cycle check among plan jobs
    order, done, temp = [], set(), set()
    by_id = {j["id"]: j for j in jobs if isinstance(j.get("id"), str)}

    def visit(n):
        if n in done or n not in by_id:
            return
        if n in temp:
            errors.append(f"Dependency cycle involving '{n}'.")
            return
        temp.add(n)
        for d in list(by_id[n].get("after", [])) + list(by_id[n].get("after_any", [])):
            visit(d)
        temp.discard(n)
        done.add(n)
        order.append(by_id[n])

    for j in jobs:
        if isinstance(j.get("id"), str):
            visit(j["id"])
    return order, errors, warns, results


def run_command(args, token):
    parts = ["python3", sys.argv[0], args.plan, "--execute", "--confirm", token, "--record", args.record]
    for k in ("project", "queue", "account"):
        if getattr(args, k):
            parts += [f"--{k}", getattr(args, k)]
    if args.mail_to:
        parts += ["--mail-to", args.mail_to]
    return " ".join(shlex.quote(x) for x in parts)


def make_token(order, opts):
    payload = [{k: j.get(k) for k in ("id", "script", "sha256", "cwd", "after", "after_any", "after_job_ids", "role", "queue")} for j in order]
    blob = json.dumps({"jobs": payload, "opts": opts}, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def qsub_args(job, opts, pbs_ids):
    args = []
    queue = job.get("queue") or opts.get("queue")   # a job's own queue wins (the stage-report job must not go to hugemem)
    for flag, value in (("-P", opts.get("project")), ("-q", queue), ("-A", opts.get("account"))):
        if value:
            args += [flag, value]
    if opts.get("mail_to"):
        # Stage jobs mail only if PBS aborts them (a quiet queue for 100 samples); the report job mails when it ends.
        args += ["-M", opts["mail_to"], "-m", "ae" if job.get("role") == "report" else "a"]
    ok = [pbs_ids.get(d, f"<{d}>") for d in job.get("after", [])] + list(job.get("after_job_ids", []))
    anyend = [pbs_ids.get(d, f"<{d}>") for d in job.get("after_any", [])]
    deps = []
    if ok:
        deps.append("afterok:" + ":".join(ok))
    if anyend:
        deps.append("afterany:" + ":".join(anyend))
    if deps:
        args += ["-W", "depend=" + ",".join(deps)]
    return args + [job["script"]]


def plan_meta(plan, args):
    """Optional plan-level facts kept in the run ledger. Returns (meta, errors)."""
    errors, meta = [], {}
    mail = args.mail_to or plan.get("mail_to")
    if mail:
        if not MAIL.match(str(mail)):
            errors.append(f"mail_to {mail!r} is not a valid email address.")
        else:
            meta["mail_to"] = str(mail)
    batch = plan.get("batch")
    if batch is not None:
        if not isinstance(batch, str) or not BATCH.match(batch):
            errors.append("batch must use letters, digits and _ . - only (max 64).")
        else:
            meta["batch"] = batch
    if plan.get("stage") is not None:
        if isinstance(plan["stage"], str) and BATCH.match(plan["stage"]):
            meta["stage"] = plan["stage"]
        else:
            errors.append("stage must use letters, digits and _ . - only (max 64).")
    ch = plan.get("choices")
    if ch is not None:
        if not isinstance(ch, dict) or len(ch) > 20 or not all(isinstance(k, str) and isinstance(v, (str, int, float)) and len(str(v)) <= 200 and len(k) <= 64 for k, v in ch.items()):
            errors.append("choices must be a small map of short text values (at most 20).")
        else:
            meta["choices"] = {k: str(v) for k, v in ch.items()}
    chk = plan.get("check")
    if chk is not None:
        if (isinstance(chk, dict) and isinstance(chk.get("stage"), str) and isinstance(chk.get("dir"), str) and chk["dir"].startswith("/")
                and (chk.get("manifest") is None or (isinstance(chk["manifest"], str) and chk["manifest"].startswith("/")))):
            meta["check"] = {"stage": chk["stage"], "dir": chk["dir"], "manifest": chk.get("manifest")}
        else:
            errors.append("check must be {\"stage\": ..., \"dir\": \"/abs/path\", \"manifest\": \"/abs/path\" (optional)}.")
    if plan.get("expected_minutes") is not None:
        if isinstance(plan["expected_minutes"], int) and 0 < plan["expected_minutes"] < 100000:
            meta["expected_minutes"] = plan["expected_minutes"]
        else:
            errors.append("expected_minutes must be a positive whole number.")
    return meta, errors


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan")
    ap.add_argument("--execute", action="store_true", help="Actually call qsub (needs --confirm)")
    ap.add_argument("--confirm", help="Token printed by the dry run")
    ap.add_argument("--record", default="submission_record.json", help="JSON file that logs what was submitted")
    ap.add_argument("--no-fs-check", action="store_true", help="Skip path-existence checks (dry run off the cluster only)")
    ap.add_argument("--approval", choices=["tty", "harness"], default="tty",
                    help="tty: a person types a confirmation at a terminal (default). harness: approval is enforced by the tool running this command")
    ap.add_argument("--allow-resubmit", action="store_true")
    ap.add_argument("--project", help="qsub -P value required by some sites")
    ap.add_argument("--queue", help="qsub -q value")
    ap.add_argument("--account", help="qsub -A value")
    ap.add_argument("--qsub", default="qsub", help="qsub executable name")
    ap.add_argument("--mail-to", help="email address for PBS notices (the plan's mail_to is used when this is not given)")
    args = ap.parse_args()

    opts = {k: getattr(args, k) for k in ("project", "queue", "account") if getattr(args, k)}
    for k, v in opts.items():
        if not SAFE_OPT.match(v):
            print(f"--{k} value {v!r} has unsupported characters.", file=sys.stderr)
            return 2
    try:
        plan = json.loads(Path(args.plan).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Cannot read plan: {exc}", file=sys.stderr)
        return 2

    rec_path = Path(args.record)
    record = load_record(rec_path)
    meta, meta_errors = plan_meta(plan, args)
    if meta.get("mail_to"):
        opts["mail_to"] = meta["mail_to"]
    order, errors, warns, _ = prepare(plan, record, check_fs=not args.no_fs_check)
    errors = meta_errors + errors
    token = make_token(order, opts) if order else None

    submitted_hashes = {j.get("sha256") for j in record.get("jobs", []) if j.get("pbs_job_id")}
    if not args.allow_resubmit:
        for j in order:
            if j.get("sha256") in submitted_hashes:
                errors.append(f"{j['id']}: an identical script was already submitted (see {rec_path}). Use --allow-resubmit only if the user asked to rerun it.")

    print(f"Jobs in plan: {len(order)}   working checks: {'paths verified' if not args.no_fs_check else 'paths NOT verified'}")
    for w in warns:
        print(f"WARN:  {w}")
    for e in errors:
        print(f"ERROR: {e}")
    if errors:
        print("\nRESULT: NOT READY. Fix the errors above; nothing was submitted.")
        return 1

    print("\nqsub commands (run in each script's directory):")
    ids = {}
    for j in order:
        print(f"  [{j['id']}] cd {j['cwd']} && {args.qsub} " + " ".join(qsub_args(j, opts, ids)))
        ids[j["id"]] = f"<{j['id']}>"
    if meta.get("mail_to"):
        print(f"\nEmail notices go to {meta['mail_to']}: PBS mails if a stage job is aborted"
              + ("; the stage-report job mails when it ends (after every stage job has ended, whatever the outcome)." if any(j.get("role") == "report" for j in order) else "."))
    if any(j.get("role") == "report" for j in order):
        print("The stage-report job only reports. It never submits another stage; the next stage needs a person's approval in a session.")

    if not args.execute:
        print(f"\nDRY RUN ONLY. Nothing was submitted.\nConfirmation token: {token}")
        print(f"To submit exactly this plan, a person runs this in a terminal and types the confirmation when asked:\n  {run_command(args, token)}")
        return 0

    if args.confirm != token:
        print(f"\nREFUSED: --confirm does not match the current plan and scripts (expected {token}). Re-run the dry run and have the user review it again.")
        return 1
    if shutil.which(args.qsub) is None:
        print(f"\nREFUSED: '{args.qsub}' not found on PATH. Run this on a machine with PBS.")
        return 1

    if args.approval == "tty":
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("\nREFUSED: submission needs a person at an interactive terminal to type a confirmation, and this command is not "
                  "running in one. Nothing was submitted.\nAsk the user to run this themselves in a terminal:\n  " + run_command(args, token) +
                  "\n(Use --approval harness only if the tool running this command shows the user this exact command and waits for their approval.)")
            return 1
        expected = f"submit {len(order)}"
        answer = input(f"\nType '{expected}' to submit {len(order)} job(s) now (anything else cancels): ").strip()
        if answer != expected:
            print("Cancelled. Nothing was submitted.")
            return 1

    run_dir = rec_path.resolve().parent
    try:
        ledger = run_state.load(run_dir) or run_state.new_ledger(run_dir)
    except ValueError as exc:
        print(f"\nREFUSED: {exc}. Nothing was submitted.")
        return 1
    try:
        with run_state.lock(run_dir, wait=float(os.environ.get("MIMA_LEDGER_WAIT", "10"))):
            return execute(args, order, token, opts, meta, record, rec_path, run_dir, ledger)
    except run_state.LedgerBusy as exc:
        print(f"\nREFUSED: {exc}. Another session may be submitting here. Nothing was submitted.")
        return 1


def execute(args, order, token, opts, meta, record, rec_path, run_dir, ledger):
    """Queue the jobs one by one, writing the record and the run ledger after each, so a cut-off loses nothing."""
    pbs_ids = {j["id"]: j["pbs_job_id"] for j in record.get("jobs", []) if j.get("pbs_job_id")}
    now = lambda: dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")  # noqa: E731
    batch = meta.get("batch") or token
    record.setdefault("host", socket.gethostname())
    record.setdefault("jobs", [])
    record.setdefault("approvals", []).append({"mode": args.approval, "at": now(), "plan_token": token, "jobs": [j["id"] for j in order]})
    rec_path.write_text(json.dumps(record, indent=2))
    entry = {"batch": batch, "stage": meta.get("stage"), "token": token, "submitted_at": now(), "approval": args.approval,
             "choices": meta.get("choices") or {}, "check": meta.get("check"), "expected_minutes": meta.get("expected_minutes"),
             "mail_to": meta.get("mail_to"), "jobs": []}
    ledger.setdefault("stages", []).append(entry)
    run_state.add_event(ledger, "approved", f"{len(order)} job(s) approved ({args.approval}); batch {batch}")
    run_state.save(run_dir, ledger)
    failed = False
    for j in order:
        cmd = [args.qsub] + qsub_args(j, opts, pbs_ids)
        try:
            cp = subprocess.run(cmd, cwd=j["cwd"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[{j['id']}] FAILED to run qsub: {exc}")
            failed = True
            break
        out = cp.stdout.strip()
        if cp.returncode != 0 or not JOB_ID.match(out):
            print(f"[{j['id']}] qsub failed (exit {cp.returncode}): {cp.stderr.strip() or out}")
            failed = True
            break
        pbs_ids[j["id"]] = out
        role = j.get("role", "stage")
        record["jobs"].append({"id": j["id"], "script": j["script"], "sha256": j["sha256"], "cwd": j["cwd"], "role": role, "batch": batch,
                               "after": j.get("after", []), "after_any": j.get("after_any", []), "pbs_job_id": out, "submitted_at": now(), "plan_token": token})
        print(f"[{j['id']}] submitted as {out}")
        rec_path.write_text(json.dumps(record, indent=2))
        entry["jobs"].append({"id": j["id"], "pbs_job_id": out, "script": j["script"], "cwd": j["cwd"], "role": role, "submitted_at": now(),
                              "after": j.get("after", []), "after_any": j.get("after_any", [])})
        run_state.save(run_dir, ledger)

    if failed:
        run_state.add_event(ledger, "submit_failed", "submission stopped part-way; jobs already queued stay in the queue")
        run_state.save(run_dir, ledger)
        print(f"\nSubmission stopped. Jobs already submitted stay in the queue and are listed in {rec_path}. Dependents of the failed job were not submitted.")
        return 1
    run_state.add_event(ledger, "submitted", f"{len(order)} job(s) queued")
    run_state.save(run_dir, ledger)
    print(f"\nAll {len(order)} job(s) submitted. Record written to {rec_path}; run ledger {run_state.ledger_path(run_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
