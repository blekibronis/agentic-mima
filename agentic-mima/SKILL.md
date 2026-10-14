---
name: agentic-mima
description: Turns plain-English shotgun metagenomics requests into validated, reproducible MIMA pipeline plans and commands (QC, Kraken2/Bracken taxonomy, HUMAnN3 functional profiling, taxonomy bar plots, alpha/beta diversity, differential abundance, Random Forest classification) for the MIMA Apptainer container on an OpenPBS cluster, then guides confirmed job submission, monitoring, output checks, troubleshooting and reporting. Use whenever the user wants to run, plan, submit, monitor, check or interpret a microbiome or metagenomics analysis, mentions MIMA, manifest.csv, CleanReads, Bracken, feature tables or PBS scripts, asks to compare healthy control and disease groups, asks why a PBS job failed, or wants a QC, taxonomic or functional profile of shotgun sequencing data, even if they do not name the pipeline. Do NOT use for 16S amplicon analysis, genome assembly or binning, or general Python or R coding.
compatibility: Needs bash and Python 3.6 or later (standard library only) for the bundled scripts; Gadi's default python3 is 3.6.8. Targets the MIMA container (mima4_v2.0.0) with Apptainer and OpenPBS. Submitting, monitoring and output checks need an execution tool with access to the cluster, or the project's MCP server (tools named mima_*); without either the skill gives the user the commands to run.
metadata:
  version: 0.3.0
  category: workflow-automation
---

# Agentic MIMA

Translate a plain-English microbiome request into a validated MIMA plan, and take it from commands through confirmed job submission, monitoring, output checking, troubleshooting and a reproducible report. The LLM never invents pipeline syntax: every command comes from `scripts/build_command.py`, which only knows approved modules and checks every parameter. Anything that changes the cluster needs the user's explicit approval.

## Execution capability

Pick the mode before acting and tell the user which one you are in and why, so they know what you can and cannot do. Cluster actions (running commands, `qsub`, `qstat`, reading output directories) work only through an execution tool with access to the cluster.

| If you have | Mode | What you do |
|---|---|---|
| An execution tool on the cluster (a login-node terminal), or the project's MCP server (tools named `mima_*`) | Cluster | Run the scripts yourself where a step says so (with the MCP tools, as set out in `references/mcp-tools.md`) and keep the run folder (`references/job-submission.md`). Submission still follows Step 7 |
| An execution tool without cluster access (a sandbox) and files the user provided | Local | Run the builder and the validators on the provided files only. Give the user every cluster command to run and ask for the output |
| No execution tool | Advice | Write the plan file and give the user the exact validator and builder commands to run; ask them to paste the output. Do not write the final commands by hand |

If tools named `mima_*` are listed, use them in place of the shell commands in the steps below and read `references/mcp-tools.md` first: it maps every command to its tool and explains the result statuses. The rules and checks do not change.

Never say a job was submitted, finished or succeeded unless a tool result or pasted output shows it.

## Critical rules

1. Generate every command with `scripts/build_command.py`. Never hand-write an `apptainer` command or add a parameter that is not in `references/module-parameters.md`.
2. Never guess inputs. Database paths, the host genome, PBS config files, the group column and its levels must come from the user.
3. Use absolute paths, and state every default and assumption in the reply.
4. Never run a MIMA command (generator or direct run) until the user has seen it and approved running it. These commands write into the user's directories.
5. Never submit a job without the confirmation protocol in `references/job-submission.md`: validate, dry run, show the user the exact jobs, receive an explicit yes, then execute. Submit only through `scripts/submit_jobs.py`, whose default mode makes a person type a confirmation at a terminal; hand the user the command to run. Never use `--approval harness` to bypass that.
6. Never delete, hold or alter jobs, edit generated scripts to pass validation, or resubmit without the user's agreement. Give them the command instead.
7. Monitoring is read-only. Base every statement about a job or output on tool output, and quote file names and lines.
8. Write for a non-specialist: the users include clinicians, wet-lab researchers and students. Say what something means in plain language first, define each technical term once, then give the numbers. Explain errors the same way.
9. If a request is outside the approved modules (assembly, 16S, custom statistics, other tools), say so and stop. If a validator reports an error, fix the input or ask; never edit the output to make it pass.

