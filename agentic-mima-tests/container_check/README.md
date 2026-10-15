# Container check: does the skill's spec match the real container?

The 99 unit tests prove the skill behaves as its own spec says. They cannot tell
whether `command_specs.json` matches the MIMA container. This folder does.

## Run it (about two minutes)

On Gadi, with the MIMA image:

    bash capture_container_help.sh /path/to/mima4_v2.0.0.sif mima_help_capture

Copy `mima_help_capture/` next to the skill, then:

    python3 compare_help_to_spec.py mima_help_capture --spec ../agentic-mima/scripts/command_specs.json

Exit code 0 means no ERROR. Each ERROR is a place where the skill would build a
command the container may reject. WARN is an option the skill cannot pass. NOTE is
informational (project policy stricter than --help, defaults the spec does not record).

## What it cannot do

It reads `--help`, which is what the container advertises. It does not prove a flag
works, does not run anything, and gets less from the R and Perl apps (no required
flags or defaults for Random Forest; positional names only for vis-taxa).
Rerun it whenever the container image changes.

`fixtures/real_help_2026-10-07.txt` is the real help text from container
mima4_v2.0.0_20240409, used by the tests.
