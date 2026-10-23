"""Approval-based submission: the model asks, the person decides, the server fails closed.

Run: python3 -m unittest discover -s tests -v  (needs Python 3.10+ and fastmcp). A fake qsub stands in for PBS.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SKILL = Path(os.environ.get("MIMA_SKILL_DIR", ROOT.parent / "agentic-mima"))
FIXTURE = HERE / "fixtures" / "real_run_taxa_profiling.pbs"
sys.path.insert(0, str(ROOT))
os.environ["MIMA_SKILL_DIR"] = str(SKILL)

from fastmcp import Client  # noqa: E402
from mcp.types import ElicitResult  # noqa: E402

from mima_client.approval import make_handler  # noqa: E402
from mima_mcp.server import mcp  # noqa: E402

SUBMIT, NO = "Submit 1 job(s) now", "Do NOT submit"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = dict(os.environ)
        os.environ.pop("MIMA_ALLOWED_ROOTS", None)
        os.environ.pop("MIMA_ALLOW_SUBMIT", None)
        os.environ.pop("MIMA_MAX_SUBMIT_JOBS", None)
        bindir = self.tmp / "bin"
        bindir.mkdir()
        qsub = bindir / "qsub"
        qsub.write_text("#!/bin/sh\necho 4242.fake\n")
        qsub.chmod(0o755)
        os.environ["PATH"] = str(bindir) + os.pathsep + os.environ["PATH"]
        self.script = self.tmp / "a.pbs"
        shutil.copy(FIXTURE, self.script)
        self.jobs = [{"id": "taxa", "script": str(self.script)}]

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_tool(self, tool, args, handler=None, **client_kw):
        async def go():
            async with Client(mcp, elicitation_handler=handler, **client_kw) as c:
                return (await c.call_tool(tool, args, raise_on_error=False)).data
        return asyncio.run(go())

    def prepare(self, jobs=None):
        r = self.run_tool("mima_prepare_submission", {"jobs": jobs or self.jobs, "run_dir": str(self.tmp), "check_fs": False, "stage_report": False})
        self.assertEqual(r["status"], "dry_run_ok", r)
        return r["token"]

    def submit(self, token, handler=None, mode="legacy", **extra):
        args = {"run_dir": str(self.tmp), "token": token, "check_fs": False}
        args.update(extra)
        return self.run_tool("mima_submit_jobs", args, handler, mode=mode)

    @property
    def record(self):
        return self.tmp / "submission_record.json"


def answering(choice, seen=None):
    async def handler(message, response_type, params, context):
        if seen is not None:
            seen.append(message)
        return choice
    return handler


def acting(action):
    async def handler(message, response_type, params, context):
        return ElicitResult(action=action)
    return handler


class TestApprovedSubmission(Base):
    def test_disabled_by_default_even_with_an_approving_client(self):
        token = self.prepare()
        seen = []
        r = self.submit(token, answering(SUBMIT, seen))
        self.assertEqual(r["status"], "disabled")
        self.assertFalse(r["submitted"])
        self.assertEqual(seen, [], "the person must not even be asked when submission is off")
        self.assertFalse(self.record.exists())

    def test_person_approves_and_the_job_is_queued_and_recorded(self):
        os.environ["MIMA_ALLOW_SUBMIT"] = "1"
        token = self.prepare()
        seen = []
        r = self.submit(token, answering(SUBMIT, seen))
        self.assertEqual(r["status"], "submitted", r)
        self.assertTrue(r["submitted"])
        self.assertEqual(r["pbs_job_ids"], {"taxa": "4242.fake"})
        self.assertIn("--approval", r["command"])
        self.assertNotIn("--allow-resubmit", r["command"])
        rec = json.loads(self.record.read_text())
        self.assertEqual(rec["jobs"][0]["pbs_job_id"], "4242.fake")
        self.assertEqual(rec["approvals"][0]["plan_token"], token)
        self.assertEqual(len(seen), 1)
        self.assertIn("APPROVAL NEEDED", seen[0])
        self.assertIn(str(self.script), seen[0])
        self.assertIn("ncpus", seen[0])  # the resource lines are shown so the person knows the cost

    def test_declining_submits_nothing(self):
        os.environ["MIMA_ALLOW_SUBMIT"] = "1"
        token = self.prepare()
        for handler in (acting("decline"), answering(NO)):
            r = self.submit(token, handler)
            self.assertEqual(r["status"], "declined", r)
            self.assertFalse(r["submitted"])
            self.assertFalse(self.record.exists())

    def test_dismissing_the_prompt_submits_nothing(self):
        os.environ["MIMA_ALLOW_SUBMIT"] = "1"
        r = self.submit(self.prepare(), acting("cancel"))
        self.assertEqual(r["status"], "cancelled")
        self.assertFalse(self.record.exists())

    def test_client_that_cannot_ask_fails_closed(self):
        os.environ["MIMA_ALLOW_SUBMIT"] = "1"
        token = self.prepare()
        r = self.submit(token, handler=None)  # no elicitation support at all
        self.assertEqual(r["status"], "approval_unavailable", r)
        self.assertFalse(r["submitted"])
        self.assertFalse(self.record.exists())

    def test_newest_protocol_era_has_no_prompt_and_fails_closed(self):
        os.environ["MIMA_ALLOW_SUBMIT"] = "1"
        r = self.submit(self.prepare(), answering(SUBMIT), mode="auto")
        self.assertEqual(r["status"], "approval_unavailable", r)
        self.assertFalse(self.record.exists())

    def test_changed_script_makes_the_token_stale(self):
        os.environ["MIMA_ALLOW_SUBMIT"] = "1"
        token = self.prepare()
        self.script.write_text(self.script.read_text() + "\necho changed\n")
        seen = []
        r = self.submit(token, answering(SUBMIT, seen))
        self.assertEqual(r["status"], "stale", r)
        self.assertEqual(seen, [])
        self.assertFalse(self.record.exists())

    def test_wrong_token_is_stale(self):
        os.environ["MIMA_ALLOW_SUBMIT"] = "1"
        self.prepare()
        r = self.submit("deadbeef0000", answering(SUBMIT))
        self.assertEqual(r["status"], "stale")

    def test_a_recorded_script_cannot_be_submitted_again(self):
        os.environ["MIMA_ALLOW_SUBMIT"] = "1"
        self.assertEqual(self.submit(self.prepare(), answering(SUBMIT))["status"], "submitted")
        again = self.run_tool("mima_prepare_submission", {"jobs": self.jobs, "run_dir": str(self.tmp), "check_fs": False, "stage_report": False})
        self.assertEqual(again["status"], "invalid")
        r = self.submit("deadbeef0000", answering(SUBMIT))
        self.assertIn(r["status"], ("invalid", "stale"))
        self.assertEqual(len(json.loads(self.record.read_text())["jobs"]), 1)

    def test_job_limit_stops_before_asking(self):
        os.environ["MIMA_ALLOW_SUBMIT"] = "1"
        os.environ["MIMA_MAX_SUBMIT_JOBS"] = "1"
        second = self.tmp / "b.pbs"
        second.write_text(self.script.read_text() + "\n# second\n")
        token = self.prepare(self.jobs + [{"id": "taxb", "script": str(second)}])
        seen = []
        r = self.submit(token, answering(SUBMIT, seen))
        self.assertEqual(r["status"], "too_many_jobs", r)
        self.assertEqual(seen, [])

    def test_missing_plan_is_a_tool_error(self):
        os.environ["MIMA_ALLOW_SUBMIT"] = "1"
        empty = Path(tempfile.mkdtemp())
        try:
            async def go():
                async with Client(mcp, elicitation_handler=answering(SUBMIT), mode="legacy") as c:
                    return await c.call_tool("mima_submit_jobs", {"run_dir": str(empty), "token": "abcd1234"}, raise_on_error=False)
            self.assertTrue(asyncio.run(go()).is_error)
        finally:
            shutil.rmtree(empty, ignore_errors=True)


class TestClientApprovalPrompt(unittest.TestCase):
    class Params:
        requestedSchema = {"type": "object", "properties": {"value": {"type": "string", "enum": [SUBMIT, NO]}}}

    def run_handler(self, typed, terminal=True):
        said = []
        h = make_handler(ask=lambda prompt: typed, say=said.append, has_terminal=lambda: terminal)
        return asyncio.run(h("APPROVAL NEEDED", None, self.Params(), None)), said

    def test_only_the_capital_word_approves(self):
        out, _ = self.run_handler("SUBMIT")
        self.assertEqual(out, SUBMIT)
        for typed in ("submit", "yes", "y", "Submit", "SUBMIT now", "", "ok"):
            out, _ = self.run_handler(typed)
            self.assertEqual(out.action, "decline", typed)

    def test_no_terminal_declines_without_asking(self):
        asked = []
        h = make_handler(ask=lambda p: asked.append(p) or "SUBMIT", say=lambda *a: None, has_terminal=lambda: False)
        out = asyncio.run(h("APPROVAL NEEDED", None, self.Params(), None))
        self.assertEqual(out.action, "decline")
        self.assertEqual(asked, [])

    def test_unreadable_request_declines(self):
        class Empty:
            requestedSchema = {}
        h = make_handler(ask=lambda p: "SUBMIT", say=lambda *a: None, has_terminal=lambda: True)
        self.assertEqual(asyncio.run(h("x", None, Empty(), None)).action, "decline")



RUN, NO_RUN = "Run this step now", "Do NOT run"
FAKE_APPTAINER = """#!/bin/sh
echo "ARGS: $@"
echo "SANDBOX=$SANDBOX"
echo "BIND=$APPTAINER_BIND"
exit ${FAKE_EXIT:-0}
"""


class TestRunStep(Base):
    """mima_run_step: one validated command, only after the person approves; a fake apptainer records what it was given."""

    def setUp(self):
        super().setUp()
        os.environ.pop("MIMA_ALLOW_RUN", None)
        os.environ.pop("FAKE_EXIT", None)
        fake = self.tmp / "bin" / "apptainer"
        fake.write_text(FAKE_APPTAINER)
        fake.chmod(0o755)
        self.image = self.tmp / "mima.sif"
        self.image.write_text("x")
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.step = {"module": "mima-qc", "params": {"-i": str(self.work / "raw"), "-o": str(self.work / "out"),
                                                      "-m": str(self.work / "manifest.csv"), "--ref": str(self.work / "ref.fna"),
                                                      "--mode": "container", "--pbs-config": str(self.work / "pbs_header_qc.cfg")}}

    def run_step(self, handler=None, mode="legacy", **extra):
        args = {"step": self.step, "image_path": str(self.image), "bind_paths": [str(self.work)], "check_fs": False}
        args.update(extra)
        return self.run_tool("mima_run_step", args, handler, mode=mode)

    def test_disabled_by_default(self):
        seen = []
        r = self.run_step(answering(RUN, seen))
        self.assertEqual((r["status"], r["ran"]), ("disabled", False))
        self.assertEqual(seen, [])

    def test_approved_step_runs_with_the_container_environment(self):
        os.environ["MIMA_ALLOW_RUN"] = "1"
        seen = []
        r = self.run_step(answering(RUN, seen))
        self.assertEqual(r["status"], "ran", r)
        self.assertTrue(r["ran"])
        self.assertIn("ARGS: run --app mima-qc " + str(self.image), r["output"])
        self.assertIn("SANDBOX=" + str(self.image), r["output"])
        self.assertIn("BIND=" + str(self.work), r["output"])
        self.assertIn("APPROVAL NEEDED", seen[0])
        self.assertIn("apptainer run --app mima-qc", seen[0])
        self.assertIn(str(self.work / "raw"), seen[0])

    def test_nonzero_exit_is_reported_as_failed(self):
        os.environ["MIMA_ALLOW_RUN"] = "1"
        os.environ["FAKE_EXIT"] = "3"
        r = self.run_step(answering(RUN))
        self.assertEqual((r["status"], r["exit_code"]), ("failed", 3))

    def test_decline_cancel_and_no_prompt_run_nothing(self):
        os.environ["MIMA_ALLOW_RUN"] = "1"
        for handler, mode, want in ((acting("decline"), "legacy", "declined"), (answering(NO_RUN), "legacy", "declined"),
                                    (acting("cancel"), "legacy", "cancelled"), (None, "legacy", "approval_unavailable"),
                                    (answering(RUN), "auto", "approval_unavailable")):
            r = self.run_step(handler, mode)
            self.assertEqual(r["status"], want, r)
            self.assertFalse(r["ran"])
            self.assertNotIn("ARGS:", r.get("output", ""))

    def test_invalid_plan_is_never_offered_to_the_person(self):
        os.environ["MIMA_ALLOW_RUN"] = "1"
        self.step["params"]["--not-a-flag"] = "x"
        seen = []
        r = self.run_step(answering(RUN, seen))
        self.assertEqual(r["status"], "invalid", r)
        self.assertEqual(seen, [])

    def test_only_one_known_module_and_a_real_image_are_accepted(self):
        os.environ["MIMA_ALLOW_RUN"] = "1"
        self.step["module"] = "rm -rf /"
        r = self.run_step(answering(RUN))
        self.assertEqual((r["status"], r["ran"]), ("invalid", False))
        self.step["module"] = "mima-qc"
        bad = self.run_tool("mima_run_step", {"step": self.step, "image_path": str(self.tmp / "x.txt"), "bind_paths": [str(self.work)]},
                            answering(RUN), mode="legacy")
        self.assertIsNone(bad)  # tool error: image is not an existing .sif

    def test_missing_apptainer_is_a_clear_tool_error(self):
        os.environ["MIMA_ALLOW_RUN"] = "1"
        (self.tmp / "bin" / "apptainer").unlink()
        saved = os.environ["PATH"]
        os.environ["PATH"] = str(self.tmp / "bin") + os.pathsep + "/nonexistent"
        try:
            r = self.run_step(answering(RUN))
        finally:
            os.environ["PATH"] = saved
        self.assertEqual(r["status"], "tool_error")
        self.assertIn("module load apptainer", r["note"])

    def test_timeout_stops_the_step(self):
        os.environ["MIMA_ALLOW_RUN"] = "1"
        (self.tmp / "bin" / "apptainer").write_text("#!/bin/sh\nsleep 30\n")
        from mima_mcp import runner
        result = asyncio.run(runner.run_built_command("apptainer run x", str(self.image), [str(self.work)], timeout=1))
        self.assertEqual(result["status"], "timeout")


class TestPromptWord(unittest.TestCase):
    class Params:
        requestedSchema = {"type": "object", "properties": {"value": {"type": "string", "enum": [RUN, NO_RUN]}}}

    def test_run_prompt_needs_the_word_run(self):
        said = []
        h = make_handler(ask=lambda p: "RUN", say=said.append, has_terminal=lambda: True)
        self.assertEqual(asyncio.run(h("x", None, self.Params(), None)), RUN)
        h = make_handler(ask=lambda p: "SUBMIT", say=said.append, has_terminal=lambda: True)
        self.assertEqual(asyncio.run(h("x", None, self.Params(), None)).action, "decline")


class TestFollowUpAndResume(Base):
    """The follow-up report job, the run ledger and mima_resume, through the server."""
    def setUp(self):
        super().setUp()
        os.environ["MIMA_ALLOW_SUBMIT"] = "1"
        qstat = Path(self.tmp / "bin" / "qstat")
        qstat.write_text('#!/bin/sh\necho \'{"Jobs": {"4242.fake": {"job_state": "F", "Exit_status": 0, "Job_Name": "x"}}}\'\n')
        qstat.chmod(0o755)

    def prepare_with_report(self, **extra):
        args = {"jobs": self.jobs, "run_dir": str(self.tmp), "check_fs": False, "stage": "taxa"}
        args.update(extra)
        return self.run_tool("mima_prepare_submission", args)

    def test_the_follow_up_job_is_planned_but_nothing_is_submitted(self):
        r = self.prepare_with_report(mail_to="rafael@example.org", choices={"threshold": "100"})
        self.assertEqual(r["status"], "dry_run_ok", r)
        self.assertFalse(r["submitted"]); self.assertIn("follow-up job", r["stage_report"])
        plan = json.loads((self.tmp / "submission_plan.json").read_text())
        self.assertEqual([j["id"] for j in plan["jobs"]], ["taxa", "stage_report"])
        rep = plan["jobs"][1]
        self.assertEqual((rep["role"], rep["after_any"], rep["queue"]), ("report", ["taxa"], "normal"))
        self.assertEqual((plan["stage"], plan["mail_to"], plan["choices"]), ("taxa", "rafael@example.org", {"threshold": "100"}))
        self.assertTrue(Path(rep["script"]).is_file()); self.assertFalse(self.record.exists()); self.assertFalse((self.tmp / "mima_run_state.json").exists())
        self.assertIn("afterany", r["output"])

    def test_the_person_is_shown_the_follow_up_job_and_the_address_and_approves_it_with_the_rest(self):
        token = self.prepare_with_report(mail_to="rafael@example.org")["token"]
        seen = []
        r = self.submit(token, answering("Submit 2 job(s) now", seen))
        self.assertEqual(r["status"], "submitted", r)
        self.assertIn("follow-up report job", seen[0]); self.assertIn("rafael@example.org", seen[0]); self.assertIn("reports only", seen[0])
        led = json.loads((self.tmp / "mima_run_state.json").read_text())
        self.assertEqual([j["role"] for j in led["stages"][0]["jobs"]], ["stage", "report"]); self.assertEqual(led["stages"][0]["approval"], "harness")

    def test_one_choice_covers_the_whole_plan_and_a_decline_queues_neither(self):
        token = self.prepare_with_report()["token"]
        r = self.submit(token, answering("Do NOT submit"))
        self.assertEqual(r["status"], "declined"); self.assertFalse(self.record.exists()); self.assertFalse((self.tmp / "mima_run_state.json").exists())

    def test_a_bad_address_or_an_output_dir_without_a_stage_is_refused(self):
        for extra, text in (({"mail_to": "x; rm -rf /"}, "mail_to"), ({"stage": None, "output_dir": str(self.tmp)}, "needs stage")):
            async def go():
                async with Client(mcp) as c:
                    try:
                        args = {"jobs": self.jobs, "run_dir": str(self.tmp), "check_fs": False, "stage": "taxa"}; args.update(extra)
                        args = {k: v for k, v in args.items() if v is not None}
                        await c.call_tool("mima_prepare_submission", args); return None
                    except Exception as exc:
                        return str(exc)
            self.assertIn(text, asyncio.run(go()))

    def test_resume_reads_the_ledger_and_the_live_state_and_changes_nothing(self):
        token = self.prepare_with_report(output_dir=str(self.tmp))["token"]
        self.assertEqual(self.submit(token, answering("Submit 2 job(s) now"))["status"], "submitted")
        before = sorted((str(f), f.stat().st_mtime_ns) for f in self.tmp.rglob("*") if f.is_file())
        r = self.run_tool("mima_resume", {"folder": str(self.tmp)})
        self.assertEqual(r["status"], "complete", r); self.assertEqual(r["data"]["stage"], "taxa"); self.assertIn("jobs", r["data"])
        self.assertEqual(before, sorted((str(f), f.stat().st_mtime_ns) for f in self.tmp.rglob("*") if f.is_file()))

    def test_resume_works_with_submission_switched_off_and_needs_no_prompt(self):
        token = self.prepare_with_report()["token"]
        self.submit(token, answering("Submit 2 job(s) now"))
        os.environ.pop("MIMA_ALLOW_SUBMIT")
        r = self.run_tool("mima_resume", {"folder": str(self.tmp)}, handler=None)
        self.assertEqual(r["status"], "complete", r)

    def test_resume_on_a_folder_with_no_run_says_not_found_and_respects_allowed_roots(self):
        empty = self.tmp / "empty"; empty.mkdir()
        self.assertEqual(self.run_tool("mima_resume", {"folder": str(empty)})["status"], "not_found")
        os.environ["MIMA_ALLOWED_ROOTS"] = str(self.tmp / "only_here")
        async def go():
            async with Client(mcp) as c:
                try:
                    await c.call_tool("mima_resume", {"folder": str(empty)}); return None
                except Exception as exc:
                    return str(exc)
        self.assertIn("outside the allowed folders", asyncio.run(go()))


if __name__ == "__main__":
    unittest.main()
