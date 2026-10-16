#!/usr/bin/env python3
"""Draft the PBS header file (pbs_header_taxa.cfg) for the MIMA taxonomy-profiling module.

Usage:
    make_pbs_header.py --module taxa (--samples N | --manifest FILE) --image SIF --path DIR [--path DIR ...]
                       [--project CODE] [--queue NAME] [--cpus N] [--out FILE] [--apply] [--format text|json]

Dry run by default: the draft is shown with the source of every value and nothing is written. With --apply and --out,
the draft is written to a NEW file (an existing file is never overwritten). The draft is checked with validate_pbs_config.py.

What is worked out here:
  * memory, CPUs and walltime from the number of samples, using the project's taxonomy sizing guideline;
  * the storage line (#PBS -l storage=) from the folders the job touches (/scratch/X and /g/data/X);
  * the container bind paths from the same folders (each is bound to itself; nested folders are merged).
What is NOT guessed: the project code, the container image, and the folders. They must come from the user.
Only the taxonomy module has a sizing rule. QC and function profiling have none that the lab has set, so they are refused.
The walltime per sample is the project owner's figure, not a measured one: calibrate it on the first real run.

Exit code: 0 = prepared, 1 = needs input (or the draft failed validation), 2 = cannot read an input file or write the output.
"""
import argparse
import csv
import json
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
SAFE_PATH = re.compile(r"^/[A-Za-z0-9_./+-]+$")
PROJECT_OK = re.compile(r"^[a-z][a-z0-9]{1,15}$")
QUEUE_OK = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")


def walltime_minutes(samples):
    est = MINUTES_PER_SAMPLE * samples
    return max(int(1.5 * est), est + BUFFER_MIN)


