#!/usr/bin/env python3
"""Prepare a user's manifest and metadata so they follow the MIMA conventions.

It never edits the user's files. It reads them, works out every change needed, and (only with
--apply) writes corrected COPIES into a new folder, together with an id_map.tsv that maps each
original sample ID to the ID used by the pipeline. Anything that would change the meaning of the
data (R1/R2 possibly swapped, ambiguous columns, files in several folders, duplicate or colliding
IDs, several lanes per sample) is not guessed: it is listed under "needs your input" and nothing
is written.

Conventions applied (sources in references/validation-rules.md):
  manifest header exactly Sample_ID,FileID_R1,FileID_R2, comma-separated, LF line endings, no BOM,
  file names relative to the QC -i folder; metadata tab-separated with sample IDs in column 1;
  sample IDs use letters, digits and underscores only (general good practice, not proven necessary).

Usage:
  prepare_inputs.py --manifest FILE [--metadata FILE] [--reads-dir DIR] --out-dir NEW_DIR [--apply]
  prepare_inputs.py --from-reads DIR [--metadata FILE] --out-dir NEW_DIR [--apply]

Exit codes: 0 prepared or unchanged, 1 needs your input, 2 unreadable input or unusable output folder.
"""
import argparse
import csv
import io
import json
import os
import re
import sys
from pathlib import Path

HEADER = ["Sample_ID", "FileID_R1", "FileID_R2"]
FASTQ_EXT = (".fq.gz", ".fastq.gz", ".fq", ".fastq", ".fq.bz2", ".fastq.bz2")
FILE_OK = re.compile(r"^[A-Za-z0-9_.+=@:,-]+$")  # the characters build_command.py accepts in a name
ID_BAD = re.compile(r"[^A-Za-z0-9_]+")  # keep in step with validate_manifest.py (a test checks this)
PAIR = re.compile(r"^(?P<stem>.+?)(?P<sep>[_.-])R?(?P<read>[12])(?:_001)?(?P<ext>\.f(?:ast)?q(?:\.gz|\.bz2)?)$", re.I)
LANE = re.compile(r"^(?P<prefix>.+)_L\d{3}$")
SYN_ID = {"sample_id", "sampleid", "sample", "sample_name", "samplename", "id", "name", "sample_ids"}
SYN_R1 = {"fileid_r1", "r1", "read1", "read_1", "forward", "fwd", "fastq_1", "fastq1", "file1", "file_1", "fq1", "fq_1",
          "r1_file", "forward_file", "forward_read", "fastq_r1"}
SYN_R2 = {"fileid_r2", "r2", "read2", "read_2", "reverse", "rev", "fastq_2", "fastq2", "file2", "file_2", "fq2", "fq_2",
          "r2_file", "reverse_file", "reverse_read", "fastq_r2"}


def clean_id(value):
    """The pipeline ID for a sample name: letters, digits and underscores only."""
    return re.sub(r"_+", "_", ID_BAD.sub("_", value)).strip("_")


def key(name):
    return re.sub(r"[\s\-]+", "_", name.strip().lower())


def looks_r1(name):
    return any(t in name for t in ("_1", "_R1", ".1", "_r1"))


def looks_r2(name):
    return any(t in name for t in ("_2", "_R2", ".2", "_r2"))


class Report(object):
    def __init__(self):
        self.changes, self.needs, self.warns, self.id_map = [], [], [], []
        self.use = {}

    def change(self, file, detail):
        self.changes.append({"file": file, "detail": detail})

    def need(self, file, detail):
        self.needs.append({"file": file, "detail": detail})

    def warn(self, detail):
        self.warns.append(detail)


def read_text(path, rep, label):
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        rep.change(label, "removed a UTF-8 byte-order mark from the start of the file")
        raw = raw[3:]
    if b"\r" in raw:
        rep.change(label, "converted Windows (CRLF) line endings to Unix line endings")
        raw = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return raw.decode("utf-8", errors="replace")


def sniff(text, allowed):
    first = next((ln for ln in text.split("\n") if ln.strip()), "")
    counts = dict((d, first.count(d)) for d in allowed)
    best = max(allowed, key=lambda d: counts[d])
    return best if counts[best] else None


def parse(text, delim):
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delim) if r and any(c.strip() for c in r)]
    return rows


def name_of(d):
    return {",": "comma", "\t": "tab", ";": "semicolon"}[d]


