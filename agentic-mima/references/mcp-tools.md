# Using the MIMA MCP tools

When your tool list contains tools named `mima_*`, the project's MCP server is connected. Use them instead of the shell commands shown in the steps of SKILL.md. They run the same bundled scripts, so every rule, check and message in this skill still applies. If no `mima_*` tools are listed, use the shell commands in the steps as written.

## Contents
- Which tool replaces which command
- Reading a tool result
- What no tool does
- Submission
- Coming back to a run
- Running a step
- When a tool refuses or fails

## Which tool replaces which command

| Step | Shell command | MCP tool | Differences |
|---|---|---|---|
| 4 | `build_command.py --list-modules` | `mima_list_modules` | none |
| 4 | `build_command.py PLAN.json` | `mima_build_commands` | pass the plan as an object (`plan`), not a file. `check_fs=true` only works when the server runs on the machine that holds the data |
| 5 | the printed MIMA command, run by hand | `mima_run_step` | asks the person to approve, then runs ONE step in the container on this machine; see Running a step. Give `step` (as for `mima_build_commands`), `image_path` and `bind_paths` |
| 3 | `prepare_inputs.py` (dry run) | `mima_prepare_inputs` | writes nothing. Give `manifest_path` or `from_reads_dir`, plus `out_dir` (a new folder), and optionally `reads_dir` and `metadata_path` |
| 3 | `prepare_inputs.py --apply` | `mima_write_prepared_inputs` | same arguments as the preview. Writes corrected copies into `out_dir`; ask the user first. Originals are never changed |
| 3 | `make_pbs_header.py` (dry run) | `mima_plan_pbs_header` | writes nothing. Give `module` (`qc`, `taxa` or `func`), `samples` or `manifest_path`, `image_path`, `paths` (a list of every folder the job uses), `project`, and optionally `queue`, `cpus`, `reads_dir` (to scale qc and func by file size) and `num_pbs_jobs` (qc). Missing facts come back as `needs_input`: ask the user |
| 3 | `make_pbs_header.py --apply` | `mima_write_pbs_header` | same arguments as the preview plus `out_file` (a new file). Writes the header; ask the user first. An existing file is never overwritten |
| 3 | `validate_manifest.py` | `mima_validate_manifest` | `reads_dir` and `check_fs` replace `--reads-dir` and `--check-fs` |
| 3 | `validate_metadata.py` | `mima_validate_metadata` | `group_column`, `groups`, `feature_tables` replace the flags; give the `_counts` or `_relAbund` table |
| 3 | `validate_pbs_config.py` | `mima_validate_pbs_config` | `module` is `qc`, `taxa` or `func` |
| 2 | `estimate_read_length.py` | `mima_estimate_read_length` | `available` is a comma list such as `50,100,150` |
| 6 | `validate_pbs_scripts.py` | `mima_validate_pbs_scripts` | `paths` is a list; `check_fs` replaces `--check-fs` |
| 7 | `submit_jobs.py` (dry run), `stage_report.py write-job` | `mima_prepare_submission` | dry run only; see Submission. Also give `stage`, `output_dir`, `manifest`, `choices`, `mail_to` and `expected_minutes` so the run ledger is useful later; the follow-up report job is added unless `stage_report` is false |
| 7 | `submit_jobs.py --execute` | `mima_submit_jobs` | asks the person to approve, then submits; see Submission. Give the same `run_dir`, `token`, `check_fs`, `project` and `queue` as the dry run |
| 8 | `check_job_status.py` | `mima_check_job_status` | give `job_ids` or `record_path`, not both |
| 8 | `run_state.py resume` | `mima_resume` | give the run folder (`folder`). Read-only. Use it first when the person comes back to a run from an earlier session; see Coming back to a run |
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
| `no_jobs` | `mima_resume` found a run folder with nothing submitted | Say so; nothing is running from there |
| `choose_run` | `mima_resume` found several runs and lists them in `data.runs` | Ask the person which one; do not pick |
| `not_found` | `mima_resume` found no run in or under that folder | Ask for the right folder |
| `unresolved` | The scheduler no longer knows a job | Stop waiting: read the PBS log with `mima_scan_logs` and `mima_check_outputs`, and say the status came from logs and outputs |
| `prepared` | Changes are needed and listed in `data.changes` (inputs), or a header draft is in `data.lines` | Explain each change in plain words and ask before `mima_write_prepared_inputs` or `mima_write_pbs_header`; afterwards use the files in `data.use` and validate them. A header draft's numbers are lab estimates: say so |
| `unchanged` | The files already follow the conventions | Use them as they are and validate |
| `needs_input` | Questions are open in `data.needs_input` | Put each to the user; do not guess; nothing is written until they are answered |
| `written`, `bad_args` | The follow-up report job script was written, or the request for it was refused | Normally internal to `mima_prepare_submission`; if it fails, say so and offer to prepare without it (`stage_report` false) |
| `bad_plan`, `unreadable`, `no_scripts`, `no_fastq`, `no_logs`, `usage_problem` | The input could not be used | Fix the input or ask the user; do not retry unchanged |
| `tool_error`, `timeout`, `unexpected_exit` | The tool itself failed (for example `qstat` is missing on this machine) | Say so, give the user the equivalent shell command to run where it works, and do not guess a result |

Long results lead with a summary. For `mima_check_outputs`, `data.summary` gives the counts (errors, warnings, missing or empty files, and how many are missing per sample) before the full list in `data.findings`. Report the summary and give the full list only when asked; long lists are easy to mis-copy, and the summary is the number to quote.

