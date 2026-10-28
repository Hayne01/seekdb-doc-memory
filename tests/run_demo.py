#!/usr/bin/env python3
"""跑一遍验证集，统计 top1 命中率与无答案问题是否留空。

    python tests/run_demo.py [--top 3]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "docmem.py"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=3)
    args = parser.parse_args()

    rows = [json.loads(line) for line in (HERE / "queries.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    hit1 = graded = no_answer_ok = 0
    total_ms = 0

    for row in rows:
        cmd = [sys.executable, str(SCRIPT), "search", row["question"], "--top", str(args.top), "--json"]
        if row.get("dir"):
            cmd += ["--dir", row["dir"]]
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        if out.returncode != 0:
            print(f"FAIL  {row['question']}\n{out.stderr.strip()}")
            continue

        result = json.loads(out.stdout)
        total_ms += result["latency_ms"]
        paths = [h["path"] for h in result["hits"]]

        if row["expect"] is None:
            ok = not paths
            no_answer_ok += 1 if ok else 0
            print(f"{'OK  ' if ok else 'LEAK'}  {row['question']}  -> {len(paths)} 条")
            continue

        graded += 1
        ok = bool(paths) and paths[0] == row["expect"]
        hit1 += 1 if ok else 0
        top = paths[0] if paths else "（空）"
        print(f"{'OK  ' if ok else 'MISS'}  {row['question']}  -> {top}  {result['latency_ms']} ms")

    if graded:
        print(f"\ntop1 {hit1}/{graded} · 无答案留空 {no_answer_ok} · 平均 {total_ms / len(rows):.0f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
