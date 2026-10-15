"""The skill's MCP mapping (references/mcp-tools.md) must stay in step with the server."""
import asyncio
import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = Path(os.environ.get("MIMA_SKILL_DIR", ROOT.parent / "agentic-mima"))
os.environ["MIMA_SKILL_DIR"] = str(SKILL)
sys.path.insert(0, str(ROOT))

from fastmcp import Client  # noqa: E402

from mima_mcp import runner  # noqa: E402
from mima_mcp.server import mcp  # noqa: E402

DOC = (SKILL / "references" / "mcp-tools.md").read_text()
SKILL_MD = (SKILL / "SKILL.md").read_text()


class TestSkillAlignment(unittest.TestCase):
    def test_every_server_tool_is_documented_and_no_unknown_tool_is_named(self):
        async def go():
            async with Client(mcp) as c:
                return {t.name for t in await c.list_tools()}
        tools = asyncio.run(go())
        named = set(re.findall(r"`(mima_[a-z_]+)`", DOC)) - {"mima_mcp"}
        self.assertEqual(named, tools)

    def test_every_wrapped_script_is_mapped_by_file_name(self):
        for script in runner.STATUS:
            self.assertIn(f"{script}.py", DOC, script)
        self.assertIn("generate_module_docs.py", DOC)

    def test_every_status_word_has_guidance(self):
        words = {w for m in runner.STATUS.values() for w in m.values()} | {"timeout", "unexpected_exit"}
        for w in sorted(words):
            self.assertIn(f"`{w}`", DOC, w)

    def test_skill_links_the_mapping_and_keeps_the_submission_rule(self):
        self.assertIn("references/mcp-tools.md", SKILL_MD)
        self.assertIn("never ask the server to bypass the typed confirmation", DOC.lower().replace("\n", " "))
        self.assertNotIn("--approval harness", DOC.replace("never use `--approval harness`", ""))

    def test_documented_parameters_exist_in_the_tool_schemas(self):
        async def go():
            async with Client(mcp) as c:
                return {t.name: set(t.model_dump(by_alias=True)["inputSchema"].get("properties", {})) for t in await c.list_tools()}
        props = asyncio.run(go())
        rows = [ln for ln in DOC.splitlines() if ln.startswith("| ") and "`mima_" in ln]
        for ln in rows:
            tool = re.search(r"`(mima_[a-z_]+)`", ln).group(1)
            for p in re.findall(r"`([a-z_]+)`", ln.split("|")[5] if ln.count("|") >= 6 else ""):
                if p.startswith("mima_") or p.endswith(".py") or p in ("true", "false"):
                    continue
                if p in ("qc", "taxa", "func", "mima_prepare_submission"):
                    continue
                self.assertIn(p, props[tool], f"{tool}: documented parameter {p!r} is not in the schema")


if __name__ == "__main__":
    unittest.main()
