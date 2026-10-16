"""Shared plumbing: one chat-completions call, MCP tool specs, MCP result text. Standard library plus fastmcp results."""
import json
import urllib.error
import urllib.request


def post(url, payload, api_key="", timeout=600):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + (api_key or "none")})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise SystemExit("HTTP %s from %s: %s" % (e.code, url, e.read().decode(errors="replace")[:600]))
    except urllib.error.URLError as e:
        raise SystemExit("Cannot reach %s: %s" % (url, e.reason))


def tool_specs(tools, hide=()):
    """MCP tool objects -> OpenAI function specs, leaving out the named tools."""
    out = []
    for t in tools:
        if t.name in hide:
            continue
        schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
        out.append({"type": "function", "function": {"name": t.name, "description": t.description or "", "parameters": schema}})
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
