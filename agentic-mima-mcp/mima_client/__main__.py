"""python -m mima_client --base-url http://HOST:8000/v1 --model kimi-k3 [--resume [RUN_FOLDER]]   (run where the skill and your data are)"""
import argparse
import asyncio
import os
import sys
import time
from pathlib import Path


def ask_yes(name, args):
    if not sys.stdin.isatty():
        print("  [client] %s needs a person's yes and there is no terminal: denied." % name)
        return False
    what = {"mima_write_prepared_inputs": "writes corrected copies into a new folder",
            "mima_write_pbs_header": "writes a new PBS header file"}.get(name, "dry run only")
    print("  [client] The model wants to run %s (%s) with: %s" % (name, what, args))
    return input("  allow? [y/N] ").strip().lower() == "y"


async def main(a):
    skill = Path(a.skill_dir or os.environ.get("MIMA_SKILL_DIR", "")).expanduser()
    if not (skill / "SKILL.md").is_file():
        sys.exit("Set --skill-dir or MIMA_SKILL_DIR to the agentic-mima skill folder.")
    os.environ["MIMA_SKILL_DIR"] = str(skill)
    from fastmcp import Client
    from mima_mcp.server import mcp
    from .agent import Agent
    from .approval import make_handler

    transcript = a.transcript or "mima_agent_%s.jsonl" % time.strftime("%Y%m%d-%H%M%S")
    # mode="legacy": the classic handshake, the one that carries the approval prompt (see mima_client/approval.py)
    async with Client(mcp, mode="legacy", elicitation_handler=make_handler()) as client:
        agent = Agent(client, await client.list_tools(), skill, a.base_url, a.model, ask_yes, api_key=a.api_key,
                      temperature=a.temperature, max_steps=a.max_steps, context_tokens=a.context_tokens,
                      timeout=a.timeout, transcript=transcript, keep_reasoning=a.keep_reasoning,
                      enable_submission_tool=not a.no_submission_tool)
        print("agentic-mima client: %d tools offered; transcript: %s" % (len(agent.specs), transcript))
        if a.resume is not None:
            print("\n[client] Looking up your earlier run (read-only; nothing is submitted or run)...")
            print(await agent.resume(a.resume))
        if a.prompt_file:
            for turn in [t.strip() for t in Path(a.prompt_file).read_text().split("\n---\n") if t.strip()]:
                print("\nyou> " + turn)
                print("\nmodel> " + await agent.turn(turn))
            return
        print("Type your request. Commands: /reset (new conversation), /quit.")
        while True:
            try:
                text = (await asyncio.to_thread(input, "\nyou> ")).strip()
            except EOFError:
                break
            if text in ("/quit", "/exit"):
                break
            if text == "/reset":
                agent.reset()
                print("[client] conversation cleared")
            elif text:
                print("\nmodel> " + await agent.turn(text))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key", default="")
    ap.add_argument("--skill-dir", default="")
    ap.add_argument("--prompt-file", default="", help="user turns separated by a line containing only ---")
    ap.add_argument("--transcript", default="")
    ap.add_argument("--temperature", type=float, default=None, help="default: the server's own setting")
    ap.add_argument("--max-steps", type=int, default=25)
    ap.add_argument("--context-tokens", type=int, default=65536)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--keep-reasoning", action="store_true", help="send the model's reasoning back each turn (uses context)")
    ap.add_argument("--resume", nargs="?", const="", default=None, metavar="RUN_FOLDER",
                    help="start by reading an earlier run: its live PBS state, logs and outputs. Give the run folder, or nothing to search the allowed folders")
    ap.add_argument("--no-submission-tool", action="store_true", help="do not offer mima_prepare_submission at all")
    asyncio.run(main(ap.parse_args()))
