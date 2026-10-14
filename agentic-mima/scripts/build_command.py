#!/usr/bin/env python3
"""Build and validate MIMA commands from a structured plan.

Only modules listed in command_specs.json can be generated. Every value is
checked against its declared type and quoted, so free-form or unsafe input
never reaches the shell.

Usage:
    build_command.py PLAN.json [--check-fs] [--format text|json]
    build_command.py --list-modules

PLAN.json is either one step or {"steps": [...]}. A step looks like:
    {"module": "mima-qc",
     "params": {"-i": "/abs/raw", "-o": "/abs/out", "-m": "/abs/manifest.csv",
                "--ref": "/abs/GRCh38.fna", "--mode": "container",
                "--pbs-config": "/abs/pbs_header_qc.cfg"},
     "stdout_log": "/abs/optional.log"}
Positional modules (mima-vis-taxa) use parameter names, not flags, as keys.

--check-fs verifies that input paths exist and that QC output directories are
clean. Use it only on the machine where the data lives.

Exit code: 0 = all steps valid, 1 = at least one error, 2 = bad plan file.
"""
import argparse
import json
import re
import shlex
import sys
from pathlib import Path

SPEC_PATH = Path(__file__).with_name("command_specs.json")

SAFE_PATH = re.compile(r"^/[A-Za-z0-9_.+=@:,/-]*$")
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.+:=/ -]+$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.+-]+$")
METAPHLAN_OPTS = re.compile(r"^\+\+bowtie2db /[A-Za-z0-9_.+=@:,/-]+( \+\+[A-Za-z0-9_]+ [A-Za-z0-9_.+=@:,/-]+)*$")


def load_specs():
    with open(SPEC_PATH) as fh:
        return json.load(fh)["modules"]


def q(value):
    if isinstance(value, list):
        return " ".join(shlex.quote(str(v)) for v in value)
    return shlex.quote(str(value))


def check_path(label, value, kind, check_fs, spec, errors, warns):
    """kind: dir_in, dir_out, file_in, file_out. Returns the path string or None."""
    if not isinstance(value, str) or not value:
        errors.append(f"{label}: expected a path string")
        return None
    if value.startswith("~") or not value.startswith("/"):
        errors.append(f"{label}: '{value}' is not an absolute path. Expand it to a full path starting with '/' (the pipeline needs absolute paths).")
        return None
    if ".." in value.split("/"):
        errors.append(f"{label}: '{value}' contains '..'. Resolve it to a plain absolute path.")
        return None
    if not SAFE_PATH.match(value):
        errors.append(f"{label}: '{value}' contains spaces or unsupported characters. Allowed: letters, digits and _ . + = @ : , / -")
        return None
    if check_fs:
        p = Path(value)
        if kind == "dir_in" and not p.is_dir():
            errors.append(f"{label}: directory does not exist: {value}")
        elif kind == "file_in" and not p.is_file():
            errors.append(f"{label}: file does not exist: {value}")
        elif kind == "dir_out" and p.exists():
            if not p.is_dir():
                errors.append(f"{label}: exists but is not a directory: {value}")
            elif spec.get("no_subdirs") and any(c.is_dir() for c in p.iterdir()):
                errors.append(f"{label}: {value} already contains subdirectories, which makes the QC step fail. Choose a new or empty directory.")
    return value


