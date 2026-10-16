# Output interpretation

Read this when the user asks what their results mean, or before writing the report. It says what each output is, what to check first, and how to word findings without overclaiming.

## Contents
- Ground rules for interpretation
- QC
- Taxonomy profiling
- Function profiling
- Taxonomy bar plots
- Alpha, beta and differential abundance
- Random Forest classification
- Reading figures

Uppercase words in file names (SAMPLE, RANK, N) stand for a sample name, a taxonomic rank or a number.

## Ground rules for interpretation

1. Quote numbers from files, never from memory. Name the file each number came from.
2. The tutorial gives no pass or fail thresholds for QC or classifier performance. Do not invent any. Where this skill uses a rule of thumb it is marked "heuristic"; present it as a prompt to look, and ask the user what their acceptance criteria are.
3. Report adjusted results first. A result that is significant only before multiple-comparison adjustment is exploratory. "Adjusted" means adjusted for multiple testing, not for confounders: `mima-visualisation` tests one variable at a time with no covariates, so call its findings preliminary associations, and say they need multivariable follow-up when age, sex, BMI or batch differ between groups. Group-size tiers, reporting defaults and the wording for dropped samples are in `lab-defaults.md`.
4. State the test, the groups, the sample size per group and the correction every time you report a significant result.
5. Associations are not causes. Do not claim a taxon or pathway drives a phenotype.
6. A non-significant result is not evidence of no difference, especially with small groups.
7. Write for a reader without bioinformatics training. Lead with what the result means for the question asked, define each technical term in one sentence the first time (for example, PERMANOVA is a test of whether groups differ in overall community composition), then give the statistics and file names.
8. Statements marked (general practice) come from standard statistical practice, not from the MIMA tutorial.
9. If a validator or `check_outputs.py` flagged a problem (dropped samples, small groups, an unrecognised table), say so next to the result it affects.

## QC

Files (in `QC_module/`): `CleanReads/SAMPLE_clean_1.fq.gz` and `_clean_2.fq.gz` (the reads used by every later stage), `QCReport/SAMPLE.json` and `.outreport.html` (fastp), `SAMPLE_qc_module.log`, and, after `mima-qc-report`, `QC_report.csv`.

`QC_report.csv` columns (read from the container code): `SampleId`, `log_status`, `Rawreads_seqs`, `Derep_seqs`, `dedup_percentage`, `Post_QC_seqs`, `low_quality_reads(%)`, `Host_seqs`, `Host(%)` and `Clean_reads`. The tutorial shows fewer columns. `dedup_percentage` is fastp's own duplication rate taken from the log, not the share clumpify removed, so it does not equal one minus `Derep_seqs` over `Rawreads_seqs`. `low_quality_reads(%)` is (`Derep_seqs` minus `Post_QC_seqs`) over `Derep_seqs`, times 100. How `Host_seqs`, `Host(%)` and `Clean_reads` are calculated has not been inspected, so report them as given.

- `log_status` should be `OK`. `missing QC log` means no `*qc_module.log` for that sample; `missing JSON log` means no fastp JSON.
- `Clean_reads` against `Rawreads_seqs` is how many reads survived. The tutorial example keeps a small fraction for some samples, so a low retention is not automatically wrong; host removal and quality trimming both discard reads. Compare samples with each other.
- Heuristic: `check_outputs.py` flags a sample whose clean-read count (`Clean_reads`, else `Post_QC_seqs`) is under 10% of the cohort median, and any sample with zero reads. Treat a flag as a reason to inspect that sample, and tell the user it is a rule of thumb.

## Taxonomy profiling

Files (in `Taxonomy_profiling/`): `kraken2/SAMPLE.kraken2.output` and `.report`; `bracken/SAMPLE_RANK` and `SAMPLE.kraken2_bracken_RANK.report` for class, family, genus, order, phylum, species; `SAMPLE_bracken.log`; `bracken-summary.tsv` after `mima-bracken-report`.

`bracken-summary.tsv` has one row per sample and rank (so each sample appears several times), with the columns `sampleID`, `taxon_rank`, `log_status`, `threshold`, `num_taxon`, `num_tx_above_thres`, `num_tx_below_thres`, `total_reads`, `reads_above_thres`, `reads_below_thres`, `reads_distributed`, `reads_not_distributed`, `reads_unclassified`, `logfile` and `output`, all read from the Bracken logs. A good row has a `log_status` starting `ok-`; `no reads found` and `exit error` mark a failed sample to investigate in its `*_bracken.log`. Only samples present in the logs get a row. Counts depend on `--threshold`.

