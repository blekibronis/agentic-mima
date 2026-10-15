#!/usr/bin/env python3
"""Compare the MIMA container's own --help text with the skill's command_specs.json.

Why this exists: the unit tests only prove the skill's scripts behave as the skill's
own spec says. They cannot show that the spec matches the container. This script
reads the real --help output (captured by capture_container_help.sh) and reports
every place where the spec and the container disagree, so a wrong flag, a missing
required option, a different default or a changed choice list is found by a
machine, not by reading.

Usage:
    compare_help_to_spec.py HELP_INPUT [--spec command_specs.json] [--format text|json]

HELP_INPUT is either
    * a directory holding one APP.txt per app (what capture_container_help.sh writes), or
    * one file where each app starts with a line such as
      '---------------- APP: mima-qc ----------------'.

What is compared, per module (read-only; Python 3.7+, standard library only):
    ERROR  a flag in the spec that the container does not have
    ERROR  an option the container requires that the spec does not mark required
    ERROR  a flag that takes a value in one place and not in the other
    ERROR  a flag that takes several values in one place and not in the other
    ERROR  different lists of allowed choices
    ERROR  a default in --help that differs from the spec default
    ERROR  a different number of positional arguments
    WARN   an option the container has that the spec does not offer
    WARN   a module in the spec with no help text captured
    NOTE   spec stricter than --help (for example --ref is required by the project
           but optional in --help), a default shown by --help that the spec does
           not record, and options the skill leaves out on purpose
           (-h/--help, --verbose, --debug)

Exit code: 0 = no ERROR, 1 = at least one ERROR, 2 = input could not be read.

Limits, stated plainly: --help describes what the container advertises. It does not
prove a flag works, and R and Perl apps give less detail than argparse apps (the
Random Forest app lists no required flags and no defaults, the vis-taxa app only
lists positional names). Those cases are reported as NOTE, not passed silently.
"""
import argparse
import json
import os
import re
import sys

IGNORED_FLAGS = {"-h", "--help", "--verbose", "--debug"}
APP_MARK = re.compile(r"^\s*(?:-{3,}|={3,})\s*APP:\s*(\S+)\s*(?:-{3,}|={3,})\s*$")
DEFAULT_RE = re.compile(r"\[default\s*[=:]\s*([^\]]*)\]", re.I)


class Opt(object):
    """One option as the container's --help describes it."""

    def __init__(self, aliases):
        self.aliases = aliases
        self.takes_value = False
        self.multi = False
        self.choices = None
        self.default = None
        self.required = None  # None = not knowable from this help format
        self.text = ""

    @property
    def name(self):
        longs = [a for a in self.aliases if a.startswith("--")]
        return longs[0] if longs else self.aliases[0]


class HelpDoc(object):
    def __init__(self):
        self.kind = None  # argparse | optparse | positional | none
        self.options = []
        self.positional = []


def normalise(text):
    """Make pasted or copied help text parse like the original terminal output.

    Copying from a terminal or a web page often turns spaces into non-breaking
    spaces and adds carriage returns or colour codes; the layout parsers below
    rely on plain spaces.
    """
    text = text.replace("\xa0", " ").replace("\r", "")
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    return text.expandtabs(8) if "\t" in text and "Usage:" not in text else text


def split_apps(text):
    """Split a combined capture into {app: text}. Returns {} if no markers."""
    apps = {}
    current = None
    for line in text.splitlines():
        m = APP_MARK.match(line)
        if m:
            current = m.group(1)
            apps[current] = []
        elif current is not None:
            apps[current].append(line)
    return {k: "\n".join(v) for k, v in apps.items()}


def load_help(path):
    """Return {app: help_text} from a directory or a combined file."""
    if os.path.isdir(path):
        out = {}
        for name in sorted(os.listdir(path)):
            if name.endswith(".txt") and not name.startswith("_"):
                with open(os.path.join(path, name), "r", errors="replace") as fh:
                    out[name[:-4]] = normalise(fh.read())
        return out
    with open(path, "r", errors="replace") as fh:
        text = normalise(fh.read())
    return split_apps(text)


def _flag_parts(head):
    """'-i INPUT_DIR, --input-dir INPUT_DIR' -> aliases, takes_value, multi, choices."""
    chunks = [c.strip() for c in head.split(", ")]
    aliases = []
    takes_value = False
    for chunk in chunks:
        bits = chunk.split(None, 1)
        flag = bits[0].split("=")[0]
        if not flag.startswith("-"):
            return None
        aliases.append(flag)
        if len(bits) > 1 or "=" in bits[0]:
            takes_value = True
    choices = None
    m = re.search(r"\{([^}]*)\}", head)
    if m:
        choices = [c.strip() for c in m.group(1).split(",") if c.strip()]
    multi = "..." in head
    return aliases, takes_value, multi, choices


