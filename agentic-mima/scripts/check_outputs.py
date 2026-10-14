#!/usr/bin/env python3
"""Check that a MIMA stage produced the outputs the tutorial says it should (read-only).

Usage:
    check_outputs.py --stage STAGE --dir DIR [--manifest manifest.csv] [--format text|json]

Stages and the directory each expects:
    qc            QC_module directory
    taxa          Taxonomy_profiling directory
    taxa-tables   Taxonomy_profiling/featureTables
    func          Function_profiling directory
    func-tables   Function_profiling/featureTables
    vis-barplot   output directory given to mima-vis-taxa
    diversity     output directory given to mima-visualisation
    classifier    output directory given to mima-classifier-RF

Expected samples come from --manifest when given, otherwise from the per-sample
scripts the generator wrote. Missing or empty files are errors; unusual values in
QC_report.csv and bracken-summary.tsv are warnings. Numbers reported as
"heuristic" are this skill's rules of thumb, not tutorial values.

Exit code: 0 = no errors, 1 = errors found, 2 = usage problem.
"""
import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_logs import scan  # noqa: E402

RANKS = {"class": "classes", "family": "families", "genus": "genuses", "order": "orders", "phylum": "phylums", "species": "species"}
FUNC_TABLES = ["merge_humann3table_genefamilies.txt", "merge_humann3table_genefamilies.cpm.txt",
               "merge_humann3table_genefamilies.cmp_uniref90_KO.txt", "merge_humann3table_pathabundance.txt",
               "merge_humann3table_pathabundance.cmp.txt", "merge_humann3table_pathcoverage.txt",
               "merge_humann3table_pathcoverage.cmp.txt"]


class Result:
    def __init__(self, stage):
        self.stage, self.findings, self.facts = stage, [], {}

    def error(self, msg):
        self.findings.append({"severity": "error", "message": msg})

    def warn(self, msg):
        self.findings.append({"severity": "warning", "message": msg})

    def info(self, msg):
        self.findings.append({"severity": "info", "message": msg})

    @property
    def ok(self):
        return not any(f["severity"] == "error" for f in self.findings)


def nonempty(path):
    return path.is_file() and path.stat().st_size > 0


def expect(res, path, label=None):
    if not path.exists():
        res.error(f"missing: {label or path}")
        return False
    if path.is_file() and path.stat().st_size == 0:
        res.error(f"empty file: {label or path}")
        return False
    return True


def samples_from(manifest, directory, pattern, exclude=()):
    if manifest:
        with open(manifest, newline="") as fh:
            return [r["Sample_ID"] for r in csv.DictReader(fh) if r.get("Sample_ID")]
    return sorted(p.stem for p in directory.glob(pattern) if p.stem not in exclude)


def header_cells(path):
    with open(path, errors="replace") as fh:
        return fh.readline().rstrip("\n").rstrip("\r").split("\t")


def scan_logs_into(res, directory):
    findings, n = scan([str(directory)])
    res.facts["log_files_scanned"] = n
    for f in findings:
        if f["severity"] == "error":
            res.error(f"log signature '{f['pattern']}'{'' if f['verified'] else ' [unverified pattern]'} in {f['file']} (line {f['lines'][0]['line']}): {f['cause']}")
    if n == 0:
        res.warn("no PBS or MIMA log files found, so job completion cannot be confirmed from logs")


def check_qc(res, d, manifest):
    samples = samples_from(manifest, d, "*.sh")
    res.facts["samples_expected"] = len(samples)
    if not samples:
        res.error("no samples found: give --manifest or check the QC_module directory")
    for s in samples:
        for f in (f"CleanReads/{s}_clean_1.fq.gz", f"CleanReads/{s}_clean_2.fq.gz", f"{s}_qc_module.log", f"QCReport/{s}.json"):
            expect(res, d / f, f"{s}: {f}")
    scan_logs_into(res, d)
    rpt = d / "QC_report.csv"
    if not rpt.exists():
        res.info("QC_report.csv not found (optional step: mima-qc-report)")
        return
    with open(rpt, newline="") as fh:
        rows = list(csv.DictReader(fh))
    bad = [r["SampleId"] for r in rows if r.get("log_status") != "OK"]
    if bad:
        res.error(f"QC_report.csv log_status is not OK for: {', '.join(bad[:10])}. 'missing QC log' or 'missing JSON log' means that sample's log file was not found.")
    # Column names as written by qc_report.py (container mima4_v2.0.0): Clean_reads is the final
    # count, Post_QC_seqs the count after fastp. Older tutorial output used "Post_QC".
    cols = rows[0].keys() if rows else []
    count_col = next((c for c in ("Clean_reads", "Post_QC_seqs", "Post_QC") if c in cols), None)
    try:
        if count_col is None:
            raise KeyError("no read-count column")
        post = {r["SampleId"]: float(r[count_col]) for r in rows}
        raw = {r["SampleId"]: float(r["Rawreads_seqs"]) for r in rows}
    except (KeyError, ValueError):
        res.warn("could not read a clean-read count (Clean_reads, Post_QC_seqs or Post_QC) and Rawreads_seqs from QC_report.csv")
        return
    res.facts["qc_count_column"] = count_col
    missing = sorted(set(samples) - {r["SampleId"] for r in rows})
    if missing:
        res.error(f"samples absent from QC_report.csv: {', '.join(missing[:10])}")
    vals = list(post.values())
    if vals:
        med = statistics.median(vals)
        res.facts.update({"post_qc_min": min(vals), "post_qc_median": med, "post_qc_max": max(vals),
                          "retention_min": round(min(post[s] / raw[s] for s in post if raw[s]), 4),
                          "retention_max": round(max(post[s] / raw[s] for s in post if raw[s]), 4)})
        for s, v in post.items():
            if v == 0:
                res.error(f"{s}: zero reads left after QC")
            elif v < 0.1 * med:
                res.warn(f"{s}: {int(v):,} reads after QC is under 10% of the cohort median {int(med):,} (heuristic outlier flag)")


