#!/usr/bin/env python3
"""失败归因：读 results/{name}.jsonl → 走 ErrorClassifier → 回填 error_type（ERROR_TAXONOMY.md §10 P4）。

用途：
  - 对某次评测结果（swebench / gaia / stress_compact）里"未 resolved"的失败实例做错误归因，
    产出 {name}_classified.jsonl（含 6 维度 error_type / secondary / confidence / classify_source）。
  - 独立运行，不侵入现有评测脚本（swebench_eval / gaia_eval / stress_compact）。
  - 默认用规则层（确定性、可审计）；可用 --llm 启用 LLM 灰区判定（需 .env 有 API key）。

用法：
  python benchmarks/classify_failures.py --name swebench
  python benchmarks/classify_failures.py --name swebench --out swebench_classified
  python benchmarks/classify_failures.py --name swebench --llm
  python benchmarks/classify_failures.py --name gaia --only-failed
  python benchmarks/classify_failures.py --name swebench --legacy-map   # 把旧数据 error_type 迁移到新维度

诚信底线：
  - 只读失败实例（官方未 resolved）；problem_statement 不含 gold / test patch。
  - 分类只是"描述"，不当作性能提升证据（见 DATA_PIPELINE.md §3）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULT_DIR = ROOT / "benchmarks" / "results"
sys.path.insert(0, str(ROOT / "agents"))

from error_classifier import (  # noqa: E402
    ErrorClassifier,
    FailureEvidence,
    Classification,
    ERROR_TYPES,
)


def _load_jobs(name: str) -> list[tuple[str, dict]]:
    """读 results/{name}.jsonl，返回 (benchmark, record) 列表。"""
    path = RESULT_DIR / f"{name}.jsonl"
    if not path.exists():
        raise SystemExit(f"未找到 {path}，请先跑对应的评测脚本")
    jobs: list[tuple[str, dict]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        jobs.append((name, rec))
    return jobs


def _is_failure(rec: dict) -> bool:
    """判定是否为失败实例：官方未 resolved（含未跑官方、pass 为 False）。"""
    if rec.get("official_resolved") is True:
        return False
    if rec.get("official_resolved") is False or rec.get("official_resolved") is None:
        return True
    return not bool(rec.get("passed"))


def _build_llm(client: object, model: str) -> "object":
    """构造一个可注入的 LLM 分类器 callable（灰区判定，规则层之外的兜底判断）。"""
    system = (
        "You are an error taxonomist. Given a failing agent task, classify the root cause "
        "into exactly one of: " + ", ".join(ERROR_TYPES) + ". "
        "Output ONLY JSON: {\"error_type\": \"...\", \"confidence\": 0.0-1.0, \"rationale\": \"...\"}."
    )

    def llm(evidence: FailureEvidence) -> Classification | None:
        prompt = (
            f"# Task (issue)\n{evidence.problem_statement or '(none)'}\n\n"
            f"# Agent final answer\n{evidence.agent_output or '(none)'}\n\n"
            f"# Failure evidence\n{evidence.test_detail or '(none)'}\n"
            f"# Verdict\n{evidence.verdict or '(none)'}\n# Error\n{evidence.error or '(none)'}\n"
        )
        try:
            resp = client.messages.create(
                model=model, system=system, messages=[{"role": "user", "content": prompt}],
                max_tokens=800,
            )
        except Exception:
            return None
        raw = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        import re
        if "```" in raw:
            m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
            if m:
                raw = m.group(1)
        s, e = raw.find("{"), raw.rfind("}")
        if s == -1 or e <= s:
            return None
        try:
            d = json.loads(raw[s:e + 1])
        except json.JSONDecodeError:
            return None
        et = ErrorClassifier.normalize(str(d.get("error_type", "")))
        if et not in ERROR_TYPES or et == "unknown":
            return None
        try:
            conf = float(d.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        return Classification(error_type=et, confidence=conf,
                              rationale=str(d.get("rationale", "")), source="llm")

    return llm


def classify_jobs(jobs: list[tuple[str, dict]], only_failed: bool, llm_cb=None) -> list[dict]:
    """对 jobs 逐条分类，返回归类记录列表。"""
    clf = ErrorClassifier(llm=llm_cb)
    out: list[dict] = []
    for benchmark, rec in jobs:
        if only_failed and not _is_failure(rec):
            continue
        ev = ErrorClassifier.build_evidence_from_record(benchmark, rec)
        ev.problem_statement = str(rec.get("problem_statement") or "")
        cls = clf.classify(ev)
        rec_out = dict(rec)
        rec_out.update({
            "benchmark": benchmark,
            "error_type": cls.error_type,
            "secondary": cls.secondary,
            "classify_confidence": cls.confidence,
            "classify_rationale": cls.rationale,
            "classify_source": cls.source,
        })
        out.append(rec_out)
    return out


def _write(out: list[dict], out_name: str) -> Path:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULT_DIR / f"{out_name}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        for rec in out:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def _legacy_map(name: str, out_name: str) -> Path:
    """迁移旧数据：把旧 error_type 映射到新 6 维度，写回 {out_name}.jsonl。"""
    jobs = _load_jobs(name)
    out: list[dict] = []
    for benchmark, rec in jobs:
        rec_out = dict(rec)
        old = str(rec_out.get("error_type") or "")
        rec_out["error_type"] = ErrorClassifier.normalize(old)
        rec_out["error_type_legacy"] = old
        out.append(rec_out)
    return _write(out, out_name)


def _summarize(out: list[dict]) -> dict:
    from collections import Counter
    types = Counter(r.get("error_type", "unknown") for r in out)
    sources = Counter(r.get("classify_source", "unknown") for r in out)
    return {
        "total": len(out),
        "by_error_type": dict(types),
        "by_classify_source": dict(sources),
        "unknown_ratio": round(types.get("unknown", 0) / len(out), 4) if out else 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="失败错误归因（ErrorClassifier）")
    parser.add_argument("--name", default="swebench", help="评测结果名（results/{name}.jsonl）")
    parser.add_argument("--out", default=None, help="输出结果名（默认 {name}_classified）")
    parser.add_argument("--only-failed", action="store_true", help="只对失败实例归类")
    parser.add_argument("--llm", action="store_true", help="启用 LLM 灰区判定（需 .env 有 API key）")
    parser.add_argument("--legacy-map", action="store_true", help="仅把旧 error_type 迁移到新维度")
    args = parser.parse_args()

    if args.legacy_map:
        out_name = args.out or f"{args.name}_mapped"
        p = _legacy_map(args.name, out_name)
        print(f"[classify] legacy → new 迁移完成，写入 {p}")
        return 0

    jobs = _load_jobs(args.name)

    llm_cb = None
    if args.llm:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=True)
        load_dotenv(ROOT / "agents" / ".env", override=True)
        import anthropic  # noqa: F401
        client = anthropic.Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
        llm_cb = _build_llm(client, os.environ.get("MODEL_ID", "deepseek-chat"))

    out = classify_jobs(jobs, args.only_failed, llm_cb)
    out_name = args.out or f"{args.name}_classified"
    p = _write(out, out_name)
    print(f"[classify] 归因 {len(out)} 条 → {p}")
    print(json.dumps(_summarize(out), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