def hms(minutes):
    return "%d:%02d:00" % (minutes // 60, minutes % 60)


def count_samples(manifest):
    raw = Path(manifest).read_bytes().decode("utf-8-sig", errors="replace")
    rows = [r for r in csv.reader(raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")) if any(c.strip() for c in r)]
    return max(len(rows) - 1, 0)  # the first row is the header


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


def merge_binds(paths):
    """Bind every folder to itself; drop folders that sit inside another listed folder."""
    clean = sorted(set(p.rstrip("/") for p in paths))
    keep = [p for p in clean if not any(p != q and p.startswith(q + "/") for q in clean)]
    return keep


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--module", required=True, help="taxa is the only module with a sizing rule")
    ap.add_argument("--samples", type=int, help="number of samples in the run")
    ap.add_argument("--manifest", help="the manifest to count samples from (use the prepared one)")
    ap.add_argument("--image", help="absolute path of the MIMA container image (.sif) or sandbox folder")
    ap.add_argument("--path", action="append", default=[], help="every absolute folder the job reads or writes (clean reads, outputs, database); repeat")
    ap.add_argument("--project", help="the user's project code; defaults to $PROJECT when that is set")
    ap.add_argument("--queue", help="queue name, only if the user knows it (a request of 320 GB usually needs a high-memory queue)")
    ap.add_argument("--cpus", type=int, default=DEFAULT_CPUS, help="default %d; the guideline is %d to %d" % ((DEFAULT_CPUS,) + CPU_RANGE))
    ap.add_argument("--no-module-load", action="store_true", help="leave out 'module load apptainer' (clusters where apptainer is always on the path)")
    ap.add_argument("--out", help="NEW file to write with --apply")
    ap.add_argument("--apply", action="store_true", help="write the file; without it nothing is written")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    a = ap.parse_args()

    needs, warns, src = [], [], {}
    if a.module != "taxa":
        needs.append("There is no sizing rule for the %s module: the lab has not set one, so I will not invent numbers. "
                     "Ask the user for a header file they trust (the tutorial example is a starting point), then validate it." % a.module)
    samples = a.samples
    if samples is None and a.manifest:
        if not Path(a.manifest).is_file():
            print("Cannot read manifest: %s" % a.manifest, file=sys.stderr)
            return 2
        samples = count_samples(a.manifest)
    if a.module == "taxa" and (not samples or samples < 1):
        needs.append("I need the number of samples: give --samples N or the prepared --manifest.")
    project = a.project or os.environ.get("PROJECT")
    if a.module == "taxa" and not project:
        needs.append("Which project (compute allocation) should the job be charged to? On Gadi this is the code shown by the 'groups' command or $PROJECT.")
    elif project and not PROJECT_OK.match(project):
        needs.append("The project code '%s' looks wrong: expected lower-case letters and digits, such as ab01." % project)
    if a.module == "taxa" and (not a.image or not SAFE_PATH.match(a.image)):
        needs.append("Where is the MIMA container image? I need its absolute path (a .sif file), and I will not guess it.")
    elif a.image and not Path(a.image).exists() and Path("/g").exists():
        warns.append("The image path %s does not exist on this machine." % a.image)
    paths = list(a.path)
    bad = [p for p in paths if not SAFE_PATH.match(p)]
    if bad:
        needs.append("These folders are not absolute paths made of letters, digits and _ . - + only: %s" % ", ".join(bad[:3]))
    elif a.module == "taxa" and not paths:
        needs.append("Which folders does the job use? List the clean-reads folder, the output folder and the reference database folder.")
    if a.queue and not QUEUE_OK.match(a.queue):
        needs.append("The queue name '%s' looks wrong." % a.queue)
    if a.out and not SAFE_PATH.match(a.out):
        needs.append("--out must be an absolute path made of letters, digits and _ . - + only.")
    if a.apply and not a.out:
        needs.append("Where should the header file be written? Give a new file name with --out.")
    if a.cpus < 1:
        needs.append("--cpus must be a positive number.")
    elif not CPU_RANGE[0] <= a.cpus <= CPU_RANGE[1]:
        warns.append("%d CPUs is outside the guideline of %d to %d for Kraken2." % ((a.cpus,) + CPU_RANGE))

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
        minutes = walltime_minutes(samples)
        binds = merge_binds(paths)
        src = {
            "name": "tutorial naming", "project": "user", "queue": "user", "ncpus": "project guideline (16 to 28), uncalibrated",
            "walltime": "lab estimate: %d min per sample plus %d min, x1.5 (project owner's figure, NOT measured); %d samples" % (MINUTES_PER_SAMPLE, BUFFER_MIN, samples),
            "mem": "lab estimate: the database needs 300 to 320 GB whatever the cohort size", "storage": "derived from the folders you listed",
            "join": "tutorial: needed so the logs are combined", "image": "user", "bind": "derived from the folders you listed"}
        lines = [("#!/bin/bash", "")]
        lines.append(("#PBS -N mima-taxa", src["name"]))
        lines.append(("#PBS -P %s" % project, src["project"]))
        if a.queue:
            lines.append(("#PBS -q %s" % a.queue, src["queue"]))
        lines += [("#PBS -l ncpus=%d" % a.cpus, src["ncpus"]), ("#PBS -l walltime=%s" % hms(minutes), src["walltime"]),
                  ("#PBS -l mem=%dGB" % MEM_GB, src["mem"])]
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
        if not a.queue:
            warns.append("No queue was given, so the job would use the default queue. %d GB is more than a standard node holds on most clusters; "
                         "ask the user which high-memory queue to use (a previous real taxonomy run on Gadi used -q hugemem)." % MEM_GB)
        if home:
            warns.append("Folders under your home directory need no storage entry and are available in the container by default.")
        warns.append("The walltime and memory are the lab's estimates, not measured values. Check the queue's limits, and compare the request with "
                     "'Walltime Used' and 'Memory Used' in the job log after the first real run.")
        with tempfile.NamedTemporaryFile("w", suffix=".cfg", delete=False) as tf:
            tf.write(text)
            tmp = tf.name
        try:
            v = subprocess.run([sys.executable, "-I", str(Path(__file__).with_name("validate_pbs_config.py")), tmp, "--module", "taxa",
                                "--samples", str(samples)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
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
