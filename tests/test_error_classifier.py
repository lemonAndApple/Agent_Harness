from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))

from error_classifier import (  # noqa: E402
    Classification,
    ErrorClassifier,
    FailureEvidence,
    build_default_rules,
    LEGACY_TO_NEW,
    ERROR_TYPES,
)


def _ev(benchmark="swebench", **kw) -> FailureEvidence:
    base = dict(
        benchmark=benchmark,
        task_id="x__y-1",
        problem_statement="some issue",
        agent_output="...",
        agent_patch="diff --git a/x.py b/x.py",
        verdict="False",
        test_detail="",
        rounds=3,
        elapsed_s=10.0,
    )
    base.update(kw)
    return FailureEvidence(**base)


def test_default_rules_cover_majority_of_swebench_apply_fail():
    clf = ErrorClassifier()
    ev = _ev(test_detail="patch apply failed: patch unexpectedly ends in middle of line")
    c = clf.classify(ev)
    assert c.error_type == "plumbing_error"
    assert c.source == "rule:swb_apply_fail"
    assert c.rules_fired == ["swb_apply_fail"]


def test_swebench_f2p_unmatched():
    clf = ErrorClassifier()
    ev = _ev(test_detail="FAIL_TO_PASS 0/2 pass; PASS_TO_PASS 50/50 pass")
    c = clf.classify(ev)
    assert c.error_type == "intent_misunderstanding"
    assert c.secondary == []            # P2P 全过，无回归 → 仅主维度
    assert c.source == "rule:swb_f2p_zero"


def test_swebench_p2p_regression_without_f2p_fail():
    """仅 P2P 回归、F2P 未提及失败 → 主维度 retrieval_failure，次因 tool_misuse。"""
    clf = ErrorClassifier()
    ev = _ev(test_detail="FAIL_TO_PASS 1/1 pass; PASS_TO_PASS 14/15 pass (1 regression)")
    c = clf.classify(ev)
    assert c.error_type == "retrieval_failure"
    assert c.secondary == ["tool_misuse"]
    assert c.source == "rule:swb_p2p_regress"


def test_swebench_both_f2p_and_p2p_fail():
    """设计文档 §5.1：F2P 与 P2P 同时失败 → 主维度 intent_misunderstanding，次级 retrieval_failure。"""
    clf = ErrorClassifier()
    ev = _ev(test_detail="FAIL_TO_PASS 0/1 pass; PASS_TO_PASS 14/15 pass (F2P failed, 1 regression)")
    c = clf.classify(ev)
    assert c.error_type == "intent_misunderstanding"
    assert "retrieval_failure" in c.secondary
    assert c.source == "rule:swb_f2p_zero"


def test_swebench_no_patch():
    clf = ErrorClassifier()
    c = clf.classify(_ev(test_detail="no patch produced"))
    assert c.error_type == "convergence_failure"


def test_swebench_timeout():
    clf = ErrorClassifier()
    c = clf.classify(_ev(error="Command timed out after 600 seconds"))
    assert c.error_type == "resource_exhaustion"


def test_swebench_setup_error():
    clf = ErrorClassifier()
    c = clf.classify(_ev(error="setup failed: git clone timed out"))
    assert c.error_type == "resource_exhaustion"


def test_gaia_unanswered():
    clf = ErrorClassifier()
    c = clf.classify(_ev(benchmark="gaia", agent_output=""))
    assert c.error_type == "convergence_failure"


def test_gaia_nonsense_by_judge():
    clf = ErrorClassifier()
    c = clf.classify(_ev(benchmark="gaia", test_detail="judge: answer is irrelevant"))
    assert c.error_type == "intent_misunderstanding"


def test_stress_not_compacting():
    clf = ErrorClassifier()
    c = clf.classify(_ev(benchmark="stress", test_detail="no compression triggered"))
    assert c.error_type == "plumbing_error"


def test_unknown_when_no_rule_and_no_llm():
    clf = ErrorClassifier()
    c = clf.classify(_ev(benchmark="unknown_bench", test_detail="something unclassifiable"))
    assert c.error_type == "unknown"
    assert c.source == "fallback"
    assert c.confidence == 0.0


