#!/usr/bin/env python3
"""通用跨 Benchmark 错误分类器（ERROR_TAXONOMY.md 的设计落地）。

设计要点（见 docs/ERROR_TAXONOMY.md）：
  - 错误分类按"错误发生的层面"划分，与具体任务无关（6 个稳定维度 + unknown）。
  - 判定分三层，逐级降级：规则层(rule，确定性可审计) → LLM 层(llm，灰区) → 兜底(fallback，不硬猜)。
  - 规则层优先于 LLM：确定、可复现、可审计，是"判分器可靠性 / 抗刷分"的基础。
  - 失败证据(FailureEvidence)是"评测结果 JSONL 逐字段搬运 + 规范化"出来的薄适配层，
    不产生新数据，只是把散落结果字段改写成统一形状。

本模块不依赖 API，规则层可直接用；LLM 层通过传入的 callable 注入（便于测试与跨模型）。

典型用法：
  from error_classifier import FailureEvidence, ErrorClassifier
  clf = ErrorClassifier()
  result = clf.classify(evidence)
  print(result.error_type, result.source)   # 如 "plumbing_error", "rule:swb_apply_fail"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

# 与任务无关的 6 个稳定错误维度 + unknown。
ERROR_TYPES: list[str] = [
    "intent_misunderstanding",  # 对任务目标/需求理解错了
    "retrieval_failure",        # 该找到的信息没找到 / 找错
    "tool_misuse",              # 工具用错：参数、接口、权限、频率
    "plumbing_error",           # 产物格式/契约问题，非逻辑问题
    "convergence_failure",      # 过程没收敛 / 没产出最终结果
    "resource_exhaustion",      # 成本/资源超限（超时、超预算、超轮数）
    "unknown",                  # 判定器确认不了时不硬猜
]

# 冲突时的裁决顺序：越靠前优先级越高。
PRIORITY: list[str] = [
    "resource_exhaustion",
    "convergence_failure",
    "plumbing_error",
    "tool_misuse",
    "retrieval_failure",
    "intent_misunderstanding",
]

# 旧(legacy)错误类型 → 新(6 维度)映射，供迁移兼容。
LEGACY_TO_NEW: dict[str, str] = {
    "patch_format_malformed": "plumbing_error",
    "behavior_unmatched": "intent_misunderstanding",
    "regression_introduced": "retrieval_failure",
    "no_patch_produced": "convergence_failure",
    "subprocess_timeout": "resource_exhaustion",
}


@dataclass
class FailureEvidence:
    """结构化失败证据（输入），与 benchmark 无关。

    来源见 ERROR_TAXONOMY.md §4.1.1：从 results/{name}.jsonl 逐字段搬运，
    再补齐 problem_statement（回查数据集）等评测结果里没存的字段。
    """

    benchmark: str            # "swebench" | "gaia" | "stress" | ...
    task_id: str              # 可追溯来源（如 django__django-10087）
    problem_statement: str = ""    # 题干（不含 gold）
    agent_output: str = ""         # 模型答复 / 最终回复
    agent_patch: Optional[str] = None  # 代码类才有；GAIA / 压测无 → None
    verdict: str = ""         # 判分结果：resolved/fail/error/none 等
    test_detail: str = ""     # 判分细节（F2P/P2P/error 描述）
    rounds: int = 0
    elapsed_s: float = 0.0
    error: Optional[str] = None   # 基础设施异常（超时 / setup 失败）

    def __post_init__(self) -> None:
        # 归一化 `test_detail` / `error` 为可判定的字符串，避免 None 干扰规则匹配。
        self.test_detail = (self.test_detail or "").lower()
        self.error = (self.error or "").lower() if self.error else ""


@dataclass
class Classification:
    """判定结果（输出）。"""

    error_type: str
    secondary: list[str] = field(default_factory=list)
    confidence: float = 0.0
    rationale: str = ""
    source: str = ""                    # "rule:<name>" | "llm" | "fallback"
    rules_fired: list[str] = field(default_factory=list)


@dataclass
class Rule:
    """一条映射规则：规则名 + 条件函数 + 命中维度 + 裁决结果。"""

    name: str
    test: Callable[[FailureEvidence], bool]
    result: str
    priority: int = 0                  # 越大越先触发（在同一实现层面内）
    rationale: str = ""
    secondary: "list[str] | Callable[[FailureEvidence], list[str]]" = field(default_factory=list)   # 可选副维度（可为 callable 动态计算）


class ErrorClassifier:
    """把失败证据归类到 6 个任务无关维度。三层降级：rule → llm → fallback。"""

    def __init__(self, rules: Optional[list[Rule]] = None,
                 llm: Optional[Callable[[FailureEvidence], Optional[Classification]]] = None) -> None:
        self.rules: list[Rule] = rules if rules is not None else build_default_rules()
        self.llm = llm
        # 规则排序：rule.layer 由 build_default_rules 设定；外部传入时按 priority 降序稳定排序。
        self.rules.sort(key=lambda r: r.priority, reverse=True)

    # ---------- 核心入口 ----------
    def classify(self, evidence: FailureEvidence) -> Classification:
        # 1) 规则层：确定性、可审计，优先。
        for rule in self.rules:
            try:
                if rule.test(evidence):
                    sec = rule.secondary
                    if callable(sec):
                        sec = sec(evidence)
                    return Classification(
                        error_type=rule.result,
                        secondary=[str(x) for x in sec],
                        rationale=rule.rationale,
                        source=f"rule:{rule.name}",
                        rules_fired=[rule.name],
                    )
            except Exception:
                continue  # 单条规则异常不应中断整次分类

        # 2) LLM 层：处理规则覆盖不到的灰区。
        if self.llm is not None:
            try:
                cand = self.llm(evidence)
            except Exception:
                cand = None
            if cand is not None and cand.error_type in ERROR_TYPES:
                cand.source = "llm"
                return cand

        # 3) 兜底：仍归不了 → unknown，绝不硬猜。
        return Classification(
            error_type="unknown",
            confidence=0.0,
            rationale="no rule matched and LLM unavailable/invalid",
            source="fallback",
        )

    @staticmethod
    def normalize(legacy_or_new: str) -> str:
        """把旧(legacy)类型或合法新类型归一化为新维度；非法则 unknown。"""
        if legacy_or_new in ERROR_TYPES:
            return legacy_or_new
        if legacy_or_new in LEGACY_TO_NEW:
            return LEGACY_TO_NEW[legacy_or_new]
        return "unknown"

    @staticmethod
    def build_evidence_from_record(benchmark: str, rec: dict) -> FailureEvidence:
        """从评测结果 JSONL 的单条 record 构造 FailureEvidence（§4.1.1 采集对照表）。"""
        return FailureEvidence(
            benchmark=benchmark,
            task_id=str(rec.get("id") or rec.get("instance_id") or ""),
            agent_output=rec.get("prediction", "") or "",
            agent_patch=rec.get("model_patch") or None,
            verdict=str(rec.get("official_resolved", rec.get("passed", "")) or ""),
            test_detail=str(rec.get("official_test_detail") or rec.get("test_detail") or ""),
            rounds=int(rec.get("rounds") or 0),
            elapsed_s=float(rec.get("elapsed_s") or 0),
            error=rec.get("error") or None,
        )


# ---------- 各 Benchmark 的默认规则（ERROR_TAXONOMY.md §5） ----------

def _contains(text: str, *subs: str) -> bool:
    return any(s in text for s in subs)


def _section(text: str, start_marker: str, end_marker: str) -> str:
    """切出 start_marker 与 end_marker 之间的文本片段（小写），未找到则回空串。"""
    i = text.find(start_marker)
    if i == -1:
        return ""
    rest = text[i + len(start_marker):]
    j = rest.find(end_marker) if end_marker else -1
    return (rest[:j] if j != -1 else rest)


def _segment_failed(segment: str) -> bool:
    """判断某测试片段是否全失败/有失败：识别 'N/M pass' 的 pass 数 < 总数。

    兼容格式：'0/2 pass'、'1/1 pass'、'0/1 pass'、'50/50 pass'。
    若无法解析但出现了失败类关键词，也保守判失败。
    """
    import re
    m = re.search(r"(\d+)\s*/\s*(\d+)\s*pass", segment)
    if m:
        passed, total = int(m.group(1)), int(m.group(2))
        return passed < total
    return _contains(segment, "failed", "fail", "regression", "regress", "0/")


def _has_f2p_failures(text: str) -> bool:
    """F2P 失败：切 'fail_to_pass' 到 'pass_to_pass' 之间，段内 pass 数 < 总数。"""
    seg = _section(text, "fail_to_pass", "pass_to_pass")
    if not seg:
        return _contains(text, "fail_to_pass", "fail")
    return _segment_failed(seg)


def _has_p2p_regression(text: str) -> bool:
    """P2P 回归：切 'pass_to_pass' 到片段末尾，段内 pass 数 < 总数。"""
    seg = _section(text, "pass_to_pass", "")
    if not seg:
        return False
    return _segment_failed(seg)


def build_default_rules() -> list[Rule]:
    """按 ERROR_TAXONOMY.md §5 构造三套 benchmark 的映射规则。"""
    rules: list[Rule] = []

    # ---- SWE-bench（代码修复）----
    rules.append(Rule(
        name="swb_apply_fail", priority=60,
        test=lambda e: e.benchmark == "swebench" and (
            _contains(e.test_detail, "apply", "patch apply failed", "malformed", "patch does not apply")
            or _contains((e.error or ""), "apply")
        ),
        result="plumbing_error",
        rationale="patch apply failed / malformed → 不属于逻辑，是产物格式问题",
    ))
    rules.append(Rule(
        name="swb_setup_error", priority=55,
        test=lambda e: e.benchmark == "swebench" and _contains(e.error or "", "setup failed", "clone"),
        result="resource_exhaustion",
        rationale="setup/clone 失败 → 资源或环境获取失败",
    ))
    rules.append(Rule(
        name="swb_timeout", priority=50,
        test=lambda e: e.benchmark == "swebench" and (
            _contains(e.error or "", "timed out", "timeout", "subprocess") or e.elapsed_s > 600
        ),
        result="resource_exhaustion",
        rationale="timeout / 超时 → 资源耗尽",
    ))
    rules.append(Rule(
        name="swb_no_patch", priority=45,
        test=lambda e: e.benchmark == "swebench" and (
            (e.agent_patch is None or not e.agent_patch.strip())
            or _contains(e.test_detail, "no patch", "empty")
        ),
        result="convergence_failure",
        rationale="未产出有效 patch → 过程未收敛",
    ))
    rules.append(Rule(
        name="swb_f2p_zero", priority=44,
        test=lambda e: e.benchmark == "swebench" and _has_f2p_failures(e.test_detail),
        result="intent_misunderstanding",
        secondary=lambda e: ["retrieval_failure"] if _has_p2p_regression(e.test_detail) else [],
        rationale="F2P 未通过 → 对需求理解偏差，改的方向不对（若同时 P2P 回归则次级为 retrieval_failure）",
    ))
    rules.append(Rule(
        name="swb_p2p_regress", priority=42,
        test=lambda e: e.benchmark == "swebench" and _has_p2p_regression(e.test_detail),
        result="retrieval_failure",
        secondary=["tool_misuse"],
        rationale="P2P 回归 → 改到了不该改的地方（次因 tool_misuse）",
    ))

    # ---- GAIA（问答/推理）----
    rules.append(Rule(
        name="gaia_api_timeout", priority=50,
        test=lambda e: e.benchmark == "gaia" and _contains(e.error or "", "timeout", "timed out", "request"),
        result="resource_exhaustion",
        rationale="GAIA 请求/超时失败 → 资源耗尽",
    ))
    rules.append(Rule(
        name="gaia_unanswered", priority=45,
        test=lambda e: e.benchmark == "gaia" and not (e.agent_output or "").strip(),
        result="convergence_failure",
        rationale="无最终答复 → 过程未收敛",
    ))
    rules.append(Rule(
        name="gaia_uncollapsed", priority=40,
        test=lambda e: e.benchmark == "gaia" and len((e.agent_output or "")) > 4000,
        result="convergence_failure",
        rationale="输出异常长 / 探测反复 → 未收敛",
    ))
    rules.append(Rule(
        name="gaia_nonsense", priority=35,
        test=lambda e: e.benchmark == "gaia" and _contains(e.test_detail, "judge", "irrelevant", "nonsense", "unrelated"),
        result="intent_misunderstanding",
        rationale="答复与问题无关 → 理解偏差",
    ))
    rules.append(Rule(
        name="gaia_missing_evidence", priority=30,
        test=lambda e: e.benchmark == "gaia" and _contains(e.test_detail, "missing", "not found", "no evidence", "retriev", "ground_truth"),
        result="retrieval_failure",
        rationale="缺关键信息 → 检索失败",
    ))

    # ---- 压测（stress_compact）----
    rules.append(Rule(
        name="stress_not_compacting", priority=50,
        test=lambda e: e.benchmark == "stress" and _contains(e.test_detail, "not compact", "no compression", "no reduction"),
        result="plumbing_error",
        rationale="未触发压缩 / token 未减少 → 压缩契约未达成",
    ))
    rules.append(Rule(
        name="stress_output_spill", priority=45,
        test=lambda e: e.benchmark == "stress" and _contains(e.test_detail, "spill", "exceed", "over limit", "not persist"),
        result="plumbing_error",
        rationale="压缩后仍超限 / 未落盘 → 输出契约问题",
    ))
    rules.append(Rule(
        name="stress_slow", priority=40,
        test=lambda e: e.benchmark == "stress" and e.elapsed_s > 60,
        result="resource_exhaustion",
        rationale="压测耗时超阈值 → 资源消耗过大",
    ))

    return rules
