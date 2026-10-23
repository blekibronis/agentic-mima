# Job submission

Read this before submitting anything. Submission changes the state of a shared cluster and spends the user's compute allocation, so it needs the user's explicit approval of the exact jobs, every time.

## Contents
- Who runs what
- The confirmation protocol
- What to submit at each stage
- Dependencies between jobs
- Site options
- What never to do
- The run folder

## Who runs what

Cluster commands run only through an execution tool that has access to the cluster (a terminal on the login node, or the project's MCP server when it is available). The server's `mima_submit_jobs` is the only tool that can queue jobs, and only after the user approves in its prompt. If you have no such tool, you cannot submit or check anything yourself. Give the user the exact commands, ask them to run them, and ask them to paste the output back. Do not describe a job as submitted unless a tool result shows it.

## The confirmation protocol

Follow these steps in order. None may be skipped, including for a single job.

1. **Validate the scripts.** Run `python3 scripts/validate_pbs_scripts.py SCRIPT_DIR --check-fs`. Any error stops here. Fix the cause (usually an unedited `pbs_header` config; see `validation-rules.md`), regenerate the scripts, and validate again.
2. **Write a submission plan.** One job per `.pbs` file:

   ```json
   {"jobs": [
     {"id": "qc0", "script": "/abs/out/QC_module/qcModule_0.pbs"},
     {"id": "qc1", "script": "/abs/out/QC_module/qcModule_1.pbs"}
   ]}
   ```

3. **Dry run.** `python3 scripts/submit_jobs.py submission_plan.json`. This validates again, prints the exact `qsub` commands and a confirmation token, and submits nothing. With the MCP tool, `mima_prepare_submission` also writes the follow-up report job (`scripts/stage_report.py write-job`) and adds it to the plan with the dependency `afterany` on every stage job; ask the person once for an email address (optional) and pass it as `mail_to`. Optional plan fields (`stage`, `choices`, `check`, `mail_to`, `batch`, `expected_minutes`) are kept in the run ledger, `mima_run_state.json`. Show the follow-up job to the person as what it is: a small job that reports when the stage has ended and submits nothing.
4. **Ask the user to approve.** Show, in plain language:
   - how many jobs, and what each stage does;
   - the resources each requests (`ncpus`, `mem`, `walltime` from the `#PBS` lines);
   - dependencies between jobs;
   - the directory each job is submitted from;
   - the exact `qsub` commands from the dry run.

   Then ask a direct question such as "Submit these 3 jobs now?". Only a clear yes to that question counts. Silence, a follow-up question, or approval of something else does not.
5. **Execute.** What the controls do: the token ties execution to the exact plan and scripts the user reviewed, so any edit invalidates it. The token does not prove who approved. That is enforced by `--approval`:
   - **Through the MCP server (`mima_submit_jobs`).** Call it only after the user has reviewed the plan and clearly asked you to submit. The program shows the user the jobs and their resources in a prompt of its own and the user answers it; the server submits only if they choose to. You cannot answer it, and a decline, a closed prompt, a client that cannot show the prompt, or a server with submission switched off all mean nothing was submitted. In that case use the next option.
   - **By hand (`tty`), the fallback.** Give the user the command printed by the dry run (it already holds the token) and ask them to run it in their own terminal. The script shows the jobs and asks them to type `submit N`. It refuses unless a person is at an interactive terminal, so a tool that runs commands without one cannot complete it. This is enforced by the script, not by this guide.
   - **`--approval harness`.** Used by the server's approval path above. Never add this flag yourself to get past the typed confirmation, and never use it when the tool runs commands without asking the user.
6. **Report.** Give the PBS job ids, where `submission_record.json` is, and how to check status (`job-monitoring.md`). The record also logs the approval (mode, time, token and job ids).

## If a submission stops part-way

`submit_jobs.py` stops at the first `qsub` failure and does not submit the jobs that depend on it. Jobs already accepted stay in the queue. There is no automatic rollback, because cancelling jobs is the user's decision. Do this:

1. Read `submission_record.json`: it lists exactly which jobs were submitted and their PBS ids.
2. Say which jobs went in and which did not, and show the `qsub` error text.
3. Offer the two options and let the user choose: keep the submitted jobs and fix the cause, or cancel them. For cancelling, give the command `qdel ID1 ID2 ...` for them to run.
4. After the cause is fixed, make a new submission plan for only the jobs that were not submitted, with `"after_job_ids"` pointing at still-queued jobs they depend on, and run the full protocol again from the dry run.

## What to submit at each stage

| Stage | Preconditions | What to submit | Submit from |
|---|---|---|---|
| `qc` | Generator run; scripts validated | Every `qcModule_N.pbs`; no dependencies between them | `QC_module/` |
| `qc-report` | All QC jobs SUCCEEDED | Not a PBS job: run the command directly | (builder adds the `cd`) |
| `taxa` | QC finished; generator run on the finished `CleanReads` | One `run_taxa_profiling.pbs` (samples run in sequence) | `Taxonomy_profiling/` |
| `taxa-tables`, `taxa-report` | Taxonomy job SUCCEEDED | Not PBS jobs: run the commands directly | see `module-parameters.md` |
| `func` | QC finished; generator run on the finished `CleanReads` | One `SAMPLE.pbs` per sample | `Function_profiling/` |
| `func-tables` | All function jobs SUCCEEDED, or submitted in the same plan with dependencies | `featureTables/generate_func_feature_tables.pbs` | `Function_profiling/featureTables/` |

Why the directory matters: PBS writes each job's log where it was submitted, and the QC and Bracken report steps read those logs. `submit_jobs.py` uses each script's own directory unless the plan sets `cwd`.

Why the taxonomy and function generators wait for QC: they build per-sample scripts from the files in the `CleanReads` directory, so run them after QC has finished. A dependency cannot bridge the generator step.

Analysis stages (`vis-barplot`, `diversity`, `classifier`) run directly, and the tutorial runs them inside an interactive job (`qsub -I -l select=1:ncpus=4:mem=4gb,walltime=6:00:00`). Starting an interactive job is not something to do on the user's behalf; ask them to start one.

## Dependencies between jobs

Add `"after": ["id1", "id2"]` to make a job wait for jobs that succeed (`qsub -W depend=afterok:...`). Ids may name jobs earlier in the same plan or jobs already in `submission_record.json`. Use `"after_job_ids": ["1234.server"]` for a job that is still queued or running but not in the record.

Do not add a dependency on a job that has already finished: many servers no longer know finished jobs and `qsub` rejects the submission. If the earlier job has succeeded, omit the dependency.

The functional feature-table job is the main use:

```json
{"id": "func_tables",
 "script": "/abs/out/Function_profiling/featureTables/generate_func_feature_tables.pbs",
 "after": ["S1", "S2", "S3"]}
```

## Site options

Some sites require a project, queue or account on every submission. Pass them with `--project`, `--queue` and `--account` (they become `-P`, `-q`, `-A`). Other site-specific directives (storage, licences, modules) belong in the stage's `pbs_header` config, so they are validated and copied into every script. Ask the user rather than guessing site rules.

## What never to do

- Never run `qsub` yourself, or any generated command, outside `submit_jobs.py --execute`.
- Never delete, hold or alter jobs (`qdel`, `qhold`, `qalter`). If the user wants a job stopped, give them the command and let them run it.
- Never resubmit a script that is already in the record unless the user asks for a rerun; then use `--allow-resubmit` and say so.
- Never edit a generated `.pbs` file to make validation pass. Fix the config and regenerate, or show the user the edit and get approval first.
- Never submit scripts that failed validation.

## The run folder

Keep one folder per analysis so the report can be reproduced. Suggested layout inside the output directory:

```
mima_run/
  plan.json                  # input to build_command.py
  build_output.txt           # validated commands, as printed
  submission_plan.json       # input to submit_jobs.py
  submission_record.json     # written by submit_jobs.py --execute
  decisions.md               # one line per default applied, input chosen or approval given, with the date
  status_YYYYMMDD.json       # check_job_status.py --format json
  output_checks/*.json       # check_outputs.py --format json
  report.md
```

Append a line to `decisions.md` whenever the user chooses a value (for example `--threshold 100`), accepts a default, approves running commands, or approves a submission, so the report can state who decided what. Write these files only where you have file access. In plain chat, give the user the contents and the file names.
