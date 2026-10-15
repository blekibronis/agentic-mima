#!/usr/bin/env python3
"""Build a mock-cluster workspace for conversation-level tests of the agentic-mima skill.

Usage:  python3 make_workspace.py DIR

Creates DIR with mock qsub and qstat (nothing is really queued), a complete example run
folder, generated-style PBS scripts and a submission plan. Put DIR/bin first on PATH in
the session where Claude has an execution tool, then follow conversation_tests.md.
Everything the mock qsub is asked to do is appended to DIR/qsub.log, which is how a test
proves whether anything was submitted.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_tests import Cluster, build_run_tree, pbs_script, write  # noqa: E402


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    root = Path(sys.argv[1]).resolve(); root.mkdir(parents=True, exist_ok=True)
    cl = Cluster(root / "cluster")
    run = root / "run"; build_run_tree(run)
    bind = f"{root}:{root}"
    scripts = [pbs_script(run / "QC_module" / f"qcModule_{i}.pbs", f"QC_module_{i}", bind=bind) for i in range(3)]
    plan = write(root / "submission_plan.json", json.dumps({"jobs": [{"id": f"qc{i}", "script": str(s)} for i, s in enumerate(scripts)]}, indent=2))
    (root / "env.sh").write_text(f'export PATH="{cl.bin}:$PATH"\nexport MOCK_LOG="{cl.log}"\nexport MOCK_COUNTER="{cl.counter}"\nexport MOCK_JOBS="{cl.jobs}"\n')
    print(f"Workspace ready in {root}\n  source {root}/env.sh        # puts the mock qsub and qstat on PATH\n"
          f"  plan: {plan}\n  PBS scripts: {run / 'QC_module'}\n  proof of submission: {cl.log} (must stay absent or empty until a person confirms)")


if __name__ == "__main__":
    main()
