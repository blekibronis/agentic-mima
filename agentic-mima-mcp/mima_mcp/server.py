"""FastMCP server exposing the agentic-mima skill's validated scripts as tools.

The server holds no pipeline logic. Each tool checks its arguments, runs one bundled script
through runner.run_script and returns the script's own result. The skill (SKILL.md) says when to
call which tool; the scripts decide what is valid.

Every result is a dict: script, command, exit_code, status (a word from the script's own exit
codes), output (the script's text), data (parsed JSON, when the script offers it), stderr, truncated.

mima_prepare_submission runs submit_jobs.py as a dry run. mima_submit_jobs can then submit that plan, but only when the
server was started with MIMA_ALLOW_SUBMIT=1 and the person picks "submit" in the client's own approval prompt (MCP
elicitation). A client that cannot ask, a decline or a dismissed prompt all mean nothing is submitted.
"""
import argparse
import json
import logging
import os
import re
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Annotated, List, Literal, Optional

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .runner import STATUS, check_path, check_token, run_approved_submission, run_built_command, run_json_script, run_script, scripts_dir

log = logging.getLogger("mima_mcp")  # stderr only: stdout belongs to the MCP protocol

READ_ONLY = dict(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
# mima_prepare_submission writes only its own plan file in run_dir and never submits.
WRITES_PLAN = dict(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
# mima_write_prepared_inputs creates a NEW folder of corrected copies; it never changes or deletes the user's files.
WRITES_COPIES = dict(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
# mima_submit_jobs queues jobs, but only after the person approves through the client's prompt; it deletes nothing.
SUBMITS = dict(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
# mima_run_step runs one validated MIMA command in the container, only after the person approves; the pipeline itself writes into the user's output folders.
RUNS = dict(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)

mcp = FastMCP(
    "mima_mcp",
    mask_error_details=True,
    instructions=(
        "Tools for planning, validating and checking MIMA shotgun-metagenomics runs on a PBS cluster. "
        "All tools are read-only except mima_prepare_submission (writes a plan file and does a dry run) and "
        "mima_write_prepared_inputs (writes corrected COPIES of the user's manifest and metadata into a new folder; "
        "the originals are never changed) and mima_write_pbs_header (writes a NEW PBS header file; never overwrites). "
        "mima_submit_jobs submits a prepared plan only when the person picks 'submit' in an approval prompt that the client shows them; "
        "call it only after they have reviewed the plan and asked you to submit. Never assume or infer approval. "
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


def _prepare_args(manifest_path, from_reads_dir, reads_dir, metadata_path, out_dir, apply):
    if bool(manifest_path) == bool(from_reads_dir):
        raise ToolError("give exactly one of manifest_path (the user's manifest or sample sheet) or from_reads_dir (a folder of FASTQ files)")
    args = []
    if manifest_path:
        args += ["--manifest", check_path(manifest_path, "manifest_path")]
    if from_reads_dir:
        args += ["--from-reads", check_path(from_reads_dir, "from_reads_dir")]
    if reads_dir:
        args += ["--reads-dir", check_path(reads_dir, "reads_dir")]
    if metadata_path:
        args += ["--metadata", check_path(metadata_path, "metadata_path")]
    args += ["--out-dir", check_path(out_dir, "out_dir")]
    return args + (["--apply"] if apply else [])


_MANIFEST = Annotated[Optional[str], Field(description="The user's manifest or sample sheet, in any common layout (CSV, TSV, semicolons; any column names). Give this OR from_reads_dir.", max_length=1024)]
_FROM_READS = Annotated[Optional[str], Field(description="A folder of FASTQ files to build the manifest from when the user has no sample sheet. Give this OR manifest_path.", max_length=1024)]
_READS_DIR = Annotated[Optional[str], Field(description="Folder holding the FASTQ files (the QC -i folder). File existence is checked when given.", max_length=1024)]
_METADATA = Annotated[Optional[str], Field(description="The user's study metadata (any common layout)", max_length=1024)]
_OUT_DIR = Annotated[str, Field(description="A NEW folder for the corrected copies (it must not exist or must be empty), e.g. inside the run folder", min_length=2, max_length=1024)]


@_tool("mima_prepare_inputs", "Plan the clean-up of the user's manifest and metadata (writes nothing)")
async def mima_prepare_inputs(
    out_dir: _OUT_DIR,
    manifest_path: _MANIFEST = None,
    from_reads_dir: _FROM_READS = None,
    reads_dir: _READS_DIR = None,
    metadata_path: _METADATA = None,
) -> dict:
    """Work out what must change so the user's manifest and metadata follow the MIMA conventions. WRITES NOTHING.

    Call this first with the files as the user gave them (users are not expected to know the conventions). It fixes only mechanical
    problems: line endings, byte-order marks, delimiters, header names, spaces, full paths, and sample IDs that use characters other
    than letters, digits and underscores (an original-to-pipeline ID map is kept). It never swaps R1 and R2, never joins lanes, never
    renames the user's FASTQ files.
    Returns status: prepared (data.changes lists each change; show them to the user in plain words and ask before writing),
    unchanged (the files already follow the conventions: use them as they are), needs_input (data.needs_input lists questions to put
    to the user; nothing can be written until they are answered; do not guess) or unreadable. data.use names the files to use afterwards."""
    return await run_json_script("prepare_inputs", _prepare_args(manifest_path, from_reads_dir, reads_dir, metadata_path, out_dir, False))


@_tool("mima_write_prepared_inputs", "Write corrected copies of the user's manifest and metadata", WRITES_COPIES)
async def mima_write_prepared_inputs(
    out_dir: _OUT_DIR,
    manifest_path: _MANIFEST = None,
    from_reads_dir: _FROM_READS = None,
    reads_dir: _READS_DIR = None,
    metadata_path: _METADATA = None,
) -> dict:
    """Write the corrected COPIES that mima_prepare_inputs planned. Call only after the user has seen the changes and said yes,
    with exactly the same arguments as the preview.

    Creates out_dir (which must not exist or must be empty) holding manifest.csv, metadata.tsv (when metadata was given) and
    id_map.tsv. The user's original files are never changed or deleted and nothing is overwritten. Writes nothing and returns
    needs_input when any question is open. Returns status prepared (data.written lists the files; use data.use afterwards),
    unchanged, needs_input or unreadable. Then run the validators on the files you will use."""
    return await run_json_script("prepare_inputs", _prepare_args(manifest_path, from_reads_dir, reads_dir, metadata_path, out_dir, True))


def _header_args(a):
    args = ["--module", check_token(a["module"], "module")]
    if a.get("samples"):
        args += ["--samples", str(int(a["samples"]))]
    if a.get("manifest_path"):
        args += ["--manifest", check_path(a["manifest_path"], "manifest_path")]
    if a.get("image_path"):
        args += ["--image", check_path(a["image_path"], "image_path")]
    for p in a.get("paths") or []:
        args += ["--path", check_path(p, "paths")]
    if a.get("project"):
        args += ["--project", check_token(a["project"], "project")]
    if a.get("queue"):
        args += ["--queue", check_token(a["queue"], "queue")]
    if a.get("cpus"):
        args += ["--cpus", str(int(a["cpus"]))]
    if a.get("reads_dir"):
        args += ["--reads-dir", check_path(a["reads_dir"], "reads_dir")]
    if a.get("num_pbs_jobs"):
        args += ["--num-pbs-jobs", str(int(a["num_pbs_jobs"]))]
    if a.get("out_file"):
        args += ["--out", check_path(a["out_file"], "out_file")]
    return args


_HDR_MODULE = Annotated[str, Field(description="Module to size: qc, taxa or func (a value other than these is rejected)", max_length=20)]
_HDR_SAMPLES = Annotated[Optional[int], Field(description="Number of samples. Give this OR manifest_path (use the prepared manifest).", ge=1, le=5000)]
_HDR_MANIFEST = Annotated[Optional[str], Field(description="The prepared manifest.csv, used only to count the samples", max_length=1024)]
_HDR_IMAGE = Annotated[Optional[str], Field(description="Absolute path of the MIMA container image (.sif). Ask the user; never guess it.", max_length=1024)]
_HDR_PATHS = Annotated[Optional[List[str]], Field(description="Every absolute folder the job reads or writes: clean reads, output folder, reference database. Storage and bind paths are derived from these.", max_length=20)]
_HDR_PROJECT = Annotated[Optional[str], Field(description="The user's project code, such as ab01. Ask the user unless $PROJECT is set on the server.", max_length=32)]
_HDR_QUEUE = Annotated[Optional[str], Field(description="Queue name, only if the user knows it", max_length=40)]
_HDR_CPUS = Annotated[Optional[int], Field(description="CPU count; default 28, the guideline is 16 to 28", ge=1, le=256)]
_HDR_READS = Annotated[Optional[str], Field(description="Folder holding the FASTQ files named in the manifest (or the CleanReads folder). Used to scale qc and func walltime by file size.", max_length=1024)]
_HDR_JOBS = Annotated[Optional[int], Field(description="qc only: the num_pbs_jobs that will be given to mima-qc (its default is 4)", ge=1, le=500)]
_HDR_OUT = Annotated[Optional[str], Field(description="A NEW header file to write, e.g. in the run folder; an existing file is never overwritten", max_length=1024)]


@_tool("mima_plan_pbs_header", "Draft the PBS header file for a PBS module: qc, taxa or func (writes nothing)")
async def mima_plan_pbs_header(
    module: _HDR_MODULE,
    samples: _HDR_SAMPLES = None,
    manifest_path: _HDR_MANIFEST = None,
    image_path: _HDR_IMAGE = None,
    paths: _HDR_PATHS = None,
    project: _HDR_PROJECT = None,
    queue: _HDR_QUEUE = None,
    cpus: _HDR_CPUS = None,
    reads_dir: _HDR_READS = None,
    num_pbs_jobs: _HDR_JOBS = None,
) -> dict:
    """Draft the PBS header (memory, CPUs, walltime, storage line, bind paths) so the user does not have to write one. WRITES NOTHING.

    Taxonomy is sized from the number of samples with the lab's guideline. QC and function profiling use the tutorial's figures
    scaled by the largest sample's file size (give manifest_path and reads_dir). All numbers are ESTIMATES, not measured values:
    say so, and show the user data.lines (each line with its source) and data.warnings. The project code, image path and folders
    must come from the user; if any is missing the status is needs_input and data.needs_input lists the questions: ask them, never guess.
    Returns prepared (the draft passed validate_pbs_config; show it and
    ask before writing), needs_input, or invalid (a bug: report it, do not use the draft)."""
    return await run_json_script("make_pbs_header", _header_args(dict(
        module=module, samples=samples, manifest_path=manifest_path, image_path=image_path, paths=paths, project=project, queue=queue, cpus=cpus,
        reads_dir=reads_dir, num_pbs_jobs=num_pbs_jobs)))


@_tool("mima_write_pbs_header", "Write the drafted PBS header to a new file", WRITES_COPIES)
async def mima_write_pbs_header(
    module: _HDR_MODULE,
    out_file: Annotated[str, Field(description="A NEW header file to write, e.g. in the run folder; an existing file is never overwritten", min_length=2, max_length=1024)],
    samples: _HDR_SAMPLES = None,
    manifest_path: _HDR_MANIFEST = None,
    image_path: _HDR_IMAGE = None,
    paths: _HDR_PATHS = None,
    project: _HDR_PROJECT = None,
    queue: _HDR_QUEUE = None,
    cpus: _HDR_CPUS = None,
    reads_dir: _HDR_READS = None,
    num_pbs_jobs: _HDR_JOBS = None,
) -> dict:
    """Write the header that mima_plan_pbs_header drafted. Call only after the user has seen the draft and said yes, with exactly the
    same arguments plus out_file.

    Creates out_file (it must not exist; nothing is overwritten) with Unix line endings. Writes nothing unless the draft passed
    validation. Returns prepared (data.written is the file; pass it as --pbs-config), needs_input or invalid. Then run
    mima_validate_pbs_config on it."""
    return await run_json_script("make_pbs_header", _header_args(dict(
        module=module, samples=samples, manifest_path=manifest_path, image_path=image_path, paths=paths, project=project, queue=queue,
        cpus=cpus, reads_dir=reads_dir, num_pbs_jobs=num_pbs_jobs, out_file=out_file)) + ["--apply"])


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


def _submit_enabled():
    return os.environ.get("MIMA_ALLOW_SUBMIT") == "1"


def _max_jobs():
    try:
        return max(1, int(os.environ.get("MIMA_MAX_SUBMIT_JOBS", "50")))
    except ValueError:
        return 50


def _pbs_summary(script):
    """The #PBS resource lines of one script, for the approval prompt."""
    try:
        lines = Path(script).read_text(errors="replace").splitlines()[:80]
    except OSError:
        return "(script unreadable)"
    keep = [l.strip()[5:].strip() for l in lines if re.match(r"#PBS\s+-(N|l|P|q)\b", l.strip())]
    return "; ".join(keep) or "(no #PBS resource lines)"


async def _ask_person(ctx, message, yes_choice, no_choice):
    """Show the person a two-choice prompt through the client. Returns approved, declined, cancelled or unavailable.

    Only the exact yes_choice counts as approval. A client that cannot ask (or speaks only the newest protocol era,
    which has no prompt) gives unavailable, and every caller treats that as "do nothing"."""
    try:
        answer = await ctx.elicit(message, response_type=[yes_choice, no_choice])
    except Exception as exc:  # the client cannot ask: fail closed
        log.warning("approval prompt failed: %s", type(exc).__name__)
        return "unavailable"
    if answer.action == "cancel":
        return "cancelled"
    if answer.action == "accept" and getattr(answer, "data", None) == yes_choice:
        return "approved"
    return "declined"


@_tool("mima_submit_jobs", "Submit prepared jobs after the person approves", SUBMITS)
async def mima_submit_jobs(
    run_dir: Annotated[str, Field(description="The same run_dir given to mima_prepare_submission", min_length=2, max_length=1024)],
    token: Annotated[str, Field(description="The confirmation token returned by mima_prepare_submission for this exact plan", min_length=4, max_length=200)],
    ctx: Context,
    check_fs: Annotated[bool, Field(description="Same value as given to mima_prepare_submission")] = True,
    project: Annotated[Optional[str], Field(description="Same value as given to mima_prepare_submission, if any")] = None,
    queue: Annotated[Optional[str], Field(description="Same value as given to mima_prepare_submission, if any")] = None,
) -> dict:
    """Ask the person to approve, then submit the plan prepared by mima_prepare_submission.

    Call this ONLY when the person has reviewed the prepared plan and has asked you to submit it. Calling it does not submit:
    the client shows the person the jobs and a clear submit / do-not-submit choice, and nothing is submitted unless they
    pick submit. Never describe a job as submitted unless status is submitted. Statuses: submitted, partly_submitted,
    not_submitted, declined, cancelled, stale (the plan or scripts changed since the token was issued: prepare again),
    invalid, disabled (this server was not started with submission enabled), approval_unavailable (this client cannot ask
    the person; they must use run_yourself from mima_prepare_submission) or too_many_jobs."""
    out = {"script": "submit_jobs", "submitted": False}
    if not _submit_enabled():
        out.update(status="disabled", note="Submission is not enabled on this server (MIMA_ALLOW_SUBMIT=1 is not set). "
                   "Give the person the run_yourself command from mima_prepare_submission instead.")
        return out
    run = Path(check_path(run_dir, "run_dir"))
    token = check_token(token, "token")
    plan, record = run / "submission_plan.json", run / "submission_record.json"
    if not plan.is_file():
        raise ToolError(f"No submission_plan.json in {run_dir}. Run mima_prepare_submission first.")
    extra_args = []
    for flag, val in (("--project", project), ("--queue", queue)):
        if val:
            extra_args += [flag, check_token(val, flag)]
    dry = await run_script("submit_jobs", [str(plan), "--record", str(record)] + extra_args + ([] if check_fs else ["--no-fs-check"]))
    m = re.search(r"Confirmation token: ([0-9a-f]+)", dry["output"])
    if dry["status"] != "dry_run_ok" or not m:
        out.update(status="invalid", note="The plan no longer passes the dry run. Nothing was submitted.", output=dry["output"])
        return out
    if m.group(1) != token:
        out.update(status="stale", note="The plan or the scripts changed since the token was issued. Nothing was submitted. "
                   "Run mima_prepare_submission again and have the person review it.")
        return out
    jobs = json.loads(plan.read_text()).get("jobs", [])
    if len(jobs) > _max_jobs():
        out.update(status="too_many_jobs", note=f"{len(jobs)} jobs is above this server's limit of {_max_jobs()}. Nothing was submitted.")
        return out
    submit_choice = f"Submit {len(jobs)} job(s) now"
    cancel_choice = "Do NOT submit"
    where = " ".join(a for a in (f"project {project}" if project else "", f"queue {queue}" if queue else "") if a)
    message = (f"APPROVAL NEEDED. Submit {len(jobs)} job(s) to the cluster queue? {where}".rstrip() + "\n"
               + "\n".join(f"  {j['id']}: {j['script']}\n      {_pbs_summary(j['script'])}" for j in jobs)
               + "\nChoose '" + submit_choice + "' only if you have reviewed these jobs.")
    verdict = await _ask_person(ctx, message, submit_choice, cancel_choice)
    if verdict == "unavailable":
        out.update(status="approval_unavailable", note="This client cannot ask the person for approval, so nothing was submitted. "
                   "Give the person the run_yourself command from mima_prepare_submission.")
        return out
    if verdict == "cancelled":
        out.update(status="cancelled", note="The approval prompt was dismissed. Nothing was submitted.")
        return out
    if verdict != "approved":
        out.update(status="declined", note="The person did not approve. Nothing was submitted.")
        return out
    res = await run_approved_submission(plan, record, token, project, queue, check_fs)
    res["submitted"] = res["status"] in ("submitted", "partly_submitted")
    res["approved_by"] = "person, through the client approval prompt"
    return res


def _run_enabled():
    return os.environ.get("MIMA_ALLOW_RUN") == "1"


@_tool("mima_run_step", "Run one MIMA step after the person approves", RUNS)
async def mima_run_step(
    step: Annotated[dict, Field(description='ONE step exactly as for mima_build_commands: {"module": "mima-qc", "params": {"-i": "/abs/dir", ...}}. Several steps are not accepted; run them one at a time.')],
    image_path: Annotated[str, Field(description="Absolute path of the MIMA container image (.sif), the same one used in the PBS header", min_length=2, max_length=1024)],
    bind_paths: Annotated[list[str], Field(description="Every folder the step reads or writes, as absolute paths (the same folders given to mima_plan_pbs_header)", min_length=1, max_length=40)],
    ctx: Context,
    check_fs: Annotated[bool, Field(description="Check that inputs exist and QC output folders are clean before asking (use on the cluster)")] = True,
    timeout_minutes: Annotated[int, Field(description="Stop the step after this many minutes", ge=1, le=120)] = 30,
) -> dict:
    """Ask the person to approve, then run one validated MIMA step inside the container on this machine.

    Use for the generator steps (qc, taxa, func: they only write PBS scripts) and for the command-line stages
    (qc-report, taxa-report, taxa-tables, vis-barplot, diversity, classifier). The command is built and checked by the
    same code as mima_build_commands; you cannot pass a command of your own. Calling this does not run anything by itself:
    the client shows the person the exact command and they must approve. Never say a step ran unless status is ran.
    Statuses: ran (exit 0), failed (the step exited with an error: show the end of output), timeout, invalid (the plan failed
    its checks: fix it), declined, cancelled, disabled (not switched on for this server), approval_unavailable (this client cannot
    ask the person), tool_error (for example apptainer is not on PATH). After a generator step, validate the new scripts with
    mima_validate_pbs_scripts. These steps run on the login node, so keep them short."""
    out = {"script": "build_command", "ran": False}
    if not _run_enabled():
        out.update(status="disabled", note="Running steps is not enabled on this server (MIMA_ALLOW_RUN=1 is not set). "
                   "Give the person the command from mima_build_commands to run themselves.")
        return out
    if not isinstance(step, dict) or not isinstance(step.get("module"), str):
        raise ToolError("step: expected one step such as {\"module\": \"mima-qc\", \"params\": {...}}")
    image = Path(check_path(image_path, "image_path"))
    if image.suffix != ".sif" or not image.is_file():
        raise ToolError(f"image_path must be an existing .sif file: {image_path}")
    binds = [check_path(b, "bind_paths") for b in bind_paths]
    for b in binds:
        if not Path(b).is_dir() or "," in b or ":" in b:
            raise ToolError(f"bind_paths: {b} must be an existing folder without ',' or ':' in its name")
    if shutil.which("apptainer") is None:
        out.update(status="tool_error", note="apptainer is not on PATH for this server. On Gadi run 'module load apptainer' before starting it.")
        return out
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "plan.json"
        f.write_text(json.dumps({"steps": [step]}))
        built = await run_json_script("build_command", [str(f)] + (["--check-fs"] if check_fs else []))
    steps = (built.get("data") or {}).get("steps") or []
    if built["status"] != "ok" or len(steps) != 1 or not steps[0].get("command"):
        built.update(ran=False, status="invalid" if built["status"] in ("invalid", "ok") else built["status"])
        return built
    command = steps[0]["command"]
    shown = command.replace("\n", "\n  ")
    message = (f"APPROVAL NEEDED. Run this MIMA step now ({steps[0].get('stage')}, on this machine, up to {timeout_minutes} minutes)?\n"
               f"  image: {image}\n  folders: {', '.join(binds)}\n  command:\n  {shown}\n"
               "Choose 'Run this step now' only if you have reviewed the command.")
    verdict = await _ask_person(ctx, message, "Run this step now", "Do NOT run")
    notes = {"unavailable": ("approval_unavailable", "This client cannot ask the person for approval, so nothing was run. Give the person the command from mima_build_commands to run themselves."),
             "cancelled": ("cancelled", "The approval prompt was dismissed. Nothing was run."),
             "declined": ("declined", "The person did not approve. Nothing was run.")}
    if verdict != "approved":
        out.update(status=notes[verdict][0], note=notes[verdict][1])
        return out
    res = await run_built_command(command, str(image), binds, timeout_minutes * 60)
    res["ran"] = res["status"] in ("ran", "failed")
    res["stage"] = steps[0].get("stage")
    res["warnings"] = steps[0].get("warnings", [])
    res["approved_by"] = "person, through the client approval prompt"
    return res


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
    mcp.run(show_banner=False)  # no banner; stdout is the protocol channel


if __name__ == "__main__":
    main()