def test_llm_layer_bridges_gray_area():
    def llm(_ev: FailureEvidence) -> Classification:
        return Classification(error_type="tool_misuse", confidence=0.7,
                              rationale="llm test", source="llm")

    clf = ErrorClassifier(llm=llm)
    c = clf.classify(_ev(benchmark="unknown_bench", test_detail="gray area"))
    assert c.error_type == "tool_misuse"
    assert c.source == "llm"


def test_llm_invalid_type_falls_back_to_unknown():
    def llm(_ev: FailureEvidence) -> Classification:
        return Classification(error_type="not_a_real_type", confidence=0.9)

    clf = ErrorClassifier(llm=llm)
    c = clf.classify(_ev(benchmark="unknown_bench"))
    assert c.error_type == "unknown"


def test_rule_layer_takes_priority_over_llm():
    def llm(_ev: FailureEvidence) -> Classification:
        return Classification(error_type="tool_misuse", confidence=0.9)

    clf = ErrorClassifier(llm=llm)
    c = clf.classify(_ev(test_detail="patch apply failed"))
    assert c.error_type == "plumbing_error"
    assert c.source.startswith("rule:")


def test_legacy_to_new_mapping():
    assert LEGACY_TO_NEW["patch_format_malformed"] == "plumbing_error"
    assert LEGACY_TO_NEW["behavior_unmatched"] == "intent_misunderstanding"
    assert LEGACY_TO_NEW["regression_introduced"] == "retrieval_failure"
    assert LEGACY_TO_NEW["no_patch_produced"] == "convergence_failure"
    assert LEGACY_TO_NEW["subprocess_timeout"] == "resource_exhaustion"


@pytest.mark.parametrize("legacy,new", LEGACY_TO_NEW.items())
def test_normalize_legacy(legacy: str, new: str) -> None:
    assert ErrorClassifier.normalize(legacy) == new


def test_normalize_new_stays_new():
    assert ErrorClassifier.normalize("plumbing_error") == "plumbing_error"
    assert ErrorClassifier.normalize("banana") == "unknown"


def test_build_evidence_from_record():
    rec = {
        "id": "psf__requests-1142",
        "model_patch": "diff --git ...",
        "prediction": "fixed",
        "passed": False,
        "official_resolved": False,
        "official_test_detail": "FAIL_TO_PASS 0/2 pass",
        "rounds": 10,
        "elapsed_s": 5.5,
        "error": None,
    }
    ev = ErrorClassifier.build_evidence_from_record("swebench", rec)
    assert ev.task_id == "psf__requests-1142"
    assert ev.agent_patch == "diff --git ..."
    assert ev.test_detail.startswith("fail_to_pass")
    assert ev.rounds == 10


def test_error_types_are_stable_and_priority_consistent():
    assert len(ERROR_TYPES) == 7
    for t in ("intent_misunderstanding", "retrieval_failure", "tool_misuse",
              "plumbing_error", "convergence_failure", "resource_exhaustion", "unknown"):
        assert t in ERROR_TYPES
    # 注册的默认规则结果必须都是合法维度
    for rule in build_default_rules():
        assert rule.result in ERROR_TYPES


def test_priority_order_swb_apply_before_f2p():
    """apply_fail 应优先于 f2p（格式问题先于逻辑问题）。"""
    ev = _ev(test_detail="patch apply failed (malformed); FAIL_TO_PASS 0/2 pass")
    clf = ErrorClassifier()
    assert clf.classify(ev).error_type == "plumbing_error"


def test_exception_in_rule_does_not_crash():
    def boom(_ev: FailureEvidence) -> bool:
        raise RuntimeError("boom")

    class BadRule:
        name = "bad"
        result = "tool_misuse"
        priority = 999
        rationale = ""
        test = boom

    clf = ErrorClassifier(rules=[BadRule()] + build_default_rules())
    c = clf.classify(_ev(test_detail="patch apply failed"))
    assert c.error_type == "plumbing_error"
