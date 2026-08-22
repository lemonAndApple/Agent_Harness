#!/usr/bin/env python3
"""GAIA 评测（阶段 3）：喂问题 → 取最后一条 assistant 文本 → 精确匹配 + LLM-as-judge 双评分。

比 SWE-bench 简单，适合作为无头 pipeline 的快速验证。
数据源：HuggingFace `gaia-benchmark/GAIA`，仅取 level1 纯文本子集（无需 web 工具）。

用法：
  python benchmarks/gaia_eval.py --max 5 --split test
  python benchmarks/gaia_eval.py --judge   # 启用 LLM-as-judge 二次评分
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

import Agent_Harness as A  # noqa: E402
from eval_runner import run_episode  # noqa: E402
from results_sink import append_result, load_results, summarize, write_benchmark_md  # noqa: E402

GAIA_DATASET = "gaia-benchmark/GAIA"


def _norm(text: str) -> str:
    """规范化文本用于精确匹配：去空白、去标点、转小写。"""
    if not text:
        return ""
    text = re.sub(r"\s+", " ", str(text))
    text = re.sub(r"[^\w\s]", "", text)
    return text.strip().lower()


def _exact_match(prediction: str, ground_truth: str) -> bool:
    """GAIA 官方要求 ground truth 精确匹配（规范化后相等）。"""
    return _norm(prediction) == _norm(ground_truth)


def _llm_judge(prediction: str, ground_truth: str, question: str) -> bool:
    """LLM-as-judge 二次评分：语义等价判定（一次小 API 调用）。"""
    prompt = (
        "Determine if the following two answers are semantically equivalent "
        "(both correctly answer the question). Reply with exactly 'YES' or 'NO'.\n\n"
        f"Question: {question}\nPrediction: {prediction}\nGround truth: {ground_truth}"
    )
    try:
        resp = A.client.messages.create(
            model=A.MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=8,
        )
        answer = "".join(b.text for b in resp.content if hasattr(b, "text")).strip().upper()
        return answer.startswith("YES")
    except Exception as e:
        A.log.error(f"LLM judge error: {e}")
        return False


def load_gaia(max_items: int = None, split: str = "test") -> list: # type: ignore
    """加载 GAIA 数据并过滤出 level1 纯文本子集。"""
    try:
        from datasets import load_dataset
    except ImportError as e:
        raise SystemExit("需要 `pip install datasets` 才能加载 GAIA 数据") from e

    ds = load_dataset(GAIA_DATASET, split=split)
    items = []
    for row in ds:
        # level1 纯文本子集：不含附件（files 为空）且 level==1
        if row.get("Level") != "1": # type: ignore
            continue
        files = row.get("file_name") or row.get("files") or []
        if files:
            continue
        items.append({
            "id": row.get("task_id"),
            "question": row.get("Question"),
            "ground_truth": row.get("Final answer"),
        })
        if max_items and len(items) >= max_items:
            break
    return items


def run_gaia(max_items: int = None, judge: bool = False, split: str = "test",
             workdir_root: Path = None, name: str = "gaia") -> None: # type: ignore
    """运行 GAIA 评测：逐条 run_episode → 双评分 → 沉淀结果。"""
    items = load_gaia(max_items, split)
    A.bootstrap()

    passed_exact = 0
    passed_judge = 0
    for i, item in enumerate(items):
        t0 = time.time()
        q = item["question"]
        gt = item["ground_truth"]
        sandbox = (workdir_root / f"gaia_{i:03d}").resolve() if workdir_root else None
        result = run_episode(q, workdir=sandbox, max_rounds=20)
        prediction = result["final_reply"]

        exact = _exact_match(prediction, gt)
        judge_ok = _llm_judge(prediction, gt, q) if (judge and not exact) else exact
        if exact:
            passed_exact += 1
        if judge_ok:
            passed_judge += 1

        record = {
            "id": item["id"], "passed": judge_ok, "passed_exact": exact,
            "rounds": result["rounds"], "elapsed_s": result["elapsed_s"],
            "prediction": prediction, "ground_truth": gt,
        }
        append_result(record, name)
        A.log.info(f"[{i + 1}/{len(items)}] exact={exact} judge={judge_ok} id={item['id']}")

    write_benchmark_md(name, config=f"gaia max={len(items)} judge={judge}")
    print(json.dumps(summarize(load_results(name)), ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GAIA 评测")
    parser.add_argument("--max", type=int, default=5, help="最多评测条数（默认 5）")
    parser.add_argument("--split", default="test", help="数据 split")
    parser.add_argument("--judge", action="store_true", help="启用 LLM-as-judge 二次评分")
    parser.add_argument("--workdir-root", type=Path, default=None, help="沙箱根目录")
    parser.add_argument("--name", default="gaia", help="结果名")
    args = parser.parse_args()
    run_gaia(args.max, args.judge, args.split, args.workdir_root, args.name)