def _required_from_usage(usage_text):
    """Flags that sit outside any [...] in an argparse usage line are required."""
    required = set()
    depth = 0
    token = ""
    # drop 'usage: prog.py'
    body = usage_text.split(None, 2)
    body = body[2] if len(body) > 2 else ""
    for ch in body + " ":
        if ch == "[":
            if depth == 0 and token.strip():
                _take(token, required)
            token = ""
            depth += 1
        elif ch == "]":
            depth = max(depth - 1, 0)
            token = ""
        elif ch.isspace():
            if depth == 0 and token.strip():
                _take(token, required)
            token = ""
        else:
            if depth == 0:
                token += ch
    return required


def _take(token, required):
    token = token.strip()
    if token.startswith("-") and not token.startswith("---"):
        required.add(token.split("=")[0])


def parse_argparse(text):
    doc = HelpDoc()
    doc.kind = "argparse"
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith("usage:")), None)
    if start is None:
        return None
    usage_lines = [lines[start]]
    i = start + 1
    while i < len(lines) and lines[i].strip():
        usage_lines.append(lines[i].strip())
        i += 1
    required = _required_from_usage(" ".join(usage_lines))

    current = None
    for line in lines[i:]:
        if re.match(r"^\[\d+\]", line) or not line.strip():
            if not line.strip():
                current = current  # blank lines inside an entry are harmless
            continue
        if re.match(r"^ {2}-", line):
            parts = re.split(r"\s{2,}", line.strip(), maxsplit=1)
            parsed = _flag_parts(parts[0])
            if parsed is None:
                current = None
                continue
            aliases, takes_value, multi, choices = parsed
            opt = Opt(aliases)
            opt.takes_value, opt.multi, opt.choices = takes_value, multi, choices
            opt.required = any(a in required for a in aliases)
            opt.text = parts[1] if len(parts) > 1 else ""
            doc.options.append(opt)
            current = opt
        elif current is not None and line.startswith(" " * 8):
            current.text += " " + line.strip()
    for opt in doc.options:
        m = DEFAULT_RE.search(opt.text)
        if m:
            opt.default = m.group(1).strip()
    return doc


def parse_optparse(text):
    doc = HelpDoc()
    doc.kind = "optparse"
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith("Usage:")), None)
    if start is None:
        return None
    for line in lines[start + 1:]:
        m = re.match(r"^\s+(-\w)(?:\s+\w+)?,\s*(--[\w-]+)(=\w+)?", line)
        if m:
            opt = Opt([m.group(1), m.group(2)])
            opt.takes_value = bool(m.group(3)) or bool(re.match(r"^\s+-\w\s+\w+,", line))
            doc.options.append(opt)
    return doc


def parse_positional(text):
    doc = HelpDoc()
    doc.kind = "positional"
    for line in text.splitlines():
        if line.startswith("perl ") or " <" in line and "USAGE" not in line and line.startswith("perl"):
            names = re.findall(r"<([^>]+)>", line)
            if names:
                doc.positional = names
                return doc
    return None


def parse_help(text):
    if not text:
        return HelpDoc()
    for parser in (parse_argparse, parse_optparse, parse_positional):
        doc = parser(text)
        if doc is not None and (doc.options or doc.positional):
            return doc
    return HelpDoc()


def _same_default(spec_value, help_value):
    s = str(spec_value).strip().lower()
    h = str(help_value).strip().lower()
    if s == h:
        return True
    try:
        return float(s) == float(h)
    except ValueError:
        return False