def check_value(label, ps, value, check_fs, errors, warns):
    """Validate one value. Returns the rendered string, or None if invalid."""
    t = ps["type"]
    if t in ("abs_dir_in", "abs_dir_out", "abs_file_in", "abs_file_out"):
        return check_path(label, value, t.replace("abs_", ""), check_fs, ps, errors, warns)
    if t == "abs_file_list":
        items = value if isinstance(value, list) else [s for s in str(value).split(",") if s]
        if not items:
            errors.append(f"{label}: provide at least one file")
            return None
        out = [check_path(f"{label}[{i}]", v, "file_in", check_fs, ps, errors, warns) for i, v in enumerate(items)]
        return ",".join(out) if all(out) else None
    if t == "abs_file_multi":
        items = value if isinstance(value, list) else [x for x in str(value).split() if x]
        if not items:
            errors.append(f"{label}: provide at least one file")
            return None
        if any("," in str(i) for i in items):
            errors.append(f"{label}: this parameter takes a space-separated list of files. Pass a JSON list, not a comma-separated string.")
            return None
        out = [check_path(f"{label}[{i}]", v, "file_in", check_fs, ps, errors, warns) for i, v in enumerate(items)]
        if not all(out):
            return None
        rule = ps.get("preferred_suffix")
        if rule:
            for f in out:
                base = f.rsplit("/", 1)[-1]
                if base.startswith(rule["applies_to_prefix"]) and not base.endswith(rule["suffix"]):
                    warns.append(f"{label}: '{base}' is not a {rule['suffix']} table. The container accepts both _counts and _relAbund (counts are used as they are for alpha diversity; relative abundances are scaled by 1e6 to pseudo-counts), so alpha results can differ. The tutorial uses {rule['suffix']}: use it unless the user chooses otherwise, and state which table was used in the report.")
        return out
    if t in ("int", "number"):
        try:
            num = float(value)
        except (TypeError, ValueError):
            errors.append(f"{label}: '{value}' is not a number")
            return None
        if t == "int" and num != int(num):
            errors.append(f"{label}: '{value}' must be a whole number")
            return None
        if "min" in ps and num < ps["min"]:
            errors.append(f"{label}: {value} is below the minimum {ps['min']}")
            return None
        if t == "number" and num <= 0:
            errors.append(f"{label}: {value} must be greater than 0")
            return None
        return str(int(num)) if t == "int" else str(value)
    if t == "choice":
        if str(value) not in ps["choices"]:
            errors.append(f"{label}: '{value}' is not allowed. Choose from: {', '.join(ps['choices'])}")
            return None
        return str(value)
    if t == "token":
        if not SAFE_TOKEN.match(str(value)):
            errors.append(f"{label}: '{value}' has unsupported characters")
            return None
        return str(value)
    if t == "name":
        if not SAFE_NAME.match(str(value)):
            errors.append(f"{label}: '{value}' must use only letters, digits and _ . + -")
            return None
        return str(value)
    if t == "csv_names":
        parts = str(value).split(",")
        if not all(SAFE_NAME.match(p) for p in parts):
            errors.append(f"{label}: '{value}' must be comma-separated names (no spaces) using letters, digits and _ . + -")
            return None
        return ",".join(parts)
    if t == "group_spec":
        parts = str(value).split(":")
        if len(parts) < 3:
            errors.append(f"{label}: '{value}' must look like column:group1:group2 (a column plus at least two groups)")
            return None
        col, groups = parts[0], parts[1:]
        if not SAFE_NAME.match(col):
            errors.append(f"{label}: column name '{col}' has unsupported characters")
            return None
        if any(not g.strip() or not SAFE_TOKEN.match(g) for g in groups):
            errors.append(f"{label}: group names must be non-empty and use letters, digits, spaces and _ . + - : = /")
            return None
        if len(set(groups)) != len(groups):
            errors.append(f"{label}: duplicate group names in '{value}'")
            return None
        warns.append(f"{label}: group values are case-sensitive; confirm them against the metadata with validate_metadata.py")
        return str(value)
    if t == "metaphlan_options":
        if not METAPHLAN_OPTS.match(str(value)):
            errors.append(f"{label}: '{value}' must start with '++bowtie2db /absolute/path' and use ++ prefixes for any further MetaPhlAn options")
            return None
        return str(value)
    errors.append(f"{label}: internal error, unknown type '{t}'")
    return None


def matches(params, cond):
    return all(str(params.get(k)) == v for k, v in cond.items())


def check_approved(name, value, list_file, warns):
    data = json.loads(Path(__file__).with_name(list_file).read_text())
    approved = [d["path"].rstrip("/") for d in data["databases"]]
    if value.rstrip("/") not in approved:
        warns.append(f"{name}: {value} is not on the approved database list ({list_file}: {', '.join(approved)}). Ask the user for the sample type and get the lab's approval before using another database.")


def check_required_file(name, ps, rule, rendered, params, spec, check_fs, errors, warns):
    """The chosen value needs a matching file inside another directory parameter (read length -> Bracken k-mer file)."""
    value = rendered.get(name, str(ps["default"]) if "default" in ps else None)
    if value is None:
        return
    wanted = rule["pattern"].format(value=value)
    base = rendered.get(rule["dir_param"])
    if not check_fs or base is None:
        warns.append(f"{name} {value}: the database must contain {wanted} (the shared GTDB database has only some read lengths). Re-run with --check-fs on the cluster to verify.")
        return
    d = Path(base)
    if (d / wanted).is_file():
        return
    have = sorted(m.group(1) for f in d.glob(rule["glob"]) for m in [re.search(rule["extract"], f.name)] if m)
    have = sorted(have, key=lambda x: int(x) if x.isdigit() else 0)
    errors.append(f"{name} {value}: {wanted} not found in {base}. Bracken fails without it. Read lengths available there: {', '.join(have) if have else 'none found'}. Choose one of those (the effective value was {'set explicitly' if name in params else 'the default ' + value}).")


