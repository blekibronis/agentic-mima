# MIMA module parameters

Generated from `scripts/command_specs.json` by `scripts/generate_module_docs.py`. The spec is the source of truth used by `build_command.py`; edit the spec, then regenerate this file.

UNVERIFIED marks details the tutorial does not confirm. Check them with `apptainer run --app MODULE $SANDBOX --help` when a container is available, then update the spec.

## Contents
- mima-qc
- mima-qc-report
- mima-taxa-profiling
- mima-bracken-report
- bracken-feature-tables
- mima-function-profiling
- mima-vis-taxa
- mima-visualisation
- mima-classifier-RF

## mima-qc

- Stage: `qc`. Requires stage: none.
- Purpose: Generate per-sample QC scripts and PBS scripts (repair, dereplicate, fastp, host decontamination).
- Notes: Generates one bash script per sample plus PBS scripts; nothing is analysed until the PBS scripts are submitted. Must finish before taxonomy or function profiling. Container code (mima4_v2.0.0): fastp runs with its default settings (no thresholds are passed); host removal is 'minimap2 -c REF R1 R2' with no preset (no -x sr), and reads named in the alignment are removed, so short reads may be matched less sensitively and some host reads can remain. --ref is listed as optional by --help but is required here, because without it no host reads are removed; the file extension is not checked. Samples per job = ceil(samples / --num-pbs-jobs), so fewer PBS scripts than requested can result (9 samples, 4 jobs gives 3 scripts). --help default for --mode is container (the tutorial says single). -w/-M/-t set the walltime (hours), memory (GB) and threads written for the PBS jobs; how they combine with the header in --pbs-config has not been checked, so prefer to set resources in the --pbs-config file and leave these unset.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `-i` | abs_dir_in | yes |  | Directory holding the raw FASTQ files named in the manifest |
| `-o` | abs_dir_out | yes |  | Output directory; fails if it already contains subdirectories |
| `-m` | abs_file_in | yes |  | manifest.csv (Sample_ID,FileID_R1,FileID_R2) |
| `--num-pbs-jobs` | int | no | 4 | Number of PBS scripts to generate |
| `--ref` | abs_file_in | yes |  | Host reference genome FASTA, e.g. GRCh38_latest_genome.fna |
| `--mode` | choice (container/single) | no | container | Use container for MIMA container runs |
| `--pbs-config` | abs_file_in | if --mode is container |  | PBS header config file (pbs_header_qc.cfg) |
| `-s` | int | no |  | Maximum number of substitutions allowed between duplicates for clumpify.sh (BBTools). Default not shown by --help. |
| `-w` | int | no |  | PBS walltime in hours (--help default 2). |
| `-M` | int | no |  | PBS memory in GB (--help default 64). |
| `-t` | int | no |  | PBS threads (--help default 8). |

## mima-qc-report

- Stage: `qc-report`. Requires stage: qc.
- Purpose: Summarise QC statistics into QC_report.csv. Run after all QC PBS jobs have finished.
- Notes: Run from inside the QC_module directory (the builder adds the cd). It reads the QCReport folder (fastp JSON) and the PBS log files named *qc_module.oJOBID. Output: QC_report.csv with columns SampleId, log_status, Rawreads_seqs, Derep_seqs, dedup_percentage, Post_QC_seqs, low_quality_reads(%), Host_seqs, Host(%), Clean_reads (container code). dedup_percentage is fastp's own duplication rate parsed from the log, not the share removed by clumpify; low_quality_reads(%) = (Derep_seqs - Post_QC_seqs) / Derep_seqs x 100. Check log_status for missing logs.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `-i` | abs_dir_in | yes |  | QC_module directory containing the *_qc_module.log files; the command is run from here |
| `--manifest` | abs_file_in | yes |  | manifest.csv used for QC |
| `-o` | abs_dir_out | no |  | Output directory for QC_report.csv (default: the input directory). |

## mima-taxa-profiling

