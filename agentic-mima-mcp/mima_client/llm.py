"""Shared plumbing: one chat-completions call, MCP tool specs, MCP result text. Standard library plus fastmcp results."""
import json
import urllib.error
import urllib.request


class LLMError(Exception):
    """The model server could not be reached or refused the request. The caller decides whether to go on."""


def _open(req, url, timeout):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise LLMError("HTTP %s from %s: %s" % (e.code, url, e.read().decode(errors="replace")[:600]))
    except urllib.error.URLError as e:
        raise LLMError("Cannot reach %s: %s" % (url, e.reason))
    except (OSError, ValueError) as e:  # timeouts and a reply that is not JSON
        raise LLMError("No usable reply from %s: %s" % (url, e))


def post(url, payload, api_key="", timeout=600):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + (api_key or "none")})
    return _open(req, url, timeout)


def get(url, api_key="", timeout=60):
    return _open(urllib.request.Request(url, headers={"Authorization": "Bearer " + (api_key or "none")}), url, timeout)


def simplify(node):
    """Optional arguments arrive from the MCP schema as anyOf [X, null]. Local models and their chat templates cope better
    with the plain type, so keep X (the argument is simply left out when unused; the server still checks everything)."""
    if isinstance(node, list):
        return [simplify(n) for n in node]
    if not isinstance(node, dict):
        return node
    node = dict(node)
    options = node.get("anyOf")
    if isinstance(options, list):
        real = [o for o in options if not (isinstance(o, dict) and o.get("type") == "null")]
        if len(real) == 1 and len(real) < len(options):
            node.pop("anyOf")
            if node.get("default") is None:
                node.pop("default", None)
            merged = dict(simplify(real[0]))
            merged.update({k: simplify(v) for k, v in node.items()})
            return merged
    return {k: simplify(v) for k, v in node.items()}


def tool_specs(tools, hide=()):
    """MCP tool objects -> OpenAI function specs, leaving out the named tools."""
    out = []
    for t in tools:
        if t.name in hide:
            continue
        schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
        out.append({"type": "function", "function": {"name": t.name, "description": t.description or "", "parameters": simplify(schema)}})
    return out


def result_text(res, limit):
    data = getattr(res, "structured_content", None)
    if data is None:
        data = getattr(res, "data", None)
    if data is None:
        data = "".join(getattr(c, "text", "") for c in (res.content or []))
    text = data if isinstance(data, str) else json.dumps(data, default=str)
    if len(text) > limit:
        text = text[:limit] + "\n[cut by the client at %d characters; ask for a narrower check]" % limit
    return text
