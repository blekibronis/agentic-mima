# Analysis plan template

Use this structure when presenting a plan. Fill every field from the user's answers or mark it as an assumption. Omit stages that are not in the request.

## Goal
One sentence restating the request in the user's terms, for example "Compare gut taxonomy and function between healthy controls and disease samples."

## Inputs confirmed
| Item | Value | Validated |
|---|---|---|
| Reads directory | absolute path | manifest check result |
| Manifest | absolute path | VALID / not checked |
| Metadata and group column | path, column, levels | VALID / not checked |
| Reference databases | host genome, Kraken2/Bracken, HUMAnN, MetaPhlAn as needed | asked / provided |
| PBS config files | path per stage | VALID / not checked |

## Steps
| # | Stage | Module | Kind | Runs after |
|---|---|---|---|---|
| 1 | QC | mima-qc | Generates PBS scripts | none |
| 2 | Taxonomy | mima-taxa-profiling | Generates PBS scripts | 1 |

Kind is either "Generates PBS scripts" (analysis starts only when those scripts are submitted) or "Runs directly".

## Assumptions and defaults
List every default the pipeline will apply (for example `--num-pbs-jobs 4`, `--threshold 1000`) and every value you inferred. The user should be able to correct any line.

## Commands
Validated commands from `build_command.py`, one code block per step, in run order. Never edit them by hand after validation; re-run the builder instead.

## Checks still to do on the cluster
Filesystem checks and any validator the user must run where the files live, with the exact command.

## Execution mode
State which of these applies, so the user knows what you can and cannot do: running scripts here with cluster access, running only the local validators on files provided, or giving commands for the user to run and paste back.

## Next step and approvals
Name the next stage and what needs the user's approval before it happens: running the generator or direct commands above, and, once scripts exist, job submission under the confirmation protocol in `references/job-submission.md`. Say plainly that nothing runs or is submitted until they approve, and what they will be asked to approve next.
