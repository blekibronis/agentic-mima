"""Run the skill's bundled scripts and return their results as data.

This module is the only place that starts a process. Rules it enforces:
  * only scripts on the allow-list below are run, from the skill's scripts folder;
  * the command is a fixed argument list, never a shell string;
  * path and token arguments are checked before they reach a script;
  * run_script runs submit_jobs.py only as a dry run: --execute, --confirm, --approval, --allow-resubmit and --qsub
    are refused there. The one way to submit is run_approved_submission, and the one way to run a MIMA command is
    run_built_command; the server calls each only after a person approves through the client's own prompt.
"""
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

from fastmcp.exceptions import ToolError

# script -> {exit code: status word}. Exit codes come from each script's own docstring.
STATUS = {
    "build_command": {0: "ok", 1: "invalid", 2: "bad_plan"},
    "validate_manifest": {0: "valid", 1: "invalid", 2: "unreadable"},
    "validate_metadata": {0: "valid", 1: "invalid", 2: "unreadable"},
    "validate_pbs_config": {0: "valid", 1: "invalid", 2: "unreadable"},
    "validate_pbs_scripts": {0: "valid", 1: "invalid", 2: "no_scripts"},
    "estimate_read_length": {0: "ok", 2: "no_fastq"},
    "check_outputs": {0: "complete", 1: "errors", 2: "usage_problem"},
    "check_job_status": {0: "complete", 1: "failed", 2: "tool_error", 3: "in_progress", 4: "unresolved"},
    "scan_logs": {0: "no_known_errors", 1: "errors_found", 2: "no_logs"},
    "check_report": {0: "ok", 1: "errors", 2: "unreadable"},
    "submit_jobs": {0: "dry_run_ok", 1: "invalid", 2: "bad_plan"},
    "prepare_inputs": {0: "prepared", 1: "needs_input", 2: "unreadable"},
    "make_pbs_header": {0: "prepared", 1: "needs_input", 2: "unreadable"},
    "run_state": {0: "ok", 2: "not_found"},          # resume and list; the JSON carries the real status word
    "stage_report": {0: "written", 1: "failed", 2: "bad_args"},   # the server only calls its write-job command
}
# Scripts whose JSON says more than their exit code can: the script's own status word is used (prepared, unchanged or invalid).
STATUS_FROM_DATA = {"prepare_inputs", "make_pbs_header", "run_state"}
REFUSED_FLAGS = {"submit_jobs": {"--execute", "--confirm", "--approval", "--allow-resubmit", "--qsub"}}
MAX_OUTPUT = 60_000  # characters; keeps one tool result from flooding the model's context
log = logging.getLogger("mima_mcp")  # stderr only: stdout belongs to the MCP protocol
TOKEN_OK = re.compile(r"^[^\x00-\x1f-][^\x00-\x1f]{0,199}$")


def scripts_dir():
    base = os.environ.get("MIMA_SKILL_DIR")
    if not base:
        raise ToolError("MIMA_SKILL_DIR is not set. Set it to the installed agentic-mima skill folder.")
    d = Path(base) / "scripts"
    if not d.is_dir():
        raise ToolError(f"No scripts folder at {d}")
    return d


