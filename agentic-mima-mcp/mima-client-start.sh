#!/bin/bash
# Start the agentic-mima client on Gadi with the right settings.
#   bash mima-client-start.sh --base-url http://HEAD_IP:8000/v1 --model kimi-k3
# Add "preflight" first to run the quick model checks instead:
#   bash mima-client-start.sh preflight --base-url http://HEAD_IP:8000/v1 --model kimi-k3
set -e
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HOME/mima-mcp-venv/bin/activate" ] && source "$HOME/mima-mcp-venv/bin/activate"
export MIMA_SKILL_DIR="${MIMA_SKILL_DIR:-$here/../agentic-mima}"
export MIMA_ALLOWED_ROOTS="${MIMA_ALLOWED_ROOTS:-$HOME:/scratch/mp96:/g/data/mp96}"
cd "$here"
if [ "$1" = "preflight" ]; then shift; exec python3 -m mima_client.preflight "$@"; fi
# Submitting and running steps are switched on only for the chat client: the server then asks YOU to type SUBMIT or RUN first.
export MIMA_ALLOW_SUBMIT="${MIMA_ALLOW_SUBMIT:-1}"
export FASTMCP_CHECK_FOR_UPDATES=off
export MIMA_ALLOW_RUN="${MIMA_ALLOW_RUN:-1}"
command -v apptainer >/dev/null 2>&1 || { type module >/dev/null 2>&1 && module load apptainer 2>/dev/null || true; }
exec python3 -m mima_client "$@"
