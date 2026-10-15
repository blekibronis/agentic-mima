#!/usr/bin/env bash
# Run this ON GADI (or any machine with Apptainer and the MIMA image).
# It saves the --help text of every MIMA app, exactly as the container prints it,
# so compare_help_to_spec.py can check the skill's command_specs.json against it.
#
# Usage:   bash capture_container_help.sh /path/to/mima.sif [OUTPUT_DIR]
# Output:  OUTPUT_DIR/<app>.txt for each app, plus OUTPUT_DIR/_capture_info.txt
#
# The help text is read straight from each app's runscript inside the image
# (/scif/apps/APP/scif/runscript), the route that worked on Gadi on 2026-10-07.
# `scif run` is not on the PATH inside the image, so it is not used.
# Nothing is written outside OUTPUT_DIR and nothing is submitted.

set -u

SIF="${1:-}"
OUT="${2:-mima_help_capture}"

if [ -z "$SIF" ] || [ ! -f "$SIF" ]; then
    echo "Usage: bash $0 /path/to/mima.sif [OUTPUT_DIR]" >&2
    echo "The image file was not given or does not exist: '$SIF'" >&2
    exit 2
fi
if ! command -v apptainer >/dev/null 2>&1; then
    echo "apptainer is not on the PATH. On Gadi run: module load singularity or apptainer, then retry." >&2
    exit 2
fi

APPS="mima-qc mima-qc-report mima-taxa-profiling mima-bracken-report mima-function-profiling mima-vis-taxa mima-visualisation mima-classifier-RF"

mkdir -p "$OUT" || { echo "Cannot create $OUT" >&2; exit 2; }

failed=0
for app in $APPS; do
    # stdout and stderr are both kept: argparse prints help on stdout, R and Perl
    # print theirs on stderr or after start-up messages. --help can exit non-zero
    # (the Perl app does), so the exit code is not treated as a failure.
    apptainer exec "$SIF" "/scif/apps/$app/scif/runscript" --help > "$OUT/$app.txt" 2>&1
    if grep -qiE "^usage:|^USAGE:" "$OUT/$app.txt"; then
        echo "captured  $app ($(wc -l < "$OUT/$app.txt") lines)"
    else
        echo "NO USAGE TEXT FOUND for $app: check $OUT/$app.txt" >&2
        failed=$((failed + 1))
    fi
done

{
    echo "captured_at_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "host: $(hostname)"
    echo "image: $SIF"
    echo "image_sha256: $(sha256sum "$SIF" 2>/dev/null | cut -d' ' -f1)"
    echo "apptainer: $(apptainer --version 2>&1)"
} > "$OUT/_capture_info.txt"

if [ "$failed" -gt 0 ]; then
    echo "$failed app(s) produced no usage text. Fix this before comparing." >&2
    exit 1
fi
echo "Done. Next: python3 compare_help_to_spec.py $OUT --spec /path/to/agentic-mima/scripts/command_specs.json"