## Instructions

### Step 1: Identify the stages

Map the request to stages. For ordering and outputs, read `references/workflow-dependencies.md`.

| The user asks for | Stage and module |
|---|---|
| quality control, clean reads, host removal | `qc` (`mima-qc`), optional `qc-report` |
| taxonomic profile, which species or genera, abundance table | `taxa` (`mima-taxa-profiling`), then `taxa-tables`, optional `taxa-report` |
| functional profile, genes, pathways | `func` (`mima-function-profiling`), then its feature-table job |
| bar plots, top taxa, composition by group | `vis-barplot` (`mima-vis-taxa`) |
| alpha or beta diversity, differential abundance, compare groups | `diversity` (`mima-visualisation`) |
| classify, predict, random forest | `classifier` (`mima-classifier-RF`) |
| full analysis, "everything", compare taxonomic and functional profiles of groups | `qc`, then `taxa` and `func`, then `taxa-tables` and `func-tables`, then `diversity` on the taxonomy table and on the function tables |

`qc` must finish before `taxa` and `func`. If the user already has clean reads or feature tables, start from the stage they need and say which earlier stages you assumed complete. `mima-qc`, `mima-taxa-profiling` and `mima-function-profiling` only generate PBS scripts; nothing is analysed until those scripts are submitted. Tell the user which commands are generators and which run directly.

### Step 2: Collect missing inputs

Check the conversation first, then ask once for everything still missing, using `references/module-parameters.md` for exact requirements. Where you have file access, log each value the user chooses and each default they accept in `decisions.md` in the run folder:

