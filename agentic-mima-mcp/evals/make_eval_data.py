#!/usr/bin/env python3
"""Create the fixed, synthetic data set the evaluation questions refer to.

Usage: python3 make_eval_data.py [TARGET_DIR]      (default /tmp/mima_eval)

Everything here is invented and deterministic. Nothing is real study data, and no cluster is needed.
"""
import gzip
import sys
from pathlib import Path

T = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/mima_eval")


def w(rel, text="x\n", binary=None):
    p = T / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if binary is not None:
        p.write_bytes(binary)
    else:
        p.write_bytes(text.encode())
    return p


def fastq_gz(rel, length, n=40):
    body = "".join(f"@r{i}\n{'A' * length}\n+\n{'I' * length}\n" for i in range(n))
    w(rel, binary=gzip.compress(body.encode()))


# 1. Manifests: one clean, one saved with Windows line endings
rows = ["Sample_ID,FileID_R1,FileID_R2"] + [f"E{i:02d},E{i:02d}_1.fq.gz,E{i:02d}_2.fq.gz" for i in range(1, 9)]
w("manifests/manifest_run1.csv", "\n".join(rows) + "\n")
w("manifests/manifest_run2.csv", "\r\n".join(rows) + "\r\n")

# 2. Metadata (12 samples) and a species table that lost two of them to the abundance threshold
samples = [f"M{i:02d}" for i in range(1, 13)]
labs = ["labA"] * 4 + ["labB"] * 4 + ["labC"] * 4
w("study/metadata.tsv", "Sample_ID\tlab\ttype\n" + "".join(f"{s}\t{l}\tstool\n" for s, l in zip(samples, labs)))
kept = [s for s in samples if s not in ("M04", "M09")]
w("study/featureTables/bracken_FT_species_relAbund", "name\t" + "\t".join(kept) + "\n" + "Bacteroides\t" + "\t".join("0.1" for _ in kept) + "\n")

# 3. Clean reads of three samples: mean lengths 120, 126, 130
for s, ln in (("E01", 120), ("E02", 126), ("E03", 130)):
    fastq_gz(f"CleanReads/{s}_clean_1.fq.gz", ln)
    fastq_gz(f"CleanReads/{s}_clean_2.fq.gz", ln)

# 4. Logs
w("logs/mima-QC.o900001", "+ fastp done\ngzip: E07_clean_1.fq.gz: unexpected end of file\n+ finished\n")
mv = "".join(f"mv: cannot stat '/data/Taxonomy_profiling/kraken2/{s}.kraken2_bracken_*.report': No such file or directory\n"
             for s in ("E02", "E05", "E06", "E08"))
w("logs/mima-taxa.o900002", "+ kraken2 done\n" + mv)
w("logs/E03_bracken.log", ">> ERROR: Input file /data/Taxonomy_profiling/kraken2/E03.kraken2.report does not exist\n")

# 5. PBS header config for the taxonomy stage with too little memory
w("config/pbs_header_taxa.cfg", "#!/bin/bash\n#PBS -N mima-taxa\n#PBS -q hugemem\n#PBS -P ab01\n#PBS -l ncpus=28\n#PBS -l walltime=10:00:00\n"
  "#PBS -l mem=120GB\n#PBS -l storage=gdata/ab01\n#PBS -j oe\n\nexport IMAGE_DIR=/data/mima.sif\nexport APPTAINER_BIND=\"/data:/data\"\n")

# 6. A QC_module folder with damage in two samples
for s in (f"E{i:02d}" for i in range(1, 7)):
    for f in (f"{s}.sh", f"{s}_qc_module.log", f"QCReport/{s}.json", f"CleanReads/{s}_clean_1.fq.gz", f"CleanReads/{s}_clean_2.fq.gz"):
        w(f"run/QC_module/{f}")
(T / "run/QC_module/CleanReads/E04_clean_2.fq.gz").unlink()
w("run/QC_module/CleanReads/E05_clean_1.fq.gz", "")
w("run/QC_module/QC_report.csv", "SampleId,log_status,Rawreads_seqs,Derep_seqs,dedup_percentage,Post_QC\n" + "".join(f"E{i:02d},OK,1000000,990000,0.01,{900000 - i * 1000}\n" for i in range(1, 7)))
w("run/QC_module/QC_module_0.o900003", "+ done\n")

# 7. A Bracken database folder that has k-mer files for some read lengths only
for n in (50, 100, 150):
    w(f"db/GTDB_Kraken/database{n}mers.kmer_distrib")
(T / "db/GTDB_Kraken").mkdir(parents=True, exist_ok=True)
for f in ("hash.k2d", "opts.k2d", "taxo.k2d"):
    w(f"db/GTDB_Kraken/{f}")
w("run/CleanReads/placeholder", "x\n")
print(f"wrote {T}")
