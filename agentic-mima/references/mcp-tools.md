# Using the MIMA MCP tools

When your tool list contains tools named `mima_*`, the project's MCP server is connected. Use them instead of the shell commands shown in the steps of SKILL.md. They run the same bundled scripts, so every rule, check and message in this skill still applies. If no `mima_*` tools are listed, use the shell commands in the steps as written.

## Contents
- Which tool replaces which command
- Reading a tool result
- What no tool does
- Submission
- When a tool refuses or fails

## Which tool replaces which command

| Step | Shell command | MCP tool | Differences |
|---|---|---|---|
| 4 | `build_command.py --list-modules` | `mima_list_modules` | none |
| 4 | `build_command.py PLAN.json` | `mima_build_commands` | pass the plan as an object (`plan`), not a file. `check_fs=true` only works when the server runs on the machine that holds the data |
| 3 | `prepare_inputs.py` (dry run) | `mima_prepare_inputs` | writes nothing. Give `manifest_path` or `from_reads_dir`, plus `out_dir` (a new folder), and optionally `reads_dir` and `metadata_path` |
| 3 | `prepare_inputs.py --apply` | `mima_write_prepared_inputs` | same arguments as the preview. Writes corrected copies into `out_dir`; ask the user first. Originals are never changed |
| 3 | `make_pbs_header.py` (dry run) | `mima_plan_pbs_header` | writes nothing. Give `module` (`taxa` only), `samples` or `manifest_path`, `image_path`, `paths` (a list of every folder the job uses), `project`, and optionally `queue` and `cpus`. Missing facts come back as `needs_input`: ask the user |
| 3 | `make_pbs_header.py --apply` | `mima_write_pbs_header` | same arguments as the preview plus `out_file` (a new file). Writes the header; ask the user first. An existing file is never overwritten |
| 3 | `validate_manifest.py` | `mima_validate_manifest` | `reads_dir` and `check_fs` replace `--reads-dir` and `--check-fs` |
| 3 | `validate_metadata.py` | `mima_validate_metadata` | `group_column`, `groups`, `feature_tables` replace the flags; give the `_counts` or `_relAbund` table |
| 3 | `validate_pbs_config.py` | `mima_validate_pbs_config` | `module` is `qc`, `taxa` or `func` |
| 2 | `estimate_read_length.py` | `mima_estimate_read_length` | `available` is a comma list such as `50,100,150` |
| 6 | `validate_pbs_scripts.py` | `mima_validate_pbs_scripts` | `paths` is a list; `check_fs` replaces `--check-fs` |
| 7 | `submit_jobs.py` (dry run) | `mima_prepare_submission` | dry run only; see Submission |
| 8 | `check_job_status.py` | `mima_check_job_status` | give `job_ids` or `record_path`, not both |
| 9 | `check_outputs.py` | `mima_check_outputs` | `stage`, `directory`, `manifest` |
| 9 | `scan_logs.py` | `mima_scan_logs` | `max_hits` (default 3, up to 50): raise it when you need every affected sample, not just the first three |
| 10 | `check_report.py` | `mima_check_report` | `stages`, `run_dir` |

All paths must be absolute. The server may confine paths to the folders its owner allowed (`MIMA_ALLOWED_ROOTS`).

## Reading a tool result

Every result has: `status`, `exit_code`, `output` (the script's own text), `data` (parsed JSON when the script offers it), `stderr`, `command` and `truncated`.

| status | Meaning | What to do |
|---|---|---|
| `ok`, `valid`, `complete`, `no_known_errors`, `dry_run_ok` | The script found no errors | Report any warnings in `output` or `data`. For `no_known_errors`, say it does not prove the run succeeded |
| `invalid`, `errors`, `errors_found`, `failed` | The script found errors | Explain each error with its fix from `references/validation-rules.md` or `references/troubleshooting.md`; never edit files to make a check pass. If `mima_plan_pbs_header` returns `invalid`, the draft failed its own check: report it and do not use the draft |
| `in_progress` | A job is queued, held or running | Estimate the time left and ask the user to come back |
| `unresolved` | The scheduler no longer knows a job | Stop waiting: read the PBS log with `mima_scan_logs` and `mima_check_outputs`, and say the status came from logs and outputs |
| `prepared` | Changes are needed and listed in `data.changes` (inputs), or a header draft is in `data.lines` | Explain each change in plain words and ask before `mima_write_prepared_inputs` or `mima_write_pbs_header`; afterwards use the files in `data.use` and validate them. A header draft's numbers are lab estimates: say so |
| `unchanged` | The files already follow the conventions | Use them as they are and validate |
| `needs_input` | Questions are open in `data.needs_input` | Put each to the user; do not guess; nothing is written until they are answered |
| `bad_plan`, `unreadable`, `no_scripts`, `no_fastq`, `no_logs`, `usage_problem` | The input could not be used | Fix the input or ask the user; do not retry unchanged |
| `tool_error`, `timeout`, `unexpected_exit` | The tool itself failed (for example `qstat` is missing on this machine) | Say so, give the user the equivalent shell command to run where it works, and do not guess a result |

Long results lead with a summary. For `mima_check_outputs`, `data.summary` gives the counts (errors, warnings, missing or empty files, and how many are missing per sample) before the full list in `data.findings`. Report the summary and give the full list only when asked; long lists are easy to mis-copy, and the summary is the number to quote.

If `truncated` is true, the output was cut: narrow the request (one stage, one folder) and ask again. A result is evidence only for what it shows. Quote file names and lines from `output` or `data` exactly, as with shell output.

## What no tool does

No tool runs a MIMA generator (`apptainer run --app ...`), a container, `qsub`, or anything that changes or deletes the user's files or jobs. The writers create new files only: `mima_write_prepared_inputs` makes corrected copies in a new folder, `mima_write_pbs_header` writes a new header file, and `mima_prepare_submission` writes its plan file. After `mima_build_commands`, give the user the commands to run themselves, after they have seen and approved them (Critical rule 4). Resume with `mima_validate_pbs_scripts` once the generator has written its scripts. `generate_module_docs.py` is a maintenance script and has no tool.

## Submission

`mima_prepare_submission` validates the scripts, writes `submission_plan.json` in the run folder, runs the dry run, and returns the qsub commands, a confirmation token and `run_yourself`: the exact `submit_jobs.py ... --execute` command. Nothing is submitted (`submitted` is false). Follow the protocol in `references/job-submission.md` unchanged: show the user the jobs and resources, ask a direct yes/no question, and only after a yes hand them `run_yourself` to run in their own terminal, where they type the confirmation. Do not try to submit by any other route, and never ask the server to bypass the typed confirmation.

## When a tool refuses or fails

- "outside the allowed folders": the path is not one the server's owner allowed. Tell the user; do not look for another path to the same file.
- "must be an absolute path": rebuild the path from what the user gave you; do not guess.
- A tool you expect is missing, or every call fails: say the MCP server is not working, then fall back to the shell commands in the steps (Local or Advice mode).
