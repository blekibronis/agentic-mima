# MIMA workflow dependencies

Read this when planning a multi-step request, or when the user asks what must run first.

Uppercase words in file names (SAMPLE, RANK) stand for a sample name or taxonomic rank.

## Stage order

```
manifest.csv + raw FASTQ
        |
      [qc]  -->  [qc-report] (optional)
        |
   CleanReads
     /      \
 [taxa]    [func]        taxa and func are independent and can run in parallel
    |         |
 [taxa-report] (optional)   [func feature tables: PBS job, submission phase]
 [taxa-tables]
    |
 feature tables (bracken_FT_RANK_counts / _relAbund)
    |
 [vis-barplot]  [diversity]  [classifier]     analysis stages, any order
```

- `qc` must finish before `taxa` or `func`. This is the one hard dependency in data processing.
- The `taxa` and `func` generators build per-sample scripts from the files in `CleanReads`, so run them after QC has finished, not while QC jobs are still queued.
- Jobs generated together can be chained: the functional feature-table job (`generate_func_feature_tables.pbs`) can wait on the per-sample function jobs. See `job-submission.md`.
- `taxa` and `func` both read the `CleanReads` directory produced by `qc`.
- `taxa-tables` needs the taxonomy PBS job to have finished.
- Analysis stages (`vis-barplot`, `diversity`, `classifier`) need feature tables and a metadata file. They do not need the processing stages to be in the same plan; users often start from existing tables.

## Two kinds of command

| Kind | Modules | What happens |
|---|---|---|
| Generators | `mima-qc`, `mima-taxa-profiling`, `mima-function-profiling` | Write bash and PBS scripts. Analysis only starts when those PBS scripts are submitted with `qsub`. |
| Direct runs | `mima-qc-report`, `mima-bracken-report`, `bracken-feature-tables`, `mima-vis-taxa`, `mima-visualisation`, `mima-classifier-RF` | Run the analysis immediately. The tutorial runs them inside an interactive PBS job. |

Always tell the user which kind each command is. A user who runs a generator and expects results has not yet finished the step.

## Where outputs land (inputs for the next stage)

| Stage | Output location under the chosen `-o` | Feeds |
|---|---|---|
| `qc` | `QC_module/CleanReads/*_clean_1.fq.gz`, `*_clean_2.fq.gz`; `QC_module/qcModule_N.pbs` | `taxa`, `func` (`-i`) |
| `qc-report` | `QC_module/QC_report.csv` | user review |
| `taxa` | `Taxonomy_profiling/run_taxa_profiling.pbs`, then `bracken/`, `kraken2/` after the job | `taxa-tables`, `taxa-report` |
| `taxa-tables` | `Taxonomy_profiling/featureTables/bracken_FT_RANK[_counts\|_relAbund]` (phylum to species) | `vis-barplot`, `diversity`, `classifier` |
| `func` | `Function_profiling/SAMPLE.pbs`, then HUMAnN3 outputs after the jobs | functional feature tables |

`qc-report` and `taxa-report` need the PBS log files, which appear in the directory where `qsub` was run. The tutorial therefore submits from the module directory.

## Environment assumptions

- An HPC with OpenPBS and Apptainer. Many systems disallow containers on the login node, so the tutorial starts an interactive PBS job first.
- The MIMA sandbox is built once and referenced through the `SANDBOX` environment variable. Generated commands use `$SANDBOX` literally; the user's shell must have it set.
- Any directory outside the home directory (raw reads, reference databases, outputs) must be bound into the container. In the PBS config this is the `APPTAINER_BIND` line.
- Reference databases are not in the container: minimap2 host genome, Kraken2/Bracken database, HUMAnN (chocophlan, uniref, utility_mapping) and MetaPhlAn databases. Their absolute paths must come from the user.
- Container version `mima4_v2.0.0` works with HUMAnN 3.8 and MetaPhlAn 4.1.0; older reference databases will not work.
