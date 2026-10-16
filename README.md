# agentic-mima

A [Claude Agent Skill](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview) that turns plain-English shotgun-metagenomics requests into **validated, reproducible MIMA pipeline plans and commands**, then guides confirmed job submission, monitoring, output checking, troubleshooting and reporting.

It is built for the **MIMA Apptainer container** (`mima4_v2.0.0`) on an **OpenPBS** cluster. It was developed on [NCI Gadi](https://opus.nci.org.au/display/Help/0.+Welcome+to+Gadi).

> **Status: v0.4.0, pre-release.** The scripts are tested and have been run on Gadi, but no complete pipeline run has been driven end to end through the skill yet. See [What has and has not been verified](#what-has-and-has-not-been-verified) before relying on it.

## What it does

You say what you want ("profile the species in these samples and compare healthy controls with disease"). The skill:

1. maps the request to MIMA stages and checks what is missing,
2. validates your manifest, metadata and PBS config files,
3. builds every command with a script that only knows approved modules and checks every parameter,
4. validates the generated PBS scripts,
5. submits **only after you have seen the exact jobs and typed a confirmation**,
6. monitors jobs, checks outputs, diagnoses failures from the logs,
7. helps you interpret the results and writes a report that is checked against the real output files.

Supported stages:

| Module | Alias | Purpose |
|---|---|---|
| `mima-qc` | `qc` | Per-sample QC scripts and PBS scripts (repair, dereplicate, fastp, host decontamination) |
| `mima-qc-report` | `qc-report` | Summarise QC statistics into `QC_report.csv` |
| `mima-taxa-profiling` | `taxa` | Kraken2/Bracken taxonomy profiling PBS script and per-sample scripts |
| `mima-bracken-report` | `taxa-report` | Summarise Bracken logs into `bracken-summary.tsv` |
| `bracken-feature-tables` | `taxa-tables` | Merge per-sample Bracken output into feature tables, one per rank |
| `mima-function-profiling` | `func` | Per-sample HUMAnN3 PBS scripts |
| `mima-vis-taxa` | `vis-barplot` | Taxonomy stacked bar plots by group |
| `mima-visualisation` | `diversity` | Alpha diversity, beta diversity (PERMANOVA), differential abundance |
| `mima-classifier-RF` | `classifier` | Random Forest classification with 3- to 10-fold cross-validation |

**Out of scope** (the skill says so and stops): 16S amplicon analysis, genome assembly or binning, custom statistics, other tools, and general Python or R coding.

## How it keeps you safe

- **The model never invents pipeline syntax.** Every command comes from `scripts/build_command.py`, driven by `scripts/command_specs.json`. A parameter that is not in the spec is refused.
- **Nothing is guessed.** Database paths, host genome, PBS config files, group columns and their levels must come from you. Defaults are stated in the reply.
- **Submission needs a person.** `scripts/submit_jobs.py` does a dry run first, then requires a typed confirmation at a real terminal. The skill hands you the command; it does not bypass the gate.
- **Monitoring is read-only.** Statements about jobs and outputs must be backed by tool output or pasted text, with file names and lines quoted.
- **Plain language first.** The skill is written for clinicians, wet-lab researchers and students as well as bioinformaticians.

## Requirements

| For | You need |
|---|---|
| The skill's scripts | Bash and Python **3.6 or later**, standard library only (Gadi's default `python3` is 3.6.8) |
| Running the pipeline | The MIMA container, Apptainer and OpenPBS on a cluster you can submit to |
| Submitting, monitoring and output checks through Claude | Either an execution tool on the cluster login node, or the optional [MCP server](#optional-mcp-server) |

The skill works in three modes and tells you which one it is in:

| If Claude has | Mode | What happens |
|---|---|---|
| A terminal or the MCP server on the cluster | **Cluster** | Runs the validators and checkers itself; you still type the submission confirmation |
| A sandbox plus files you provide | **Local** | Runs the builder and validators on your files; gives you the cluster commands to run |
| No execution tool | **Advice** | Writes the plan and the exact commands for you to run and paste back |

## Install

The skill is the folder [`agentic-mima/`](agentic-mima/). Its entry point is `SKILL.md`.

**Claude.ai.** Zip the `agentic-mima` folder (so the zip contains the folder, not loose files) and upload it under *Settings → Capabilities → Skills*.

**Claude Code.** Copy the folder into your skills directory:

```bash
cp -r agentic-mima ~/.claude/skills/
```

**On the cluster.** Clone this repository into your home directory (`git clone <repository URL>`), then point your execution tool or the MCP server at the `agentic-mima/` folder inside it. Update later with `git pull`.

Skill menus and install steps change between Claude releases. If these steps do not match your version, follow the current [Agent Skills documentation](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview).

## Example requests

- "I have a manifest.csv of 40 paired-end samples on Gadi. Check it and plan the QC step."
- "Build the taxonomy profiling job for these samples and show me the PBS script before anything is submitted."
- "My Bracken job failed. Here is the log. What went wrong?"
- "Compare the species-level profiles of healthy controls and disease. Here is my metadata table."
- "Check the report I drafted against the actual output files."

## Repository layout

```
agentic-mima/            the skill (what Claude loads)
  SKILL.md               workflow, rules, mode selection
  scripts/               builder, validators, submission gate, monitors, log scanner (Python, stdlib only)
  references/            parameters, lab defaults, troubleshooting, interpretation, MCP tool mapping
  assets/                plan and report templates
agentic-mima-mcp/        optional MCP server that exposes the scripts as tools (FastMCP), plus a small client for locally hosted models
agentic-mima-tests/      test kit: 123 automated tests, conversation tests, trigger queries
```

The skill folder deliberately contains only `SKILL.md`, `scripts/`, `references/` and `assets/`. The tests and server live beside it.

## Optional MCP server

[`agentic-mima-mcp/`](agentic-mima-mcp/) wraps the skill's scripts as 12 MCP tools (all prefixed `mima_`) using [FastMCP](https://gofastmcp.com). It needs Python 3.10 or later.

- **No tool submits a job.** `mima_prepare_submission` does a dry run and returns the command and confirmation token for *you* to run in a terminal. The server refuses the flags that would execute a submission.
- **Paths can be confined.** Set `MIMA_ALLOWED_ROOTS` to a colon-separated list of folders; any path outside them is refused.
- **Read-only by default.** All tools except `mima_prepare_submission` (writes a plan file) and `mima_write_prepared_inputs` (writes corrected copies of the user's manifest and metadata into a new folder; originals are never changed) are annotated read-only.

Start-up check (after installing `fastmcp` in a Python 3.10+ environment):

```bash
export MIMA_SKILL_DIR=$HOME/agentic-mima
python -m mima_mcp --check
```

Setup, the tool list and how to connect a client over SSH are in [`agentic-mima-mcp/README.md`](agentic-mima-mcp/README.md). Which tool replaces which shell command is in [`agentic-mima/references/mcp-tools.md`](agentic-mima/references/mcp-tools.md).

## Testing

```bash
cd agentic-mima-tests
python3 run_tests.py                    # 123 tests; needs Python 3.8 or later
```

```bash
cd agentic-mima-mcp
python -m unittest discover -s tests    # 35 tests; needs Python 3.10 or later and fastmcp
```

A pass shows that the scripts behave as designed on mock inputs and that the skill's files are consistent. It does not show that Claude follows the instructions in a conversation; use `agentic-mima-tests/conversation_tests.md` and `trigger_queries.md` for that.

## What has and has not been verified

**Verified**

- The scripts run on Gadi's Python 3.6.8 with the current files: manifest, metadata, PBS config and PBS script validation, command building (good plans accepted, bad plans refused), read-length estimation, QC output checks, log scanning and job-status reading.
- 123 automated tests pass. A static check confirms the scripts use only Python 3.6 features.
- The MCP server's 35 tests (v0.4.0) pass on Gadi (Python 3.12.1, fastmcp 4.1.0), and the 10 evaluation questions recompute correctly by direct tool calls.
- A language model (Antigravity 2.0, model not recorded) answered all 10 evaluation questions correctly through the server on Gadi, using `mima_validate_manifest`, `mima_validate_metadata`, `mima_estimate_read_length`, `mima_scan_logs`, `mima_check_outputs`, `mima_validate_pbs_config` and `mima_build_commands`. One run, on synthetic data, with questions written by Claude: it shows the tool descriptions are usable, not that the tools meet the lab's needs.
- An MCP client (Antigravity 2.0) connected to the server on Gadi over SSH and listed 11 tools. Through it, six read-only tools returned the same results as the command-line scripts on real Gadi data: `mima_list_modules`, `mima_validate_manifest`, `mima_scan_logs`, `mima_check_outputs` (including its short summary), `mima_check_job_status` (a finished job aged out of history is reported as unresolved), and a path outside the allowed folders was refused.

**Not yet verified**

- A complete run of every stage on real data, and a successful taxonomy run, so the expected-output lists for taxonomy are provisional.
- `scripts/submit_jobs.py` on a real submission, and `scripts/check_report.py` beyond its help text.
- 17 of the 22 patterns in `scripts/error_patterns.json` have not been seen in real logs (5 have).
- The same 10 questions answered by a second, locally hosted model (`agentic-mima-mcp/evals/run_llm_eval.py` is ready; not yet run).
- `mima_validate_pbs_scripts`, `mima_check_report` and `mima_prepare_submission` called through a client.
- Behaviour in different Claude clients and versions.

## Known limitations

- **Defaults await sign-off.** `references/lab-defaults.md` records choices the project owner proposed (for example the Bracken threshold of 100 and the approved database path). They are pending confirmation by the supervisor, and the skill reports them as defaults rather than as established policy.
- **One reference database is approved.** GTDB r95 (2020) for bacterial and archaeal gut and stool metagenomes. Newer species may be missing.
- **Cause of one real failure is unknown.** A Bracken failure in the tutorial run could not be diagnosed because its logs were gone; the skill recognises the visible symptom but not the root cause.
- **Cluster-specific.** The PBS directives, container path and storage layout follow Gadi. Other clusters need the container path and PBS config adjusted.

## Contributing

Issues and pull requests are welcome. Please:

- keep `scripts/` to the Python standard library and Python 3.6 syntax,
- change parameters in `scripts/command_specs.json` (the single source of truth), then regenerate `references/module-parameters.md` with `scripts/generate_module_docs.py`,
- add or update a test in `agentic-mima-tests/run_tests.py` for any behaviour change, and run the suite before opening a pull request,
- mark any new error pattern as verified only if it comes from a real log.

## Licence

MIT. See [LICENSE](LICENSE).
