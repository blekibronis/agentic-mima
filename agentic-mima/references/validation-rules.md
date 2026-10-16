# Validation rules

Read this when a validator reports an error you need to explain, or when checking rules that scripts cannot enforce. "Enforced by" says where each rule is checked; rules marked "Ask the user" cannot be checked from a chat and must be confirmed.

## Inputs

| Rule | Why it matters | Enforced by |
|---|---|---|
| Manifest header is exactly `Sample_ID,FileID_R1,FileID_R2` (case-sensitive) | The QC step reads these column names | `validate_manifest.py` |
| Sample IDs use letters, digits and underscores only | Sample_ID names every downstream file and must match the metadata and feature tables. This is general good practice and a project convention; it has not been shown to be required by the container | `validate_manifest.py`, `validate_metadata.py` (warnings), fixed by `prepare_inputs.py` |
| Manifest has Unix line endings, no BOM | CRLF corrupts the last column value and silently breaks file lookup | `validate_manifest.py` |
| Manifest file names are relative to the QC `-i` directory, not absolute | The pipeline joins `-i` and the file name | `validate_manifest.py` (warning) |
| Sample IDs and FASTQ names are unique; R1 differs from R2 | Duplicates overwrite outputs or misalign pairs | `validate_manifest.py` |
| Reads are paired-end, two files per sample | Data processing does not handle single-end reads | Ask the user |
| Metadata is tab-separated; the ID column matches the feature-table sample columns | Unmatched samples are dropped from analysis | `validate_metadata.py` |
| Group column exists and group values match exactly (case-sensitive) | `lab:labA:labB` fails or drops samples if the case differs | `validate_metadata.py`, `build_command.py` (syntax) |
| `mima-visualisation` receives feature tables as a space-separated list; `mima-classifier-RF` as a comma-separated list without spaces | The two tools parse their lists differently, so a wrong separator merges files into one bad path | `build_command.py` |
| Feature tables start with `# Taxonomy`, `name`, `# Pathway` or `# Gene`. A table headed `# Gene Family` is rejected (container check on Gadi: `Could not detect data matrix format`) | The module stops | `validate_metadata.py` (error; other `#` headers are warnings) |
| The `mima-classifier-RF` `-c` column name is not a substring of another metadata header (e.g. `Gender` and `Gender_2`) | The module finds the column by pattern matching (grep), so several columns are selected and the run breaks | `validate_metadata.py` (warning) |
| `--study-groups` columns are categorical with at least 2 levels; a column with more than 5 distinct numeric values is treated as continuous (this skill's heuristic) | A numeric Age column crashed the container at the CCA/RDA step | `validate_metadata.py` (error; a few numeric codes give a warning) |
| Each grouping column has 2 or more levels; groups with fewer than 3 samples give unreliable statistics (this skill's heuristic, not a tutorial value) | Tests need replicates | `validate_metadata.py` |
| Bracken tables passed to `mima-visualisation` are `_relAbund` (the tutorial's choice) or `_counts` | Both are accepted. Counts are used as they are for alpha diversity; relative abundances are scaled by 1e6 to pseudo-counts, so alpha results can differ. The base `bracken_FT_RANK` file holds both together and has not been tested | `build_command.py` and `validate_metadata.py` (warnings) |
| Class labels for Random Forest are mutually exclusive | Each sample belongs to exactly one class | Ask the user |

## Commands

| Rule | Why it matters | Enforced by |
|---|---|---|
| Only modules in `command_specs.json` are generated | Restricts the LLM to approved pipeline functions | `build_command.py` |
| All paths are absolute, contain no `..`, spaces or shell characters | The tutorial recommends absolute paths; the character check blocks shell injection | `build_command.py` |
| Required parameters present; enumerations respected (`--read-length` in 50, 75, 100, 150) | Wrong values fail late, after queue time | `build_command.py` |
| The `--read-length` actually used (explicit or the default 150) has a `databaseNmers.kmer_distrib` file in `--reference-path`. The shared GTDB database on Gadi has only 50, 100 and 150, so 75 passes the enumeration but Bracken fails | Bracken needs that file | `build_command.py --check-fs` (error); without `--check-fs`, a warning |
| A taxonomy or QC generator produced per-sample `.sh` files | A wrong `--fwd-suffix` or `--rev-suffix` exits 0 and writes an empty PBS script | `validate_pbs_scripts.py --check-fs` (error) |
| `--mode container` requires `--pbs-config` | The generator cannot write container PBS scripts without the header file | `build_command.py` |
| `--mode` omitted: `--help` in the container says the default is `container` while the tutorial says `single` | Neither can be relied on, so always state `--mode container` | `build_command.py` (warning) |
| QC `-o` directory must not already contain subdirectories | The QC step fails if it does | `build_command.py --check-fs` |
| `qc` precedes `taxa` and `func` in a plan | Profiling reads QC output | `build_command.py` |
| `mima-vis-taxa` arguments are positional and ordered | A swapped argument produces wrong output without an error | `build_command.py` (fixed order) |
| Kraken2 and Bracken databases are in the same directory | The taxonomy script reads both from `--reference-path` | Ask the user |
| `--read-length` is the option closest to the sequenced read length | Bracken uses the matching k-mer distribution | Ask the user |
| `--threshold` is stated explicitly (project default 100; container default 1000) | It filters features and changes every downstream table | `build_command.py` (warning when omitted) |
| `--reference-path` is on the approved database list | Other databases need the lab's approval | `build_command.py` (warning) |
| Group sizes: below 4 descriptive only, below 8 exploratory, 10 or more recommended | An exact rank test cannot reach p < 0.05 below 4 per group | `validate_metadata.py` (warnings) |
| No person appears in more than one sample (`--subject-column`) | `mima-visualisation` treats samples as independent | `validate_metadata.py` (error; warning when detected by column name) |
| Taxonomy walltime is at least 20 minutes per sample plus 1 hour; 16 to 28 cores | A killed job wastes the queue wait | `validate_pbs_config.py --samples N` (warning) |

## PBS header config (`--pbs-config`)

| Rule | Enforced by |
|---|---|
| Shebang, `#PBS -N`, `ncpus`, `walltime`, `mem` present; `-j oe` recommended | `validate_pbs_config.py` |
| `IMAGE_DIR` is set (plain or exported) and points to the MIMA image (.sif) or sandbox | `validate_pbs_config.py` |
| `APPTAINER_BIND` has no `leftover /path/to/ paths` placeholders and uses `/host:/container` pairs | `validate_pbs_config.py` |
| Taxonomy jobs request about 300GB memory or more | `validate_pbs_config.py --module taxa` (warning) |
| The bind list covers raw reads, reference databases and outputs outside the home directory | Ask the user |

## Reporting validator output

- Quote each error with its fix. Do not paraphrase away the file name or line number.
- Treat warnings as decisions to surface, not noise: unset defaults, skipped filesystem checks and small group sizes all belong in the summary to the user.
- When `--check-fs` was skipped, say so and give the exact command to re-run on the cluster.

## Preparing user files

`scripts/prepare_inputs.py` makes corrected copies that follow the rules above. It changes only what has one right answer (line endings, byte-order mark, delimiter, header names, spaces, absolute paths, sample ID characters). It stops and asks when the meaning could change: R1 and R2 that look swapped, duplicate or colliding IDs, several lanes per sample, unpaired files, files in several folders, ambiguous columns. It writes `id_map.tsv` (original ID, pipeline ID) so reports can use the user's own names. The user's originals are never modified.
