# Troubleshooting

Read this when a job failed, an output is missing, or a result looks wrong.

## Contents
- Diagnosis procedure
- Rerunning safely
- Symptoms and fixes
- Known issues
- Extending the catalogue

## Diagnosis procedure

1. **Establish what happened.** `check_job_status.py --record ...` for the exit status and resource use, then `check_outputs.py --stage ...` for what is missing.
2. **Read the logs.** `scan_logs.py JOB_DIRECTORY` matches log lines against `scripts/error_patterns.json` and prints a cause and fix with the file and line. Each finding says whether its pattern is verified against a real log; an UNVERIFIED pattern is a lead to confirm in the log text, not a diagnosis. A failure with no match is still a failure: open the end of the PBS log (`JOBNAME.oJOBID`) and the per-sample log yourself.
3. **Find the first error, not the last.** Later errors are often consequences. The `file-not-found` pattern is a warning for this reason.
4. **Tie it to a stage and a cause.** Use the table below, then say what you found, what evidence supports it (file and line), and how confident you are.
5. **Propose the fix and wait.** Give the user the exact change and the exact commands. Do not change scripts, configs or data, and do not resubmit, until they agree.

## Rerunning safely

- Rerun only what failed. If 2 of 9 function jobs failed, only those two scripts go in the new plan.
- Resource failures (walltime, memory) are fixed in the stage's `pbs_header` config. The generated `.pbs` files copy that header, so regenerate the scripts, re-validate, and follow the confirmation protocol in `job-submission.md`.
- A script that was already submitted needs `--allow-resubmit`, and only when the user asked for the rerun.
- Remove partial outputs of the failed sample before rerunning it, after telling the user which files they are.
- The QC generator fails if its output directory already has subdirectories, so regenerating QC needs a clean `-o`.

## Symptoms and fixes

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| `mima-classifier-RF` fails right after reading the metadata, or gives odd class labels | `-c` matched more than one column, because the code finds the column by pattern matching (code inspection) | `validate_metadata.py` warns when the name is part of another header | Rename the columns or use a metadata copy without the other matching columns |
| Builder says `--pbs-config` is missing | `--mode container` needs a header file | the command | Provide the stage's `pbs_header_*.cfg` |
| Generated `.pbs` contains `/path/to/...` or `placeholder text` | The config's `APPTAINER_BIND`, `IMAGE_DIR` or other lines were not edited | `validate_pbs_config.py`, `validate_pbs_scripts.py` | Edit the config, regenerate |
| Job exits at once; log shows `command not found` | Apptainer is not on the compute node PATH | `scan_logs.py` | Add the site's module load line to the config; regenerate |
| `container creation failed` / mount error | A bind source path does not exist or is missing | `APPTAINER_BIND` | Fix the paths; bind every directory with reads, references and outputs outside the home directory; regenerate |
| Reads or database not found inside the container | Directory not bound | log `No such file or directory` | Add the directory to `APPTAINER_BIND` (the tutorial calls this out for reads outside the home directory) |
| QC generator fails immediately | `-o` already contains subdirectories | `ls` of the output directory | Use a new or empty output directory |
| QC samples missing from outputs | Manifest names do not match files, or CRLF in the manifest | `validate_manifest.py` | Fix the manifest and rerun QC for those samples |
| `QC_report.csv` shows `missing QC log` or `missing JSON log` | The report step was run outside `QC_module`, or logs are absent | where it was run; `*_qc_module.log` and `QCReport/*.json` exist | Run from `QC_module` after all QC jobs finish (the builder adds the `cd`) |
| Bracken summary or Bracken log reports an error | Wrong `--read-length` for the database, or a database problem | `scan_logs.py` (`bracken-kmer`) | Use a read length whose database file exists; regenerate the taxonomy scripts. See also the known issues |
| Kraken2 killed or stuck queued | Needs about 300GB memory | `#PBS -l mem` in the config | Request enough memory; large requests wait longer |
| Sample missing from a feature table | Removed by the abundance threshold | `bracken-summary.tsv`, `--threshold` | Not an error: report it. Lower `--threshold` only if the user wants those samples kept, and rerun from the Bracken step |
| Analysis step fails or drops samples | Sample IDs differ between metadata and feature table, or group values differ in case | `validate_metadata.py` | Fix the metadata; group names are case-sensitive |
| Group comparison error, "1 level" | The grouping column has fewer than 2 levels after filtering | `validate_metadata.py` | Choose another column or check which samples remain in the table |
| `mima-visualisation` fails with `Could not detect data matrix format` | Header cell is `# Gene Family`, which the table reader rejects (confirmed) | `validate_metadata.py`, `check_outputs.py --stage func-tables` | Use a copy with `# Gene` as the first header cell; see known issues |
| `mima-visualisation` ends with `Input data must be a numeric vector or matrix` after beta diversity | Reproduced without MIMA data (22 Oct 2026): the container's call form hands a data frame to `calc_shannon`, which accepts only vectors or matrices. Not the data, not `taxhead` | `scan_logs.py` (`calc-shannon-input`) | Report alpha as unavailable; keep other outputs that exist; the fix is in the container (MIMA maintainers) |
| `mima-visualisation` fails at CCA/RDA with a numeric group column | A continuous variable such as Age was given as `--study-groups` | `validate_metadata.py` | Bin it into labelled groups in a new metadata column |
| Bracken fails with `--read-length 75` | No `database75mers.kmer_distrib` in the database (shared GTDB database has 50, 100, 150) | `build_command.py --check-fs` | Use a read length whose file exists; regenerate the taxonomy scripts |
| Taxonomy or QC generator exits 0 but the PBS script is empty | Suffix does not match the file names, so no samples were found | `validate_pbs_scripts.py --check-fs` | Correct `--fwd-suffix` and `--rev-suffix` and regenerate |
| Functional jobs time out | HUMAnN needs more than 8 hours for the sample | `check_job_status.py` | Increase walltime in the config and regenerate |
| MetaPhlAn or HUMAnN cannot use a database | Version mismatch: the container with MetaPhlAn 4.1.0 and HUMAnN 3.8 needs newer databases | database versions | Use matching databases, or `--mpa3` where the tutorial says it applies |
| Scripts fail with `$'\r': command not found` | CRLF line endings | `scan_logs.py` (`crlf-script`) | `sed -i 's/\r$//' FILE` on the config, manifest and scripts; regenerate |

