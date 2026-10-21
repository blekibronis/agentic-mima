#!/usr/bin/env python3
"""Let a language model answer evaluation.xml through the mima_* tools.

The model is any server with an OpenAI-compatible /v1/chat/completions endpoint
that supports tool calling (for example vLLM started with --enable-auto-tool-choice
and the right --tool-call-parser). The tools are called in-process, so run this
script where the skill and the data are (on Gadi, from the server's venv).

    export MIMA_SKILL_DIR=$HOME/agentic-mima-repo/agentic-mima
    export MIMA_ALLOWED_ROOTS=$HOME
    python3 make_eval_data.py $HOME/mima_eval
    python3 run_llm_eval.py --base-url http://HOST:8000/v1 --model NAME \
        --data-dir $HOME/mima_eval --out results.json

Nothing here submits a job or writes a file: mima_prepare_submission, mima_submit_jobs, mima_write_prepared_inputs and mima_write_pbs_header are withheld from the model
unless --allow-prepare-submission is given. The score is a string comparison
with the answers in evaluation.xml; read the transcripts in --out as well.
"""
import argparse
import asyncio
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mima_client.llm import LLMError, post, result_text, tool_specs  # noqa: E402

SYSTEM = (
    "You answer questions about metagenomics files using ONLY the mima_* tools you are given. "
    "Do not guess and do not answer from memory: call a tool, read its result, then answer. "
    "When you are done, finish with one last line in exactly this form:\n"
    "ANSWER: <your answer>\n"
    "The answer must follow the format the question asks for (a number, an ID, a file name, Yes or No) and nothing else."
)


def norm(s):
    return (s or "").strip().strip("`'\". *").lower()


def extract_answer(text):
    found = re.findall(r"^\s*ANSWER:\s*(.+?)\s*$", text or "", flags=re.M | re.I)
    return found[-1] if found else None


async def run(args):
    from fastmcp import Client
    from mima_mcp.server import mcp

    questions = []
    for qa in ET.parse(args.questions).getroot().findall("qa_pair"):
        q = qa.findtext("question").strip()
        if args.data_dir:
            q = q.replace("/tmp/mima_eval", args.data_dir.rstrip("/"))
        questions.append((q, qa.findtext("answer").strip()))
    if args.only:
        keep = set(args.only)
        questions = [(i, q, a) for i, (q, a) in enumerate(questions, 1) if i in keep]
    else:
        questions = [(i, q, a) for i, (q, a) in enumerate(questions, 1)]

    url = args.base_url.rstrip("/") + "/chat/completions"
    report, correct = [], 0
    async with Client(mcp) as client:
        specs = tool_specs(await client.list_tools(), () if args.allow_prepare_submission else ("mima_prepare_submission", "mima_submit_jobs", "mima_run_step", "mima_write_prepared_inputs", "mima_write_pbs_header"))
        print("tools offered: %d" % len(specs))
        for num, q, expected in questions:
            msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": q}]
            used, final, note, t0 = [], None, "", time.time()
            for step in range(args.max_steps):
                body = {"model": args.model, "messages": msgs, "tools": specs,
                        "tool_choice": "auto", "temperature": args.temperature}
                try:
                    reply = post(url, body, args.api_key, args.timeout)["choices"][0]["message"]
                except (LLMError, KeyError, IndexError) as e:
                    note = "model request failed: %s" % e
                    break
                msgs.append({k: v for k, v in reply.items() if v is not None})
                calls = reply.get("tool_calls") or []
                if not calls:
                    final = reply.get("content") or ""
                    break
                for c in calls:
                    name = c["function"]["name"]
                    try:
                        targs = json.loads(c["function"].get("arguments") or "{}")
                    except ValueError:
                        targs = None
                    used.append(name)
                    if targs is None:
                        out = "Tool arguments were not valid JSON."
                    elif name not in {s["function"]["name"] for s in specs}:
                        out = "No such tool: %s" % name
                    else:
                        try:
                            out = result_text(await client.call_tool(name, targs, raise_on_error=False), args.result_limit)
                        except Exception as e:  # tool-side refusal is data for the model, not a crash
                            out = "Tool error: %s" % e
                    msgs.append({"role": "tool", "tool_call_id": c.get("id", name), "content": out})
            else:
                note = "no final answer within %d steps" % args.max_steps
            if note.startswith("model request failed"):
                final = None
            got = extract_answer(final)
            ok = got is not None and norm(got) == norm(expected)
            correct += ok
            print("Q%-2d %-4s expected=%r got=%r tools=%s (%.0fs)%s" % (
                num, "ok" if ok else "FAIL", expected, got, ",".join(sorted(set(used))) or "none",
                time.time() - t0, " [" + note + "]" if note else ""))
            report.append({"question": num, "expected": expected, "got": got, "correct": ok,
                           "tools_used": used, "note": note, "final_text": final, "messages": msgs})
    print("score: %d/%d" % (correct, len(questions)))
    if args.out:
        Path(args.out).write_text(json.dumps({"model": args.model, "score": correct, "total": len(questions),
                                              "results": report}, indent=2, default=str))
        print("transcripts written to %s" % args.out)
    return 0 if correct == len(questions) else 1


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True, help="e.g. http://gadi-gpu-h200-0001:8000/v1")
    ap.add_argument("--model", required=True, help="model name as listed by GET /v1/models")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--questions", default=str(here / "evaluation.xml"))
    ap.add_argument("--data-dir", default="", help="replace /tmp/mima_eval in the questions with this folder")
    ap.add_argument("--only", type=int, nargs="*", help="question numbers to run")
    ap.add_argument("--max-steps", type=int, default=12)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--timeout", type=int, default=600, help="seconds per model call")
    ap.add_argument("--result-limit", type=int, default=12000, help="characters of one tool result shown to the model")
    ap.add_argument("--allow-prepare-submission", action="store_true")
    ap.add_argument("--out", default="")
    sys.exit(asyncio.run(run(ap.parse_args())))


if __name__ == "__main__":
    main()