def build_step(step, specs, check_fs):
    errors, warns = [], []
    result = {"module": step.get("module"), "stage": None, "ok": False, "errors": errors, "warnings": warns, "command": None}
    module = step.get("module")
    spec = specs.get(module)
    if spec is None:
        errors.append(f"'{module}' is not an approved module. Approved: {', '.join(sorted(specs))}")
        return result
    result["stage"] = spec["stage"]
    params = {k: v for k, v in (step.get("params") or {}).items()}

    for name in params:
        if name not in spec["params"]:
            errors.append(f"Unknown parameter '{name}'. Approved for {module}: {', '.join(spec['params'])}")

    rendered = {}
    for name, ps in spec["params"].items():
        present = name in params and params[name] is not None and params[name] != "" and params[name] is not False
        required = ps.get("required", False) or (ps.get("required_if") and matches(params, ps["required_if"]))
        if not present:
            if required:
                why = f" (required when {', '.join(f'{k} is {v}' for k, v in ps['required_if'].items())})" if ps.get("required_if") and not ps.get("required") else ""
                errors.append(f"{name}: missing required parameter{why}. {ps.get('help', '')}".strip())
            elif ps.get("absent_warning"):
                warns.append(ps["absent_warning"])
            elif "default" in ps and ps["type"] != "flag" and not ps.get("silent_default"):
                warns.append(f"{name}: not set, so the pipeline default '{ps['default']}' applies. State this to the user.")
            continue
        if ps.get("warn_unless") and not matches(params, ps["warn_unless"]):
            warns.append(f"{name}: has no effect unless {', '.join(f'{k} is {v}' for k, v in ps['warn_unless'].items())}")
        if ps["type"] == "flag":
            rendered[name] = True
            continue
        out = check_value(name, ps, params[name], check_fs, errors, warns)
        if out is not None:
            rendered[name] = out

    for name, ps in spec["params"].items():
        rule = ps.get("requires_file_in")
        if rule:
            check_required_file(name, ps, rule, rendered, params, spec, check_fs, errors, warns)

    for name, ps in spec["params"].items():
        if ps.get("approved_list") and isinstance(rendered.get(name), str):
            check_approved(name, rendered[name], ps["approved_list"], warns)
    if spec.get("known_issue"):
        warns.append("Known issue: " + spec["known_issue"])

    log = step.get("stdout_log")
    log_str = None
    if log:
        log_str = check_path("stdout_log", log, "file_out", check_fs, {}, errors, warns)

    if errors:
        return result

    style = spec["style"]
    if style == "options":
        parts = []
        for name in spec["params"]:
            if name in rendered:
                parts.append(name if rendered[name] is True else f"{name} {q(rendered[name])}")
        body = spec["invocation"] + "".join(f" \\\n  {p}" for p in parts)
    elif style == "positional":
        body = spec["invocation"] + "".join(f" \\\n  {q(rendered[n])}" for n in spec["params"])
    else:  # template
        body = spec["template"].format(**{k: q(v) for k, v in rendered.items()})

    if spec.get("cd_to_param"):
        body = f"cd {q(rendered[spec['cd_to_param']])} && \\\n{body}"
    if log_str:
        body += f" \\\n  > {q(log_str)}"
    pre = [c.format(**{k: q(v) for k, v in rendered.items()}) for c in spec.get("pre_commands", [])]
    result["command"] = "\n".join(pre + [body])
    result["ok"] = True
    return result


def check_order(steps, specs, results):
    """Warn or fail when a step's prerequisite stage is missing or comes later."""
    notes = []
    stages = [specs[s["module"]]["stage"] if s.get("module") in specs else None for s in steps]
    for i, s in enumerate(steps):
        spec = specs.get(s.get("module"))
        if not spec:
            continue
        for req in spec["requires"]:
            if req in stages[i + 1:] and req not in stages[:i]:
                results[i]["errors"].append(f"Stage '{req}' must run before '{spec['stage']}' but appears later in the plan.")
                results[i]["ok"] = False
                results[i]["command"] = None
            elif req not in stages:
                notes.append(f"Step {i + 1} ({s['module']}) assumes the '{req}' stage has already been completed; it is not in this plan.")
    return notes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan", nargs="?", help="Plan JSON file, or - for stdin")
    ap.add_argument("--check-fs", action="store_true", help="Verify paths exist (run on the cluster)")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--list-modules", action="store_true")
    args = ap.parse_args()
    specs = load_specs()

    if args.list_modules:
        for name, s in specs.items():
            print(f"{name:26s} [{s['stage']}] {s['summary']}")
        return 0
    if not args.plan:
        ap.error("plan file required")
    try:
        plan = json.load(sys.stdin if args.plan == "-" else open(args.plan))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Cannot read plan: {exc}", file=sys.stderr)
        return 2
    steps = plan["steps"] if isinstance(plan, dict) and "steps" in plan else [plan]
    if not steps or not all(isinstance(s, dict) for s in steps):
        print("Plan must contain at least one step object", file=sys.stderr)
        return 2

    results = [build_step(s, specs, args.check_fs) for s in steps]
    notes = check_order(steps, specs, results)
    if not args.check_fs:
        notes.append("Filesystem checks skipped: paths were not verified to exist. Re-run with --check-fs on the cluster before executing.")
    all_ok = all(r["ok"] for r in results)

    if args.format == "json":
        print(json.dumps({"ok": all_ok, "notes": notes, "steps": results}, indent=2))
    else:
        for i, r in enumerate(results, 1):
            print(f"=== Step {i}: {r['module']} [{r['stage']}] {'OK' if r['ok'] else 'INVALID'}")
            for e in r["errors"]:
                print(f"  ERROR: {e}")
            for w in r["warnings"]:
                print(f"  WARN:  {w}")
            if r["command"]:
                print(r["command"])
            print()
        for n in notes:
            print(f"NOTE: {n}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
