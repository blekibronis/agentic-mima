"""Cheap checks before a long run with a real model:  python3 -m mima_client.preflight --base-url http://HOST:8000/v1 --model NAME

Four short model calls and no cluster work. It checks that the server answers and lists the model, how many tokens the fixed
instructions and tool list use, that the model makes a well-formed tool call, that it can pass a nested argument (a plan
object), and how it returns its reasoning. Run it once the model is up and before the evaluation or the end-to-end test."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .agent import READ_TOOL, read_skill_file
from .llm import LLMError, get, post, result_text

LOOKUPS = ("mima_list_modules", READ_TOOL)  # read-only; allowed before the build call


def line(ok, text):
    print("  [%s] %s" % ("ok" if ok is True else ("WARN" if ok is None else "FAIL"), text))
    return ok


async def run(a):
    skill = Path(a.skill_dir or os.environ.get("MIMA_SKILL_DIR", "")).expanduser()
    if not (skill / "SKILL.md").is_file():
        sys.exit("Set --skill-dir or MIMA_SKILL_DIR to the agentic-mima skill folder.")
    os.environ["MIMA_SKILL_DIR"] = str(skill)
    from fastmcp import Client
    from mima_mcp.server import mcp
    from .agent import Agent

    base = a.base_url.rstrip("/")
    results = []
    print("1. Server and model")
    try:
        models = get(base + "/models", a.api_key)
    except LLMError as e:
        line(False, str(e))
        return 1
    found = [m for m in models.get("data", []) if m.get("id") == a.model]
    results.append(line(bool(found), "model %r %s the server's list: %s" % (a.model, "is in" if found else "is NOT in",
                                                                        ", ".join(m.get("id", "?") for m in models.get("data", [])) or "(empty)")))
    window = (found[0].get("max_model_len") if found else None)
    if window:
        results.append(line(window >= 32768, "context window reported by the server: %d tokens" % window))

    async with Client(mcp) as client:
        agent = Agent(client, await client.list_tools(), skill, base, a.model, lambda n, x: False, say=lambda s: None, api_key=a.api_key)
        url = base + "/chat/completions"

        def ask(prompt):
            msgs = [agent.messages[0], {"role": "user", "content": prompt}]
            body = {"model": a.model, "messages": msgs, "tools": agent.specs, "tool_choice": "auto"}
            return msgs, post(url, body, a.api_key, a.timeout)

        print("2. A simple tool call (also measures the fixed prompt)")
        try:
            msgs, r = ask("List the approved MIMA modules by calling the right tool, then tell me how many there are.")
        except LLMError as e:
            line(False, str(e))
            return 1
        m = r["choices"][0]["message"]
        used = (r.get("usage") or {}).get("prompt_tokens")
        if used:
            share = 100.0 * used / (window or a.context_tokens)
            results.append(line(share < 30 or None, "instructions and tools use %d tokens (%.0f%% of the window)" % (used, share)))
        calls = m.get("tool_calls") or []
        ok = bool(calls) and calls[0]["function"]["name"] == "mima_list_modules"
        results.append(line(ok, "tool call: %s" % (calls[0]["function"]["name"] if calls else "none (reply was text: %r)" % (m.get("content") or "")[:120])))
        keys = sorted(k for k in m if m[k] not in (None, "", []) and k not in ("role", "content", "tool_calls"))
        line(True, "extra fields in the reply: %s" % (", ".join(keys) or "none (reasoning, if any, is not returned separately)"))
        if ok:
            try:
                args = json.loads(calls[0]["function"].get("arguments") or "{}")
                results.append(line(isinstance(args, dict), "arguments are valid JSON: %s" % json.dumps(args)))
            except ValueError:
                results.append(line(False, "arguments are not valid JSON: %r" % calls[0]["function"].get("arguments")))
                args = None
            if args is not None:
                out = result_text(await client.call_tool("mima_list_modules", args, raise_on_error=False), 12000)
                msgs += [{k: v for k, v in m.items() if v is not None and k not in ("reasoning_content", "reasoning")},
                         {"role": "tool", "tool_call_id": calls[0].get("id", "x"), "content": out}]
                body = {"model": a.model, "messages": msgs, "tools": agent.specs, "tool_choice": "auto"}
                final = post(url, body, a.api_key, a.timeout)["choices"][0]["message"]
                text = final.get("content") or ""
                results.append(line(bool(text.strip()) and not final.get("tool_calls"), "final answer after the result: %r" % text[:160]))

        print("3. A nested argument")
        # The skill tells the model to check module names (mima_list_modules) and parameters (reference files, read with
        # skill_read_file) before building, so looking them up first is correct. Let it do that (read-only, up to 4 rounds)
        # and judge the first mima_build_commands call.
        msgs, r = ask("Build the command for the mima-bracken-report module with input folder /scratch/mp96/demo/Taxonomy_profiling. Use the tool; do not write the command yourself.")
        build, seen = None, []
        for _ in range(4):
            msg = r["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            seen += [c["function"]["name"] for c in calls]
            build = next((c for c in calls if c["function"]["name"] == "mima_build_commands"), None)
            if build or not calls or any(c["function"]["name"] not in LOOKUPS for c in calls):
                break
            msgs.append({k: v for k, v in msg.items() if v is not None and k not in ("reasoning_content", "reasoning")})
            for c in calls:
                if c["function"]["name"] == READ_TOOL:
                    try:
                        rel = json.loads(c["function"].get("arguments") or "{}").get("path", "")
                    except (ValueError, AttributeError):
                        rel = ""
                    out = read_skill_file(skill, rel)[1]
                else:
                    out = result_text(await client.call_tool("mima_list_modules", {}, raise_on_error=False), 12000)
                msgs.append({"role": "tool", "tool_call_id": c.get("id", "x"), "content": out})
            r = post(url, {"model": a.model, "messages": msgs, "tools": agent.specs, "tool_choice": "auto"}, a.api_key, a.timeout)
        good = False
        if build:
            try:
                plan = json.loads(build["function"]["arguments"]).get("plan")
                steps = plan.get("steps") if isinstance(plan, dict) and "steps" in plan else [plan]
                good = isinstance(steps[0], dict) and steps[0].get("module") == "mima-bracken-report" and isinstance(steps[0].get("params"), dict)
            except (ValueError, AttributeError, IndexError, TypeError):
                good = False
        results.append(line(good, "mima_build_commands got a plan object with the module and its params (after: %s)" % (", ".join(seen) or "nothing") if good else
                            "no well-formed plan; the model called: %s" % (", ".join(seen) or "no tool")))

    bad = [r for r in results if r is False]
    print("\nRESULT: %s" % ("READY" if not bad else "NOT READY (%d check(s) failed)" % len(bad)))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key", default="")
    ap.add_argument("--skill-dir", default="")
    ap.add_argument("--context-tokens", type=int, default=65536)
    ap.add_argument("--timeout", type=int, default=600)
    sys.exit(asyncio.run(run(ap.parse_args())))


if __name__ == "__main__":
    main()
