"""The agent loop against a scripted OpenAI-style endpoint (no model needed). Needs Python 3.10+ and fastmcp."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SKILL = Path(os.environ.get("MIMA_SKILL_DIR", ROOT.parent / "agentic-mima"))
sys.path.insert(0, str(ROOT))
os.environ["MIMA_SKILL_DIR"] = str(SKILL)

from fastmcp import Client  # noqa: E402

from mima_client.agent import READ_TOOL, SUBMISSION_TOOL, WRITE_TOOL, Agent, read_skill_file  # noqa: E402
from mima_mcp.server import mcp  # noqa: E402


def call(name, args, cid):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


class Scripted(BaseHTTPRequestHandler):
    replies, seen = [], []

    def do_POST(self):
        Scripted.seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        out = json.dumps({"choices": [{"message": Scripted.replies.pop(0)}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def run_agent(replies, user_text, approve=lambda n, a: False, transcript=None, **kw):
    Scripted.replies, Scripted.seen = list(replies), []
    srv = HTTPServer(("127.0.0.1", 0), Scripted)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    async def go():
        async with Client(mcp) as c:
            agent = Agent(c, await c.list_tools(), SKILL, "http://127.0.0.1:%d/v1" % srv.server_port, "fake", approve,
                          say=lambda s: None, transcript=transcript, **kw)
            return agent, await agent.turn(user_text)
    try:
        return asyncio.run(go())
    finally:
        srv.shutdown()
        srv.server_close()


class ReaderTest(unittest.TestCase):
    def test_reads_references_and_refuses_everything_else(self):
        ok, text = read_skill_file(SKILL, "references/mcp-tools.md")
        self.assertTrue(ok)
        self.assertIn("mima_", text)
        for bad in ("../SKILL.md", "scripts/build_command.py", "references/../scripts/build_command.py", "/etc/passwd",
                    "references", "references/nope.md", "SKILL.md"):
            ok, msg = read_skill_file(SKILL, bad)
            self.assertFalse(ok, bad)
            self.assertIn("references/mcp-tools.md", msg)  # the refusal lists what can be read


class AgentTest(unittest.TestCase):
    def test_skill_in_prompt_tools_and_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "t.jsonl"
            agent, final = run_agent([call(READ_TOOL, {"path": "references/mcp-tools.md"}, "a"),
                                      call("mima_list_modules", {}, "b"),
                                      {"role": "assistant", "content": "Nine modules."}], "What can you do?", transcript=log)
            events = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(final, "Nine modules.")
        system = Scripted.seen[0]["messages"][0]["content"]
        self.assertIn("=== SKILL.md ===", system)
        self.assertIn("name: agentic-mima", system)
        offered = {t["function"]["name"] for t in Scripted.seen[0]["tools"]}
        self.assertEqual(len(offered), 19)  # 18 MCP tools + the reference reader
        self.assertIn(READ_TOOL, offered)
        self.assertEqual([e["name"] for e in events if e["event"] == "tool"], [READ_TOOL, "mima_list_modules"])
        tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
        self.assertIn("mima_", tool_msgs[0]["content"])
        self.assertIn('"status"', tool_msgs[1]["content"])

    def test_dry_run_tool_needs_a_yes(self):
        plan = {"submission_plan_path": "/nonexistent/plan.json"}
        asked = []

        def deny(name, args):
            asked.append(name)
            return False
        agent, _ = run_agent([call(SUBMISSION_TOOL, plan, "a"), {"role": "assistant", "content": "ok"}], "dry run", approve=deny)
        self.assertEqual(asked, [SUBMISSION_TOOL])
        self.assertIn("Not approved", [m for m in agent.messages if m["role"] == "tool"][0]["content"])

    def test_writer_tool_needs_a_yes_and_the_previewer_does_not(self):
        asked = []

        def deny(name, args):
            asked.append(name)
            return False
        args = {"manifest_path": "/nonexistent/m.csv", "out_dir": "/nonexistent/out"}
        run_agent([call(WRITE_TOOL, args, "a"), call("mima_prepare_inputs", args, "b"), {"role": "assistant", "content": "ok"}], "prepare", approve=deny)
        self.assertEqual(asked, [WRITE_TOOL])

    def test_submission_tool_can_be_withheld_and_reasoning_dropped(self):
        reply = {"role": "assistant", "content": "done", "reasoning_content": "thinking..."}
        agent, _ = run_agent([reply], "hi", enable_submission_tool=False)
        offered = {t["function"]["name"] for t in Scripted.seen[0]["tools"]}
        self.assertNotIn(SUBMISSION_TOOL, offered)
        self.assertNotIn("reasoning_content", agent.messages[-1])

    def test_step_limit_and_unknown_tool(self):
        replies = [call("nonexistent_tool", {}, str(i)) for i in range(3)]
        agent, final = run_agent(replies, "loop", max_steps=3)
        self.assertIn("Stopped after 3", final)
        self.assertIn("No such tool", agent.messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
