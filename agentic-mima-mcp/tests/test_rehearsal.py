"""A scripted rehearsal of the end-to-end QC scenario: the real client loop and the real server and scripts, with a
scripted model. It proves the plumbing (tool arguments, paths, the typed-yes gates, hand-over between steps), not what a
real model will decide. Needs Python 3.10+ and fastmcp."""
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from test_agent import SKILL, WRITE_TOOL, SUBMISSION_TOOL, call, run_agent

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "real_run_taxa_profiling.pbs"
SHEET = "sample,read1,read2\r\nPatient-01,{r}/S1_1.fastq.gz,{r}/S1_2.fastq.gz\r\nPatient-02,{r}/S2_1.fastq.gz,{r}/S2_2.fastq.gz\r\n"


class Rehearsal(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.reads = self.tmp / "raw_data"
        self.reads.mkdir()
        for n in ("S1_1", "S1_2", "S2_1", "S2_2"):
            (self.reads / (n + ".fastq.gz")).write_text("x")
        self.sheet = self.tmp / "my_samples.csv"
        self.sheet.write_bytes(SHEET.format(r=self.reads).encode())
        self.prepared, self.qc = self.tmp / "prepared", self.tmp / "out" / "QC_module"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_whole_flow_with_both_gates(self):
        md5 = hashlib.md5(self.sheet.read_bytes()).hexdigest()
        self.qc.mkdir(parents=True)
        shutil.copy(FIXTURE, self.qc / "qcModule_0.pbs")  # stands in for what the generator would write
        prep = {"manifest_path": str(self.sheet), "reads_dir": str(self.reads), "out_dir": str(self.prepared)}
        plan = {"steps": [{"module": "mima-qc", "params": {
            "-i": str(self.reads), "-o": str(self.tmp / "out"), "-m": str(self.prepared / "manifest.csv"),
            "--ref": str(self.tmp / "host.fa"), "--mode": "container", "--pbs-config": str(self.tmp / "hdr.cfg")}}]}
        replies = [
            call("mima_prepare_inputs", prep, "1"),
            call(WRITE_TOOL, prep, "2"),
            call("mima_validate_manifest", {"manifest_path": str(self.prepared / "manifest.csv"), "reads_dir": str(self.reads), "check_fs": True}, "3"),
            call("mima_build_commands", {"plan": plan}, "4"),
            call("mima_validate_pbs_scripts", {"paths": [str(self.qc)]}, "5"),
            call(SUBMISSION_TOOL, {"jobs": [{"id": "qc0", "script": str(self.qc / "qcModule_0.pbs")}], "run_dir": str(self.tmp), "check_fs": False}, "6"),
            {"role": "assistant", "content": "done"},
        ]
        asked = []

        def approve(name, args):
            asked.append(name)
            return True
        (self.tmp / "host.fa").write_text(">x\nA\n")
        (self.tmp / "hdr.cfg").write_text("#PBS -l ncpus=8\n#PBS -l walltime=2:00:00\n#PBS -l mem=64GB\n#PBS -j oe\nIMAGE_DIR=/x.sif\nexport APPTAINER_BIND=\"/tmp:/tmp\"\n")
        agent, final = run_agent(replies, "go", approve=approve)
        self.assertEqual(final, "done")
        self.assertEqual(asked, [WRITE_TOOL, SUBMISSION_TOOL])  # only the two writing tools needed a yes
        results = {}
        for m in agent.messages:
            if m["role"] == "tool":
                results[m["tool_call_id"]] = m["content"]
        self.assertIn('"status": "prepared"', results["1"])
        self.assertIn("Patient_01", results["1"])
        self.assertIn("written", results["2"])
        self.assertEqual(sorted(p.name for p in self.prepared.iterdir()), ["id_map.tsv", "manifest.csv"])
        self.assertEqual(hashlib.md5(self.sheet.read_bytes()).hexdigest(), md5)  # the user's sheet is untouched
        self.assertIn('"status": "valid"', results["3"])
        self.assertIn("apptainer run --app mima-qc", results["4"])
        self.assertIn("prepared/manifest.csv", results["4"])
        self.assertNotIn("Tool error", results["5"] + results["6"])
        self.assertIn("--execute", results["6"])
        self.assertIn('"submitted": false', results["6"].lower().replace('"submitted":false', '"submitted": false'))
