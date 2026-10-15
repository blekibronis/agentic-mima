# agentic-mima test kit

Lives next to the skill folder, not inside it (the guide keeps the skill folder to SKILL.md, scripts, references and assets).

| File | What it does |
|---|---|
| `run_tests.py` | 110 automated tests: builder, validators, PBS script checks, the submission gate including human approval through a real pseudo-terminal, status parsing, log scanning, output and report checks, and a consistency lint of the skill |
| `container_check/` | `capture_container_help.sh` (run on Gadi) saves the container's `--help` for every app; `compare_help_to_spec.py` reports every place the skill's `command_specs.json` disagrees with it. Covered by 18 tests against real help text from 2026-10-07. See its README |
| `make_workspace.py` | builds a mock cluster and example run folder for conversation tests |
| `conversation_tests.md` | what to test with Claude itself: safety gate, adversarial approval, failure diagnosis, interpretation, report loop, mode transparency, with-and-without comparison |
| `trigger_queries.md` | 12 should-trigger and 8 should-not-trigger queries with a scoring sheet |

Run: `python3 run_tests.py` (skill found next to this folder, or `MIMA_SKILL=/path/to/agentic-mima`). The skill's scripts need Python 3.6 or later (vermin reports 3.6 for every script; four were also run on Gadi's Python 3.6.8). The test kit itself needs Python 3.8 or later: run it on your own machine, or after `module load` of a newer Python on Gadi.

## What a pass means

A pass shows the scripts behave as designed against mock inputs and that the skill's files are consistent with the guide's rules and with each other. It does not show the skill works on a real cluster, with the real container, or that Claude follows the instructions in a conversation. The conversation tests and the first-real-use checklist cover that.

Re-run after every change. The consistency tests exist because an earlier edit left stale text in one file while another was rewritten.
