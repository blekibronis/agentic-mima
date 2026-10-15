"""FastMCP server exposing the agentic-mima skill's validated scripts as tools.

The server holds no pipeline logic. Each tool checks its arguments, runs one bundled script
through runner.run_script and returns the script's own result. The skill (SKILL.md) says when to
call which tool; the scripts decide what is valid.

Every result is a dict: script, command, exit_code, status (a word from the script's own exit
codes), output (the script's text), data (parsed JSON, when the script offers it), stderr, truncated.

No tool submits a job. mima_prepare_submission runs submit_jobs.py as a dry run and returns the
command a person must run themselves in a terminal.
"""
import argparse
import json
import logging
import re
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Annotated, Literal, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .runner import STATUS, check_path, check_token, run_json_script, run_script, scripts_dir

READ_ONLY = dict(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
# mima_prepare_submission writes only its own plan file in run_dir and never submits.
WRITES_PLAN = dict(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

mcp = FastMCP(
    "mima_mcp",
    mask_error_details=True,
    instructions=(
        "Tools for planning, validating and checking MIMA shotgun-metagenomics runs on a PBS cluster. "
        "All tools are read-only except mima_prepare_submission, which writes a plan file and does a dry run. "
        "Nothing here submits a job: a person runs the command that mima_prepare_submission returns. "
        "Every result has a 'status'; report it as returned and do not guess when it is not ok/valid/complete."
    ),
)

JOB_ID = re.compile(r"^[0-9]+(\.[A-Za-z0-9_-]+)?(\[\])?$")
STEP_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

AbsPath = Annotated[str, Field(description="Absolute path on the machine running this server, e.g. /scratch/mp96/user/mima_tutorial/manifest.csv", min_length=2, max_length=1024)]


def _tool(name, title, annotations=READ_ONLY):
    return mcp.tool(name=name, annotations=ToolAnnotations(title=title, **annotations))


def _paths(values, name):
    out = [check_path(v, name) for v in values]
    if not out:
        raise ToolError(f"{name}: give at least one path")
    return out


@_tool("mima_list_modules", "List MIMA modules")
async def mima_list_modules() -> dict:
    """List the MIMA modules mima_build_commands can generate, with one line each.

    Use when: deciding which module a request needs. Don't use when: you already know the module."""
    return await run_script("build_command", ["--list-modules"])


@_tool("mima_build_commands", "Validate a plan and build MIMA commands")
async def mima_build_commands(
    plan: Annotated[dict, Field(description='One step {"module": "mima-qc", "params": {"-i": "/abs/dir", ...}} or {"steps": [step, ...]}. Use mima_list_modules for module names. Parameter keys are the flags (for example "--read-length").')],
    check_fs: Annotated[bool, Field(description="Also check that input paths exist and QC output folders are clean. Only meaningful when this server runs on the machine that holds the data.")] = False,
) -> dict:
    """Validate every value in a plan against the module specs and return ready-to-run commands.

    Returns status ok (commands in data/output), invalid (read the ERROR lines and fix the plan) or bad_plan.
    Warnings (for example an unapproved database or an unset --threshold) must be reported to the user.
    Use when: turning a request into commands. Don't use when: submitting jobs (use mima_prepare_submission)."""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "plan.json"
        f.write_text(json.dumps(plan))
        return await run_json_script("build_command", [str(f)] + (["--check-fs"] if check_fs else []))


@_tool("mima_validate_manifest", "Validate manifest.csv")
async def mima_validate_manifest(
    manifest_path: AbsPath,
    reads_dir: Annotated[Optional[str], Field(description="Folder the FASTQ names in the manifest are relative to (the QC -i folder). Required for check_fs.")] = None,
    check_fs: Annotated[bool, Field(description="Confirm the FASTQ files exist in reads_dir")] = False,
) -> dict:
    """Check manifest.csv: exact header Sample_ID,FileID_R1,FileID_R2, line endings (CRLF is an error), duplicates, empty fields.

    Returns status valid, invalid (see output for each error and its fix) or unreadable."""
    args = [check_path(manifest_path, "manifest_path")]
    if reads_dir:
        args += ["--reads-dir", check_path(reads_dir, "reads_dir")]
    if check_fs:
        args.append("--check-fs")
    return await run_script("validate_manifest", args)


@_tool("mima_validate_metadata", "Validate study metadata")
async def mima_validate_metadata(
    metadata_path: AbsPath,
    group_column: Annotated[str, Field(description="Metadata column holding the groups. Case-sensitive, e.g. 'lab'.", min_length=1, max_length=100)],
    groups: Annotated[Optional[list[str]], Field(description="Group values expected in that column, e.g. ['labA','labB']. Must match the data exactly.")] = None,
    feature_tables: Annotated[Optional[list[str]], Field(description="Feature tables whose sample columns must match the metadata IDs. Use the _counts or _relAbund table, not the base table.")] = None,
    id_column: Annotated[Optional[str], Field(description="Sample ID column (default: first column)")] = None,
    subject_column: Annotated[Optional[str], Field(description="Column identifying the person each sample came from; repeated values are out of scope for mima-visualisation")] = None,
    covariates: Annotated[Optional[list[str]], Field(description="Clinical or technical covariates to compare between groups")] = None,
) -> dict:
    """Check tab-separated metadata: layout, the group column, 2 or more group levels, expected group values and that sample IDs match the feature tables.

    Returns status valid (warnings, such as samples missing from a table, may still be listed), invalid or unreadable."""
    args = [check_path(metadata_path, "metadata_path"), "--group-column", check_token(group_column, "group_column")]
    if groups:
        args += ["--groups"] + [check_token(g, "groups") for g in groups]
    if feature_tables:
        args += ["--feature-table"] + [check_path(t, "feature_tables") for t in feature_tables]
    if id_column:
        args += ["--id-column", check_token(id_column, "id_column")]
    if subject_column:
        args += ["--subject-column", check_token(subject_column, "subject_column")]
    for c in covariates or []:
        args += ["--covariate", check_token(c, "covariates")]
    return await run_script("validate_metadata", args)


@_tool("mima_validate_pbs_config", "Validate a PBS header config")
async def mima_validate_pbs_config(
    config_path: AbsPath,
    module: Annotated[Optional[Literal["qc", "taxa", "func"]], Field(description="Which stage the config is for; 'taxa' also checks memory for Kraken2")] = None,
    samples: Annotated[Optional[int], Field(description="Number of samples; with module='taxa', checks walltime against the sizing guideline", ge=1, le=100000)] = None,
) -> dict:
    """Check a pbs_header_*.cfg file used with --pbs-config: shebang, #PBS lines, IMAGE_DIR, APPTAINER_BIND, placeholders, line endings.

    Returns status valid, invalid or unreadable."""
    args = [check_path(config_path, "config_path")]
    if module:
        args += ["--module", module]
    if samples is not None:
        args += ["--samples", str(samples)]
    return await run_script("validate_pbs_config", args)


@_tool("mima_validate_pbs_scripts", "Validate generated PBS scripts")
async def mima_validate_pbs_scripts(
    paths: Annotated[list[str], Field(description="Absolute paths of .pbs files or folders (searched recursively)", min_length=1, max_length=200)],
    check_fs: Annotated[bool, Field(description="Verify referenced paths exist (only meaningful on the cluster)")] = False,
) -> dict:
    """Pre-submission checks on generated .pbs scripts: placeholders, missing resources, CRLF, unsafe commands, IMAGE_DIR.

    Returns status valid, invalid (data lists each script's errors) or no_scripts. Run before mima_prepare_submission."""
    return await run_json_script("validate_pbs_scripts", _paths(paths, "paths") + (["--check-fs"] if check_fs else []))


@_tool("mima_estimate_read_length", "Estimate Bracken read length")
async def mima_estimate_read_length(
    paths: Annotated[list[str], Field(description="Absolute paths of the clean FASTQ folder(s) or files", min_length=1, max_length=200)],
    reads: Annotated[Optional[int], Field(description="Records to sample per file", ge=1, le=1000000)] = None,
    available: Annotated[Optional[str], Field(description="Read lengths the Bracken database has, comma-separated, e.g. '50,100,150'", pattern=r"^\d+(,\d+)*$")] = None,
) -> dict:
    """Estimate --read-length for mima-taxa-profiling from the first reads of each forward FASTQ, mapped to the nearest available length.

    Returns status ok (the estimate is in output) or no_fastq."""
    args = _paths(paths, "paths")
    if reads is not None:
        args += ["--reads", str(reads)]
    if available:
        args += ["--available", available]
    return await run_script("estimate_read_length", args)


@_tool("mima_check_outputs", "Check a stage's outputs")
async def mima_check_outputs(
    stage: Annotated[Literal["qc", "taxa", "taxa-tables", "func", "func-tables", "vis-barplot", "diversity", "classifier"], Field(description="The stage that finished")],
    directory: Annotated[str, Field(description="Absolute path of the stage's output folder, e.g. the QC_module folder for stage 'qc'", min_length=2, max_length=1024)],
    manifest: Annotated[Optional[str], Field(description="Absolute path of manifest.csv; otherwise expected samples come from the generated per-sample scripts")] = None,
) -> dict:
    """Check that a finished stage produced the files the tutorial lists, for every expected sample.

    Returns status complete (no errors; warnings may be listed) or errors (missing or empty files in data).
    data.summary gives the counts first (errors, warnings, missing or empty files, and the number missing per sample); data.findings
    lists every file. When reporting to the user, quote the summary and give the full list only if asked: long lists are easy to mis-copy.
    Use after a job reports complete, before starting the next stage."""
    args = ["--stage", stage, "--dir", check_path(directory, "directory")]
    if manifest:
        args += ["--manifest", check_path(manifest, "manifest")]
    return await run_json_script("check_outputs", args)


@_tool("mima_check_job_status", "Check PBS job status")
async def mima_check_job_status(
    job_ids: Annotated[Optional[list[str]], Field(description="PBS job ids such as '175269652.gadi-pbs'", max_length=200)] = None,
    record_path: Annotated[Optional[str], Field(description="Absolute path of the submission_record.json written at submission")] = None,
) -> dict:
    """Report PBS job state by calling qstat (read-only). Give exactly one of job_ids or record_path.

    Returns status complete, failed, in_progress (something is queued, held or running), unresolved (nothing is
    active but the server no longer knows a job: read the PBS log with mima_scan_logs and mima_check_outputs
    instead of waiting) or tool_error (for example qstat is not available on this machine)."""
    if bool(job_ids) == bool(record_path):
        raise ToolError("Give exactly one of job_ids or record_path")
    if record_path:
        args = ["--record", check_path(record_path, "record_path")]
    else:
        for j in job_ids:
            if not isinstance(j, str) or not JOB_ID.match(j):
                raise ToolError(f"job_ids: {j!r} is not a PBS job id such as 175269652.gadi-pbs")
        args = ["--jobs"] + list(job_ids)
    return await run_json_script("check_job_status", args, timeout=60)


@_tool("mima_scan_logs", "Scan logs for known failures")
async def mima_scan_logs(
    paths: Annotated[list[str], Field(description="Absolute paths of log files or folders (PBS logs JOBNAME.o<id>, *.log, *.out, *.err)", min_length=1, max_length=200)],
    max_hits: Annotated[int, Field(description="Matching lines to show per failure type per file", ge=1, le=50)] = 3,
) -> dict:
    """Scan logs for known failure signatures. Each finding has the pattern id, severity, matching lines (file and line number), cause and fix.

    Findings marked verified were seen in real logs; unverified ones are general knowledge, so confirm in the log.
    Returns status errors_found, no_known_errors (this does not prove the run succeeded) or no_logs."""
    return await run_json_script("scan_logs", _paths(paths, "paths") + ["--max-hits", str(max_hits)])


@_tool("mima_check_report", "Check an analysis report")
async def mima_check_report(
    report_path: AbsPath,
    stages: Annotated[Optional[list[Literal["qc", "taxa", "func", "diversity", "classifier"]]], Field(description="Stages that were run; each needs a Results subsection and stages not run must have none")] = None,
    run_dir: Annotated[Optional[str], Field(description="Absolute path of the run folder, to confirm that files named in the report exist")] = None,
) -> dict:
    """Check a finished report against the report template: placeholders, nine numbered sections, a Methods command block, sources for numbers.

    Returns status ok (warnings allowed), errors or unreadable."""
    args = [check_path(report_path, "report_path")]
    if stages:
        args += ["--stages"] + list(stages)
    if run_dir:
        args += ["--run-dir", check_path(run_dir, "run_dir")]
    return await run_json_script("check_report", args)


@_tool("mima_prepare_submission", "Prepare a job submission (does not submit)", WRITES_PLAN)
async def mima_prepare_submission(
    jobs: Annotated[list[dict], Field(description='[{"id": "qc0", "script": "/abs/qcModule_0.pbs", "after": ["other_id"], "after_job_ids": ["1234.server"]}]; id is a short unique name, after lists earlier ids in this plan', min_length=1, max_length=200)],
    run_dir: Annotated[str, Field(description="Existing absolute folder where submission_plan.json is written and where the record will go", min_length=2, max_length=1024)],
    check_fs: Annotated[bool, Field(description="Verify that paths in the scripts exist (use on the cluster)")] = True,
    project: Annotated[Optional[str], Field(description="qsub -P project code, if the site needs it")] = None,
    queue: Annotated[Optional[str], Field(description="qsub -q queue name")] = None,
) -> dict:
    """Validate scripts and prepare a submission. THIS DOES NOT SUBMIT ANYTHING.

    Writes submission_plan.json in run_dir, runs submit_jobs.py as a dry run and returns the exact command (run_yourself),
    with its confirmation token, that a person must run in their own terminal and confirm by typing. Show the user the
    qsub commands and that command. Returns status dry_run_ok, invalid (scripts failed validation) or bad_plan."""
    run = Path(check_path(run_dir, "run_dir"))
    if not run.is_dir():
        raise ToolError(f"run_dir does not exist: {run_dir}. Create it first.")
    clean, seen = [], set()
    for j in jobs:
        extra = set(j) - {"id", "script", "after", "after_job_ids", "cwd"}
        if extra:
            raise ToolError(f"unknown job field(s): {', '.join(sorted(extra))}")
        jid = j.get("id", "")
        if not isinstance(jid, str) or not STEP_ID.match(jid) or jid in seen:
            raise ToolError(f"job id {jid!r} is missing, invalid or repeated; use letters, digits, - and _ (max 40), unique per job")
        seen.add(jid)
        item = {"id": jid, "script": check_path(j.get("script"), "script")}
        if j.get("after"):
            item["after"] = [check_token(a, "after") for a in j["after"]]
        if j.get("after_job_ids"):
            ids = j["after_job_ids"]
            if not all(isinstance(i, str) and JOB_ID.match(i) for i in ids):
                raise ToolError("after_job_ids: expected PBS job ids such as 1234.server")
            item["after_job_ids"] = list(ids)
        if j.get("cwd"):
            item["cwd"] = check_path(j["cwd"], "cwd")
        clean.append(item)
    plan, record = run / "submission_plan.json", run / "submission_record.json"
    plan.write_text(json.dumps({"jobs": clean}, indent=2) + "\n")
    extra_args = []
    for flag, val in (("--project", project), ("--queue", queue)):
        if val:
            extra_args += [flag, check_token(val, flag)]
    dry = await run_script("submit_jobs", [str(plan), "--record", str(record)] + extra_args + ([] if check_fs else ["--no-fs-check"]))
    m = re.search(r"Confirmation token: ([0-9a-f]+)", dry["output"])
    dry["plan_file"] = str(plan)
    dry["submitted"] = False
    if dry["status"] == "dry_run_ok" and m:
        cmd = ["python3", str(scripts_dir() / "submit_jobs.py"), str(plan), "--execute", "--confirm", m.group(1),
               "--record", str(record)] + extra_args
        dry["token"] = m.group(1)
        dry["run_yourself"] = " ".join(shlex.quote(c) for c in cmd)
        dry["note"] = ("Nothing was submitted. Review the qsub commands above, then run `run_yourself` in your own terminal "
                       "and type the confirmation when asked. It refuses to run without an interactive terminal.")
    return dry


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m mima_mcp", description="MCP server (stdio) for the agentic-mima skill. "
                                 "Set MIMA_SKILL_DIR to the installed skill folder; optionally MIMA_ALLOWED_ROOTS to confine paths.")
    ap.add_argument("--check", action="store_true", help="check MIMA_SKILL_DIR and the bundled scripts, then exit")
    args = ap.parse_args(argv)
    try:
        folder = scripts_dir()
    except ToolError as exc:
        print(f"mima_mcp: {exc}", file=sys.stderr)
        sys.exit(2)
    missing = [f"{s}.py" for s in sorted(STATUS) if not (folder / f"{s}.py").is_file()]
    if missing:
        print(f"mima_mcp: the skill at {folder.parent} is incomplete; missing {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)
    if args.check:
        print(f"OK: {len(STATUS)} scripts found in {folder}")
        return
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(message)s")
    mcp.run()


if __name__ == "__main__":
    main()
