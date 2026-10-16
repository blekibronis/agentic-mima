# Lab defaults and policies

Read this when choosing a value the tutorial leaves open, or when wording results. Every item was proposed by the project owner (Rafael) and is pending confirmation by the supervisor, Dr Jiang. Until then, state the value you used and say it is a project default. The user can override any of them; record the override in `decisions.md`.

## Contents
- Taxonomy settings
- Reference databases
- Job sizing
- Group size and design
- What the analysis can claim
- Reporting defaults
- Wording for dropped samples

## Taxonomy settings

| Item | Default | Notes |
|---|---|---|
| `--threshold` | 100 | The tutorial's example value. The container default is 1000. Keep one value for a whole project. Report it and the number of samples dropped. On Gadi (GTDB r95, `-r 150`, species level) a test set kept 4,809, 1,724 and 627 species at 10, 100 and 1000 |
| `--read-length` | nearest available length to the median per-sample mean read length, per mate (so 2x150 is 150) | Mean 75 or less gives 50; 76 to 125 gives 100; 126 or more gives 150 (ties go lower). Use one value for the whole cohort. Reads above 150 bp (for example 2x250) are an approximation, because the shared database has no longer distribution. Run `scripts/estimate_read_length.py`, then `build_command.py --check-fs`. This is a project rule, not a Bracken rule |

## Reference databases

Approved: `/g/data/mp96/REFDB/GTDB_Kraken` (GTDB r95) for bacterial and archaeal gut and stool metagenomes (`scripts/approved_databases.json`). It dates from 2020, so newer species may be missing, and GTDB names differ from NCBI names. Fungi or protozoa need a different database (an NCBI PlusPF-type one) that the lab must approve. Viruses need a dedicated approach, not Kraken2. `build_command.py` warns for any other path.

## Job sizing (taxonomy)

| Item | Default |
|---|---|
| Memory | 300 to 320 GB whatever the cohort size, because the database sets it. Use 320 GB for margin |
| CPUs | 16 to 28 |
| Walltime | 20 minutes per sample (15M to 20M read pairs on 28 cores) plus 1 hour. For example 10 samples need 4:20 and 30 samples need 11:00. Recommended: 1.5 times the per-sample estimate when that is longer, because a killed job wastes the queue wait |

QC and function profiling have no lab sizing. For the prototype, `make_pbs_header.py` takes the tutorial's figures as estimates: QC 8 CPUs, 64 GB and 40 minutes per sample (the tutorial runs 3 samples per job in 2 hours); function profiling 8 CPUs, 64 GB and 8 hours per sample (the module's own default is 24 hours). Walltime is scaled up when the largest sample's gzipped read files exceed 5 GB (a rough assumption for about 20M read pairs of 2x150), times a 1.5 margin, capped at 48 hours. These are uncalibrated and unreviewed by the supervisor.

The 20 minutes is the project owner's figure and has not been measured by this skill. Calibrate it on the first real run. Check the queue's memory and walltime limits on the cluster. `validate_pbs_config.py --module taxa --samples N` applies this guideline.

## Group size and design

For two groups, the smallest p-value an exact rank test can reach is 2 / C(2n, n): 0.10 at 3 per group, 0.029 at 4, 0.0079 at 5. With FDR over 600 species, a single perfectly separating species reaches adjusted p of about 0.09 at 8 per group and 0.006 at 10.

| Per group | Status |
|---|---|
| Below 4 | Descriptive only: no test can reach p < 0.05, so do not claim a difference |
| 4 to 7 | Exploratory: report effect sizes and call findings preliminary |
| 8 or more | Usable minimum for differential abundance with FDR |
| 10 or more | Recommended |

Repeated or paired samples (several samples from one person or household) are out of scope for `mima-visualisation`, which treats samples as independent. Warn, do not analyse, and name alternatives the skill does not run: MaAsLin2 or ANCOM-BC2 with a subject random effect, or PERMANOVA with restricted permutations. `validate_metadata.py --subject-column` enforces this.

## What the analysis can claim

Treat `mima-visualisation` results as unadjusted, one-variable-at-a-time screening: preliminary associations. "Adjusted p-value" here means adjusted for multiple testing, not for confounders such as age, sex, BMI or sequencing batch. If such covariates differ between the groups (`validate_metadata.py --covariate`), say the findings need multivariable follow-up (MaAsLin2 or ANCOM-BC2), which the skill does not run. Wilcoxon tests on relative abundances are compositional, so say so.

## Reporting defaults

- Rank: species is the default for diversity and differential abundance, with genus reported alongside as a consistency check. Phylum is for composition bar plots only. Run species and genus as separate runs with separate output directories.
- Alpha diversity: observed richness, Shannon and inverse Simpson. Do not emphasise Chao1 or Chao2, which assume integer counts with singletons and are computed here from pseudo-counts. State read depth per group, because observed richness depends on depth and the module does not rarefy.
- Beta diversity: Bray-Curtis PCoA or NMDS with the PERMANOVA statistic (R-squared and p, if the results table has them) and say that dispersion was not tested.

## Function profiling and classifier (open)

- MetaPhlAn database: the container has MetaPhlAn 4.1.0 (default database vJun23). The tutorial example uses vJan21 with `++index`, and the module has `--mpa3` for MetaPhlAn 3 databases. No lab standard has been chosen and no combination has been tested in HUMAnN 3.8; ask the user, and say it is unconfirmed.
- Random Forest: report the AUC range over the fold counts for the table chosen in advance, with its confidence interval, not the best of all tables and fold counts. The module has no tuning, no stratified folds and no independent test set, so call the result a preliminary estimate. A minimum sample size and an external hold-out policy are lab decisions not yet made.
- QC: the tutorial sets no acceptance threshold. The host-removal step runs minimap2 without the short-read preset, so some host reads may remain.

## Wording for dropped samples

Say, in plain words: "N samples had no species above the detection cut-off of 100 reads and are left out of the taxonomic profile. This reflects low microbial read depth, not an absence of microbes." Name the samples and their groups. If the dropped samples come mostly from one group, say that the comparison may be biased.
