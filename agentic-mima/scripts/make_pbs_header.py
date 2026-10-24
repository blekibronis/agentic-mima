#!/usr/bin/env python3
"""Draft the PBS header file (pbs_header_qc.cfg, pbs_header_taxa.cfg or pbs_header_func.cfg) for a MIMA module.

Usage:
    make_pbs_header.py --module qc|taxa|func (--samples N | --manifest FILE) --image SIF --path DIR [--path DIR ...]
                       [--reads-dir DIR] [--num-pbs-jobs N] [--project CODE] [--queue NAME] [--cpus N]
                       [--out FILE] [--apply] [--format text|json]

Dry run by default: the draft is shown with the source of every value and nothing is written. With --apply and --out,
the draft is written to a NEW file (an existing file is never overwritten). The draft is checked with validate_pbs_config.py.

What is worked out here:
  * memory, CPUs and walltime for the module (see "Sizing" below);
  * the storage line (#PBS -l storage=) from the folders the job touches (/scratch/X and /g/data/X);
  * the container bind paths from the same folders (each is bound to itself; nested folders are merged).
What is NOT guessed: the project code, the container image, and the folders. They must come from the user.

Sizing (every figure is an estimate; calibrate on the first real run):
  taxa  Lab guideline: 320 GB, 28 CPUs, (20 min per sample + 60 min) x 1.5. The 20 minutes is the project owner's figure.
  qc    Tutorial header: 8 CPUs, 64 GB; the tutorial runs 3 samples per job in 2 h, i.e. 40 min per sample at the reference depth.
  func  Tutorial example: 8 CPUs, 64 GB, 8 h per sample (one PBS job per sample); the module's own default is 24 h.
  For qc and func the lab has set no sizing. The walltime is the tutorial figure scaled up when the largest sample's FASTQ files
  (found through --manifest and --reads-dir) are bigger than the reference size of REF_GB GB, times a 1.5 margin, capped at 48 h.
  The reference size is a rough assumption (about 20M read pairs of 2x150 bp, gzipped), not a lab figure.

Exit code: 0 = prepared, 1 = needs input (or the draft failed validation), 2 = cannot read an input file or write the output.
"""
import argparse
import csv
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Keep these equal to validate_pbs_config.py; the tests check that a generated header validates without warnings.
MEM_GB = 320            # lab-defaults.md: 300 to 320 GB whatever the cohort size
DEFAULT_CPUS = 28       # guideline range 16 to 28
CPU_RANGE = (16, 28)
MINUTES_PER_SAMPLE = 20  # project owner's figure, uncalibrated
BUFFER_MIN = 60
MARGIN = 1.5
REF_GB = 5.0             # assumed gzipped FASTQ size (R1 + R2) of the tutorial-scale sample; a rough assumption
MAX_MIN = 48 * 60        # a long request may exceed the queue limit; the user must check
# module: job name, CPUs, memory GB, minutes per sample (per sample for func, per sample within a job for qc), source of the figures
SIZING = {
    "qc": dict(name="mima-qc", cpus=8, mem=64, per_sample=40, src="tutorial (QC header: 8 CPUs, 64 GB, 2 h for 3 samples per job)"),
    "func": dict(name="mima-func", cpus=8, mem=64, per_sample=480, src="tutorial (function example: 8 CPUs, 64 GB, 8 h per sample)"),
}
SAFE_PATH = re.compile(r"^/[A-Za-z0-9_./+-]+$")
PROJECT_OK = re.compile(r"^[a-z][a-z0-9]{1,15}$")
QUEUE_OK = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")


def sized_minutes(module, samples_per_job, gb):
    """Walltime for qc and func: tutorial minutes per sample, scaled up for larger files, times the margin."""
    scale = max(1.0, gb / REF_GB) if gb else 1.0
    minutes = int(math.ceil(SIZING[module]["per_sample"] * samples_per_job * scale * MARGIN))
    return max(minutes, 60)


def walltime_minutes(samples):
    est = MINUTES_PER_SAMPLE * samples
    return max(int(1.5 * est), est + BUFFER_MIN)


