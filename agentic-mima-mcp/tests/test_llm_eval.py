"""The evaluation runner against a scripted OpenAI-style endpoint (no model needed). Needs Python 3.10+ and fastmcp."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SKILL = Path(os.environ.get("MIMA_SKILL_DIR", ROOT.parent / "agentic-mima"))
RUNNER = ROOT / "evals" / "run_llm_eval.py"
XML = """<evaluation><qa_pair><question>How many modules does the skill list at /tmp/mima_eval?</question><answer>9</answer></qa_pair>
<qa_pair><question>Second question</question><answer>x</answer></qa_pair></evaluation>"""


class Fake(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fake.seen.append(body)
        tool_done = any(m["role"] == "tool" for m in body["messages"])
        if not tool_done:
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "mima_list_modules", "arguments": "{}"}}]}
        else:
            first = "Second" not in body["messages"][1]["content"]
            msg = {"role": "assistant", "content": "Found them.\nANSWER: `%s`." % ("9" if first else "wrong")}
        out = json.dumps({"choices": [{"message": msg}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class RunnerTest(unittest.TestCase):
    def test_scores_tool_use_and_hides_submission_tool(self):
        srv = HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as d:
                xml = Path(d) / "q.xml"
                xml.write_text(XML)
                out = Path(d) / "r.json"
                env = dict(os.environ, MIMA_SKILL_DIR=str(SKILL))
                p = subprocess.run([sys.executable, str(RUNNER), "--base-url", "http://127.0.0.1:%d/v1" % srv.server_port,
                                    "--model", "fake", "--questions", str(xml), "--data-dir", d, "--out", str(out)],
                                   capture_output=True, text=True, env=env, timeout=120)
                res = json.loads(out.read_text())
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)  # second answer is wrong on purpose
        self.assertEqual((res["score"], res["total"]), (1, 2))
        self.assertTrue(res["results"][0]["correct"])  # "`9`." counts as 9
        self.assertEqual(res["results"][0]["tools_used"], ["mima_list_modules"])
        offered = {t["function"]["name"] for t in Fake.seen[0]["tools"]}
        self.assertEqual(len(offered), 13)
        self.assertNotIn("mima_write_prepared_inputs", offered)
        self.assertNotIn("mima_write_pbs_header", offered)
        self.assertNotIn("mima_prepare_submission", offered)


if __name__ == "__main__":
    unittest.main()