- Stage: `taxa`. Requires stage: qc.
- Purpose: Generate the Kraken2/Bracken taxonomy profiling PBS script and per-sample scripts.
- Notes: Kraken2 needs about 300GB memory, so one PBS script runs samples sequentially. Kraken2 and Bracken databases must share one directory. The tutorial example uses --threshold 100 while the default is 1000: always state which value is used. The generator lists the samples in the CleanReads folder once, when it runs: samples added later are ignored until it is run again, and a wrong --fwd-suffix/--rev-suffix exits 0 and writes an empty PBS script with no per-sample .sh files (validate_pbs_scripts.py --check-fs reports this). Verified on Gadi: --read-length maps to Bracken -r and --threshold to Bracken -t. --help defaults: walltime 48 h, memory 320 GB, 28 threads; --threshold default 1000 (taxa below it are removed); --read-length has no stated default (the tutorial says 150). On the Gadi GTDB r95 database only 50, 100 and 150 have k-mer files (75 fails). -w/-M/-t set the walltime (hours), memory (GB) and threads written for the PBS jobs; how they combine with the header in --pbs-config has not been checked, so prefer to set resources in the --pbs-config file and leave these unset.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `-i` | abs_dir_in | yes |  | CleanReads directory produced by QC |
| `-o` | abs_dir_out | yes |  | Output directory (Taxonomy_profiling is created inside) |
| `--reference-path` | abs_dir_in | yes |  | Kraken2 database directory; the Bracken database must be in the same directory |
| `--read-length` | choice (50/75/100/150) | no | 150 | Closest to the sequenced read length. The tutorial lists 50, 75, 100, 150, but Bracken also needs databaseNmers.kmer_distrib for that N in the database directory: the shared GTDB database on Gadi has only 50, 100 and 150, and 75 fails. Checked with --check-fs. Estimate it with estimate_read_length.py (nearest available length from the median per-sample mean; ties go lower). |
| `--threshold` | int | no | 1000 | Bracken -t: minimum read count for a species to be kept. Container default 1000; project default 100 (see references/lab-defaults.md). On Gadi (GTDB r95, -r 150, species level) a test sample set kept 4,809 / 1,724 / 627 species at 10 / 100 / 1000. Keep one value for a whole project and state it, with the number of samples dropped, in the report. |
| `--mode` | choice (container/single) | no | container | Use container for MIMA container runs |
| `--pbs-config` | abs_file_in | if --mode is container |  | PBS header config file (pbs_header_taxa.cfg) |
| `--fwd-suffix` | token | no | _clean_1.fq.gz | Suffix of cleaned forward reads |
| `--rev-suffix` | token | no | _clean_2.fq.gz | Suffix of cleaned reverse reads |
| `-w` | int | no |  | PBS walltime in hours (--help default 48). |
| `-M` | int | no |  | PBS memory in GB (--help default 320). |
| `-t` | int | no |  | PBS threads (--help default 28). |
| `--taxon-profiler` | choice (kraken2) | no | kraken2 | Only kraken2 is available. |
| `-f` | token | no | fastq | Input file type (default fastq). |

## mima-bracken-report

- Stage: `taxa-report`. Requires stage: taxa.
- Purpose: Summarise Bracken logs into bracken-summary.tsv. Run after the taxonomy PBS job has finished.
- Notes: Output: bracken-summary.tsv in the input directory unless -o is given. One row per sample and rank, with columns sampleID, taxon_rank, log_status, threshold, num_taxon, num_tx_above_thres, num_tx_below_thres, total_reads, reads_above_thres, reads_below_thres, reads_distributed, reads_not_distributed, reads_unclassified, logfile, output, all parsed from the Bracken logs. log_status is 'ok-...' for a good row, or 'no reads found' / 'exit error'. Only samples that appear in the logs get a row; exits 1 if no *_bracken.log files are found.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `-i` | abs_dir_in | yes |  | Taxonomy_profiling directory containing the *_bracken.log files |
| `-o` | abs_file_out | no |  | Optional full path of the output file (default: bracken-summary.tsv beside the input) |

## bracken-feature-tables

