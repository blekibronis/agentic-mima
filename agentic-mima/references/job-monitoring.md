# Job monitoring

Read this after jobs have been submitted, or when the user asks how their run is going. Monitoring is read-only: nothing here submits, changes or deletes a job.

## Contents
- Checking status
- What each status means
- Exit status and resource notes
- How often to check
- Gates: what must be true before the next stage
- When the server has forgotten a job

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
