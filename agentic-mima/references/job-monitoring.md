# Job monitoring

Read this after jobs have been submitted, or when the user asks how their run is going. Monitoring is read-only: nothing here submits, changes or deletes a job.

## Contents
- Checking status
- What each status means
- Exit status and resource notes
- How often to check
- Gates: what must be true before the next stage
- When the server has forgotten a job
- Coming back in a new session

## Checking status

Use the record written at submission, so every job is covered:

```bash
python3 scripts/check_job_status.py --record mima_run/submission_record.json
```

For jobs not in a record, `--jobs 1234.server 1235.server`. Add `--format json` to save the result in the run folder. The script calls `qstat -x -f -F json`, so it needs a PBS version that can print JSON. Without a PBS execution tool, ask the user to run `qstat -x -f JOBID` and paste the output.

Exit codes: 0 all succeeded, 1 at least one failed, 3 still in progress (queued, held or running), 4 unresolved (nothing is active, but the server does not know a job), 2 tool problem.

## What each status means

| Status | Meaning | What to do |
|---|---|---|
| QUEUED | Waiting for resources, or waiting on a dependency | Nothing. Large requests (Kraken2 asks for about 300GB) can queue for a long time |
| HELD | Held by the user, the admin, or an unmet dependency | Tell the user; do not release it yourself |
| RUNNING | Executing | Compare walltime used with requested |
| SUCCEEDED | Finished with exit status 0 | Still verify outputs (see the gates below) |
| FAILED | Finished with a non-zero exit status | Go to `troubleshooting.md` |
| UNKNOWN | The server does not know the job | See "When the server has forgotten a job" |

## Exit status and resource notes

| Exit status | Typical meaning |
|---|---|
| 0 | The script finished normally |
| 1 to 255 | The job script exited with that status; read the job log |
| 256 or more | Terminated by a signal (value minus 256); often a walltime or memory kill, or a deletion by the user. Confirm with the log message |
| Negative | PBS could not start or run the job; the server `comment` usually says why |

When a job used 90% or more of its requested walltime or memory (this skill's threshold), the script says so. For a failed job this is usually the cause; for a succeeded job it is a warning to give more headroom next time. Report the numbers; do not change resource requests without the user's agreement.

## How often to check

Do not poll in a loop inside a conversation. Estimate how long the work should take from the `#PBS -l walltime` lines (the tutorial's defaults are about 2 h for each QC job, 10 h for the taxonomy job and 8 h for each function job, and queue time comes on top), tell the user, and ask them to come back when the jobs are due. Use `--wait` only when the execution tool is able to hold a long-running command.

## Gates: what must be true before the next stage

| Moving to | Required first |
|---|---|
| `qc-report` | Every QC job SUCCEEDED, and `check_outputs.py --stage qc` shows the clean reads and logs for every sample |
| generating `taxa` or `func` scripts | Same as above, because those generators read the `CleanReads` directory |
| `taxa-tables`, `taxa-report` | The taxonomy job SUCCEEDED, and `check_outputs.py --stage taxa` is complete |
| analysis stages | Feature tables exist: `check_outputs.py --stage taxa-tables` or `func-tables` complete |
| the report | Every stage you will describe has a complete `check_outputs.py` result |

After any job reaches SUCCEEDED, run the matching `check_outputs.py --stage ...` and `scan_logs.py` on the job's directory. A job can exit 0 and still leave a sample unprocessed, and a clean exit does not prove the outputs are complete.

## When the server has forgotten a job

Servers that do not keep job history report finished jobs as unknown. Then rely on the evidence on disk: the PBS log (`JOBNAME.oJOBID` in the submission directory), `scan_logs.py` and `check_outputs.py`. Say plainly in the report that status came from logs and outputs rather than from the scheduler. `check_job_status.py` reports this case as `UNRESOLVED` (exit code 4), not as in progress: waiting longer will not change it, so go to the logs now.

## Coming back in a new session

A stage can run for hours or days, longer than a chat. The jobs do not need the chat: they run in the queue on their own. What a new session needs is on disk in the run folder, so nothing depends on your memory of an earlier conversation.

**What the program leaves behind**

| File in the run folder | What it holds | Written by |
|---|---|---|
| `mima_run_state.json` (the run ledger) | per stage: the stage name, the person's choices, the jobs and PBS ids, the email address, where outputs should appear, and the stage report once it exists | `submit_jobs.py` and `stage_report.py`, never the model |
| `submission_record.json` | the jobs submitted and each approval (mode, time, token) | `submit_jobs.py` |
| `STAGE_REPORT_BATCH.json` and `.md` | what PBS said about every job of the stage when the last one ended, the log scan and the output check | the follow-up job |

The follow-up job (`scripts/stage_report.py`, 1 CPU, 10 minutes) is queued with the stage and starts only after every job of the stage has ended, whatever the outcome (finished, failed, killed at the walltime limit or deleted). It runs the same read-only checks as Step 8 and 9, writes the report, and emails a one-line notice if an address was given at submission. It reads and reports only. It never submits the next stage: that needs the person's approval in a session.

**What to do**

1. Call `mima_resume` with the run folder (shell: `python3 scripts/run_state.py resume --run-dir DIR`). If you do not have the folder, ask for it; if the folder holds no run, the tool lists runs below it, newest first, and you ask which one.
2. Read `status`: `in_progress` (tell them how it stands and to come back later; do not plan the next stage), `complete` (go to Step 9: check outputs, then the gates below), `failed` (explain with `references/troubleshooting.md`; an identical script cannot be submitted again unless they ask for a rerun), `unresolved` (PBS no longer knows a job and no stage report recorded it: decide from the logs and outputs, not from time passed), `no_jobs` (nothing was submitted from that folder).
3. If `notes` says the outcome came from the stage report, say so: PBS keeps finished jobs only for a short time, and the report was written while it still remembered them.
4. A folder from before ledgers existed has only `submission_record.json`. Resume still works from it, but the stage and the person's choices are unknown: ask.

Resume is read-only. It does not submit, run or change anything, and coming back is not approval: every `RUN` and `SUBMIT` is asked again. The ledger can be out of date, so PBS is asked every time.