def compare_module(name, spec, doc):
    """Return a list of (severity, message) for one module."""
    findings = []
    style = spec.get("style")
    params = spec.get("params", {})

    if style == "template":
        return [("NOTE", "template module, not a container app: nothing to compare")]
    if doc is None or doc.kind in (None, "none"):
        return [("WARN", "no usable help text captured: cannot compare this module")]

    if style == "positional":
        if doc.kind != "positional":
            return [("WARN", "spec says positional but help is not in the positional layout")]
        if len(doc.positional) != len(params):
            findings.append(("ERROR", "container lists {} positional arguments, spec has {}: {} vs {}".format(
                len(doc.positional), len(params), doc.positional, list(params))))
        else:
            findings.append(("NOTE", "positional count matches ({}). Order and meaning follow the help "
                             "names {} and cannot be checked by name".format(len(params), doc.positional)))
        return findings

    by_alias = {}
    for opt in doc.options:
        for alias in opt.aliases:
            by_alias[alias] = opt

    covered = set()
    for key, p in params.items():
        opt = by_alias.get(key)
        if opt is None:
            findings.append(("ERROR", "spec flag {} is not in the container's --help".format(key)))
            continue
        covered.add(id(opt))
        is_flag = p.get("type") == "flag"
        # multi_value_as_one_string: --help shows a list but the skill deliberately
        # passes one quoted string (the tutorial form); the spec must say so.
        is_multi = str(p.get("type", "")).endswith("_multi") or bool(p.get("multi_value_as_one_string"))
        if is_flag and opt.takes_value:
            findings.append(("ERROR", "{}: spec says it is a flag, the container expects a value".format(key)))
        if not is_flag and not opt.takes_value:
            findings.append(("ERROR", "{}: spec says it takes a value, the container treats it as a flag".format(key)))
        if opt.takes_value and is_multi != opt.multi:
            findings.append(("ERROR", "{}: spec multi-value={}, container multi-value={}".format(
                key, is_multi, opt.multi)))
        if p.get("type") == "choice" or opt.choices or p.get("choices"):
            sc = set(str(c) for c in p.get("choices", []))
            hc = set(opt.choices or [])
            if not hc and sc:
                findings.append(("NOTE", "{}: the skill limits values to {}; the container's --help lists no choices".format(
                    key, sorted(sc))))
            elif sc != hc:
                findings.append(("ERROR", "{}: spec choices {} differ from container choices {}".format(
                    key, sorted(sc), sorted(hc))))
        if opt.default is not None and "default" in p:
            if not _same_default(p["default"], opt.default):
                findings.append(("ERROR", "{}: spec default {!r} but --help says [default={}]".format(
                    key, p["default"], opt.default)))
        elif opt.default is not None and opt.default.lower() != "none":
            findings.append(("NOTE", "{}: --help shows [default={}], not recorded in the spec".format(
                key, opt.default)))
        elif "default" in p and opt.default is None:
            findings.append(("NOTE", "{}: spec default {!r} is not shown by --help".format(key, p["default"])))
        conditional = bool(p.get("required_if"))
        if opt.required is True and not p.get("required"):
            findings.append(("ERROR", "{} is required by the container but the spec does not require it".format(key)))
        elif p.get("required") and opt.required is False:
            findings.append(("NOTE", "{} is required by the skill but optional in --help (project policy)".format(key)))
        elif opt.required is None and p.get("required"):
            findings.append(("NOTE", "{}: the help format does not say whether it is required".format(key)))
        if conditional and opt.required is True:
            findings.append(("NOTE", "{} is always required by the container; the spec makes it conditional".format(key)))

    for opt in doc.options:
        if id(opt) in covered:
            continue
        if set(opt.aliases) & IGNORED_FLAGS:
            continue
        label = "/".join(opt.aliases)
        sev = "ERROR" if opt.required else "WARN"
        findings.append((sev, "container option {} is not in the spec{}".format(
            label, " and it is required" if opt.required else " (the skill cannot pass it)")))

    if doc.kind == "optparse":
        findings.append(("NOTE", "R/optparse help: required flags and defaults are not shown, "
                         "so only flag names and whether they take a value were compared"))
    return findings


def run(help_input, spec_path):
    with open(spec_path, "r") as fh:
        spec = json.load(fh)
    helps = load_help(help_input)
    results = {}
    for name, mod in sorted(spec["modules"].items()):
        text = helps.get(name)
        doc = parse_help(text) if text else None
        results[name] = compare_module(name, mod, doc)
    extra = sorted(set(helps) - set(spec["modules"]))
    return results, extra


def default_spec():
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in ("../../agentic-mima/scripts/command_specs.json",
                "../agentic-mima/scripts/command_specs.json"):
        cand = os.path.normpath(os.path.join(here, rel))
        if os.path.isfile(cand):
            return cand
    return "command_specs.json"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Compare MIMA container --help text with command_specs.json.")
    ap.add_argument("help_input", help="directory of APP.txt files, or one combined file with 'APP: name' markers")
    ap.add_argument("--spec", default=None, help="path to command_specs.json (default: the skill next to this folder)")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args(argv)
    spec_path = args.spec or default_spec()
    try:
        results, extra = run(args.help_input, spec_path)
    except (IOError, OSError, ValueError) as exc:
        print("Cannot read input: {}".format(exc), file=sys.stderr)
        return 2
    if not results:
        print("The spec has no modules.", file=sys.stderr)
        return 2

    n_err = sum(1 for f in results.values() for s, _ in f if s == "ERROR")
    n_warn = sum(1 for f in results.values() for s, _ in f if s == "WARN")
    if args.format == "json":
        print(json.dumps({"spec": spec_path, "errors": n_err, "warnings": n_warn,
                          "help_not_in_spec": extra,
                          "modules": {k: [{"severity": s, "message": m} for s, m in v]
                                      for k, v in results.items()}}, indent=2))
    else:
        print("Spec: {}".format(spec_path))
        for name, findings in results.items():
            bad = sum(1 for s, _ in findings if s in ("ERROR", "WARN"))
            print("\n{}  ({})".format(name, "differences" if bad else "no differences"))
            for sev, msg in findings:
                print("  {:5} {}".format(sev, msg))
        if extra:
            print("\nHelp captured for apps the spec does not list: {}".format(", ".join(extra)))
        print("\nRESULT: {} error(s), {} warning(s)".format(n_err, n_warn))
        if n_err:
            print("Each ERROR means the skill would build a command the container may reject or "
                  "mis-handle. Fix command_specs.json (or note why the container is wrong), "
                  "regenerate module-parameters.md, and rerun.")
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())