## Known issues

**Gene-family tables are rejected by `mima-visualisation`.**
Confirmed on Gadi (container `mima4_v2.0.0_20240409`): the table reader (`models.py`) accepts the first header cells `# Taxonomy`, `name`, `# Pathway` and `# Gene`, and raises `ValueError: Could not detect data matrix format` for `# Gene Family`, which standard HUMAnN gene-family tables use. Workaround, to be shown to the user and run by them or with their approval: write a renamed copy, for example `sed '1s/^# Gene Family/# Gene/' IN.tsv > OUT.tsv`, never in place, record it in `decisions.md`, and pass the copy. Whether the renamed copy runs through all analyses has not been tested. The permanent fix is in the container's reader (a prefix test), for the MIMA maintainers.

**Alpha diversity stops with `Input data must be a numeric vector or matrix` (cause found in the package; container fix pending).**
Observed on Gadi with both `_relAbund` and `_counts` tables: beta diversity completes, then `calc_shannon` fails. Reproduced on 22 Oct 2026 inside container `mima4_v2.0.0_20240409` (otuSummary 0.1.2, R 4.2.1) with a plain count matrix and no MIMA data:
- `alphaDiversity(otutab = m, siteInCol = TRUE, taxhead = "taxonomy")` (the form `alpha-diversity.R` uses) fails with this error.
- The same call without `taxhead` fails too, so `taxhead` is not the cause (an earlier reading of this page blamed it; that was wrong).
- `alphaDiversity(otutab = t(m), siteInCol = FALSE)` works.
- The same numbers passed as a data frame with `siteInCol = FALSE` fail; as a matrix they work.
- Reading the package source: with `siteInCol = TRUE` the table goes through `typeConvert()`, which returns a data frame of integer columns; `calc_shannon` stops unless `is.numeric(data)` is TRUE, and R's `is.numeric()` is FALSE for a data frame.

So the call form in the container cannot work with this package version, whatever table it is given. It is not caused by the user's data and cannot be prevented by input preparation. The fix belongs in the container (the call in `alpha-diversity.R`, or the package version) and is for the MIMA maintainers. Not yet shown: that the `siteInCol = FALSE` matrix form gives the intended results on real MIMA tables, and why the container's authors saw it work (possibly a different package version; unverified).

What to do: do not retry with other inputs, tell the user alpha diversity did not complete, and report only outputs that exist. Beta diversity and differential abundance outputs from the same run can still be used. To test the workaround, edit a copy of the script and bind it over the original (or use a writable sandbox). Never edit the shared container.

**Bracken fails on the shared GTDB-based database.**
Two causes are known. (1) A read length with no `databaseNmers.kmer_distrib` file (75 on the shared database): checked by `build_command.py --check-fs`. (2) Non-standard rank codes (for example a `C1` sub-rank) reported earlier for this project: the log line and fix are not recorded, and `nodes.dmp` of the shared database holds only standard ranks (domain, phylum, class, order, family, genus, species), so treat this cause as unconfirmed.

**Dependencies on finished jobs.**
`qsub -W depend=afterok:ID` fails if the server no longer knows the job. Omit the dependency once the earlier job has succeeded.

## Extending the catalogue

When a new failure is found and fixed:

1. Add the symptom to the table above.
2. Add a pattern to `scripts/error_patterns.json` with a unique `id`, a regex that matches one log line, `severity`, `confidence`, `cause` and `fix`.
3. Run `python3 scripts/scan_logs.py` on a real log containing the error to confirm it matches, and on a clean log to confirm it does not.
4. Only then set `"verified": true` on the pattern. Leave it `false` for patterns that came from general knowledge.