def check_taxa(res, d, manifest):
    samples = samples_from(manifest, d, "*.sh")
    res.facts["samples_expected"] = len(samples)
    if not samples:
        res.error("no samples found: give --manifest or check the Taxonomy_profiling directory")
    for s in samples:
        for f in (f"kraken2/{s}.kraken2.output", f"kraken2/{s}.kraken2.report", f"{s}_bracken.log"):
            expect(res, d / f, f"{s}: {f}")
        for rank, plural in RANKS.items():
            expect(res, d / "bracken" / f"{s}_{rank}", f"{s}: bracken/{s}_{rank}")
            expect(res, d / "bracken" / f"{s}.kraken2_bracken_{plural}.report", f"{s}: bracken/{s}.kraken2_bracken_{plural}.report")
    scan_logs_into(res, d)
    summ = d / "bracken-summary.tsv"
    if not summ.exists():
        res.info("bracken-summary.tsv not found (optional step: mima-bracken-report)")
        return
    with open(summ, newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    if not rows or "log_status" not in rows[0]:
        res.warn("bracken-summary.tsv has no log_status column")
        return
    odd = sorted({(r["sampleID"], r["log_status"]) for r in rows if not r["log_status"].lower().startswith("ok")})
    for s, status in odd[:10]:
        res.warn(f"bracken-summary.tsv: {s} has log_status '{status}' (anything not starting with 'ok' needs a look at that sample's *_bracken.log)")
    absent = sorted(set(samples) - {r["sampleID"] for r in rows})
    if absent:
        res.error(f"samples absent from bracken-summary.tsv: {', '.join(absent[:10])}")
    res.facts["bracken_summary_samples"] = len({r["sampleID"] for r in rows})


def check_taxa_tables(res, d, manifest):
    for rank in RANKS:
        for suffix in ("", "_counts", "_relAbund"):
            expect(res, d / f"bracken_FT_{rank}{suffix}", f"bracken_FT_{rank}{suffix}")
    sets = {}
    for rank in RANKS:
        c, r = d / f"bracken_FT_{rank}_counts", d / f"bracken_FT_{rank}_relAbund"
        if nonempty(c) and nonempty(r):
            hc, hr = header_cells(c)[1:], header_cells(r)[1:]
            sets[rank] = set(hc)
            if set(hc) != set(hr):
                res.error(f"{rank}: _counts and _relAbund tables list different samples")
    if sets:
        sizes = {rank: len(s) for rank, s in sets.items()}
        res.facts["samples_per_rank"] = sizes
        if len(set(sizes.values())) > 1:
            res.warn(f"sample counts differ across ranks: {sizes}")
        if manifest:
            expected = set(samples_from(manifest, d, ""))
            lost = sorted(expected - set.intersection(*sets.values()))
            if lost:
                res.warn(f"{len(lost)} manifest sample(s) are not in every feature table: {', '.join(lost[:10])}. The abundance --threshold can remove samples; state this in the report.")


def check_func(res, d, manifest):
    samples = samples_from(manifest, d, "*.pbs")
    res.facts["samples_expected"] = len(samples)
    if not samples:
        res.error("no samples found: give --manifest or check the Function_profiling directory")
    for s in samples:
        for f in ("genefamilies", "pathabundance", "pathcoverage"):
            expect(res, d / f"{s}_combine_{f}.tsv", f"{s}: {s}_combine_{f}.tsv")
    scan_logs_into(res, d)


def check_func_tables(res, d, manifest):
    for name in FUNC_TABLES:
        if not expect(res, d / name):
            continue
        first = header_cells(d / name)[0].strip()
        res.facts[f"{name} header"] = first
        if "genefamilies" in name and first != "# Gene":
            res.warn(f"{name}: header cell is '{first}'. mima-visualisation rejects '# Gene Family' (confirmed on Gadi: 'Could not detect data matrix format'); pass it a copy whose first header cell is '# Gene' (see references/troubleshooting.md).")
    scan_logs_into(res, d)


def check_vis_barplot(res, d, manifest):
    pdfs = list(d.glob("*.barchart.pdf"))
    res.facts["barchart_pdfs"] = len(pdfs)
    if not pdfs:
        res.error("no *.barchart.pdf files found")
    elif len(pdfs) < 4:
        res.warn(f"expected 4 bar chart PDFs (all samples and group means, with and without separate groups), found {len(pdfs)}")
    for pat in ("*.meta.txt", "*.table.txt"):
        if not list(d.glob(pat)):
            res.error(f"no {pat} files found")


def check_diversity(res, d, manifest):
    blocks = {"alpha": sorted(d.glob("*_alpha")), "beta": sorted(d.glob("*_beta")), "diff-abundance": sorted(d.glob("*_diff-abundance"))}
    for name, found in blocks.items():
        if not found:
            res.error(f"no *_{name} directory found in {d}")
    for a in blocks["alpha"]:
        expect(res, a / "alpha_diversity_raw_data.txt", f"{a.name}/alpha_diversity_raw_data.txt")
        for sub in ("corrected_sig", "non_sig", "uncorrected_sig"):
            n = len(list((a / "boxplots" / sub).glob("*.png"))) if (a / "boxplots" / sub).is_dir() else None
            if n is None:
                res.error(f"missing: {a.name}/boxplots/{sub}")
            else:
                res.facts[f"{a.name}/{sub} plots"] = n
    for b in blocks["beta"]:
        perm = list((b / "permanova").glob("*.tsv")) if (b / "permanova").is_dir() else []
        res.facts[f"{b.name} permanova result files"] = [p.name for p in perm]
        if not perm:
            res.error(f"no PERMANOVA result files in {b.name}/permanova")
        plots = list((b / "plots").glob("*.png")) if (b / "plots").is_dir() else []
        if not plots:
            res.error(f"no ordination plots in {b.name}/plots")
    for x in blocks["diff-abundance"]:
        for sub in ("barplot", "boxplots", "volcano", "wilcox-pairwise"):
            if not (x / sub).is_dir():
                res.error(f"missing: {x.name}/{sub}")
        for sub in ("corrected_sig", "uncorrected_sig"):
            p = x / "boxplots" / sub
            res.facts[f"{x.name}/{sub} plots"] = len(list(p.glob("*.png"))) if p.is_dir() else 0
    vis_log = d.parent / "visualisation.log"
    if vis_log.is_file():
        findings, _ = scan([str(vis_log)])
        for f in findings:
            if f["severity"] == "error":
                res.error(f"log signature '{f['pattern']}' in {f['file']} (line {f['lines'][0]['line']}): {f['cause']}")


def check_classifier(res, d, manifest):
    for f in ("cv_auc.pdf", "roc_classif.randomForest_dataset.pdf"):
        expect(res, d / f)
    per_input = sorted(d.glob("roc_data_*_classifier.pdf"))
    res.facts["inputs_classified"] = len(per_input)
    if not per_input:
        res.error("no roc_data_*_classifier.pdf files found")
    for p in per_input:
        twin = d / p.name.replace("_classifier", "")
        expect(res, twin, f"{twin.name} (pair of {p.name})")
    if not (d / "socket_cluster_log.txt").exists():
        res.warn("socket_cluster_log.txt not found")


STAGES = {"qc": check_qc, "taxa": check_taxa, "taxa-tables": check_taxa_tables, "func": check_func,
          "func-tables": check_func_tables, "vis-barplot": check_vis_barplot, "diversity": check_diversity,
          "classifier": check_classifier}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", required=True, choices=sorted(STAGES))
    ap.add_argument("--dir", required=True)
    ap.add_argument("--manifest")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()
    d = Path(args.dir)
    if not d.is_dir():
        print(f"Directory not found: {d}", file=sys.stderr)
        return 2
    res = Result(args.stage)
    STAGES[args.stage](res, d, args.manifest)

    if args.format == "json":
        print(json.dumps({"stage": res.stage, "ok": res.ok, "facts": res.facts, "findings": res.findings}, indent=2))
    else:
        order = {"error": 0, "warning": 1, "info": 2}
        for f in sorted(res.findings, key=lambda x: order[x["severity"]]):
            print(f"{f['severity'].upper():<7} {f['message']}")
        for k, v in res.facts.items():
            print(f"FACT    {k}: {v}")
        n_err = sum(1 for f in res.findings if f["severity"] == "error")
        print(f"\nRESULT: {'COMPLETE' if res.ok else 'INCOMPLETE'} for stage '{res.stage}' ({n_err} error(s))")
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
