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
        r = self.run_tool("mima_prepare_submission", {"jobs": jobs or self.jobs, "run_dir": str(self.tmp), "check_fs": False})
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
        again = self.run_tool("mima_prepare_submission", {"jobs": self.jobs, "run_dir": str(self.tmp), "check_fs": False})
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


if __name__ == "__main__":
    unittest.main()
