#!/usr/bin/env python3
"""结果沉淀（阶段 5）：汇总评测结果 → JSONL / 表格，写 BENCHMARK.md。

所有评测结果统一落盘到 benchmarks/results/，便于复现与对比。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def append_result(result: dict, name: str = "eval") -> Path:
    """把单条评测结果追加写入 results/{name}.jsonl（每行一个 JSON）。"""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{name}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    return path


def load_results(name: str = "eval") -> list:
    """读取某次评测的全部结果记录。"""
    path = RESULTS_DIR / f"{name}.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def summarize(results: list) -> dict:
    """汇总指标：条数、通过率、平均轮次、平均耗时、平均成本。"""
    if not results:
        return {"count": 0}
    total = len(results)
    passed = sum(1 for r in results if r.get("passed"))
    return {
        "count": total,
        "passed": passed,
        "pass_rate": round(passed / total, 4),
        "avg_rounds": round(sum(r.get("rounds", 0) for r in results) / total, 2),
        "avg_elapsed_s": round(sum(r.get("elapsed_s", 0) for r in results) / total, 2),
        "total_cost_usd": round(sum(r.get("cost_usd", 0) for r in results), 4),
    }


def write_benchmark_md(name: str = "eval", model: str = "", config: str = "") -> Path:
    """把汇总结果沉淀为 BENCHMARK.md。"""
    results = load_results(name)
    summary = summarize(results)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / "BENCHMARK.md"

    lines = [
        "# BENCHMARK",
        "",
        f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 数据集: {name}",
        f"- 模型: {model or 'see .env MODEL_ID'}",
        f"- 配置: {config or '(默认)'}",
        "",
        "## 汇总",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 条数 | {summary.get('count', 0)} |",
        f"| 通过 | {summary.get('passed', 0)} |",
        f"| 通过率 | {summary.get('pass_rate', 0)} |",
        f"| 平均轮次 | {summary.get('avg_rounds', 0)} |",
        f"| 平均耗时(s) | {summary.get('avg_elapsed_s', 0)} |",
        f"| 总成本($) | {summary.get('total_cost_usd', 0)} |",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="汇总评测结果并沉淀 BENCHMARK.md")
    parser.add_argument("--name", default="eval", help="评测结果名（对应 results/{name}.jsonl）")
    parser.add_argument("--model", default="", help="模型名")
    parser.add_argument("--config", default="", help="运行配置描述")
    args = parser.parse_args()
    p = write_benchmark_md(args.name, args.model, args.config)
    print(json.dumps(summarize(load_results(args.name)), ensure_ascii=False))
    print(f"wrote {p}")
