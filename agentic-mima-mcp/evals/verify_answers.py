#!/usr/bin/env python3
"""Recompute each evaluation answer by calling the server's tools directly (no model involved).

Usage: MIMA_SKILL_DIR=/path/to/agentic-mima python3 verify_answers.py    (after make_eval_data.py)
Prints one line per question and exits 1 if any recomputed answer differs from evaluation.xml.
"""
import asyncio
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastmcp import Client  # noqa: E402

from mima_mcp.server import mcp  # noqa: E402

D = "/tmp/mima_eval"


async def main():
    async with Client(mcp) as c:
        async def call(name, **args):
            return (await c.call_tool(name, args)).data

        ans = []
        ok = [p for p in ("manifest_run1.csv", "manifest_run2.csv")
              if (await call("mima_validate_manifest", manifest_path=f"{D}/manifests/{p}"))["status"] == "valid"]
        ans.append(ok[0] if len(ok) == 1 else f"ambiguous:{ok}")
        meta = await call("mima_validate_metadata", metadata_path=f"{D}/study/metadata.tsv", group_column="lab",
                          feature_tables=[f"{D}/study/featureTables/bracken_FT_species_relAbund"])
        miss = re.search(r"(\d+) metadata sample\(s\) missing from this table.*?: ([A-Za-z0-9, ]+)", meta["output"])
        ids = sorted(i.strip() for i in miss.group(2).split(","))
        ans += [miss.group(1), ids[0]]
        rl = await call("mima_estimate_read_length", paths=[f"{D}/CleanReads"])
        ans.append(re.search(r"--read-length (\d+)", rl["output"]).group(1))
        qc = await call("mima_scan_logs", paths=[f"{D}/logs/mima-QC.o900001"])
        sample = re.search(r"(E\d\d)_", qc["data"]["findings"][0]["lines"][0]["text"]).group(1)
        out = await call("mima_check_outputs", stage="qc", directory=f"{D}/run/QC_module", manifest=f"{D}/manifests/manifest_run1.csv")
        ans.append("No" if any(f"{sample}:" in f["message"] for f in out["data"]["findings"]) else "Yes")
        taxa = await call("mima_scan_logs", paths=[f"{D}/logs/mima-taxa.o900002"], max_hits=50)
        ans.append(str(len({re.search(r"/(E\d\d)\.", h["text"]).group(1) for f in taxa["data"]["findings"] for h in f["lines"]})))
        e03 = await call("mima_scan_logs", paths=[f"{D}/logs/E03_bracken.log"])
        ans.append(e03["data"]["findings"][0]["pattern"])
        cfg = await call("mima_validate_pbs_config", config_path=f"{D}/config/pbs_header_taxa.cfg", module="taxa")
        m = re.search(r"Memory (\d+)GB is below ~(\d+)GB", cfg["output"])
        ans.append(str(int(m.group(2)) - int(m.group(1))))
        bad = []
        for n in ("50", "75", "100", "150"):
            plan = {"module": "mima-taxa-profiling", "params": {"-i": f"{D}/run/CleanReads", "-o": f"{D}/run/Taxonomy_profiling",
                    "--reference-path": f"{D}/db/GTDB_Kraken", "--read-length": n, "--threshold": "100", "--mode": "container",
                    "--pbs-config": f"{D}/config/pbs_header_taxa.cfg"}}
            r = await call("mima_build_commands", plan=plan, check_fs=True)
            if "kmer_distrib not found" in str(r.get("data")) + r["output"]:
                bad.append(n)
        ans.append(bad[0] if len(bad) == 1 else f"ambiguous:{bad}")
        gone = next(f["message"] for f in out["data"]["findings"] if f["message"].startswith("samples absent"))
        absent = set(re.findall(r"E\d\d", gone))
        ans.append(str(len(absent)))
    expected = [e.findtext("answer") for e in ET.parse(Path(__file__).with_name("evaluation.xml")).getroot().iter("qa_pair")]
    bad = 0
    for i, (a, e) in enumerate(zip(ans, expected), 1):
        flag = "ok " if a == e else "DIFF"
        bad += a != e
        print(f"{flag} Q{i}: computed={a!r} expected={e!r}")
    return 1 if bad or len(ans) != len(expected) else 0


sys.exit(asyncio.run(main()))