def prepare_manifest(path, reads_dir, rep):
    text = read_text(path, rep, "manifest")
    delim = sniff(text, [",", "\t", ";"])
    if delim is None:
        rep.need("manifest", "The file has one column only. It needs three: sample name, forward file and reverse file.")
        return [], reads_dir
    if delim != ",":
        rep.change("manifest", "converted %s-separated columns to comma-separated" % name_of(delim))
    rows = parse(text, delim)
    if not rows:
        rep.need("manifest", "The file is empty.")
        return [], reads_dir
    rows = [[c.strip() for c in r] for r in rows]
    first = rows[0]
    headerless = len(first) >= 3 and first[1].endswith(FASTQ_EXT) and first[2].endswith(FASTQ_EXT)
    if headerless:
        idx = (0, 1, 2)
        rep.change("manifest", "added the header line Sample_ID,FileID_R1,FileID_R2 (the file had none)")
        body = rows
    else:
        keys = [key(h) for h in first]
        found = [[i for i, k in enumerate(keys) if k in syn] for syn in (SYN_ID, SYN_R1, SYN_R2)]
        if any(len(f) != 1 for f in found) or len(set(f[0] for f in found)) != 3:
            rep.need("manifest", "I cannot tell which columns hold the sample name, the forward file and the reverse file. "
                     "Columns found: %s. Tell me which is which." % ", ".join(first))
            return [], reads_dir
        idx = (found[0][0], found[1][0], found[2][0])
        if [first[i] for i in idx] != HEADER:
            rep.change("manifest", "renamed the header from %s to %s" % (",".join(first[i] for i in idx), ",".join(HEADER)))
        extra = [h for i, h in enumerate(first) if i not in idx]
        if extra:
            rep.change("manifest", "left out columns the pipeline does not use: %s" % ", ".join(extra))
        body = rows[1:]
    if not body:
        rep.need("manifest", "The file has a header but no samples.")
        return [], reads_dir
    out, dirs = [], set()
    for n, row in enumerate(body, start=1 if headerless else 2):
        if len(row) <= max(idx):
            rep.need("manifest", "Line %d has %d columns; expected at least %d." % (n, len(row), max(idx) + 1))
            continue
        sid, r1, r2 = row[idx[0]], row[idx[1]], row[idx[2]]
        if not (sid and r1 and r2):
            rep.need("manifest", "Line %d has an empty sample name or file name." % n)
            continue
        files = []
        for f in (r1, r2):
            if f.startswith("/"):
                dirs.add(os.path.dirname(f))
                f = os.path.basename(f)
            files.append(f)
        if files[0] == files[1]:
            rep.need("manifest", "Line %d: the forward and reverse file are the same ('%s')." % (n, files[0]))
        if looks_r2(files[0]) and looks_r1(files[1]) and not looks_r1(files[0]) and not looks_r2(files[1]):
            rep.need("manifest", "Line %d: the forward file '%s' looks like read 2 and the reverse file '%s' like read 1. "
                     "I did not swap them; tell me which is right." % (n, files[0], files[1]))
        out.append((sid, files[0], files[1]))
    if dirs:
        if len(dirs) == 1 and reads_dir in (None, "", next(iter(dirs))):
            reads_dir = next(iter(dirs))
            rep.change("manifest", "file names were full paths; kept the names only. The reads folder (-i) is %s" % reads_dir)
        elif len(dirs) > 1:
            rep.need("manifest", "The files are in several folders (%s). The pipeline reads all samples from one folder." % ", ".join(sorted(dirs)))
        else:
            rep.need("manifest", "The file paths point to %s but you gave the reads folder %s." % (next(iter(dirs)), reads_dir))
    return out, reads_dir


def prepare_from_reads(reads_dir, rep):
    pairs, lanes, unpaired = {}, {}, []
    for f in sorted(os.listdir(reads_dir)):
        if not f.endswith(FASTQ_EXT):
            continue
        m = PAIR.match(f)
        if not m:
            unpaired.append(f)
            continue
        slot = pairs.setdefault(m.group("stem"), {})
        if m.group("read") in slot:
            rep.need("reads", "Two files look like read %s of '%s': %s and %s." % (m.group("read"), m.group("stem"), slot[m.group("read")], f))
        slot[m.group("read")] = f
    if not pairs and not unpaired:
        rep.need("reads", "No FASTQ files found in %s." % reads_dir)
    for f in unpaired:
        rep.need("reads", "'%s' does not look like a read 1 or read 2 file (no _1/_2 or _R1/_R2), so I cannot pair it." % f)
    out = []
    for stem, slot in sorted(pairs.items()):
        if "1" not in slot or "2" not in slot:
            rep.need("reads", "'%s' has only read %s (%s); the pair is incomplete." % (stem, next(iter(slot)), next(iter(slot.values()))))
            continue
        m = LANE.match(stem)
        if m:
            lanes.setdefault(m.group("prefix"), []).append(stem)
        out.append((stem, slot["1"], slot["2"]))
    for prefix, stems in lanes.items():
        if len(stems) > 1:
            rep.need("reads", "Sample '%s' is split over %d lanes (%s). The pipeline takes one pair per sample, so the lanes would "
                     "have to be joined first. I do not do that." % (prefix, len(stems), ", ".join(sorted(stems))))
    if out:
        rep.change("manifest", "built the manifest from %d read pairs found in %s, using each file-name stem as the sample name" % (len(out), reads_dir))
    return out


