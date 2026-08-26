#!/usr/bin/env python3
"""合成数据 · 任务2：rubric / verifier 生成（复用 GAIA 的 LLM-as-judge 思路）。

作用：
  对 synth_negatives.py 产出的对比对，为每个样本生成"判分标准 rubric"，并做
  LLM-as-judge 二分类判定：修正 patch 是否真能修对（GOOD/BAD）。
  同时输出质控统计（正/反例比例、判分置信度、人工抽查标记），用于验证
  "判分器可靠性"与"抗刷分"——这是数据规范里的人工抽样质检环节。

诚实的约束（对应 JD"分辨数据泄漏与判分漏洞"）：
  - 同一模型既合成又判定，存在"自说自话"偏差；这里如实标注 confounder，并建议
    人工抽样 + 跨模型交叉 judge。绝不声称"没有偏差"。

用法：
  python benchmarks/synth_rubric.py --name synth_negatives --out synth_rubric
  python benchmarks/synth_rubric.py --sample 2          # 只判前 2 条
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULT_DIR = ROOT / "benchmarks" / "results"
sys.path.insert(0, str(ROOT / "agents"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)
load_dotenv(ROOT / "agents" / ".env", override=True)

import anthropic  # noqa: E402

client = anthropic.Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ.get("MODEL_ID", "deepseek-chat")


def _judge(sample: dict) -> dict:
    """为一个合成样本生成 rubric + 判定。"""
    prompt = (
        "# GitHub issue\n" + sample.get("problem_statement", "") + "\n\n"
        "# Candidate fix (unified diff)\n```diff\n"
        + (sample.get("revised_good_patch", "") or "(none)")
        + "\n```\n\n"
        "You are an impartial referee grading whether this candidate fix correctly and "
        "completely resolves the issue WITHOUT introducing regressions. Output ONLY JSON:\n"
        '{"rubric": ["<criterion 1>", "<criterion 2>", "<criterion 3>"], '
        '"verdict": "GOOD", "confidence": "<0.0-1.0>", '
        '"rationale": "<short justification>"}\n'
        "Verdict is GOOD only if the fix plausibly passes the issue and does not obviously "
        "break existing behavior. If the fix is unrelated, partial, or introduces regressions, "
        "return BAD."
    )
    try:
        resp = client.messages.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
        )
        raw = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    except Exception as e:
        print(f"[rubric] API error: {e}")
        return {"rubric": [], "verdict": "UNKNOWN", "confidence": 0.0, "rationale": f"error: {e}"}

    # 提取 JSON
    import re
    if "```" in raw:
        m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
        if m:
            raw = m.group(1)
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e <= s:
        return {"rubric": [], "verdict": "BAD", "confidence": 0.5, "rationale": raw[:500]}
    try:
        d = json.loads(raw[s:e + 1])
    except json.JSONDecodeError:
        return {"rubric": [], "verdict": "BAD", "confidence": 0.5, "rationale": raw[:500]}
    return {
        "rubric": d.get("rubric", []),
        "verdict": str(d.get("verdict", "UNKNOWN")).upper(),
        "confidence": float(d.get("confidence", 0.0) or 0.0),
        "rationale": d.get("rationale", ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="合成样本 rubric / verifier（Phase 6 任务2）")
    parser.add_argument("--name", default="synth_negatives", help="读取的合成结果名")
    parser.add_argument("--out", default="synth_rubric", help="输出结果名")
    parser.add_argument("--sample", type=int, default=0, help="只处理前 N 条（0=全部）")
    parser.add_argument("--judge-model", default=MODEL, help="judge 模型（建议用与合成不同的模型交叉验证）")
    args = parser.parse_args()

    src = RESULT_DIR / f"{args.name}.jsonl"
    if not src.exists():
        print(f"[rubric] 未找到 {src}，先跑 benchmarks/synth_negatives.py")
        return 1

    samples = [json.loads(line) for line in src.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.sample:
        samples = samples[:args.sample]
    print(f"[rubric] 待判 {len(samples)} 条（judge 模型={args.judge_model}，注意与合成同模型的偏差）")

    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULT_DIR / f"{args.out}.jsonl"
    records = []
    for i, s in enumerate(samples):
        j = _judge(s)
        rec = {
            "id": s.get("id"),
            "source_instance": s.get("source_instance"),
            "error_type": s.get("error_type"),
            "verdict": j["verdict"],
            "confidence": j["confidence"],
            "rubric": j["rubric"],
            "rationale": j["rationale"],
            "judge_model": args.judge_model,
            "gold_patch_used": False,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        records.append(rec)
        print(f"[rubric] {i + 1}/{len(samples)} {rec['id']} -> {rec['verdict']} (conf={rec['confidence']})")

    # 质控统计：正/反例比例 + 置信度分布 + 需人工抽查项
    verdicts = [r["verdict"] for r in records]
    good = verdicts.count("GOOD")
    bad = verdicts.count("BAD")
    conf = [float(r["confidence"]) for r in records]
    low_conf = [r["id"] for r in records if float(r["confidence"]) < 0.7]
    qc = {
        "total": len(records),
        "good": good,
        "bad": bad,
        "good_ratio": round(good / len(records), 4) if records else 0,
        "avg_confidence": round(sum(conf) / len(conf), 4) if conf else 0,
        "low_confidence_sample_ids": low_conf,
        "confounder_note": "同一模型合成+判定存在自说自话偏差；建议人工抽查 + 跨模型交叉 judge",
        "manual_review_flag": True,
    }
    json.dump(qc, open(RESULT_DIR / f"{args.out}_qc.json", "w"), ensure_ascii=False, indent=2)
    print(json.dumps(qc, ensure_ascii=False))
    print(f"[rubric] 写入 {out_path} 与 {RESULT_DIR / (args.out + '_qc.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
