"""The person's approval prompt for job submission (MCP elicitation).

The server asks; this handler shows the person exactly what would be submitted and accepts only the word SUBMIT typed in
capitals. Anything else, no terminal, or an unreadable request means "decline". The model never sees or writes this answer.
"""
import sys

from mcp.types import ElicitResult


def _choices(params):
    """The option strings in the server's request, in order (the first one is the 'submit' option)."""
    schema = getattr(params, "requestedSchema", None) or {}
    found = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("enum"), list):
                found.extend(str(x) for x in node["enum"])
            for key in ("oneOf", "anyOf"):
                for opt in node.get(key, []) or []:
                    if isinstance(opt, dict) and "const" in opt:
                        found.append(str(opt["const"]))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return list(dict.fromkeys(found))


def make_handler(ask=input, say=print, has_terminal=lambda: sys.stdin.isatty()):
    """Build the elicitation handler. ask/say/has_terminal can be replaced in tests."""

    async def handler(message, response_type, params, context):
        say("\n  [client] " + message.replace("\n", "\n  "))
        options = _choices(params)
        if not options:
            say("  [client] The approval request could not be read: declined.")
            return ElicitResult(action="decline")
        if not has_terminal():
            say("  [client] Submission needs a person at the keyboard and there is no terminal: declined.")
            return ElicitResult(action="decline")
        word = options[0].split()[0].upper()  # "Submit ..." -> SUBMIT, "Run ..." -> RUN
        answer = ask("  Type %s (capitals) to go ahead. Anything else cancels: " % word).strip()
        if answer == word:
            say("  [client] Approved by you. Going ahead.")
            return options[0]
        say("  [client] Not approved. Nothing was submitted.")
        return ElicitResult(action="decline")

    return handler
