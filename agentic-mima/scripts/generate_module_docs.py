#!/usr/bin/env python3
"""Regenerate references/module-parameters.md from command_specs.json.

Run this after editing the spec so the documentation cannot drift from the
validator:  python3 scripts/generate_module_docs.py
"""
import json
from pathlib import Path

root = Path(__file__).resolve().parent.parent
modules = json.loads((root / "scripts" / "command_specs.json").read_text())["modules"]

out = [
    "# MIMA module parameters",
    "",
    "Generated from `scripts/command_specs.json` by `scripts/generate_module_docs.py`. The spec is the source of truth used by `build_command.py`; edit the spec, then regenerate this file.",
    "",
    "UNVERIFIED marks details the tutorial does not confirm. Check them with `apptainer run --app MODULE $SANDBOX --help` when a container is available, then update the spec.",
    "",
    "## Contents",
]
out += [f"- {m}" for m in modules]
out.append("")

for name, mod in modules.items():
    out += [
        f"## {name}",
        "",
        f"- Stage: `{mod['stage']}`. Requires stage: {', '.join(mod['requires']) or 'none'}.",
        f"- Purpose: {mod['summary']}",
        f"- Notes: {mod.get('notes', '')}",
        "",
        "| Parameter | Type | Required | Default | Description |",
        "|---|---|---|---|---|",
    ]
    for pname, ps in mod["params"].items():
        if ps.get("required"):
            req = "yes"
        elif ps.get("required_if"):
            k, v = next(iter(ps["required_if"].items()))
            req = f"if {k} is {v}"
        else:
            req = "no"
        ptype = ps["type"] + (f" ({'/'.join(ps['choices'])})" if ps.get("choices") else "")
        out.append(f"| `{pname}` | {ptype} | {req} | {ps.get('default', '')} | {ps.get('help', '')} |")
    out.append("")

(root / "references" / "module-parameters.md").write_text("\n".join(out))
print(f"Wrote references/module-parameters.md ({len(modules)} modules)")