def apply_ids(rows, rep):
    """rows: (sample, r1, r2). Returns rows with pipeline IDs and fills rep.id_map; flags duplicates and collisions."""
    seen, new_of, result = {}, {}, []
    for sid, r1, r2 in rows:
        if sid in seen:
            rep.need("manifest", "The sample name '%s' appears more than once." % sid)
            continue
        seen[sid] = True
        new = clean_id(sid)
        if not new:
            rep.need("manifest", "The sample name '%s' has no letters or digits to build an ID from." % sid)
            continue
        new_of.setdefault(new, []).append(sid)
        rep.id_map.append({"original": sid, "pipeline": new, "changed": new != sid})
        result.append((new, r1, r2))
    for new, origs in new_of.items():
        if len(origs) > 1:
            rep.need("manifest", "These sample names would all become '%s': %s. Tell me how to tell them apart." % (new, ", ".join(origs)))
    changed = [m for m in rep.id_map if m["changed"]]
    if changed:
        rep.change("manifest", "renamed %d sample name(s) to use letters, digits and underscores only (for example '%s' to '%s'); "
                   "the full list is in id_map.tsv" % (len(changed), changed[0]["original"], changed[0]["pipeline"]))
    files = {}
    for sid, r1, r2 in result:
        for f in (r1, r2):
            if f in files and files[f] != sid:
                rep.need("manifest", "The file '%s' is used for both '%s' and '%s'." % (f, files[f], sid))
            files.setdefault(f, sid)
    return result


def prepare_metadata(path, rep):
    text = read_text(path, rep, "metadata")
    delim = "\t" if "\t" in next((ln for ln in text.split("\n") if ln.strip()), "") else sniff(text, [",", ";"])
    if delim is None:
        rep.need("metadata", "The file has one column only; metadata needs a sample ID column and at least one more.")
        return None
    if delim != "\t":
        rep.change("metadata", "converted %s-separated columns to tab-separated" % name_of(delim))
    rows = parse(text, delim)
    if len(rows) < 2:
        rep.need("metadata", "The file needs a header line and at least one sample.")
        return None
    header = [c.strip() for c in rows[0]]
    if any(not h for h in header):
        rep.need("metadata", "A column in the header line has no name.")
        return None
    if header != rows[0]:
        rep.change("metadata", "removed spaces around column names")
    lookup = dict((m["original"], m["pipeline"]) for m in rep.id_map)
    out, seen, new_of, trimmed, renamed = [header], set(), {}, False, 0
    for n, row in enumerate(rows[1:], start=2):
        if len(row) != len(header):
            rep.need("metadata", "Line %d has %d columns; the header has %d." % (n, len(row), len(header)))
            continue
        cells = [c.strip() for c in row]
        trimmed = trimmed or cells != row
        if any("\t" in c or "\n" in c for c in cells):
            rep.need("metadata", "Line %d has a tab or line break inside a value." % n)
            continue
        sid = cells[0]
        if not sid:
            rep.need("metadata", "Line %d has no sample ID." % n)
            continue
        if sid in seen:
            rep.need("metadata", "The sample ID '%s' appears more than once." % sid)
            continue
        seen.add(sid)
        new = lookup.get(sid) or clean_id(sid)
        if not new:
            rep.need("metadata", "The sample ID '%s' has no letters or digits." % sid)
            continue
        new_of.setdefault(new, []).append(sid)
        if sid not in lookup:
            rep.id_map.append({"original": sid, "pipeline": new, "changed": new != sid})
        renamed += new != sid
        cells[0] = new
        out.append(cells)
    for new, origs in new_of.items():
        if len(origs) > 1:
            rep.need("metadata", "These sample IDs would all become '%s': %s." % (new, ", ".join(origs)))
    if trimmed:
        rep.change("metadata", "removed spaces around values")
    if renamed:
        rep.change("metadata", "renamed %d sample ID(s) in the first column to match the manifest rules" % renamed)
    return out


