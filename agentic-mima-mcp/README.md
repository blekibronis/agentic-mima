# agentic-mima-mcp (draft skeleton, v0.1)

A thin MCP server (FastMCP) that gives a model the agentic-mima skill's validated scripts as tools.
The skill (SKILL.md) says *when* to call each tool. The scripts decide *what is valid*. This server only
checks arguments, runs one script, and returns its exit code, a status word and its output.

## Tools (12)

| Tool | Script it runs | Reads/writes | Status words |
|---|---|---|---|
| `mima_list_modules` | build_command --list-modules | read | ok |
| `mima_build_commands` | build_command | read (temp plan file) | ok, invalid, bad_plan |
| `mima_validate_manifest` | validate_manifest | read | valid, invalid, unreadable |
| `mima_validate_metadata` | validate_metadata | read | valid, invalid, unreadable |
| `mima_validate_pbs_config` | validate_pbs_config | read | valid, invalid, unreadable |
| `mima_validate_pbs_scripts` | validate_pbs_scripts | read | valid, invalid, no_scripts |
| `mima_estimate_read_length` | estimate_read_length | read | ok, no_fastq |
| `mima_check_outputs` | check_outputs | read | complete, errors, usage_problem |
| `mima_check_job_status` | check_job_status (calls qstat) | read | complete, failed, in_progress, unresolved, tool_error |
| `mima_scan_logs` | scan_logs | read | no_known_errors, errors_found, no_logs |
| `mima_check_report` | check_report | read | ok, errors, unreadable |
| `mima_prepare_inputs` | prepare_inputs (preview) | nothing | prepared, unchanged, needs_input, unreadable |
| `mima_write_prepared_inputs` | prepare_inputs `--apply` | corrected copies of the manifest and metadata in a NEW folder (`out_dir`); originals untouched | prepared, unchanged, needs_input, unreadable |
| `mima_prepare_submission` | submit_jobs **dry run** | writes `submission_plan.json` in `run_dir` | dry_run_ok, invalid, bad_plan |

## Safety design

* **No tool submits a job.** `mima_prepare_submission` returns `run_yourself`: the exact `submit_jobs.py ... --execute --confirm TOKEN`
  command for a person to run in their own terminal. `submit_jobs.py` then asks for a typed confirmation and refuses without
  an interactive terminal. The runner refuses `--execute`, `--confirm`, `--approval`, `--allow-resubmit` and `--qsub`, so
  the server cannot be used to bypass this.
* Only scripts on an allow-list run, from `$MIMA_SKILL_DIR/scripts`, with a fixed argument list (no shell).
* Paths must be absolute, may not start with `-`, and (when `MIMA_ALLOWED_ROOTS` is set) must resolve inside those
  folders, symlinks included. Job ids and plan fields are checked against patterns.
* The token printed by a dry run binds a submission to the reviewed plan and script contents. It does not prove a
  person approved; the typed confirmation does that.

## Install and run (on the machine that holds the data, e.g. a Gadi login node)