- Reads directory, `manifest.csv`, host reference genome (QC)
- Kraken2/Bracken database directory, read length, threshold (taxonomy). Ask for the sequenced read length rather than assuming 150; on the shared GTDB database only 50, 100 and 150 have Bracken files, so run `build_command.py --check-fs` before submitting. Use `--threshold` 100 (project default, pending the supervisor's confirmation) unless the user chooses another value, and state it: the container default is 1000. Estimate the read length with `scripts/estimate_read_length.py` and keep one value for the cohort. Samples below the threshold drop out of the tables. Defaults, policies and wording are in `references/lab-defaults.md`
- HUMAnN chocophlan, uniref, utility_mapping and MetaPhlAn database directories (function). The container has MetaPhlAn 4.1.0; the tutorial example uses the older vJan21 database with `++index`, and `--mpa3` exists for MetaPhlAn 3 databases. Which combination works has not been tested, so ask which database the user has and say that this is unconfirmed (`references/lab-defaults.md`).
- A `pbs_header_*.cfg` per generator stage, and an output directory
- Metadata file, grouping column and levels, and the feature table or tables (analysis). Use the `_relAbund` Bracken table for taxonomy (`_counts` is also accepted; say which); HUMAnN pathway or gene tables are accepted for function, but a `# Gene Family` header is rejected (see `references/troubleshooting.md`). The group column must be categorical, not numeric. For `mima-classifier-RF` the `-c` column is found by pattern matching, so its name must not be part of another header (`validate_metadata.py` warns), and the module is built for two groups. Ask whether any person was sampled more than once: if so, run `validate_metadata.py --subject-column`, and stop for `mima-visualisation` (repeated samples are out of scope; name MaAsLin2 or ANCOM-BC2 as alternatives you do not run). Ask for covariates such as age, sex, BMI and batch, and check them with `--covariate`. Group sizes below 4 are descriptive only and below 8 exploratory (`references/lab-defaults.md`). For taxonomy jobs pass the sample count to `validate_pbs_config.py --samples`

### Step 3: Validate the input files

```bash
python3 scripts/validate_manifest.py MANIFEST.csv
python3 scripts/validate_metadata.py METADATA.tsv --group-column lab --groups labA labB labC --feature-table FEATURE_TABLE [FEATURE_TABLE ...]
python3 scripts/validate_pbs_config.py pbs_header_taxa.cfg --module taxa
```

If the user uploaded the file, run the validator. If it lives on the cluster and you cannot see it, give the user the command (add `--reads-dir DIR --check-fs` for the manifest) and ask for the output. Expected output: each validator ends with `RESULT: VALID` (exit code 0), with any warnings listed above it. Exit code 1 means errors: explain each with its fix (`references/validation-rules.md` gives the reason for every rule). Do not treat unvalidated files as valid.

### Step 4: Build and validate the commands

Write a plan file, one step per module in run order, and run the builder:

```json
{"steps": [
  {"module": "mima-qc",
   "params": {"-i": "/abs/raw", "-o": "/abs/out", "-m": "/abs/manifest.csv",
              "--ref": "/abs/GRCh38.fna", "--mode": "container",
              "--pbs-config": "/abs/pbs_header_qc.cfg"}}
]}
```

```bash
python3 scripts/build_command.py plan.json
```

- Always set `--mode container` on generator modules.
- `mima-vis-taxa` takes positional values keyed by name (`feature_table`, `metadata`, `group_spec`, `output_dir`, `prefix`, `top_n`, `figure_width`, `figure_height`).
- `mima-visualisation` `--feature-table` and `mima-classifier-RF` `-i` take a JSON list; the builder joins them as each tool expects (spaces, and commas without spaces).
- Expected output: each step prints `OK` followed by a ready-to-run command, and the exit code is 0. Fix the plan and rerun until it is; if the same error persists after two fixes, stop and ask the user. The builder cannot see the cluster: when paths were not checked, tell the user to rerun with `--check-fs` there.

### Step 5: Present the plan and commands

Use `assets/plan-template.md`: goal, inputs and their validation status, ordered steps with their kind, assumptions and defaults, the builder's commands unchanged, and checks left for the cluster. Ask whether to run the first stage's commands. Stop here if the user only wanted a plan.

### Step 6: Validate the generated PBS scripts

After a generator has run, check what it wrote before anything is submitted:

```bash
python3 scripts/validate_pbs_scripts.py OUT/QC_module --check-fs
```

It catches leftover placeholders (an unedited `APPTAINER_BIND` or `IMAGE_DIR`), missing resources, CRLF, unsafe commands and missing paths. Expected output: every script `OK` and `0 invalid`. Any error stops the run: fix the config, regenerate, validate again. Generators for `taxa` and `func` wait until QC has finished, because they read `CleanReads`.

### Step 7: Submit, only with confirmation

Read `references/job-submission.md` and follow the protocol exactly: validate, write the submission plan, run the dry run, show the user the jobs and resources, ask a direct yes/no question, then give the user the dry run's `--execute` command to run in their terminal, where they type the confirmation. Expected output: the dry run ends with `DRY RUN ONLY` and a token; the real run, after the person types the confirmation, prints one `submitted as JOBID` line per job and writes the record file. Report the PBS job ids and the record file.

### Step 8: Monitor

Read `references/job-monitoring.md`. Run `scripts/check_job_status.py --record ...` (expected output: one line per job and an `OVERALL:` summary), explain each status, and flag jobs that used 90% or more of their walltime or memory. Estimate how long jobs should take, tell the user, and ask them to come back instead of polling in a loop. Respect the stage gates before moving on.

### Step 9: Verify outputs and troubleshoot

When jobs finish, run `scripts/check_outputs.py --stage STAGE --dir DIR [--manifest M]` and `scripts/scan_logs.py DIR` (expected output: `RESULT: COMPLETE` and no error signatures; add `--format json` to keep structured results in the run folder). A job that exited 0 can still leave samples missing. For any failure, follow the diagnosis procedure in `references/troubleshooting.md`: first error, evidence with file and line, proposed fix, then wait for the user. Rerun only what failed, through the same confirmation protocol.

### Step 10: Interpret and report

Read `references/output-interpretation.md` before describing results. Report adjusted results first, with the test, groups, sample sizes and caveats, and no causal claims. Call `mima-visualisation` results preliminary associations (no covariate adjustment), say "adjusted" means adjusted for multiple testing only, and use the dropped-sample wording and reporting defaults in `references/lab-defaults.md`. For a report, copy `assets/report-template.md`, fill every field from a file or record in the run folder, and mark anything unavailable as such. Produce Markdown unless the user asks for another format. Then run `python3 scripts/check_report.py report.md --stages STAGES_RUN --run-dir mima_run` (expected output: `RESULT: READY`), fix every error, and rerun. If the same error remains after three rounds, stop and ask the user for the missing information instead of guessing it.

## Examples

**Example 1: full comparison, plan only**
User says: "Run microbiome analysis for my gut shotgun data to compare healthy controls and disease."
Actions: stages `qc`, `taxa`, `func`, `taxa-tables`, `func-tables`, `diversity` (taxonomic and functional); ask once for all missing paths and the group column; validate the manifest, metadata and PBS configs; build the plan; present it.
Result: a validated ordered plan and commands, with generators identified and nothing run.

**Example 2: separate steps (QC, then a taxonomic profile)**
User says: "Run quality control on these samples and generate a taxonomic profile."
Actions: stages `qc` then `taxa` (its `-i` is `OUT/QC_module/CleanReads`), then `taxa-tables`; ask for the missing paths; validate the manifest and PBS configs; build and present the commands, noting that the taxonomy scripts are generated only after QC has finished.
Result: validated commands for the requested steps only, in dependency order.

**Example 3: group comparison on existing tables**
User says: "Compare alpha and beta diversity of the control and disease groups and give me statistics and plots."
Actions: stage `diversity` only, with processing stages assumed done; ask for the feature table and metadata; run `validate_metadata.py` with the group column and levels; use the `_relAbund` taxonomy table (add HUMAnN tables for function); build.
Result: one validated `mima-visualisation` command, with the group column and levels checked against the metadata.

**Example 4: submit after generation**
User says: "The QC scripts are generated, go ahead and submit them."
Actions: run `validate_pbs_scripts.py --check-fs`; write the submission plan; dry run; show 3 jobs with their resources and the `qsub` commands; ask "Submit these 3 jobs now?"; on a clear yes, give the user the dry run's `--execute` command to run in their terminal and tell them to type `submit 3` when asked; once they report back, check the record.
Result: jobs submitted exactly as reviewed, by the person's own confirmation, with a record file. If the answer is anything other than yes, or the person does not type the confirmation, nothing is submitted.

**Example 5: a failed job**
User says: "My taxonomy job died, what went wrong?"
Actions: `check_job_status.py` (exit 271, memory 98% of request); `scan_logs.py` finds a memory kill; explain the cause with the log line; propose raising `mem` in the config and regenerating; wait for approval.
Result: a diagnosis tied to evidence, a proposed fix, no job altered or resubmitted yet.

**Example 6: interpretation**
User says: "What do these diversity results mean?"
Actions: run `check_outputs.py --stage diversity`; count the plots in `corrected_sig`, `non_sig` and `uncorrected_sig`; read the PERMANOVA tables; summarise with tests, groups and n.
Result: adjusted findings first, uncorrected ones labelled exploratory, limitations stated.

## Troubleshooting the skill itself

**Builder says a path is not absolute or a module is not approved**
Ask for the full path. For a module outside the pipeline, explain the limit; extend only by editing `scripts/command_specs.json` and running `scripts/generate_module_docs.py`.

**`submit_jobs.py` refuses the token**
Something changed since the dry run. Rerun the dry run, show the user again, and use the new token.

**`qstat` or `qsub` not found**
No PBS on this machine. Give the user the commands and ask for pasted output.

**Python is not available**
Tell the user which command to run elsewhere and ask for the output; do not skip validation.

## Where to read next

| When | Read |
|---|---|
| Lab defaults and policies (threshold, read length, databases, sizing, group size, claims, wording) | `references/lab-defaults.md` |
| Parameters for a module | `references/module-parameters.md` |
| Stage order, outputs, environment | `references/workflow-dependencies.md` |
| Explaining a validation error | `references/validation-rules.md` |
| `mima_*` tools are listed | `references/mcp-tools.md` |
| Before submitting | `references/job-submission.md` |
| After submitting | `references/job-monitoring.md` |
| Something failed | `references/troubleshooting.md` |
| Explaining or reporting results | `references/output-interpretation.md`, `assets/report-template.md`, then `scripts/check_report.py` |

`scripts/command_specs.json` is the single source of truth for approved modules; `scripts/error_patterns.json` holds the log signatures. Mark anything not confirmed against the container as UNVERIFIED.