def tsv(rows, delim):
    buf = io.StringIO()
    csv.writer(buf, delimiter=delim, lineterminator="\n").writerows(rows)
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", help="the user's manifest or sample sheet (any common layout)")
    ap.add_argument("--from-reads", metavar="DIR", help="build the manifest from the FASTQ files in this folder")
    ap.add_argument("--reads-dir", help="folder holding the FASTQ files (the QC -i folder); file existence is checked when given")
    ap.add_argument("--metadata", help="the user's study metadata")
    ap.add_argument("--out-dir", required=True, help="NEW folder for the prepared copies; must not exist or must be empty")
    ap.add_argument("--apply", action="store_true", help="write the files; without it nothing is written")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    a = ap.parse_args()

    if bool(a.manifest) == bool(a.from_reads):
        ap.error("give exactly one of --manifest or --from-reads")
    rep = Report()
    for label, p in (("manifest", a.manifest), ("metadata", a.metadata)):
        if p and not Path(p).is_file():
            print("Cannot read %s file: %s" % (label, p), file=sys.stderr)
            return 2
    if a.from_reads and not Path(a.from_reads).is_dir():
        print("Not a folder: %s" % a.from_reads, file=sys.stderr)
        return 2
    out_dir = Path(a.out_dir)
    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        print("The output folder %s already exists and is not empty. Choose a new folder; nothing is overwritten." % out_dir, file=sys.stderr)
        return 2
    if a.apply and not out_dir.parent.is_dir():
        print("The parent folder %s does not exist." % out_dir.parent, file=sys.stderr)
        return 2

    reads_dir = a.reads_dir or a.from_reads
    if a.from_reads:
        rows = prepare_from_reads(a.from_reads, rep)
    else:
        rows, reads_dir = prepare_manifest(a.manifest, reads_dir, rep)
    rows = apply_ids(rows, rep)
    bad = sorted(set(f for _, r1, r2 in rows for f in (r1, r2) if not FILE_OK.match(f)))
    if bad:
        rep.need("manifest", "%d file name(s) contain spaces or unusual characters, for example '%s'. The names end up in generated shell scripts, "
                 "and I never rename your data files. Rename them to use letters, digits and _ . - only, then run me again." % (len(bad), bad[0]))
    if reads_dir and Path(reads_dir).is_dir():
        missing = [f for _, r1, r2 in rows for f in (r1, r2) if not (Path(reads_dir) / f).is_file()]
        if missing:
            rep.need("manifest", "%d file(s) are not in %s, for example %s." % (len(missing), reads_dir, ", ".join(missing[:3])))
    elif reads_dir:
        rep.need("manifest", "The reads folder %s does not exist." % reads_dir)
    meta = prepare_metadata(a.metadata, rep) if a.metadata else None
    if meta:
        ms, fs = set(r[0] for r in meta[1:]), set(r[0] for r in rows)
        if rows and ms - fs:
            rep.warn("%d sample(s) are in the metadata but not the manifest, for example %s." % (len(ms - fs), sorted(ms - fs)[0]))
        if rows and fs - ms:
            rep.warn("%d sample(s) are in the manifest but not the metadata, for example %s." % (len(fs - ms), sorted(fs - ms)[0]))

    planned = {}
    if rows and not rep.needs and (rep.changes or a.from_reads):
        planned["manifest.csv"] = tsv([HEADER] + [list(r) for r in rows], ",")
    if meta and not rep.needs and any(c["file"] == "metadata" for c in rep.changes):
        planned["metadata.tsv"] = tsv(meta, "\t")
    if planned:
        planned["id_map.tsv"] = tsv([["original_id", "pipeline_id", "changed"]] +
                                    [[m["original"], m["pipeline"], "yes" if m["changed"] else "no"] for m in rep.id_map], "\t")
    status = "needs_input" if rep.needs else ("prepared" if planned else "unchanged")
    written = []
    if a.apply and status == "prepared":
        out_dir.mkdir(exist_ok=True)
        for name, content in planned.items():
            (out_dir / name).write_text(content)
            written.append(str(out_dir / name))
    use = {"reads_dir": reads_dir}
    if a.manifest or a.from_reads:
        use["manifest"] = str(out_dir / "manifest.csv") if "manifest.csv" in planned else a.manifest
    if a.metadata:
        use["metadata"] = str(out_dir / "metadata.tsv") if "metadata.tsv" in planned else a.metadata
    data = {"status": status, "applied": bool(written), "changes": rep.changes, "needs_input": rep.needs, "warnings": rep.warns,
            "id_map": rep.id_map, "files": [str(out_dir / n) for n in planned], "written": written,
            "use": use if status != "needs_input" else {}}
    if a.format == "json":
        print(json.dumps(data, indent=2))
    else:
        for c in rep.changes:
            print("CHANGE [%s]: %s" % (c["file"], c["detail"]))
        for n in rep.needs:
            print("NEEDS YOUR INPUT [%s]: %s" % (n["file"], n["detail"]))
        for w in rep.warns:
            print("WARN:  %s" % w)
        if status == "prepared":
            print(("WROTE" if written else "WOULD WRITE (dry run, nothing written; add --apply)") + ": " + ", ".join(data["files"]))
        elif status == "unchanged":
            print("Nothing to change: your files already follow the conventions. Use them as they are.")
        print("RESULT: " + status.upper().replace("_", " "))
    return 1 if status == "needs_input" else 0


if __name__ == "__main__":
    sys.exit(main())
