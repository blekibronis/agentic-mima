"""Tests for the MCP server. Run: python3 -m unittest discover -s tests -v  (needs Python 3.10+ and fastmcp)."""
import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SKILL = Path(os.environ.get("MIMA_SKILL_DIR", ROOT.parent / "agentic-mima"))
FIXTURE = HERE / "fixtures" / "real_run_taxa_profiling.pbs"  # a real Gadi taxonomy script
sys.path.insert(0, str(ROOT))
os.environ["MIMA_SKILL_DIR"] = str(SKILL)

from fastmcp import Client  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

from mima_mcp import runner  # noqa: E402
from mima_mcp.server import mcp  # noqa: E402

EXPECTED_TOOLS = {"mima_list_modules", "mima_build_commands", "mima_validate_manifest", "mima_validate_metadata", "mima_validate_pbs_config",
                  "mima_validate_pbs_scripts", "mima_estimate_read_length", "mima_check_outputs", "mima_check_job_status", "mima_scan_logs",
                  "mima_check_report", "mima_prepare_submission", "mima_prepare_inputs", "mima_write_prepared_inputs",
                  "mima_plan_pbs_header", "mima_write_pbs_header"}
BRACKEN = ">> ERROR: Input file /scratch/x/kraken2/S1.kraken2.report does not exist"


def call(tool, args):
    async def go():
        async with Client(mcp) as c:
            return (await c.call_tool(tool, args)).data
    return asyncio.run(go())


def call_error(tool, args):
    async def go():
        async with Client(mcp) as c:
            try:
                await c.call_tool(tool, args)
            except ToolError as exc:
                return str(exc)
        return None
    return asyncio.run(go())


