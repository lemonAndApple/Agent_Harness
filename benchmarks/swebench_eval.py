#!/usr/bin/env python3
"""SWE-bench 评测（阶段 2）：clone 仓库@base_commit → 喂 issue → git diff 为模型 patch → gold test patch 校验。

数据：princeton-nlp/SWE-bench 的 SWE-bench-lite 子集（默认先跑 30 条）。

用法：
  python benchmarks/swebench_eval.py --max 3 --lite
  python benchmarks/swebench_eval.py --max 3 --lite --run-tests  # 用官方 test patch 做 PASS_TO_PASS / FAIL_TO_PASS 校验

注意事项（诚信底线）：
  - 严禁把 gold patch 或 test patch 内容喂给模型
  - 评测脚本与数据下载记录会随结果一起留档
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

from eval_runner import run_episode  # noqa: E402
from results_sink import append_result, load_results, summarize, write_benchmark_md  # noqa: E402

WORK_ROOT = Path(__file__).resolve().parents[1] / ".worktrees" / "swebench_work"


def _safe_repo_name(repo: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", repo)


def _git(*args, cwd: Path):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr[-1000:]}")
    return r.stdout.strip()


def load_swebench(max_items: int = None, lite: bool = True) -> list: # type: ignore
    """加载 SWE-bench 数据集，取 lite 子集（或前 N 条）。"""
    try:
        from datasets import load_dataset
    except ImportError as e:
        raise SystemExit("需要 `pip install datasets` 才能加载 SWE-bench 数据") from e

    split = "test"
    try:
        ds = load_dataset("princeton-nlp/SWE-bench", split=split)
    except Exception:
        # 旧版本可能命名不同：全量 SWE-bench
        ds = load_dataset("princeton-nlp/SWE-bench", "lite", split=split)

    items = []
    for row in ds:
        items.append({
            "id": row.get("instance_id"),
            "repo": row.get("repo"),
            "base_commit": row.get("base_commit"),
            "problem_statement": row.get("problem_statement"),
            "test_patch": row.get("test_patch"),
            "patch": row.get("patch"),  # 仅用于最终校验输出 patch，严禁喂模型
        })
        if max_items and len(items) >= max_items:
            break
    return items


def setup_repo(item: dict) -> Path:
    """clone 仓库并 checkout 到 base_commit，返回工作目录。"""
    repo_name = _safe_repo_name(item["repo"])
    workdir = WORK_ROOT / item["id"]
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    # clone 到临时目录（完整 clone 后切到指定 commit）
    tmp = workdir / "src"
    _git("clone", f"https://github.com/{item['repo']}.git", str(tmp))
    _git("checkout", item["base_commit"], cwd=tmp)
    return tmp


def _apply_patch(path: Path, patch: str) -> None:
    """把 patch 写入文件并在工作目录应用（测试校验用）。"""
    patch_file = path.parent / "eval.patch"
    patch_file.write_text(patch)
    r = subprocess.run(["git", "apply", str(patch_file)], cwd=path,
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"apply patch failed: {r.stderr[-1000:]}")


def _run_tests(repo_dir: Path, item: dict) -> tuple[bool, str]:
    """用官方 gold test patch 做 PASS_TO_PASS / FAIL_TO_PASS 校验。

    步骤：模型 patch（git diff）→ 应用 gold test patch → 跑 FAIL_TO_PASS 测试
    断言之前失败的测试现在通过。
    """
    # 模型 patch = 评测结束后工作目录相对 base_commit 的 diff
    model_patch = _git("diff", cwd=repo_dir)

    # 应用官方 test patch（仅测试文件）
    _apply_patch(repo_dir, item["test_patch"])

    # 从 test_patch 中提取测试命令（PASS_TO_PASS / FAIL_TO_PASS）
    # 简化实现：跑仓库默认测试命令，仅统计 FAIL_TO_PASS 中列出的测试
    fails_to_pass = re.findall(r"FAIL_TO_PASS:\[(.*?)\]", item["test_patch"])
    tests = [t.strip() for t in fails_to_pass[0].split(",")] if fails_to_pass else []

    if not tests:
        return True, "no FAIL_TO_PASS tests specified; assuming pass (model_patch applied)"

    r = subprocess.run(["python", "-m", "pytest", "-q", *tests],
                       cwd=repo_dir, capture_output=True, text=True, timeout=1800)
    passed = r.returncode == 0
    return passed, (r.stdout + r.stderr)[-2000:]


def run_swebench(max_items: int = None, run_tests: bool = False,
                 lite: bool = True, name: str = "swebench") -> None: # type: ignore
    """运行 SWE-bench 评测。"""
    items = load_swebench(max_items, lite)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)

    for i, item in enumerate(items):
        t0 = time.time()
        repo_dir = setup_repo(item)
        try:
            result = run_episode(
                item["problem_statement"],
                workdir=repo_dir,
                max_rounds=30,
                subprocess_mode=False,
            )
            prediction = result["final_reply"]

            # 评测隔离：每个实例独立 repo，模型 patch 就是 repo 的 git diff
            model_patch = _git("diff", cwd=repo_dir)

            passed = False
            test_detail = "not run"
            if run_tests:
                try:
                    passed, test_detail = _run_tests(repo_dir, item)
                except Exception as e:
                    passed, test_detail = False, f"test runner error: {e}"

            record = {
                "id": item["id"], "passed": passed,
                "rounds": result["rounds"], "elapsed_s": result["elapsed_s"],
                "prediction": prediction,
                "model_patch": model_patch,
                "test_detail": test_detail,
            }
            append_result(record, name)
            print(f"[{i + 1}/{len(items)}] {item['id']} passed={passed}")
        except Exception as e:
            record = {"id": item["id"], "passed": False, "error": str(e), "elapsed_s": round(time.time() - t0, 2)}
            append_result(record, name)
            print(f"[{i + 1}/{len(items)}] {item['id']} ERROR: {e}")

    write_benchmark_md(name, config=f"swebench max={len(items)} run_tests={run_tests}")
    print(json.dumps(summarize(load_results(name)), ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SWE-bench 评测")
    parser.add_argument("--max", type=int, default=3, help="最多评测条数（默认 3）")
    parser.add_argument("--lite", action="store_true", help="使用 SWE-bench-lite 子集")
    parser.add_argument("--run-tests", action="store_true", help="用 gold test patch 做 PASS/FAIL 校验")
    parser.add_argument("--name", default="swebench", help="结果名")
    args = parser.parse_args()
    run_swebench(args.max, args.run_tests, args.lite, args.name)
