"""Agent loop: the skill's SKILL.md as instructions, the mima_* MCP tools, and a reader for the skill's reference files.

Safety: the model cannot approve a submission. mima_submit_jobs asks the person (client prompt, typed SUBMIT) and the server
submits only if they answer; the dry run and the writers each need a person's yes first.
"""
import json
import time
from pathlib import Path

from .llm import LLMError, post, result_text, tool_specs

SUBMISSION_TOOL = "mima_prepare_submission"
SUBMIT_TOOL = "mima_submit_jobs"  # not in GATED: the person is asked by the server through the approval prompt
RUN_TOOL = "mima_run_step"  # not in GATED either: the server asks the person
WRITE_TOOL = "mima_write_prepared_inputs"
HEADER_TOOL = "mima_write_pbs_header"
GATED = (SUBMISSION_TOOL, WRITE_TOOL, HEADER_TOOL)  # each call needs a person's yes
RESUME_TOOL = "mima_resume"
READ_TOOL = "skill_read_file"
READABLE_DIRS = ("references", "assets")
READ_LIMIT = 40000
DROP_KEYS = ("reasoning_content", "reasoning")

PREFACE = (
    "You are the agentic-mima assistant, running in Cluster mode through the project's MCP server on the cluster login node.\n"
    "The tools named mima_* are the MCP tools described in references/mcp-tools.md. You have no shell. The only way to read the "
    "skill's own files is the tool skill_read_file (files in references/ and assets/). Where the skill tells you to run a script, "
    "call the matching mima_* tool; where it tells you to read a reference file, call skill_read_file. Always pass absolute paths "
    "to mima_* tools.\n"
    "You cannot approve a submission yourself. Call mima_submit_jobs only after the person has reviewed the prepared plan and asked you to submit; "
    "the program then asks them to approve, and nothing is submitted unless they do. Never say a job was submitted unless the result status is submitted. "
    "If a step needs something you do not have, say so instead of guessing.\n"
    "You remember nothing of earlier sessions. When the person is returning to a run, a note from the program with its live state "
    "comes before their message; use it, and call mima_resume yourself if they mean a different run. Coming back is never approval to submit or run anything.\n"
    "The skill follows. Follow it exactly.\n\n=== SKILL.md ===\n"
)


def read_skill_file(skill_dir, rel):
    """Return (ok, text). Only .md and .json files inside references/ and assets/ can be read."""
    skill = Path(skill_dir).resolve()
    try:
        p = (skill / str(rel)).resolve()
    except (OSError, ValueError):
        return False, "Invalid path."
    allowed = [(skill / d).resolve() for d in READABLE_DIRS]
    if not any(a == p.parent or a in p.parents for a in allowed) or p.suffix not in (".md", ".json") or not p.is_file():
        return False, "Not readable. Use a path such as references/mcp-tools.md. Available: " + ", ".join(list_skill_files(skill))
    text = p.read_text(errors="replace")
    if len(text) > READ_LIMIT:
        text = text[:READ_LIMIT] + "\n[cut at %d characters]" % READ_LIMIT
    return True, text


def list_skill_files(skill_dir):
    skill = Path(skill_dir)
    return sorted(str(f.relative_to(skill)) for d in READABLE_DIRS for f in (skill / d).glob("*") if f.suffix in (".md", ".json"))


def read_tool_spec(skill_dir):
    return {"type": "function", "function": {
        "name": READ_TOOL,
        "description": "Read one reference file of the agentic-mima skill (SKILL.md is already in your instructions). "
                       "Available: " + ", ".join(list_skill_files(skill_dir)),
        "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "e.g. references/mcp-tools.md"}},
                       "required": ["path"]}}}