async def _tool_doc(name):
    async with Client(mcp) as c:
        return [(tt.description or "") for tt in await c.list_tools() if tt.name == name]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ.pop("MIMA_ALLOWED_ROOTS", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("MIMA_ALLOWED_ROOTS", None)


class TestStartup(Base):
    def run_cli(self, *args, skill=None):
        env = {k: v for k, v in os.environ.items() if k != "MIMA_SKILL_DIR"}
        if skill is not None:
            env["MIMA_SKILL_DIR"] = str(skill)
        env["PYTHONPATH"] = str(ROOT)
        return subprocess.run([sys.executable, "-m", "mima_mcp", *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, timeout=60, env=env)

    def test_help_exits_without_starting_the_server(self):
        p = self.run_cli("--help")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("--check", p.stdout)

    def test_check_passes_with_the_real_skill(self):
        p = self.run_cli("--check", skill=SKILL)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("OK", p.stdout)

    def test_check_names_the_problem_when_the_skill_is_missing_or_incomplete(self):
        p = self.run_cli("--check")
        self.assertEqual(p.returncode, 2)
        self.assertIn("MIMA_SKILL_DIR", p.stderr)
        (self.tmp / "scripts").mkdir()
        (self.tmp / "scripts" / "scan_logs.py").write_text("x")
        p = self.run_cli("--check", skill=self.tmp)
        self.assertEqual(p.returncode, 2)
        self.assertIn("validate_manifest.py", p.stderr)


class TestToolSurface(Base):
    def test_exact_tool_list_and_no_submit_tool(self):
        async def go():
            async with Client(mcp) as c:
                return {t.name for t in await c.list_tools()}
        names = asyncio.run(go())
        self.assertEqual(names, EXPECTED_TOOLS)
        self.assertFalse([n for n in names if "execute" in n or n in ("submit", "submit_jobs", "qsub", "mima_submit")])

    def test_annotations_match_behaviour(self):
        async def go():
            async with Client(mcp) as c:
                return {t.name: t.annotations for t in await c.list_tools()}
        ann = asyncio.run(go())
        for name, a in ann.items():
            self.assertIsNotNone(a, name)
            d = a.model_dump(by_alias=True)
            self.assertTrue(d["title"], name)
            self.assertTrue(name.startswith("mima_"), name)
            self.assertIs(d["destructiveHint"], False, name)
            self.assertIs(d["openWorldHint"], False, name)
            self.assertIs(d["readOnlyHint"], name not in ("mima_prepare_submission", "mima_write_prepared_inputs", "mima_write_pbs_header"), name)

    def test_runner_refuses_execution_flags(self):
        for flag in ("--execute", "--confirm", "--approval", "--allow-resubmit", "--qsub"):
            with self.assertRaises(ToolError, msg=flag):
                asyncio.run(runner.run_script("submit_jobs", ["/x/plan.json", flag]))

    def test_runner_refuses_unlisted_scripts(self):
        with self.assertRaises(ToolError):
            asyncio.run(runner.run_script("generate_module_docs", []))

    def test_timeout_kills_the_script_and_reports_it(self):
        fake = self.tmp / "skill" / "scripts"
        fake.mkdir(parents=True)
        (fake / "validate_manifest.py").write_text("import time\ntime.sleep(30)\n")
        old = os.environ["MIMA_SKILL_DIR"]
        os.environ["MIMA_SKILL_DIR"] = str(self.tmp / "skill")
        try:
            r = asyncio.run(runner.run_script("validate_manifest", ["/x.csv"], timeout=1))
        finally:
            os.environ["MIMA_SKILL_DIR"] = old
        self.assertEqual((r["status"], r["exit_code"]), ("timeout", None))

    def test_long_output_is_truncated_and_flagged(self):
        fake = self.tmp / "skill" / "scripts"
        fake.mkdir(parents=True)
        (fake / "validate_manifest.py").write_text("print('x' * 200000)\n")
        old = os.environ["MIMA_SKILL_DIR"]
        os.environ["MIMA_SKILL_DIR"] = str(self.tmp / "skill")
        try:
            r = asyncio.run(runner.run_script("validate_manifest", ["/x.csv"]))
        finally:
            os.environ["MIMA_SKILL_DIR"] = old
        self.assertTrue(r["truncated"])
        self.assertLess(len(r["output"]), runner.MAX_OUTPUT + 100)


class TestArguments(Base):
    def test_rejects_dash_relative_and_nul_paths(self):
        for bad in ("-rf", "relative/m.csv", "/a\x00b"):
            self.assertIsNotNone(call_error("mima_validate_manifest", {"manifest_path": bad}), bad)

    def test_allowed_roots_confine_paths(self):
        f = self.tmp / "manifest.csv"
        f.write_text("Sample_ID,FileID_R1,FileID_R2\nS1,a_1.fq.gz,a_2.fq.gz\n")
        os.environ["MIMA_ALLOWED_ROOTS"] = str(self.tmp)
        self.assertEqual(call("mima_validate_manifest", {"manifest_path": str(f)})["status"], "valid")
        err = call_error("mima_validate_manifest", {"manifest_path": "/etc/passwd"})
        self.assertIn("outside the allowed folders", err)
        link = self.tmp / "link.csv"
        link.symlink_to("/etc/passwd")
        self.assertIn("outside the allowed folders", call_error("mima_validate_manifest", {"manifest_path": str(link)}))

    def test_job_ids_are_checked_and_one_source_required(self):
        self.assertIn("not a PBS job id", call_error("mima_check_job_status", {"job_ids": ["1; rm -rf /"]}))
        self.assertIn("exactly one", call_error("mima_check_job_status", {}))
        self.assertIn("exactly one", call_error("mima_check_job_status", {"job_ids": ["1.s"], "record_path": "/x.json"}))


class TestPrepareInputsTools(Base):
    def _messy(self):
        m = self.tmp / "sheet.csv"
        m.write_bytes(b"\xef\xbb\xbfsample;r1;r2\r\nA-1;a1_R1.fq.gz;a1_R2.fq.gz\r\nB-2;b2_R1.fq.gz;b2_R2.fq.gz\r\n")
        return m

    def test_preview_writes_nothing_and_apply_writes_copies_only(self):
        m = self._messy()
        before = m.read_bytes()
        out = self.tmp / "prepared"
        r = call("mima_prepare_inputs", {"manifest_path": str(m), "out_dir": str(out)})
        self.assertEqual(r["status"], "prepared", r)
        self.assertFalse(out.exists())
        w = call("mima_write_prepared_inputs", {"manifest_path": str(m), "out_dir": str(out)})
        self.assertEqual(w["status"], "prepared", w)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["id_map.tsv", "manifest.csv"])
        self.assertEqual(m.read_bytes(), before)
        self.assertEqual(call("mima_validate_manifest", {"manifest_path": str(out / "manifest.csv")})["status"], "valid")

    def test_clean_manifest_is_unchanged_and_open_questions_write_nothing(self):
        ok = self.tmp / "ok.csv"
        ok.write_text("Sample_ID,FileID_R1,FileID_R2\nS1,a_1.fq.gz,a_2.fq.gz\n")
        self.assertEqual(call("mima_prepare_inputs", {"manifest_path": str(ok), "out_dir": str(self.tmp / "o1")})["status"], "unchanged")
        dup = self.tmp / "dup.csv"
        dup.write_text("Sample_ID,FileID_R1,FileID_R2\nS1,a_1.fq.gz,a_2.fq.gz\nS1,b_1.fq.gz,b_2.fq.gz\n")
        out = self.tmp / "o2"
        r = call("mima_write_prepared_inputs", {"manifest_path": str(dup), "out_dir": str(out)})
        self.assertEqual(r["status"], "needs_input")
        self.assertTrue(r["data"]["needs_input"])
        self.assertFalse(out.exists())

    def test_argument_checks(self):
        m = self._messy()
        self.assertIn("exactly one", call_error("mima_prepare_inputs", {"out_dir": str(self.tmp / "o")}))
        self.assertIn("exactly one", call_error("mima_prepare_inputs", {"manifest_path": str(m), "from_reads_dir": str(self.tmp), "out_dir": str(self.tmp / "o")}))
        os.environ["MIMA_ALLOWED_ROOTS"] = str(self.tmp)
        self.assertIn("outside the allowed folders", call_error("mima_write_prepared_inputs", {"manifest_path": str(m), "out_dir": "/etc/prepared"}))
        self.assertEqual(call("mima_prepare_inputs", {"manifest_path": str(self.tmp / "missing.csv"), "out_dir": str(self.tmp / "o")})["status"], "unreadable")


class TestWrappedScripts(Base):
    def test_manifest_valid_and_crlf(self):
        good = self.tmp / "m.csv"
        good.write_text("Sample_ID,FileID_R1,FileID_R2\nS1,a_1.fq.gz,a_2.fq.gz\n")
        self.assertEqual(call("mima_validate_manifest", {"manifest_path": str(good)})["status"], "valid")
        crlf = self.tmp / "crlf.csv"
        crlf.write_bytes(b"Sample_ID,FileID_R1,FileID_R2\r\nS1,a_1.fq.gz,a_2.fq.gz\r\n")
        r = call("mima_validate_manifest", {"manifest_path": str(crlf)})
        self.assertEqual((r["status"], r["exit_code"]), ("invalid", 1))
        self.assertIn("CRLF", r["output"])

    def test_build_commands_good_and_bad_plan(self):
        step = {"module": "mima-taxa-profiling", "params": {
            "-i": "/data/CleanReads", "-o": "/data/Taxonomy_profiling", "--reference-path": "/db/GTDB_Kraken",
            "--read-length": "150", "--threshold": "100", "--mode": "container", "--pbs-config": "/data/pbs_header_taxa.cfg"}}
        r = call("mima_build_commands", {"plan": step})
        self.assertEqual((r["status"], r["exit_code"]), ("ok", 0))
        step["params"]["--read-length"] = "151"
        r = call("mima_build_commands", {"plan": step})
        self.assertEqual((r["status"], r["exit_code"]), ("invalid", 1))
        self.assertIn("151", json.dumps(r["data"]))

    def test_scan_logs_names_the_real_bracken_line(self):
        log = self.tmp / "S1_bracken.log"
        log.write_text("start\n" + BRACKEN + "\n")
        r = call("mima_scan_logs", {"paths": [str(log)]})
        self.assertEqual(r["status"], "errors_found")
        self.assertEqual([f["pattern"] for f in r["data"]["findings"]], ["bracken-input-missing"])

    def test_check_outputs_leads_with_a_short_summary(self):
        d = self.tmp / "Taxonomy_profiling"
        d.mkdir()
        m = self.tmp / "manifest.csv"
        m.write_text("Sample_ID,FileID_R1,FileID_R2\nS1,a_1.fq.gz,a_2.fq.gz\nS2,b_1.fq.gz,b_2.fq.gz\n")
        r = call("mima_check_outputs", {"stage": "taxa", "directory": str(d), "manifest": str(m)})
        self.assertEqual((r["status"], r["exit_code"]), ("errors", 1))
        s = r["data"]["summary"]
        self.assertEqual((s["missing_or_empty_files"], s["samples_with_missing_or_empty_files"]), (30, 2))
        self.assertEqual(s["missing_or_empty_files_by_sample"], {"S1": 15, "S2": 15})
        self.assertEqual(list(r["data"])[:3], ["stage", "ok", "summary"])
        self.assertIn("data.summary", asyncio.run(_tool_doc("mima_check_outputs"))[0])

    def test_job_status_without_qstat_is_a_tool_error_not_a_crash(self):
        env = dict(os.environ)
        os.environ["PATH"] = "/nonexistent"
        try:
            r = call("mima_check_job_status", {"job_ids": ["1.s"]})
        finally:
            os.environ.clear()
            os.environ.update(env)
        self.assertEqual((r["exit_code"], r["status"]), (2, "tool_error"))


class TestPrepareSubmission(Base):
    def make(self):
        script = self.tmp / "a.pbs"
        shutil.copy(FIXTURE, script)
        return script

    def test_dry_run_only_returns_command_for_a_person(self):
        script = self.make()
        r = call("mima_prepare_submission", {"jobs": [{"id": "taxa", "script": str(script)}], "run_dir": str(self.tmp), "check_fs": False})
        self.assertEqual(r["status"], "dry_run_ok", r)
        self.assertFalse(r["submitted"])
        self.assertTrue(r["token"])
        self.assertIn("--execute", r["run_yourself"])
        self.assertIn(r["token"], r["run_yourself"])
        self.assertTrue((self.tmp / "submission_plan.json").is_file())
        self.assertFalse((self.tmp / "submission_record.json").exists(), "a dry run must not write a record")

    def test_the_returned_command_refuses_without_a_terminal(self):
        """With a working (fake) qsub on PATH, only the terminal check can stop the command."""
        script = self.make()
        bindir = self.tmp / "bin"
        bindir.mkdir()
        qsub = bindir / "qsub"
        qsub.write_text("#!/bin/sh\necho 1.fake\n")
        qsub.chmod(0o755)
        r = call("mima_prepare_submission", {"jobs": [{"id": "taxa", "script": str(script)}], "run_dir": str(self.tmp), "check_fs": False})
        argv = shlex.split(r["run_yourself"])
        argv[0] = sys.executable
        argv.append("--no-fs-check")
        env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"])
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60, env=env)
        text = p.stdout + p.stderr
        self.assertEqual(p.returncode, 1, text)
        self.assertNotIn("qsub' not found", text)
        self.assertIn("--approval harness", text)
        self.assertFalse((self.tmp / "submission_record.json").exists())

    def test_bad_job_definitions_are_rejected(self):
        script = self.make()
        for jobs in ([], [{"id": "a b", "script": str(script)}], [{"id": "a", "script": "rel.pbs"}],
                     [{"id": "a", "script": str(script)}, {"id": "a", "script": str(script)}],
                     [{"id": "a", "script": str(script), "extra": 1}]):
            self.assertIsNotNone(call_error("mima_prepare_submission", {"jobs": jobs, "run_dir": str(self.tmp)}), jobs)
        self.assertIsNotNone(call_error("mima_prepare_submission", {"jobs": [{"id": "a", "script": str(script)}], "run_dir": str(self.tmp / "missing")}))