- Stage: `taxa-tables`. Requires stage: taxa.
- Purpose: Merge per-sample Bracken output into feature tables, one per rank. Run after the taxonomy PBS job has finished.
- Notes: Runs the generated Python script directly (not a PBS job). The script takes no arguments and reads the relative path ../bracken/*_RANK, so it must be run from inside the featureTables directory. It loops over species, genus, family, order, class and phylum and writes bracken_FT_RANK plus _counts (Bracken _num columns) and _relAbund (Bracken _frac columns, split out unchanged). Bracken defines the fraction over the reads kept at that rank after the threshold, so shares are relative to the retained taxa, not all reads. Run it only after every sample has finished: samples still running are silently left out.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `featuretables_dir` | abs_dir_in | yes |  | Taxonomy_profiling/featureTables directory |

## mima-function-profiling

- Stage: `func`. Requires stage: qc.
- Purpose: Generate per-sample HUMAnN3 PBS scripts.
- Notes: Generates one PBS script per sample (HUMAnN3; the tutorial example asks for 8 CPUs, 64 GB and 8 h, --help defaults are 24 h, 64 GB, 28 threads). Forward and reverse reads are concatenated into SAMPLE_combine.fq.gz first, so HUMAnN sees them as one set of reads. The container has MetaPhlAn 4.1.0 (default database vJun23); the tutorial example uses vJan21 with ++index, and --mpa3 exists for MetaPhlAn 3 databases. The feature-table step (generate_func_feature_tables.pbs/.sh, written by func_profiling.py) is a PBS job for the later submission phase; it regroups the cpm gene-family table to KOs with humann_regroup_table -g uniref90_rxn -c UTILITY/map_ko_uniref90.txt.gz, and the 'cmp' in some output names is a spelling slip for cpm. Whether -c overrides -g is untested: check that KO rows start with K numbers. -w/-M/-t set the walltime (hours), memory (GB) and threads written for the PBS jobs; how they combine with the header in --pbs-config has not been checked, so prefer to set resources in the --pbs-config file and leave these unset.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `-i` | abs_dir_in | yes |  | CleanReads directory produced by QC |
| `-o` | abs_dir_out | yes |  | Output directory (Function_profiling is created inside) |
| `--nucleotide-database` | abs_dir_in | yes |  | HUMAnN chocophlan directory |
| `--protein-database` | abs_dir_in | yes |  | HUMAnN uniref directory |
| `--utility-database` | abs_dir_in | yes |  | HUMAnN utility_mapping directory |
| `--metaphlan-options` | metaphlan_options | yes |  | String such as '++bowtie2db /abs/path/metaphlan_databases'; MetaPhlAn options use ++ prefixes. The container's --help shows a list (nargs='+'); the tutorial passes one quoted string and the builder does the same. That the container accepts the single-string form rests on the tutorial run, not on code inspection. |
| `--mode` | choice (container/single) | no | container | Use container for MIMA container runs |
| `--pbs-config` | abs_file_in | if --mode is container |  | PBS header config file (pbs_header_func.cfg) |
| `--mpa3` | flag | no |  | Use the MetaPhlAn 3 algorithm, for backward compatibility with MetaPhlAn 3 databases (--help). The container has MetaPhlAn 4.1.0; which of the default database or an older one plus --mpa3 and ++index runs correctly in HUMAnN 3.8 has not been tested. |
| `-w` | int | no |  | PBS walltime in hours (--help default 24). |
| `-M` | int | no |  | PBS memory in GB (--help default 64). |
| `-t` | int | no |  | PBS threads (--help default 28). |
| `--function-profiler` | choice (humann3/humann2) | no | humann3 | Function profiler (default humann3). |
| `--fwd-suffix` | token | no | _clean_1.fq.gz | Suffix of cleaned forward reads. |
| `--rev-suffix` | token | no | _clean_2.fq.gz | Suffix of cleaned reverse reads. |
| `--pipeline-dir` | abs_dir_in | no |  | Directory of the pipeline scripts, used to find combine_fastq.jl. |

## mima-vis-taxa

- Stage: `vis-barplot`. Requires stage: taxa-tables.
- Purpose: Taxonomy stacked bar plots by group. Arguments are positional and order matters.
- Notes: All arguments are positional and exactly eight are required (the script stops with a usage message otherwise). Use the _relAbund table. Group values are case-sensitive and must match the metadata. Only samples in the listed groups are selected (outputs are named PREFIX.select.*); output file names contain the top_N value given, and the remaining taxa are pooled as 'Others'.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `feature_table` | abs_file_in | yes |  | Relative abundance feature table (bracken_FT_RANK_relAbund) |
| `metadata` | abs_file_in | yes |  | Tab-separated study metadata |
| `group_spec` | group_spec | yes |  | column:group1:group2[:...] (case-sensitive) |
| `output_dir` | abs_dir_out | yes |  | Output directory |
| `prefix` | name | yes |  | Prefix for output files |
| `top_n` | int | yes |  | Number of top taxa to colour; the rest become Other |
| `figure_width` | number | yes |  | Figure width in inches |
| `figure_height` | number | yes |  | Figure height in inches |

## mima-visualisation

- Stage: `diversity`. Requires stage: taxa-tables.
- Purpose: Alpha diversity, beta diversity (PERMANOVA) and differential abundance between groups.
- Notes: Runs alpha diversity, beta diversity (PERMANOVA) and differential abundance for every column in --study-groups. Verified on Gadi (container mima4_v2.0.0): --feature-table is a space-separated list (nargs='+'); both _relAbund and _counts are accepted (relative abundance tables are multiplied by 1e6 to pseudo-counts for alpha diversity; counts are converted to relative abundance for beta diversity and differential abundance); the table reader (models.py) accepts first header cells '# Taxonomy', 'name', '# Pathway' and '# Gene' and REJECTS '# Gene Family' (ValueError: Could not detect data matrix format), so a gene-family table needs a copy with '# Gene' as its first header cell (untested whether it then runs). --study-groups columns must be categorical with at least 2 levels: a numeric column such as Age fails at the CCA/RDA step. Alpha diversity: Wilcoxon (2 groups) or Kruskal-Wallis with Dunn post-hoc (3 or more), Benjamini-Hochberg/FDR adjusted. Beta diversity: Bray-Curtis on square-root relative abundances only, PERMANOVA (adonis2, 999 permutations), no dispersion test and no strata, so paired or longitudinal designs are not supported. KNOWN ISSUE (error observed by the project owner; cause unconfirmed): the project owner's reading of the code is that alpha-diversity.R line 21 calls alphaDiversity(otutab = t(microbial_variables), siteInCol = TRUE, taxhead = "taxonomy") and the run stops with 'Input data must be a numeric vector or matrix' in calc_shannon after beta diversity has finished; the otuSummary documentation says taxhead names a taxonomy column if the table has one, which fits that reading but does not prove it. No fix has been applied or tested by this skill. Pass taxonomic and functional tables together only if the user wants them analysed in one run. Further container findings (code and --help): --metadata may be tab- or comma-separated; --help lists the accepted first-column headers as empty, ID, '# Pathway', '# Gene' and '# IGGsearch species table'; there is no code for UNMAPPED, UNGROUPED, UNINTEGRATED or stratified (|) rows, so they are analysed as ordinary features unless removed first; for pairwise Wilcoxon tests the reference group is the one whose name contains Control, control or CTRL if exactly one matches (variance-stats.R carries a TODO that this may not hold), so name groups accordingly or check effect directions. Not verified here: whether a group named otherwise changes effect direction.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `--feature-table` | abs_file_multi | yes |  | One or more feature tables, space-separated on the command line: Bracken _relAbund or _counts tables (both accepted; the tutorial uses _relAbund) and/or HUMAnN pathway or gene tables. Header of the first cell must be one of '# Taxonomy', 'name', '# Pathway', '# Gene'; '# Gene Family' is rejected. |
| `--metadata` | abs_file_in | yes |  | Tab-separated study metadata |
| `--study-groups` | csv_names | yes |  | Comma-separated metadata columns; each needs 2 or more levels |
| `--output-dir` | abs_dir_out | yes |  | Output directory |
| `--plot-insignificant` | flag | no |  | Also plot comparisons that are not significant (skipped by default). |
| `--ini-file` | abs_file_in | no |  | Configuration file for figure parameters such as size (default figures.ini). |

## mima-classifier-RF

- Stage: `classifier`. Requires stage: none.
- Purpose: Random Forest classification with 3- to 10-fold cross-validation.
- Notes: Creates 3- to 10-fold CV results per input table plus a combined data_all model. Container code (classifier_pipeline.R): -i is a comma-separated list with no spaces, each file tab separated with the feature name in column 1 and samples in the other columns; all-zero features are dropped. Only randomForest (mlr) is run with default settings and no tuning; seed 101 is fixed before the folds; folds are created by caret::createFolds on sample names, so they are random, not stratified by group; each K runs once; one AUC with a bootstrapped 95% CI is computed from the pooled out-of-fold predictions, using the second probability column, so the module is built for two groups. -c is matched to the metadata header by pattern (grep), so a column name that is part of another header (Gender vs Gender_2) selects several columns and breaks the run. Feature-importance files (feature_importance_TABLE_classif.randomForest.txt) are written if the model returns importances; not verified in a run. Class labels must be mutually exclusive.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `-i` | abs_file_list | yes |  | One or more feature tables, comma-separated with no spaces |
| `-m` | abs_file_in | yes |  | Tab-separated metadata |
| `-c` | name | yes |  | Metadata column holding the mutually exclusive class labels |
| `-o` | abs_dir_out | yes |  | Output directory (created beforehand with mkdir -p) |