If `truncated` is true, the output was cut: narrow the request (one stage, one folder) and ask again. A result is evidence only for what it shows. Quote file names and lines from `output` or `data` exactly, as with shell output.

## What no tool does

Only `mima_run_step` runs a MIMA command (`apptainer run --app ...`), one validated step at a time and only after the person approves (see Running a step); none deletes, holds or alters the user's files or jobs. The only tool that queues jobs is `mima_submit_jobs`, and only after the person approves (see Submission). The writers create new files only: `mima_write_prepared_inputs` makes corrected copies in a new folder, `mima_write_pbs_header` writes a new header file, and `mima_prepare_submission` writes its plan file. After `mima_build_commands`, give the user the commands to run themselves, after they have seen and approved them (Critical rule 4). Resume with `mima_validate_pbs_scripts` once the generator has written its scripts. `generate_module_docs.py` is a maintenance script and has no tool.

## Submission

`mima_prepare_submission` validates the scripts, writes `submission_plan.json` in the run folder, runs the dry run, and returns the qsub commands, a confirmation token and `run_yourself`: the exact `submit_jobs.py ... --execute` command. Nothing is submitted (`submitted` is false). Follow the protocol in `references/job-submission.md`: show the user the jobs and resources and ask a direct question such as "Submit these 3 jobs now?".

`mima_submit_jobs` submits that plan. Call it only after the user has reviewed the plan and clearly asked you to submit it. Calling it does not submit anything by itself: the program shows the user a prompt with the jobs and their resources, and the user must answer it themselves. You never see or give that answer, so never say or imply that the user approved. Read the result `status`:

| status | What it means and what to say |
|---|---|
| `submitted` | The jobs are queued. Report `pbs_job_ids` and where `submission_record.json` is |
| `partly_submitted` | Some jobs went in and one failed. Follow "If a submission stops part-way" in `references/job-submission.md` |
| `not_submitted` | `qsub` refused the first job. Show the error text from `output`; nothing is queued |
| `declined`, `cancelled` | The user chose not to submit, or closed the prompt. Nothing was submitted. Ask what they would like to change; do not call the tool again unless they ask you to |
| `stale` | The scripts or plan changed after the dry run. Run `mima_prepare_submission` again and have the user review the new plan |
| `invalid` | The plan no longer passes the dry run (for example a script was already submitted). Show the errors; never resubmit without the user's agreement |
| `too_many_jobs` | The plan is larger than this server allows. Split it into smaller plans |
| `disabled`, `approval_unavailable` | This server or this chat cannot ask the user, so nothing was submitted. Give them the `run_yourself` command from `mima_prepare_submission` to run in their own terminal, where they type the confirmation |

Do not try to submit by any other route, and never ask the server to skip the user's prompt.

## Coming back to a run

A new session has no memory of the old one, and the jobs of a stage can outlive many chats. The run folder holds what is needed: `mima_run_state.json` (the run ledger), `submission_record.json` and, once the stage has ended, `STAGE_REPORT_BATCH.md`. Call `mima_resume` with the folder first. It reads the ledger, asks PBS for the live state of the last submitted stage, scans the logs and checks the outputs when the ledger names them, and returns `data.next` in plain words. Report what it shows; do not say jobs finished unless its status is `complete`. It is read-only and coming back is not approval: any next stage still goes through `mima_run_step` or `mima_submit_jobs`, and the person is asked again. The follow-up job that writes the stage report (`stage_report.py`) is queued with the stage by `mima_prepare_submission`; no tool runs it directly. `references/job-monitoring.md` has the full procedure.

## Running a step

`mima_run_step` takes one step in the same form as `mima_build_commands`, builds and checks the command with the same code, and runs it on this machine inside the container with `SANDBOX` set to `image_path` and `APPTAINER_BIND` set to `bind_paths` (give the same folders and image as in the PBS header). You cannot pass a command of your own. Calling it does not run anything by itself: the program shows the user the exact command and the user must approve. Use it for the generators (`qc`, `taxa`, `func` only write PBS scripts) and the command-line stages. They run on the login node, so it stops them after `timeout_minutes` (default 30). Read the result `status`:

| status | What it means and what to say |
|---|---|
| `ran` | The command exited with 0. Quote the end of `output`. After a generator, validate the new scripts with `mima_validate_pbs_scripts` |
| `failed` | The command exited with an error. Show the end of `output` and `stderr` and use `references/troubleshooting.md`; never rerun unchanged |
| `timeout` | Stopped at the time limit. Say so and check what was written before anything is rerun |
| `declined`, `cancelled` | The user chose not to run it. Nothing ran. Ask what to change |
| `invalid` | The plan failed its checks. Show the errors and fix the plan; the user was not asked |
| `disabled`, `approval_unavailable` | Running is off, or this chat cannot ask the user. Give them the command from `mima_build_commands` to run themselves |
| `tool_error` | For example `apptainer` is not on the path. Say so; on Gadi the server must be started after `module load apptainer` |

## When a tool refuses or fails

- "outside the allowed folders": the path is not one the server's owner allowed. Tell the user; do not look for another path to the same file.
- "must be an absolute path": rebuild the path from what the user gave you; do not guess.
- A tool you expect is missing, or every call fails: say the MCP server is not working, then fall back to the shell commands in the steps (Local or Advice mode).