Needs Python 3.10 or later for the server (the skill's scripts still run on 3.6+).

    python3 -m venv ~/mima-mcp-venv && ~/mima-mcp-venv/bin/pip install fastmcp
    export MIMA_SKILL_DIR=$HOME/agentic-mima
    export MIMA_ALLOWED_ROOTS=/scratch/mp96/<user>:/g/data/mp96     # optional, recommended
    ~/mima-mcp-venv/bin/python -m mima_mcp --check                    # checks the skill folder, then exits
    ~/mima-mcp-venv/bin/python -m mima_mcp                            # stdio server

Client over SSH (UNTESTED; shown as the intended shape; needs non-interactive SSH login):

    command: ssh
    args:    [gadi.nci.org.au, "MIMA_SKILL_DIR=$HOME/agentic-mima", "$HOME/mima-mcp-venv/bin/python", "-m", "mima_mcp"]

## Tests

    MIMA_SKILL_DIR=/path/to/agentic-mima python3 -m unittest discover -s tests -v

44 tests (24 server + 5 alignment with the skill's `references/mcp-tools.md`), run against the real scripts: tool surface, refused flags, path confinement (including a symlink), tool annotations, argument
checks, real manifest/plan/log cases, and the submission gate (the command that `mima_prepare_submission` returns is run
without a terminal, with a working fake `qsub`, and must refuse and write no record).
Tested with fastmcp 4.1.0 on Python 3.13.

## Before a run with a real model

    bash mima-client-start.sh preflight --base-url http://HOST:8000/v1 --model NAME

`mima_client.preflight` makes four short model calls and no cluster work. It checks that the server lists the model, how many
tokens the fixed instructions and tool list use, that the model makes a well-formed tool call and passes a nested plan
object, and which extra fields (for example reasoning) its replies carry. It ends with `RESULT: READY` or `NOT READY`.
`mima-client-start.sh` sets `MIMA_SKILL_DIR` and `MIMA_ALLOWED_ROOTS` and activates the venv, then starts the client.
The client sends tool schemas with the optional-argument `null` branches removed (some local chat templates cope badly with
`anyOf`), tracks the server's own token count, stops a turn before the context window overflows, and reports a failed model
request instead of exiting.

## Client for locally hosted models (`mima_client`)

Antigravity and Claude clients load tools and skills themselves. A model served by vLLM (for example Kimi-K3 on Gadi) needs a
client, so `mima_client` is a small one. It talks to any OpenAI-compatible endpoint that supports tool calling, calls the server's
tools in-process, and puts the skill's `SKILL.md` in the model's instructions. A tool `skill_read_file` lets the model read the
skill's `references/` and `assets/` files on demand (nothing else; paths are confined), which replaces the file reading a Claude
client would do for itself. It cannot submit: `mima_prepare_submission` is a dry run, and it and `mima_write_prepared_inputs` (corrected copies in a new folder) each need your typed yes per call.
Without a terminal the call is denied. Every message, tool call and result is saved to a JSONL transcript.

    export MIMA_SKILL_DIR=$HOME/agentic-mima-repo/agentic-mima
    export MIMA_ALLOWED_ROOTS=$HOME:/scratch/mp96
    python -m mima_client --base-url http://HOST:8000/v1 --model kimi-k3
    python -m mima_client --base-url ... --model ... --prompt-file turns.txt     # user turns separated by a line with ---

Commands in the chat: `/reset`, `/quit`. Tested against a scripted endpoint only (`tests/test_agent.py`); not yet used with a real model.

## Evaluation (mcp-builder Phase 4)

`evals/evaluation.xml` holds 10 read-only questions in the mcp-builder format; `evals/make_eval_data.py` creates the synthetic
data they use (default `/tmp/mima_eval`); `evals/verify_answers.py` recomputes every answer by calling the tools directly.

    python3 evals/make_eval_data.py
    MIMA_SKILL_DIR=/path/to/agentic-mima python3 evals/verify_answers.py     # all 10 should print ok

`verify_answers.py` shows the questions are answerable with these tools. It does NOT show that a model can do it. To measure that,
run the questions through a model. Either use mcp-builder's `scripts/evaluation.py` (needs an Anthropic API key, not used here), or
`evals/run_llm_eval.py`, which works with any OpenAI-compatible endpoint that supports tool calling (for example vLLM started with
`--enable-auto-tool-choice` and the tool-call parser for that model). It calls the tools in-process, withholds
`mima_prepare_submission` and `mima_write_prepared_inputs`, scores each answer by string comparison and writes full transcripts:

    python3 evals/make_eval_data.py $HOME/mima_eval
    python3 evals/run_llm_eval.py --base-url http://HOST:8000/v1 --model NAME --data-dir $HOME/mima_eval --out results.json

The data folder must be inside `MIMA_ALLOWED_ROOTS`. A first run through Antigravity 2.0 scored 10/10 (one run, questions written by Claude).

## Design review against mcp-builder (2026-10-14)

Followed: `mima_` tool prefix and `mima_mcp` server name; annotations with titles on every tool; Field descriptions and
constraints in the schemas; docstrings with returns and when-to-use; actionable errors; async execution with kill-on-timeout;
output cap with a `truncated` flag; stdio transport; logging to stderr only; read-only evaluation questions.
Deliberate differences: uses the standalone `fastmcp` package, not `mcp.server.fastmcp`; tools are thin wrappers (the skill
holds the workflow); results are structured dicts (status, output, data), not a markdown/JSON switch; no resources, prompts or
pagination (no tool returns an unbounded list).

## Not done yet

* Not run on Gadi, and not connected to any client (Antigravity or otherwise).
* No MCP resources or prompts (the skill's references could be exposed as resources later).
* No real submission has ever been made through `submit_jobs.py` on Gadi; do one small job before trusting that path.
* Whether the client supports in-client confirmation (elicitation) is unchecked; it would be an extra layer, not a replacement.
* Server-side logging/audit of tool calls.