def hms(minutes):
    return "%d:%02d:00" % (minutes // 60, minutes % 60)


def read_manifest(manifest):
    raw = Path(manifest).read_bytes().decode("utf-8-sig", errors="replace")
    rows = [r for r in csv.reader(raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")) if any(c.strip() for c in r)]
    return rows[1:]  # the first row is the header


def largest_sample_gb(rows, reads_dir):
    """Size in GiB of the biggest sample's read files, or None when no sample's files can be found."""
    best = None
    for r in rows:
        if len(r) < 3:
            continue
        sample = r[0].strip()
        for names in ((r[1].strip(), r[2].strip()), (sample + "_clean_1.fq.gz", sample + "_clean_2.fq.gz")):
            files = [Path(reads_dir) / n for n in names]
            if all(f.is_file() for f in files):
                total = sum(f.stat().st_size for f in files) / 1024.0 ** 3
                best = total if best is None else max(best, total)
                break
    return best


def storage_for(paths):
    """Return (storage entries, paths that need no entry, paths this script cannot map)."""
    entries, home, unknown = [], [], []
    for p in paths:
        m = re.match(r"^/scratch/([a-z0-9]+)(/|$)", p) or None
        g = re.match(r"^/g/data/([a-z0-9]+)(/|$)", p) or None
        if m:
            e = "scratch/" + m.group(1)
        elif g:
            e = "gdata/" + g.group(1)
        elif p.startswith("/home/") or p.startswith(os.path.expanduser("~") + "/"):
            home.append(p)
            continue
        else:
            unknown.append(p)
            continue
        if e not in entries:
            entries.append(e)
    return sorted(entries), home, unknown


FOLDER_HELP = {
    "qc": "List the raw reads folder, the output folder and the folder holding the host reference genome.",
    "taxa": "List the clean-reads folder, the output folder and the reference database folder.",
    "func": "List the clean-reads folder, the output folder and the folder holding the HUMAnN3 and MetaPhlAn databases.",
}


def merge_binds(paths):
    """Bind every folder to itself; drop folders that sit inside another listed folder."""
    clean = sorted(set(p.rstrip("/") for p in paths))
    keep = [p for p in clean if not any(p != q and p.startswith(q + "/") for q in clean)]
    return keep


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--module", required=True, choices=("qc", "taxa", "func"))
    ap.add_argument("--samples", type=int, help="number of samples in the run")
    ap.add_argument("--manifest", help="the manifest to count samples from (use the prepared one)")
    ap.add_argument("--image", help="absolute path of the MIMA container image (.sif) or sandbox folder")
    ap.add_argument("--path", action="append", default=[], help="every absolute folder the job reads or writes (clean reads, outputs, database); repeat")
    ap.add_argument("--project", help="the user's project code; defaults to $PROJECT when that is set")
    ap.add_argument("--queue", help="queue name, only if the user knows it (a request of 320 GB usually needs a high-memory queue)")
    ap.add_argument("--cpus", type=int, help="default: taxa %d (guideline %d to %d), qc and func 8" % ((DEFAULT_CPUS,) + CPU_RANGE))
    ap.add_argument("--reads-dir", help="folder holding the FASTQ files named in the manifest (or the CleanReads folder); used to size qc and func by file size")
    ap.add_argument("--num-pbs-jobs", type=int, default=4, help="qc only: the --num-pbs-jobs given to mima-qc (its default is 4)")
    ap.add_argument("--no-module-load", action="store_true", help="leave out 'module load apptainer' (clusters where apptainer is always on the path)")
    ap.add_argument("--out", help="NEW file to write with --apply")
    ap.add_argument("--apply", action="store_true", help="write the file; without it nothing is written")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    a = ap.parse_args()

    needs, warns, src = [], [], {}
    samples, rows = a.samples, []
    if a.manifest:
        if not Path(a.manifest).is_file():
            print("Cannot read manifest: %s" % a.manifest, file=sys.stderr)
            return 2
        rows = read_manifest(a.manifest)
        if samples is None:
            samples = len(rows)
    if not samples or samples < 1:
        needs.append("I need the number of samples: give --samples N or the prepared --manifest.")
    if a.num_pbs_jobs < 1:
        needs.append("--num-pbs-jobs must be at least 1.")
    if a.reads_dir and not SAFE_PATH.match(a.reads_dir):
        needs.append("--reads-dir must be an absolute path made of letters, digits and _ . - + only.")
    project = a.project or os.environ.get("PROJECT")
    if not project:
        needs.append("Which project (compute allocation) should the job be charged to? On Gadi this is the code shown by the 'groups' command or $PROJECT.")
    elif project and not PROJECT_OK.match(project):
        needs.append("The project code '%s' looks wrong: expected lower-case letters and digits, such as ab01." % project)
    if not a.image or not SAFE_PATH.match(a.image):
        needs.append("Where is the MIMA container image? I need its absolute path (a .sif file), and I will not guess it.")
    elif a.image and not Path(a.image).exists() and Path("/g").exists():
        warns.append("The image path %s does not exist on this machine." % a.image)
    paths = list(a.path)
    bad = [p for p in paths if not SAFE_PATH.match(p)]
    if bad:
        needs.append("These folders are not absolute paths made of letters, digits and _ . - + only: %s" % ", ".join(bad[:3]))
    elif not paths:
        needs.append("Which folders does the job use? %s" % FOLDER_HELP[a.module])
    if a.queue and not QUEUE_OK.match(a.queue):
        needs.append("The queue name '%s' looks wrong." % a.queue)
    if a.out and not SAFE_PATH.match(a.out):
        needs.append("--out must be an absolute path made of letters, digits and _ . - + only.")
    if a.apply and not a.out:
        needs.append("Where should the header file be written? Give a new file name with --out.")
    cpus = a.cpus or (DEFAULT_CPUS if a.module == "taxa" else SIZING[a.module]["cpus"])
    if cpus < 1:
        needs.append("--cpus must be a positive number.")
    elif a.module == "taxa" and not CPU_RANGE[0] <= cpus <= CPU_RANGE[1]:
        warns.append("%d CPUs is outside the guideline of %d to %d for Kraken2." % ((cpus,) + CPU_RANGE))

    storage, home, unknown = storage_for(paths if not bad else [])
    if unknown:
        needs.append("I cannot tell which storage allocation covers %s. Only /scratch/CODE and /g/data/CODE folders are mapped; tell me the storage line to use." % ", ".join(unknown[:3]))
    if a.out and Path(a.out).exists():
        needs.append("%s already exists. I never overwrite a file; choose a new name." % a.out)
    if a.out and not Path(a.out).parent.is_dir():
        needs.append("The folder for %s does not exist." % a.out)

    data = {"status": "needs_input", "applied": False, "needs_input": needs, "warnings": warns, "lines": [], "validation": [],
            "written": None, "samples": samples}
    if not needs:
        binds = merge_binds(paths)
        if a.module == "taxa":
            minutes, mem = walltime_minutes(samples), MEM_GB
            how = {"ncpus": "project guideline (16 to 28), uncalibrated",
                   "walltime": "lab estimate: %d min per sample plus %d min, x1.5 (project owner's figure, NOT measured); %d samples" % (MINUTES_PER_SAMPLE, BUFFER_MIN, samples),
                   "mem": "lab estimate: the database needs 300 to 320 GB whatever the cohort size"}
            jobname = "mima-taxa"
        else:
            z = SIZING[a.module]
            per_job = int(math.ceil(samples / float(min(a.num_pbs_jobs, samples)))) if a.module == "qc" else 1
            gb = largest_sample_gb(rows, a.reads_dir) if rows and a.reads_dir else None
            minutes = sized_minutes(a.module, per_job, gb)
            if minutes > MAX_MIN:
                warns.append("The walltime would be %s, over 48 hours, so it is capped at 48:00:00. That is the longest many queues allow, "
                             "and the job may still not finish: ask the user to check the queue's limit or split the samples." % hms(minutes))
                minutes = MAX_MIN
            mem, jobname = z["mem"], z["name"]
            size_note = ("largest sample %.1f GB (reference %.0f GB)" % (gb, REF_GB)) if gb else "file sizes not measured, so the reference size is assumed"
            per_unit = "%d min per sample x %d sample(s) per job" % (z["per_sample"], per_job) if a.module == "qc" else "%d h per sample, one job per sample" % (z["per_sample"] // 60)
            how = {"ncpus": z["src"], "mem": z["src"],
                   "walltime": "tutorial figure (%s), scaled by file size, x%.1f margin; %s; NOT measured" % (per_unit, MARGIN, size_note)}
            if not gb:
                warns.append("I could not measure the read files (give --manifest and --reads-dir), so the walltime assumes a sample of about %.0f GB gzipped (roughly 20M read pairs). "
                             "Bigger samples need more time." % REF_GB)
            if a.module == "qc":
                warns.append("Add the folder holding the host reference genome to the folders, or the container cannot see it. The header is used for every QC job "
                             "(%d sample(s) per job with --num-pbs-jobs %d)." % (per_job, a.num_pbs_jobs))
        src = {
            "name": "tutorial naming", "project": "user", "queue": "user", "ncpus": how["ncpus"], "walltime": how["walltime"], "mem": how["mem"],
            "storage": "derived from the folders you listed", "join": "tutorial: needed so the logs are combined", "image": "user",
            "bind": "derived from the folders you listed"}
        lines = [("#!/bin/bash", "")]
        lines.append(("#PBS -N %s" % jobname, src["name"]))
        lines.append(("#PBS -P %s" % project, src["project"]))
        if a.queue:
            lines.append(("#PBS -q %s" % a.queue, src["queue"]))
        lines += [("#PBS -l ncpus=%d" % cpus, src["ncpus"]), ("#PBS -l walltime=%s" % hms(minutes), src["walltime"]),
                  ("#PBS -l mem=%dGB" % mem, src["mem"])]
        if storage:
            lines.append(("#PBS -l storage=%s" % "+".join(storage), src["storage"]))
        lines.append(("#PBS -j oe", src["join"]))
        lines.append(("set -x", ""))
        if not a.no_module_load:
            lines.append(("module load apptainer", ""))
        lines.append(("IMAGE_DIR=%s" % a.image, src["image"]))
        lines.append(('export APPTAINER_BIND="%s"' % ",".join("%s:%s" % (p, p) for p in binds), src["bind"]))
        text = "\n".join(l for l, _ in lines) + "\n"
        data["lines"] = [{"line": l, "source": s} for l, s in lines]
        if not a.queue and a.module == "taxa":
            warns.append("No queue was given, so the job would use the default queue. %d GB is more than a standard node holds on most clusters; "
                         "ask the user which high-memory queue to use (a previous real taxonomy run on Gadi used -q hugemem)." % MEM_GB)
        if home:
            warns.append("Folders under your home directory need no storage entry and are available in the container by default.")
        warns.append("The generator's -t (threads) is set to %d by the command builder when this header is passed as --pbs-config, so the tools and PBS use the same number." % cpus)
        warns.append("The walltime and memory are estimates, not measured values. Check the queue's limits, and compare the request with "
                     "'Walltime Used' and 'Memory Used' in the job log after the first real run.")
        with tempfile.NamedTemporaryFile("w", suffix=".cfg", delete=False) as tf:
            tf.write(text)
            tmp = tf.name
        try:
            v = subprocess.run([sys.executable, "-I", str(Path(__file__).with_name("validate_pbs_config.py")), tmp, "--module", a.module] +
                               (["--samples", str(samples)] if a.module == "taxa" else []), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
            data["validation"] = [ln for ln in v.stdout.splitlines() if ln.strip()]
            valid = v.returncode == 0
        finally:
            os.unlink(tmp)
        if not valid:
            data["status"] = "invalid"
        else:
            data["status"] = "prepared"
            if a.apply:
                try:
                    with open(a.out, "x") as f:
                        f.write(text)
                except OSError as exc:
                    print("Cannot write %s: %s" % (a.out, exc), file=sys.stderr)
                    return 2
                data["applied"], data["written"] = True, a.out
        data["use"] = {"pbs_config": a.out} if data["written"] else {}
    data["warnings"] = warns

    if a.format == "json":
        print(json.dumps(data, indent=2))
    else:
        for l in data["lines"]:
            print("%-60s %s" % (l["line"], ("# " + l["source"]) if l["source"] else ""))
        for n in needs:
            print("NEEDS YOUR INPUT: %s" % n)
        for ln in data["validation"]:
            print("CHECK: %s" % ln)
        for w in warns:
            print("WARN:  %s" % w)
        if data["written"]:
            print("WROTE: %s" % data["written"])
        elif data["status"] == "prepared":
            print("WOULD WRITE (dry run, nothing written; add --apply --out NEWFILE)")
        print("RESULT: " + data["status"].upper().replace("_", " "))
    return 0 if data["status"] == "prepared" else 1


if __name__ == "__main__":
    sys.exit(main())