class Agent:
    def __init__(self, client, tools, skill_dir, base_url, model, approve, say=print, api_key="", temperature=None,
                 max_steps=25, result_limit=12000, context_tokens=65536, timeout=600, transcript=None, keep_reasoning=False,
                 enable_submission_tool=True):
        self.client, self.skill_dir, self.model = client, Path(skill_dir), model
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.approve, self.say, self.api_key, self.temperature = approve, say, api_key, temperature
        self.max_steps, self.result_limit, self.timeout = max_steps, result_limit, timeout
        self.context_tokens, self.keep_reasoning = context_tokens, keep_reasoning
        hide = () if enable_submission_tool else (SUBMISSION_TOOL, SUBMIT_TOOL, RUN_TOOL)
        self.specs = tool_specs(tools, hide) + [read_tool_spec(self.skill_dir)]
        self.names = {s["function"]["name"] for s in self.specs}
        skill_md = (self.skill_dir / "SKILL.md").read_text()
        self.messages = [{"role": "system", "content": PREFACE + skill_md}]
        self.transcript = Path(transcript) if transcript else None
        self.used = 0  # tokens the server reported for the conversation after its last reply
        self._note = ""  # live state of a run the person is returning to; goes in front of their next message
        self._log({"event": "start", "model": model, "tools": sorted(self.names), "skill_chars": len(skill_md)})

    def _log(self, rec):
        if self.transcript:
            rec = dict(rec, t=time.strftime("%Y-%m-%dT%H:%M:%S"))
            with self.transcript.open("a") as f:
                f.write(json.dumps(rec, default=str) + "\n")

    def _tokens(self):
        """The larger of the server's own count after its last reply and a rough estimate (3.5 characters per token)."""
        return max(self.used, len(json.dumps(self.messages, default=str)) // 3.5)

    def reset(self):
        self.messages = self.messages[:1]
        self.used = 0
        self._note = ""

    async def resume(self, folder=""):
        """Read an earlier run (mima_resume) and keep the result for the model's next message. Returns plain text for the person.

        Nothing is submitted or run here: the tool is read-only, and the note tells the model that coming back is not approval."""
        out = await self._call(RESUME_TOOL, {"folder": folder} if folder else {})
        self._log({"event": "resume", "folder": folder, "result": out})
        self._note = ("[From the program, not from the person: they are returning to an earlier run. This is its state read just now "
                      "from the run folder and from PBS. The ledger is memory; PBS is the truth. Nothing was submitted or run.]\n" + out)
        return resume_summary(out)

    async def _call(self, name, args):
        if name == READ_TOOL:
            return read_skill_file(self.skill_dir, args.get("path", ""))[1]
        if name not in self.names:
            return "No such tool: %s" % name
        if name in GATED and not self.approve(name, args):
            return "Not approved by the person. Do not retry; tell them what you wanted to check and why."
        try:
            return result_text(await self.client.call_tool(name, args, raise_on_error=False), self.result_limit)
        except Exception as e:  # a tool-side refusal is information for the model
            return "Tool error: %s" % e

    async def turn(self, user_text):
        """One user message -> the model's final reply (after any number of tool calls)."""
        if self._note:
            user_text, self._note = self._note + "\n\n" + user_text, ""
        self.messages.append({"role": "user", "content": user_text})
        self._log({"event": "user", "content": user_text})
        warned = False
        for _ in range(self.max_steps):
            if self._tokens() > 0.95 * self.context_tokens:
                return "[client] The conversation is about to exceed the model's context window (about %d of %d tokens). Use /reset to start again." % (self._tokens(), self.context_tokens)
            if self._tokens() > 0.75 * self.context_tokens and not warned:
                warned = True
                self.say("[client] warning: about %d of %d tokens used" % (self._tokens(), self.context_tokens))
            body = {"model": self.model, "messages": self.messages, "tools": self.specs, "tool_choice": "auto"}
            if self.temperature is not None:
                body["temperature"] = self.temperature
            try:
                reply = post(self.url, body, self.api_key, self.timeout)
                msg = reply["choices"][0]["message"]
            except LLMError as e:
                self._log({"event": "error", "detail": str(e)})
                return "[client] The model request failed and nothing was lost: %s\nSend your message again, or use /reset." % e
            except (KeyError, IndexError, TypeError):
                self._log({"event": "error", "detail": "unexpected reply shape", "reply": reply})
                return "[client] The model server's reply had no message in it. Send your message again, or use /reset."
            usage = reply.get("usage") or {}
            self.used = usage.get("total_tokens") or usage.get("prompt_tokens") or self.used
            keep = {k: v for k, v in msg.items() if v is not None and (self.keep_reasoning or k not in DROP_KEYS)}
            self.messages.append(keep)
            self._log({"event": "assistant", "message": msg})
            calls = msg.get("tool_calls") or []
            if not calls:
                return msg.get("content") or ""
            for c in calls:
                name = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except ValueError:
                    args = None
                self.say("  -> %s %s" % (name, json.dumps(args)[:200]))
                out = "Tool arguments were not valid JSON." if args is None else await self._call(name, args)
                self.say("  <- %s" % _headline(out))
                self._log({"event": "tool", "name": name, "args": args, "result": out})
                self.messages.append({"role": "tool", "tool_call_id": c.get("id", name), "content": out})
        return "[client] Stopped after %d tool steps without a final reply." % self.max_steps


def resume_summary(out):
    """A few plain lines from a mima_resume result, for the person at the start of a resumed session."""
    try:
        d = json.loads(out)
    except ValueError:
        return out[:500]
    data = d.get("data") if isinstance(d.get("data"), dict) else {}
    lines = []
    if data.get("run_dir"):
        lines.append("Run folder: %s" % data["run_dir"])
    if data.get("stage"):
        lines.append("Stage: %s   submitted: %s" % (data["stage"], data.get("submitted_at") or "unknown"))
    lines.append("Status: %s" % (d.get("status") or data.get("status") or "unknown"))
    for r in data.get("jobs", [])[:12]:
        lines.append("  %s  %s  %s" % (r.get("id"), r.get("pbs_job_id"), r.get("status")))
    if len(data.get("jobs", [])) > 12:
        lines.append("  ... and %d more" % (len(data["jobs"]) - 12))
    for run in data.get("runs", [])[:10]:
        lines.append("  found: %s" % run.get("run_dir"))
    for n in data.get("notes", []):
        lines.append("Note: %s" % n)
    if data.get("next"):
        lines.append("Next: %s" % data["next"])
    if not data and (d.get("stderr") or "").strip():
        lines.append(d["stderr"].strip()[:300])
    return "\n".join(lines)


def _headline(text):
    try:
        d = json.loads(text)
        if isinstance(d, dict) and "status" in d:
            return "status=%s exit_code=%s (%d chars)" % (d.get("status"), d.get("exit_code"), len(text))
    except ValueError:
        pass
    return "%d chars: %s" % (len(text), text[:100].replace("\n", " "))
