"""Readiness helpers for a real model: schema simplification, survivable model errors, the context guard and the preflight."""
import json
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from test_agent import SKILL, call, run_agent, Scripted

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mima_client.llm import LLMError, post, simplify  # noqa: E402


class Simplify(unittest.TestCase):
    def test_optional_arguments_lose_the_null_branch_and_keep_their_text(self):
        s = {"type": "object", "properties": {
            "a": {"anyOf": [{"type": "string", "maxLength": 9}, {"type": "null"}], "default": None, "description": "d"},
            "b": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
            "c": {"anyOf": [{"type": "null"}, {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "null"}]}}], "default": None}}}
        out = simplify(s)["properties"]
        self.assertEqual(out["a"], {"type": "string", "maxLength": 9, "description": "d"})
        self.assertEqual(len(out["b"]["anyOf"]), 2)  # a real choice between types is left alone
        self.assertEqual(out["c"]["type"], "array")
        self.assertEqual(out["c"]["items"], {"type": "string"})
        self.assertNotIn("null", json.dumps(out["a"]))

    def test_every_offered_tool_has_no_null_branches(self):
        _, _ = run_agent([{"role": "assistant", "content": "hi"}], "hi")
        text = json.dumps(Scripted.seen[0]["tools"])
        self.assertNotIn('{"type": "null"}', text)


class Survives(unittest.TestCase):
    def test_a_failed_request_is_reported_not_fatal(self):
        class Boom(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(500)
                self.send_header("Content-Length", "4")
                self.end_headers()
                self.wfile.write(b"boom")

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), Boom)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(LLMError):
                post("http://127.0.0.1:%d/v1/chat/completions" % srv.server_port, {}, timeout=5)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_agent_returns_a_message_when_the_server_fails(self):
        import test_agent
        Scripted.replies = []  # pop from empty list -> the handler fails the request
        agent, final = run_agent([], "hello")
        self.assertIn("[client]", final)
        self.assertIn("nothing was lost", final)

    def test_context_guard_works_inside_a_turn(self):
        big = {"role": "assistant", "content": None, "usage": None,
               "tool_calls": [{"id": "1", "type": "function", "function": {"name": "mima_list_modules", "arguments": "{}"}}]}
        agent, final = run_agent([big, big, big], "go", context_tokens=1000)
        self.assertIn("exceed the model's context window", final)

    def test_server_reported_usage_is_used(self):
        reply = {"role": "assistant", "content": "ok"}
        import test_agent
        agent, _ = run_agent([reply], "hi")
        self.assertEqual(agent.used, 0)  # the scripted endpoint reports no usage, so the estimate is used
        self.assertGreater(agent._tokens(), 0)


class FakeModel(BaseHTTPRequestHandler):
    flow = "good"

    def _send(self, obj):
        out = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        self._send({"data": [{"id": "fake-model", "max_model_len": 65536}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        msgs = body["messages"]
        last = msgs[-1]
        usage = {"prompt_tokens": 9000, "completion_tokens": 20, "total_tokens": 9020}
        bracken = "mima-bracken-report" in msgs[1]["content"]
        if bracken and self.flow == "lookup_first" and last["role"] != "tool":
            msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "l", "type": "function", "function": {
                "name": "mima_list_modules", "arguments": "{}"}}]}
        elif last["role"] == "tool" and not bracken:
            msg = {"role": "assistant", "content": "There are nine modules."}
        elif bracken:
            plan = {"module": "mima-bracken-report", "params": {"-i": "/scratch/mp96/demo/Taxonomy_profiling"}}
            if self.flow == "bad":
                plan = "mima-bracken-report"
            msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "b", "type": "function", "function": {
                "name": "mima_build_commands", "arguments": json.dumps({"plan": plan})}}]}
        else:
            msg = {"role": "assistant", "content": None, "reasoning_content": "thinking", "tool_calls": [{"id": "a", "type": "function", "function": {
                "name": "mima_list_modules", "arguments": "{}"}}]}
        self._send({"choices": [{"message": msg}], "usage": usage})

    def log_message(self, *a):
        pass


class Preflight(unittest.TestCase):
    def run_it(self, flow, model="fake-model"):
        FakeModel.flow = flow
        srv = HTTPServer(("127.0.0.1", 0), FakeModel)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            p = subprocess.run([sys.executable, "-m", "mima_client.preflight", "--base-url", "http://127.0.0.1:%d/v1" % srv.server_port,
                                "--model", model, "--skill-dir", str(SKILL)], cwd=ROOT, capture_output=True, text=True, timeout=120)
        finally:
            srv.shutdown()
            srv.server_close()
        return p.returncode, p.stdout + p.stderr

    def test_ready(self):
        code, out = self.run_it("good")
        self.assertEqual(code, 0, out)
        self.assertIn("RESULT: READY", out)
        self.assertIn("reasoning_content", out)  # the reply's extra field is reported
        self.assertIn("9000 tokens", out)

    def test_looking_up_module_names_first_is_fine(self):
        """The skill says to check module names with mima_list_modules; a model that does so before building is correct."""
        code, out = self.run_it("lookup_first")
        self.assertEqual(code, 0, out)
        self.assertIn("after: mima_list_modules, mima_build_commands", out)

    def test_not_ready_when_the_model_name_is_wrong_or_the_plan_is_not_an_object(self):
        code, out = self.run_it("good", model="other")
        self.assertEqual(code, 1, out)
        self.assertIn("is NOT in", out)
        code, out = self.run_it("bad")
        self.assertEqual(code, 1, out)
        self.assertIn("NOT READY", out)