Feature tables (in `featureTables/`): one table per rank, with taxa as rows and samples as columns. The base file `bracken_FT_RANK` holds counts and relative abundances for each sample; `_counts` holds discrete counts and `_relAbund` holds relative abundances. Use `_relAbund` for the bar plot. The `_counts` and `_relAbund` files are Bracken's `_num` and `_frac` columns split out unchanged, and Bracken defines the fraction over the reads kept at that rank after the threshold, so shares are relative to the retained taxa and not to all reads. The script covers six ranks (species, genus, family, order, class, phylum); the tutorial's genus-table preview shows `s__` rows, which is most likely a mislabelled example, so check the first rows of a real genus table. `mima-visualisation` accepts `_relAbund` or `_counts`; say which was used.

Samples can be absent from the feature tables. In the tutorial's example data, 4 of 56 samples fell below the abundance threshold and the final table had 52. Whenever `check_outputs.py` reports samples missing from the tables, say which samples and that the threshold removed them, and give the threshold used.

## Function profiling

Per sample (in `Function_profiling/`): `SAMPLE_combine_genefamilies.tsv`, `_pathabundance.tsv` and `_pathcoverage.tsv` from HUMAnN3. After the feature-table job, `featureTables/` holds seven `merge_humann3table_*` files: gene families (plain, `.cpm`, and `.cmp_uniref90_KO`), pathway abundance (plain and `.cmp`) and pathway coverage (plain and `.cmp`). The HUMAnN3 documentation describes each output in detail.

`generate_func_feature_tables.sh` is written by `func_profiling.py`. It regroups the cpm gene-family table with a KEGG orthology mapping (`humann_regroup_table -g uniref90_rxn -c map_ko_uniref90.txt.gz`); the `cmp` in some file names is a spelling slip for `cpm`, so the KO table is in copies per million. Whether `-c` overrides `-g` has not been tested: the rows of a KO table should start with K numbers. Forward and reverse reads are concatenated before HUMAnN runs, so HUMAnN treats them as one set of reads. The container has MetaPhlAn 4.1.0; the tutorial example uses the older vJan21 database. The special rows UNMAPPED, UNGROUPED and UNINTEGRATED and stratified rows have no special handling in the visualisation scripts, so they are analysed as ordinary features unless removed first; say which choice was made. if the user wants a different mapping, that line is edited before the job is run. State which mapping was used.

Gene family values are reads per kilobase (RPK): they correct for gene length but not for sequencing depth, so compare samples only after normalising (copies per million or relative abundance). The tables also hold special rows (`UNMAPPED`, `UNGROUPED`, `UNINTEGRATED`) and rows split by contributing species; the split rows are a breakdown of the community-level row, so do not analyse them together with it as separate features. Say how these rows were handled (general practice).

Header check: `mima-visualisation` reads tables whose first header cell is `# Taxonomy`, `name`, `# Pathway` or `# Gene`. Gene-family tables from HUMAnN start `# Gene Family`, which the container rejects (confirmed on Gadi). `check_outputs.py` warns; use a renamed copy (see troubleshooting). Pathway tables are not affected.

## Taxonomy bar plots

`mima-vis-taxa` writes, with the chosen prefix, bar charts of per-sample relative abundance for the top N taxa (everything else is grouped as Others) and bar charts of the group-mean relative abundance, each with and without separate groups, plus the tables behind them (`*.table_top_*`, `*.average.top_*`, `*.meta.txt`) and the R scripts that draw them.

The output file names contain the `top_N` value that was given. Only samples in the listed groups are plotted. Describe composition, not statistics: these plots show which taxa dominate and how group means differ. They do not test anything. Say which rank and which N the plot uses. The tutorial's example output names say top 7 although N was 8; this discrepancy has not been explained, so read the legend of the figure rather than the file name to count the taxa (tutorial discrepancy, unresolved). Repeat the command with another feature table for other ranks.

## Alpha, beta and differential abundance