def check_path(value, name="path"):
    """Absolute, no NUL, no leading dash, and inside MIMA_ALLOWED_ROOTS when that is set."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ToolError(f"{name}: expected a non-empty path")
    if value.startswith("-"):
        raise ToolError(f"{name}: a path may not start with '-'")
    if not os.path.isabs(value):
        raise ToolError(f"{name}: must be an absolute path, got {value!r}")
    roots = [r for r in os.environ.get("MIMA_ALLOWED_ROOTS", "").split(os.pathsep) if r]
    if roots:
        real = os.path.realpath(value)
        if not any(real == os.path.realpath(r) or real.startswith(os.path.realpath(r).rstrip("/") + "/") for r in roots):
            raise ToolError(f"{name}: {value} is outside the allowed folders")
    return value


def check_token(value, name="value"):
    if not isinstance(value, str) or not TOKEN_OK.match(value):
        raise ToolError(f"{name}: invalid value {value!r}")
    return value


async def run_script(script, args, timeout=120):
    """Run one allow-listed script without blocking the server, and return its result as a dict."""
    if script not in STATUS:
        raise ToolError(f"{script} is not an allowed script")
    bad = REFUSED_FLAGS.get(script, set()) & set(args)
    if bad:
        raise ToolError(f"{script}: {', '.join(sorted(bad))} is not available through this server")
    path = scripts_dir() / (script + ".py")
    if not path.is_file():
        raise ToolError(f"Script not found: {path}")
    argv = [sys.executable, "-I", str(path)] + [str(a) for a in args]
    result = {"script": script, "command": argv[2:], "truncated": False}
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as exc:
        result.update(exit_code=None, status="tool_error", output="", stderr=str(exc))
        return result
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("%s timed out after %ss", script, timeout)
        result.update(exit_code=None, status="timeout", output="", stderr=f"timed out after {timeout}s; narrow the request or run it in a terminal")
        return result
    out = out_b.decode("utf-8", "replace")
    if len(out) > MAX_OUTPUT:
        out, result["truncated"] = out[:MAX_OUTPUT] + "\n[output truncated]", True
    result.update(exit_code=proc.returncode, status=STATUS[script].get(proc.returncode, "unexpected_exit"),
                  output=out, stderr=err_b.decode("utf-8", "replace")[-4000:])
    log.info("%s exit=%s status=%s", script, proc.returncode, result["status"])
    return result


async def run_approved_submission(plan, record, token, project=None, queue=None, check_fs=True, timeout=300):
    """Submit a prepared plan. Call this ONLY after a person has approved it through the client's own prompt.

    The argument list is fixed here: the caller supplies a plan file, record file, the token from the dry run and
    optionally a project and queue. --allow-resubmit and --qsub can never be passed, so a script already recorded
    as submitted is refused by submit_jobs.py itself."""
    path = scripts_dir() / "submit_jobs.py"
    if not path.is_file():
        raise ToolError(f"Script not found: {path}")
    argv = [sys.executable, "-I", str(path), str(plan), "--record", str(record), "--execute",
            "--confirm", check_token(token, "token"), "--approval", "harness"]
    for flag, val in (("--project", project), ("--queue", queue)):
        if val:
            argv += [flag, check_token(val, flag)]
    if not check_fs:
        argv.append("--no-fs-check")
    result = {"script": "submit_jobs", "command": argv[2:], "truncated": False}
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:  # before OSError: on Python 3.11+ TimeoutError is a kind of OSError
        proc.kill()
        await proc.wait()
        result.update(exit_code=None, status="timeout", output="", stderr=f"timed out after {timeout}s; check the record file before retrying")
        return result
    except OSError as exc:
        result.update(exit_code=None, status="tool_error", output="", stderr=str(exc))
        return result
    out = out_b.decode("utf-8", "replace")[:MAX_OUTPUT]
    ids = re.findall(r"^\[([A-Za-z0-9_-]+)\] submitted as (\S+)$", out, re.M)
    status = "submitted" if proc.returncode == 0 else ("partly_submitted" if ids else "not_submitted")
    result.update(exit_code=proc.returncode, status=status, output=out, stderr=err_b.decode("utf-8", "replace")[-4000:],
                  pbs_job_ids={i: j for i, j in ids})
    log.info("submit_jobs (approved) exit=%s status=%s jobs=%s", proc.returncode, status, len(ids))
    return result


async def run_built_command(command, image, binds, timeout=1800):
    """Run a command produced by build_command.py, exactly as a person pasting it into a shell would.

    Call this ONLY after a person has approved the command through the client's own prompt. The command text comes from
    build_command.py (every value type-checked and shell-quoted there), never from the model. SANDBOX and APPTAINER_BIND
    are set in the environment, as the MIMA tutorial does by hand."""
    env = dict(os.environ, SANDBOX=image, APPTAINER_BIND=",".join(binds))
    result = {"script": "build_command", "command": command, "truncated": False}
    try:
        proc = await asyncio.create_subprocess_exec("/bin/bash", "-c", command, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:  # before OSError: on Python 3.11+ TimeoutError is a kind of OSError
        proc.kill()
        await proc.wait()
        result.update(exit_code=None, status="timeout", output="", stderr=f"stopped after {timeout // 60} minutes; check what was written before running it again")
        return result
    except OSError as exc:
        result.update(exit_code=None, status="tool_error", output="", stderr=str(exc))
        return result
    out = out_b.decode("utf-8", "replace")
    if len(out) > MAX_OUTPUT:
        out, result["truncated"] = out[-MAX_OUTPUT:], True
    result.update(exit_code=proc.returncode, status="ran" if proc.returncode == 0 else "failed", output=out,
                  stderr=err_b.decode("utf-8", "replace")[-4000:])
    log.info("approved step exit=%s", proc.returncode)
    return result


async def run_json_script(script, args, timeout=120):
    """Like run_script, but parses stdout as JSON when the script was asked for --format json."""
    r = await run_script(script, list(args) + ["--format", "json"], timeout)
    if not r["truncated"] and r["output"].strip().startswith(("{", "[")):
        try:
            r["data"] = json.loads(r["output"])
            r["output"] = ""
            if script in STATUS_FROM_DATA and isinstance(r["data"], dict) and r["data"].get("status") and r["exit_code"] in (0, 1):
                r["status"] = r["data"]["status"]
        except ValueError:
            pass
    return r
