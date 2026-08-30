#!/usr/bin/env python3
"""合成数据 · 任务1：构造"错误对比对"(negative contrast pairs)。

用途（对应 JD 的"数据合成"标准）：
  从 SWE-bench 官方判定的失败实例里，把 Agent 的"失败 patch + 失败证据"作为负例，
  用 LLM 分析"错在哪一步"并修正出"理想/改进 diff"作为正例，产出
  (problem, bad patch, error_type, good patch) 的对比对 JSONL，供后续
  训练 / 评测增强 / rubric 校验使用。

数据规范（schema）见 docs/DATA_PIPELINE.md。本脚本是"数据构建"一环。

评测纪律（关键）：
  - 严禁读取 / 注入 gold patch / test patch 到任何 prompt —— 只用
    agent 自己产出的失败 patch + 官方判定的失败原因。
  - 输出的 positive 样本来自 LLM 修正，非 gold patch，属于"合成正例"。

用法：
  python benchmarks/synth_negatives.py --max 4 --name synth_negatives
  python benchmarks/synth_negatives.py --dry    # 不调 API，仅打印将处理的负例
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
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

# 通用错误分类器（ERROR_TAXONOMY.md）：规则层确定性归类，替代旧的硬编码兜底。
sys.path.insert(0, str(ROOT / "agents"))
from error_classifier import ErrorClassifier, ERROR_TYPES as NEW_ERROR_TYPES  # noqa: E402

# ---------- 失败实例的定位 ----------
# 用官方判定为「失败 / 未 resolve / 无效 patch」的实例作为负例来源。
# 注意：只用 agent 产出的 model_patch；绝不触碰 gold patch。

FAILURE_IDS = [  # 与 docs/BENCHMARK.md 失败复盘一致的 4 类
    "django__django-10087",       # patch 格式错误（缺换行）
    "pallets__flask-4045",        # F2P 0/2（行为未匹配）
    "pytest-dev__pytest-10051",   # F2P 0/1 + 1 回归
    "sphinx-doc__sphinx-10021",   # 未产出有效 diff
    "sympy__sympy-11232",         # 子进程超时
]

ERROR_TYPES = [
    "patch_format_malformed",     # 补丁格式非法（缺换行 / 上下文错）
    "behavior_unmatched",         # F2P 未通过（改的方向不对）
    "regression_introduced",      # 额外破坏了 PASS_TO_PASS
    "no_patch_produced",          # 探索未收敛 / 没产出有效 diff
    "subprocess_timeout",         # 工具循环触发超时
]

# 新 6 维度清单，供 LLM prompt 使用（通用跨 benchmark，见 docs/ERROR_TAXONOMY.md）。
CLASSIFIER = ErrorClassifier()


def _load_instances() -> list:
    """读取保留的实例集（问题 + agent 失败 patch + 失败证据）。"""
    # 实例元数据（问题描述，不读 gold patch）
    meta = {}
    local = RESULT_DIR / "swebench_local.json"
    if local.exists():
        for inst in json.loads(local.read_text(encoding="utf-8")):
            meta[inst.get("instance_id")] = {
                "problem_statement": inst.get("problem_statement", ""),
            }

    rows = []
    for line in (RESULT_DIR / "swebench.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        # 只保留失败实例
        if r.get("official_resolved") in (True, "RESOLVED"):
            continue
        if not r.get("model_patch") and not r.get("test_detail"):
            continue
        rows.append({
            "id": r.get("id"),
            "problem_statement": meta.get(r.get("id"), {}).get("problem_statement", ""),
            "agent_patch": r.get("model_patch", "") or "",
            "prediction": r.get("prediction", "") or "",
            "test_detail": r.get("official_test_detail") or r.get("test_detail") or "",
            "rounds": r.get("rounds"),
        })
    return rows


SYSTEM = (
    "You are a senior software engineer building SFT data for an RL/SFT code agent. "
    "You are shown a GitHub issue and a FAILED agent patch plus the failure evidence. "
    "Your job: diagnose WHY it failed, classify the failure, and produce an improved diff "
    "that correctly fixes the issue. Never output the gold solution verbatim, but produce "
    "a working corrected diff that a competent engineer would write."
)


def _call(prompt: str, max_tokens: int = 2000) -> str:  # type: ignore
    try:
        resp = client.messages.create(
            model=MODEL, system=SYSTEM, messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
        )
        return "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    except Exception as e:
        print(f"[synth] API error: {e}")
        return ""


def _extract_json(text: str) -> dict:
    """从模型文本里尽力解析出 JSON（容忍 code fence 与前后缀）。"""
    text = text.strip()
    if "```" in text:
        # 取第一个 ```json ... ``` 块
        import re
        m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
        if m:
            text = m.group(1)
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e <= s:
        return {"error_type": "unknown", "reasoning": text[:2000], "revised_patch": text}
    try:
        return json.loads(text[s:e + 1])
    except json.JSONDecodeError:
        return {"error_type": "unknown", "reasoning": text[:2000], "revised_patch": text}


def _default_error_type(instance_id: str) -> str:
    """【迁移期兜底】旧硬编码映射；新代码优先走 CLASSIFIER 规则层。

    仅在旧的 `--dry` 展示兜底使用。正常合成路径用 _classify_via_rules / classify()。
    """
    m = {
        "django__django-10087": "patch_format_malformed",
        "pallets__flask-4045": "behavior_unmatched",
        "pytest-dev__pytest-10051": "regression_introduced",
        "sphinx-doc__sphinx-10021": "no_patch_produced",
        "sympy__sympy-11232": "subprocess_timeout",
    }
    return m.get(instance_id, "behavior_unmatched")


def _classify(inst: dict) -> tuple[str, str]:
    """用 ErrorClassifier 对失败实例归类，返回 (error_type, classify_source)。

    先走规则层（确定性）；规则不命中则回退旧硬编码并做 legacy→new 归一化。
    """
    ev = ErrorClassifier.build_evidence_from_record(
        "swebench",
        {
            "id": inst.get("id"),
            "model_patch": inst.get("agent_patch"),
            "prediction": inst.get("prediction"),
            "passed": False,
            "official_resolved": False,
            "official_test_detail": inst.get("test_detail") or "",
            "rounds": inst.get("rounds"),
        },
    )
    ev.problem_statement = inst.get("problem_statement") or ""
    cls = CLASSIFIER.classify(ev)
    if cls.source.startswith("rule:"):
        return cls.error_type, cls.source
    # 规则不命中：旧硬编码 → 归一化到新维度
    return ErrorClassifier.normalize(_default_error_type(inst["id"])), "legacy-fallback"


def synth_one(inst: dict) -> dict:
    """对一个失败实例生成对比对。"""
    problem = inst["problem_statement"] or "(no problem statement recorded)"
    bad_patch = inst["agent_patch"] or "(none)"
    evidence = inst["test_detail"] or inst["prediction"] or "(none)"

    prompt = (
        "# GitHub issue (problem statement)\n"
        + problem
        + "\n\n# FAILED agent patch\n```diff\n"
        + bad_patch
        + "\n```\n\n# Failure evidence\n"
        + evidence
        + "\n\nTask: output ONLY a JSON object (no prose) with exactly these fields:\n"
        + '{"error_type": "<one of: <<ERROR_TYPES>>>", '
        + '"reasoning": "<2-4 sentences: where the agent went wrong>", '
        + '"revised_patch": "<a corrected unified diff that fixes the issue, '
        + 'or a concrete code-change plan with hunks>"}'
    ).replace("<<ERROR_TYPES>>", ", ".join(NEW_ERROR_TYPES))
    raw = _call(prompt)
    payload = _extract_json(raw)
    # LLM 给出的是新 6 维度；校验不通过则回退规则层/旧硬编码。
    compat_types = NEW_ERROR_TYPES
    error_type = payload.get("error_type", "")
    if error_type not in compat_types:
        error_type, classify_source = _classify(inst)
    else:
        classify_source = "llm"

    return {
        "id": f"synth_neg_{inst['id'].split('__')[-1]}",
        "source_instance": inst["id"],
        "error_type": error_type,
        "classify_source": classify_source,
        "problem_statement": problem,
        "agent_bad_patch": bad_patch,
        "revised_good_patch": payload.get("revised_patch", ""),
        "reasoning": payload.get("reasoning", ""),
        "reward_neg": 0,          # agent 失败 patch = 负例
        "reward_pos": 1,          # LLM 修正 patch = 正例（合成）
        "verifier": "pending",
        "gold_patch_used": False,  # 明确声明未使用 gold patch
        "model": MODEL,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="合成负例对比对（Phase 6 任务1）")
    parser.add_argument("--max", type=int, default=4, help="最多处理实例数（默认 4）")
    parser.add_argument("--name", default="synth_negatives", help="结果名")
    parser.add_argument("--dry", action="store_true", help="不调 API，仅列出将处理的负例")
    args = parser.parse_args()

    instances = _load_instances()
    if not instances:
        print("[synth] 无可用失败实例（检查 results/swebench.jsonl 与 swebench_local.json）")
        return 1
    print(f"[synth] 将处理 {len(instances)} 个失败实例（仅用 agent 失败 patch，gold patch 零注入）")

    if args.dry:
        for i in instances[:args.max]:
            print(f"  - {i['id']} | rounds={i['rounds']} | patch_len={len(i['agent_patch'])} | type={_default_error_type(i['id'])}")
        return 0

    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULT_DIR / f"{args.name}.jsonl"
    done = 0
    for inst in instances[:args.max]:
        t0 = time.time()
        rec = synth_one(inst)
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        done += 1
        print(f"[synth] {done}/{min(args.max, len(instances))} {inst['id']} -> {rec['error_type']} "
              f"(patch_len={len(rec['revised_good_patch'])}, {time.time() - t0:.1f}s)")

    print(f"[synth] 完成，写入 {out_path}（{done} 条）。若要 rubric 校验请跑 benchmarks/synth_rubric.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