`mima-visualisation` writes one folder block per analysis, named after the table type (the tutorial's taxonomy run uses `Kraken_alpha`, `Kraken_beta`, `Kraken_diff-abundance`). Each `--study-groups` column gets its own set of results, shown in the file names.

**Alpha diversity** (`*_alpha/`). Diversity within a sample (richness, evenness, Shannon, Simpson and related indices; `alpha_diversity_raw_data.txt` has every value per sample). Two groups are compared with the Wilcoxon rank-sum test; more than two with Kruskal-Wallis (`*_kw_stat_summary.txt`) and Dunn post-hoc pairwise tests (`*_pairwise_dunn_test.txt`). Plots are sorted into `boxplots/corrected_sig/` (significant after adjustment), `boxplots/non_sig/` (not significant after adjustment) and `boxplots/uncorrected_sig/` (significant only before adjustment). Counting the plots in each folder gives the headline: for example, "4 indices significant after adjustment and 3 not significant". Pairwise text results are in `wilcox-pairwise/`. The container adjusts p-values with Benjamini-Hochberg (FDR; Dunn post-hoc also uses BH), checked in its code. Alpha diversity is computed with `otuSummary::alphaDiversity` (observed richness, Shannon, Simpson, inverse Simpson, Chao1, Chao2, evenness). If the run stopped with `Input data must be a numeric vector or matrix`, alpha diversity did not complete (see troubleshooting); do not describe alpha results for that run; the cause is unconfirmed (see troubleshooting).

**Beta diversity** (`*_beta/`). Dissimilarity between samples, with Bray-Curtis as the tutorial's example. `permanova/*_results.tsv` holds the PERMANOVA (adonis in vegan) results per grouping column, with pairwise results where there are several groups; significance is judged at p < 0.05. `plots/` has ordinations (PCoA, NMDS, PCA, CA, DCA, CCA, RDA), coloured by group. A significant PERMANOVA means the group centroids or spread differ; read the ordination to see whether samples separate visually, and remember PERMANOVA is sensitive to differences in dispersion between groups (general practice). The tutorial also describes Jaccard distance, but the container computes only Bray-Curtis (code inspection), so do not report a Jaccard result. The module uses Bray-Curtis on square-root relative abundances only, with 999 permutations, and does not run a dispersion test or stratify permutations, so say that dispersion was not tested, and do not offer paired or longitudinal comparisons from this module (checked in the container's code). The adjustment method used for differential abundance has not been inspected: say "adjusted" without naming a method.

**Differential abundance** (`*_diff-abundance/`). Tests every feature (taxon, gene or pathway) between groups. `boxplots/corrected_sig/` and `boxplots/uncorrected_sig/` hold one plot per feature, so these folders can contain thousands of files; summarise by count and name the top features rather than listing them. `volcano/` shows significance against magnitude of change. `barplot/` shows the Hodges-Lehmann estimator and is suitable for two groups only; with more than two groups it compares the first two, so say which two. For pairwise Wilcoxon tests the code takes as reference the group whose name contains Control, control or CTRL when exactly one matches (a TODO in `variance-stats.R` says this may not always hold), so state which group was the reference and check the direction of each effect. `wilcox-pairwise/` holds the text results.

When reporting, give for each comparison: groups and n, the test, the adjusted result, and the effect direction. Use the lab and mock-community example in the tutorial only as an illustration, never as expected results.

## Random Forest classification

Outputs (in the `-o` directory): per input table `roc_data_N.pdf` and `roc_data_N_classifier.pdf` with one page per cross-validation from 3-fold to 10-fold, `cv_auc.pdf` (AUC against the number of folds, one page per input), `roc_classif.randomForest_dataset.pdf` (all inputs compared, one page per fold count) and `socket_cluster_log.txt`. `data_all` is the combined input.

- The ROC curve plots sensitivity against specificity. A curve toward the top left is a better classifier; a curve on the diagonal is no better than guessing.
- AUC runs from 0 to 1 and is annotated at the top of each figure with a bootstrapped 95% confidence interval. It is computed once per table and fold count from the pooled out-of-fold predictions. Report the AUC per input table and fold count, and its range over the fold counts, rather than a single "accuracy" or the best result of 8 fold counts times N tables, which is biased upward.
- The module runs randomForest with default settings and no tuning, a fixed seed (101), and random folds that are not stratified by group. It is built for two groups (the AUC uses one probability column). Feature-importance files (`feature_importance_TABLE_classif.randomForest.txt`) are written if the model returns them, which has not been confirmed in a run; importance from one forest is unstable, so do not name driver taxa from it.
- Say which feature tables were inputs, the number of samples per class, and that classes were mutually exclusive.
- With fewer than about 50 samples the AUC estimate is very uncertain, and with fewer than 8 per class the folds are tiny. The best fold count or table among many is biased upwards, so report the range over fold counts and the table chosen in advance, not the maximum (general practice).
- Cross-validation estimates performance on held-out folds. With few samples the estimate is noisy and the spread across fold counts matters; say so, and do not call a classifier validated without independent data (general practice).

## Reading figures

PNG figures can be viewed when they are available as files in the session. PDFs are pages of plots; render the pages you need to images to read the AUC annotations. If you cannot see a figure, say so and rely on the text tables (`*_results.tsv`, `alpha_diversity_raw_data.txt`) instead of describing a plot you have not looked at.