class TestHeaderTools(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._env = dict(os.environ)
        os.environ.pop("PROJECT", None)
        os.environ["MIMA_ALLOWED_ROOTS"] = "%s:/scratch/ab01:/g/data/ab01" % self.tmp

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def args(self, **over):
        a = {"module": "taxa", "samples": 6, "image_path": "/g/data/ab01/mima.sif", "project": "ab01",
             "paths": ["/scratch/ab01/run", "/g/data/ab01/REFDB/GTDB_Kraken"]}
        a.update(over)
        return a

    def test_preview_writes_nothing_and_apply_writes_a_validated_new_file(self):
        out = self.tmp / "pbs_header_taxa.cfg"
        r = call("mima_plan_pbs_header", self.args())
        self.assertEqual(r["status"], "prepared", r)
        self.assertIn("#PBS -l walltime=3:00:00", [x["line"] for x in r["data"]["lines"]])
        self.assertFalse(out.exists())
        w = call("mima_write_pbs_header", self.args(out_file=str(out)))
        self.assertEqual((w["status"], w["data"]["written"]), ("prepared", str(out)), w)
        self.assertEqual(call("mima_validate_pbs_config", {"config_path": str(out), "module": "taxa"})["status"], "valid")
        again = call("mima_write_pbs_header", self.args(out_file=str(out)))
        self.assertEqual(again["status"], "needs_input")  # never overwrites

    def test_missing_facts_and_other_modules_are_questions(self):
        r = call("mima_plan_pbs_header", {"module": "taxa", "samples": 3})
        self.assertEqual(r["status"], "needs_input")
        self.assertTrue(r["data"]["needs_input"])
        self.assertEqual(call("mima_plan_pbs_header", self.args(module="qc"))["status"], "needs_input")

    def test_arguments_are_checked(self):
        self.assertIn("outside the allowed folders", call_error("mima_write_pbs_header", self.args(out_file="/etc/h.cfg")))
        self.assertIn("outside the allowed folders", call_error("mima_plan_pbs_header", self.args(paths=["/etc"])))
        self.assertIn("outside the allowed folders", call_error("mima_plan_pbs_header", self.args(image_path="/opt/x.sif")))
        self.assertIn("invalid value", call_error("mima_plan_pbs_header", self.args(project="-x")))


if __name__ == "__main__":
    unittest.main()
