#!/usr/bin/env python3
"""SWE-bench 结果可视化：把 results/{name}.jsonl 渲染为图表（matplotlib）。

用法：
  python benchmarks/visualize.py                      # 默认渲染 swebench
  python benchmarks/visualize.py --name gaia          # 渲染其它评测
  python benchmarks/visualize.py --no-show            # 只存图不弹窗

输出：
  benchmarks/results/visualization/
    dashboard.png   整体看板（通过率 + 轮次 + 耗时 + 命中文件）
    per_repo.png    按仓库汇总
    per_instance.png 单条明细
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

from results_sink import load_results, summarize  # noqa: E402

# 中文字体（找不到就退回 DejaVu Sans 英文渲染）
_CN_FONT = None
for _cand in ["Noto Sans CJK SC", "WenQuanYi Zen Hei", "PingFang SC", "Microsoft YaHei", "SimHei"]:
    if any(f.name == _cand for f in fm.fontManager.ttflist):
        _CN_FONT = _cand
        break
if _CN_FONT:
    plt.rcParams["font.sans-serif"] = [_CN_FONT] + plt.rcParams.get("font.sans-serif", [])
plt.rcParams["axes.unicode_minus"] = False

OUT_DIR = Path(__file__).resolve().parent / "results" / "visualization"


def _short(id_: str, n: int = 34) -> str:
    return id_ if len(id_) <= n else id_[: n - 1] + "…"


def _ascii(s) -> str:
    """把可能含 CJK 的内容转成 ASCII，避免缺字形告警。"""
    return "".join(c if ord(c) < 128 else "?" for c in str(s))


def _dedupe(results: list) -> list:
    """同 id 只保留最后一条（可能因重复运行产生旧记录）。"""
    seen: dict = {}
    for r in results:
        seen[r.get("id") or r.get("name") or "?"] = r
    return list(seen.values())


def _show(no_show: bool = False) -> None:
    if not no_show:
        try:
            plt.show()
        except Exception:
            pass


def plot_dashboard(results: list, out: Path) -> Path:
    """整体看板：通过率 / 轮次 / 耗时 / 命中文件 Top。"""
    ok = [r for r in results if r.get("passed")]
    n_ok = len(ok)

    fig, axes = plt.subplots(2, 2, figsize=(14, 9))
    fig.suptitle(
        f"SWE-bench 评测总览  (n={len(results)}  ·  通过 {n_ok}  ·  通过率 {n_ok / max(len(results), 1):.0%})",
        fontsize=16, fontweight="bold",
    )

    # 1) 通过率
    ax = axes[0][0]
    ax.bar(["Passed", "Failed"], [n_ok, len(results) - n_ok],
           color=["#4CAF50", "#F44336"], width=0.5)
    ax.set_title("Passed / Failed (file-level hit proxy)")
    ax.set_ylabel("count")
    for i, v in enumerate([n_ok, len(results) - n_ok]):
        ax.text(i, v + 0.02, str(v), ha="center", fontweight="bold")

    # 2) 轮次
    ax = axes[0][1]
    ids = [_short(r.get("id", "?")) for r in results]
    rounds = [r.get("rounds") or 0 for r in results]
    bars = ax.barh(ids, rounds, color=["#4CAF50" if r.get("passed") else "#F44336" for r in results])
    ax.set_title("Agent rounds per instance")
    ax.set_xlabel("rounds")
    ax.invert_yaxis()
    for b, v in zip(bars, rounds):
        ax.text(v + 0.3, b.get_y() + b.get_height() / 2, str(v), va="center", fontsize=8)

    # 3) 耗时
    ax = axes[1][0]
    elapsed = [round((r.get("elapsed_s") or 0) / 60, 1) for r in results]
    ax.barh(ids, elapsed, color="#2196F3")
    ax.set_title("Elapsed per instance (min)")
    ax.set_xlabel("minutes")
    ax.invert_yaxis()
    for i, v in enumerate(elapsed):
        ax.text(v + 0.05, i, f"{v}", va="center", fontsize=8)

    # 4) 命中文件 Top
    ax = axes[1][1]
    files: Counter = Counter()
    for r in results:
        for f in r.get("hit_files") or []:
            files[f] += 1
    if files:
        top = files.most_common(8)
        ax.barh([f.split("/")[-1] for f, _ in top], [c for _, c in top], color="#FF9800")
        ax.set_title("Gold files hit by model (Top8)")
        ax.set_xlabel("hit count")
        ax.invert_yaxis()
    else:
        ax.text(0.5, 0.5, "no hits recorded", ha="center", va="center")

    fig.tight_layout(rect=[0, 0, 1, 0.95])
    fig.savefig(out, dpi=130, bbox_inches="tight")
    return out


def plot_per_repo(results: list, out: Path) -> Path:
    """按仓库聚合：实例数、通过率。"""
    by_repo: dict = {}
    for r in results:
        repo = r.get("id", "?").split("__")[0] if "__" in r.get("id", "") else r.get("id", "?")
        by_repo.setdefault(repo, []).append(r)

    repos = sorted(by_repo)
    counts = [len(by_repo[r]) for r in repos]
    valid = [sum(1 for x in by_repo[r] if x.get("passed")) for r in repos]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))
    fig.suptitle("按仓库汇总", fontsize=15, fontweight="bold")

    ax1.bar(repos, counts, color="#9E9E9E")
    ax1.set_title("Instance count")
    ax1.set_ylabel("count")

    ax2.bar(repos, valid, color="#4CAF50")
    ax2.set_title("Passed (file-level hit)")
    ax2.set_ylabel("count")

    for ax in (ax1, ax2):
        ax.set_xticks(range(len(repos)))
        ax.set_xticklabels(repos, rotation=30, ha="right", fontsize=9)

    fig.tight_layout()
    fig.savefig(out, dpi=130, bbox_inches="tight")
    return out


def plot_per_instance(results: list, out: Path) -> Path:
    """单条明细：状态 × 轮次 × 耗时 热力表。"""
    ids = [_short(r.get("id", "?")) for r in results]
    rounds = [r.get("rounds") or 0 for r in results]
    elapsed = [r.get("elapsed_s") or 0 for r in results]
    status = ["PASS" if r.get("passed") else "FAIL" for r in results]
    hits = [",".join(f.split("/")[-1] for f in (r.get("hit_files") or [])) or "-" for r in results]
    errs = [("ERR: " + str(r.get("error", ""))[:40]) if r.get("error") else "-" for r in results]
    errs = [_ascii(e) for e in errs]

    fig, ax = plt.subplots(figsize=(13, 0.6 * len(results) + 2))
    ax.axis("off")
    col_labels = ["instance", "status", "rounds", "elapsed_s", "hit_files", "error"]
    table = ax.table(
        cellText=[[i, s, str(r), str(e), h, er] for i, s, r, e, h, er in zip(ids, status, rounds, elapsed, hits, errs)],
        colLabels=col_labels, loc="center", cellLoc="left",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 1.4)
    ax.set_title("逐实例明细", fontsize=15, fontweight="bold")
    fig.tight_layout()
    fig.savefig(out, dpi=130, bbox_inches="tight")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="SWE-bench 结果可视化")
    parser.add_argument("--name", default="swebench", help="结果名（results/{name}.jsonl）")
    parser.add_argument("--no-show", action="store_true", help="只存图不弹窗")
    args = parser.parse_args()

    results = _dedupe(load_results(args.name))
    if not results:
        print(f"no results for '{args.name}'")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary = summarize(results)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    paths = {
        "dashboard": plot_dashboard(results, OUT_DIR / "dashboard.png"),
        "per_repo": plot_per_repo(results, OUT_DIR / "per_repo.png"),
        "per_instance": plot_per_instance(results, OUT_DIR / "per_instance.png"),
    }
    for k, p in paths.items():
        print(f"wrote {p}")

    # 打印一张简易文本表格，方便终端直接看
    print("\n" + "=" * 88)
    print(f"{'instance':<34} {'status':<6} {'rounds':>6} {'elapsed_s':>9}  hit_files")
    print("-" * 88)
    for r in results:
        iid = _short(r.get("id", "?"), 34)
        st = "PASS" if r.get("passed") else "FAIL"
        print(f"{iid:<34} {st:<6} {r.get('rounds') or 0:>6} {r.get('elapsed_s') or 0:>9}  "
              f"{','.join(f.split('/')[-1] for f in (r.get('hit_files') or [])) or '-'}")
    print("=" * 88)

    _show(args.no_show)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
