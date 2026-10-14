# MIMA analysis report template

How to use this template (delete this section from the finished report):

- Fill each field from a file or record in the run folder, and write the source in the "Source" column or in brackets. A number with no source does not belong in the report.
- Replace every `[FILL: ...]` placeholder. If the information does not exist, write "Not available" and say why; do not estimate.
- Include a section only if that stage was run. Delete the others rather than leaving them empty.
- Describe results using the rules in `references/output-interpretation.md`: adjusted results first, test and sample sizes stated, no causal claims.
- Format: Markdown by default. Produce a Word or PDF file only when the user asks for one.

---

# [FILL: title, e.g. "Shotgun metagenomic profiling of control and disease stool samples"]

Prepared: [FILL: date]   Prepared for: [FILL: name or project]
Run folder: [FILL: absolute path to mima_run/]

## 1. Summary

[FILL: 3 to 5 sentences. The question asked, the data used (number of samples, groups), the stages run, and the main findings with the key numbers. Every statement here must appear in a later section with its source.]

## 2. Objective and design

- Question: [FILL: the user's request in their words]
- Groups and comparison: [FILL: grouping column, levels, samples per level] (source: metadata file, `validate_metadata.py` output)
- Study design caveats: [FILL: e.g. small group sizes, batch or site structure, paired or unpaired]

## 3. Data

| Item | Value | Source |
|---|---|---|
| Sequencing data | [FILL: platform, paired-end, read length if known] | user |
| Samples | [FILL: n] | manifest.csv |
| Raw data location | [FILL: path] | plan.json |
| Manifest check | [FILL: VALID, warnings] | `validate_manifest.py` |
| Metadata check | [FILL: VALID, warnings] | `validate_metadata.py` |

## 4. Methods

State exactly what was run, with versions and parameters. This section must be enough for someone else to repeat the analysis.

### 4.1 Software and references

| Component | Version or identifier | Source |
|---|---|---|
| MIMA container | [FILL: image name and version, e.g. mima4_v2.0.0 plus build date] | user / `apptainer` |
| HUMAnN, MetaPhlAn | [FILL] | container / user |
| Host reference genome | [FILL: file name and release] | user |
| Kraken2/Bracken database | [FILL: name and release] | user |
| HUMAnN and MetaPhlAn databases | [FILL: versions] | user |
| Scheduler | [FILL: PBS flavour and site, if known] | user |

### 4.2 Stages and parameters

For each stage run, give the module, every parameter including defaults, and the job resources.

| Stage | Module | Parameters (with defaults marked) | Resources requested |
|---|---|---|---|
| [FILL: qc] | [FILL: mima-qc] | [FILL] | [FILL: ncpus, mem, walltime] |

Commands as validated by `build_command.py` (copy from `build_output.txt`):

```
[FILL: commands]
```

### 4.3 Execution record

| Job | PBS id | Submitted | Status | Walltime used / requested | Memory used / requested |
|---|---|---|---|---|---|
| [FILL] | [FILL] | [FILL] | [FILL] | [FILL] | [FILL] |

Source: `submission_record.json` and `status_*.json`. If scheduler history was unavailable, say that status was inferred from logs and outputs.

### 4.4 Statistical methods

[FILL, only for stages run: alpha-diversity indices and tests (Wilcoxon or Kruskal-Wallis with Dunn post-hoc), beta-diversity metric and PERMANOVA, differential abundance tests, multiple-comparison adjustment, classifier and cross-validation settings. State the significance level used.]

## 5. Results

### 5.1 Quality control

[FILL: number of samples, reads before and after QC (min, median, max), samples flagged and why.] Source: `QC_report.csv`, `check_outputs.py --stage qc`.

### 5.2 Taxonomic profile

[FILL: Bracken threshold used, samples retained in each feature table, samples removed by the threshold (named), headline composition at the chosen rank.] Source: `bracken-summary.tsv`, `check_outputs.py --stage taxa-tables`.

### 5.3 Functional profile

[FILL: tables produced, regrouping or mapping used, samples included.] Source: `check_outputs.py --stage func-tables`.

### 5.4 Group comparisons

[FILL, per comparison: groups and n, test, adjusted result, direction of effect, files.] Present adjusted results first and label results significant only before adjustment as exploratory.

### 5.5 Classification

[FILL: input tables, classes and sample counts, AUC per input and fold count, which input performed best.] Source: `roc_*` PDFs.

## 6. Quality checks and deviations

- Validation results: [FILL: every warning raised, and what was done about it]
- Samples excluded or lost: [FILL: names and reasons]
- Failed or rerun jobs: [FILL: what failed, cause with evidence, what was changed, who approved the rerun]
- Unconfirmed items: [FILL: e.g. parameters not verified against the container]

## 7. Limitations

[FILL: sample size, unbalanced groups, batch effects, threshold effects on sample retention, reference database coverage, reliance on a single classifier validation, anything flagged as heuristic. Keep each limitation tied to this analysis.]

## 8. Reproducibility

- Run folder contents and where they are stored: [FILL]
- Plan and command files: `plan.json`, `build_output.txt`, `submission_plan.json`, `submission_record.json`
- Script versions: skill `agentic-mima` [FILL: version from SKILL.md]
- To repeat: [FILL: the ordered list of commands, from the validated plan, with the same databases and container]

## 9. Files delivered

| File | Description |
|---|---|
| [FILL: path] | [FILL] |
