#!/usr/bin/env python3
"""SWE-bench 评测（阶段 2）：clone 仓库@base_commit → 喂 issue → git diff 为模型 patch → gold test patch 校验。

数据：princeton-nlp/SWE-bench 的 SWE-bench-lite 子集（默认先跑 30 条）。

用法：
  python benchmarks/swebench_eval.py --max 3 --lite
  python benchmarks/swebench_eval.py --max 3 --lite --run-tests  # 用官方 test patch 做 PASS_TO_PASS / FAIL_TO_PASS 校验

注意事项（诚信底线）：
  - 严禁把 gold patch 或 test patch 内容注入模型输入
  - 评测脚本与数据下载记录会随结果一起留档
"""

from __future__ import annotations

import argparse
import json
import os
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
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr[-1000:]}")
    return r.stdout.strip()


def _github_clone(repo: str, target: Path, cwd: Path) -> None:
    """clone GitHub 仓库，直连失败时自动回退到国内加速镜像（GIT_MIRROR 可覆盖）。"""
    url = f"https://github.com/{repo}.git"
    mirror = os.environ.get("GIT_MIRROR")
    candidates = [url] + ([mirror + "/" + url] if mirror else ["https://ghfast.top/" + url])
    for i, cand in enumerate(candidates):
        r = subprocess.run(["git", "clone", cand, str(target)], cwd=cwd,
                           capture_output=True, text=True, timeout=900)
        if r.returncode == 0:
            if i > 0:
                print(f"[swebench] cloned via mirror: {cand}")
            return
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
    raise RuntimeError(f"git clone {url} failed via all endpoints")


def load_swebench(max_items: int = None, lite: bool = True, ids: list = None) -> list: # type: ignore
    """加载 SWE-bench 数据集，取 lite 子集（或前 N 条）。

    直连 huggingface.co 不可达时可用镜像：HF_ENDPOINT=https://hf-mirror.com
    （脚本内若已设置环境变量则优先，否则尝试默认端点）。
    """
    try:
        from datasets import load_dataset
    except ImportError as e:
        raise SystemExit("需要 `pip install datasets` 才能加载 SWE-bench 数据") from e

    # 若默认端点不可达，回退到国内镜像端点
    if not os.environ.get("HF_ENDPOINT"):
        import urllib.request
        try:
            urllib.request.urlopen("https://huggingface.co", timeout=5)
        except Exception:
            os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

    split = "test"
    ds = load_dataset("princeton-nlp/SWE-bench", split=split)
    if lite:
        # SWE-bench-Lite = 精选 300 条子集，可从数据集中按 instance_id 筛选；
        # 此处简化为取 test split 前 max_items 条（Lite 本身在 default 中无独立 config）
        pass

    items = []
    for row in ds:
        iid = row.get("instance_id")
        if ids and iid not in ids:
            continue
        items.append({
            "id": iid,
            "repo": row.get("repo"),
            "base_commit": row.get("base_commit"),
            "problem_statement": row.get("problem_statement"),
            "test_patch": row.get("test_patch"),
            "patch": row.get("patch"),  # 仅用于最终校验输出 patch，严禁喂模型
            "FAIL_TO_PASS": row.get("FAIL_TO_PASS") or [],
            "PASS_TO_PASS": row.get("PASS_TO_PASS") or [],
        })
        if max_items and len(items) >= max_items:
            break
    return items


def setup_repo(item: dict) -> Path:
    """clone 仓库并 checkout 到 base_commit，返回工作目录。"""
    workdir = WORK_ROOT / item["id"]
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    # clone 到临时目录（完整 clone 后切到指定 commit）
    tmp = workdir / "src"
    _github_clone(item["repo"], tmp, workdir)
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
    """用数据集自带的 FAIL_TO_PASS / PASS_TO_PASS 做官方式校验。

    步骤：模型 patch（git diff，且剔除对测试文件的改动）→ 还原测试文件到
    base_commit → 应用官方 gold test_patch → 运行 FAIL_TO_PASS 测试，
    全部通过即 resolve；再跑 PASS_TO_PASS 确认无回归。

    注意：模型 patch 为空（如 API 失败）时直接判失败，绝不"假定通过"。
    """
    # 模型 patch = 评测结束后工作目录相对 base_commit 的 diff
    model_patch = _git("diff", cwd=repo_dir)
    if not model_patch.strip():
        return False, "model_patch is empty (agent produced no changes); cannot pass"

    # 提取模型改动中涉及的文件路径，剔除测试文件（模型不应改测试）
    model_touched = [
        m.group(1) for m in re.finditer(r"^\+\+\+ b/([^\t\n]+)", model_patch, re.MULTILINE)
    ]
    test_files = [
        f for f in model_touched if "test" in f.lower()
    ]
    if test_files:
        # 模型改了测试文件：还原为 base_commit 版本，保证 gold test_patch 能干净应用
        for f in test_files:
            _git("checkout", "--", f, cwd=repo_dir)
        model_patch = _git("diff", cwd=repo_dir)

    # 应用官方 gold test patch（仅测试文件）
    try:
        _apply_patch(repo_dir, item["test_patch"])
    except RuntimeError as e:
        return False, f"apply test_patch failed: {e}"

    fail_to_pass = item.get("FAIL_TO_PASS") or []
    pass_to_pass = item.get("PASS_TO_PASS") or []
    if not fail_to_pass:
        return False, "dataset has no FAIL_TO_PASS tests; cannot verify (not assumed pass)"

    def _run(sel: list, label: str) -> tuple[bool, str]:
        if not sel:
            return True, f"{label}: (none)"
        r = subprocess.run(["python", "-m", "pytest", "-q", *sel],
                           cwd=repo_dir, capture_output=True, text=True, timeout=1800)
        ok = r.returncode == 0
        return ok, f"{label}: {'PASS' if ok else 'FAIL'} {r.returncode}"

    f2p_ok, f2p_detail = _run(fail_to_pass, "FAIL_TO_PASS")
    p2p_ok, p2p_detail = _run(pass_to_pass, "PASS_TO_PASS")

    passed = f2p_ok and p2p_ok
    return passed, f"{f2p_detail} | {p2p_detail} | tail={_git('log', '-1', '--format=%h', cwd=repo_dir)}"


def _augment_context(path: str = None) -> str:
    """从合成负例（synth_negatives.jsonl）构造"需避免的失败模式"上下文。

    仅使用合成样本里的 error_type + reasoning + 失败 patch 摘要，绝不包含 gold patch。
    若未提供 path 或文件不可读，返回空串（即不增强）。
    """
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        print(f"[swebench][augment] 未找到 {path}，跳过增强")
        return ""
    tips = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        et = r.get("error_type", "unknown")
        rz = r.get("reasoning", "")
        tips.append(f"- error_type={et}: {rz[:240]}")
    if not tips:
        return ""
    head = (
        "The following are KNOWN failure modes observed on similar problems. "
        "Avoid repeating them:\n" + "\n".join(tips)
    )
    return head


def run_swebench(max_items: int = None, run_tests: bool = False,
                 lite: bool = True, name: str = "swebench", ids: list = None,
                 augment: str = None) -> None: # type: ignore
    """运行 SWE-bench 评测。"""
    items = load_swebench(max_items, lite, ids)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    augment_ctx = _augment_context(augment)
    if augment_ctx:
        print("[swebench] 已启用合成负例增强（baseline => augmented 对比）")

    for i, item in enumerate(items):
        t0 = time.time()
        try:
            repo_dir = setup_repo(item)
        except Exception as e:
            # setup 失败（如 clone 超时/镜像不可达）也要留档并继续，不能中断整批
            record = {"id": item["id"], "passed": False,
                      "error": f"setup failed: {e}", "elapsed_s": round(time.time() - t0, 2)}
            append_result(record, name)
            print(f"[{i + 1}/{len(items)}] {item['id']} SETUP ERROR: {e}")
            continue
        try:
            prompt = item["problem_statement"]
            if augment_ctx:
                prompt = augment_ctx + "\n\n# Task\n" + prompt
            result = run_episode(
                prompt,
                workdir=repo_dir,
                max_rounds=30,
                subprocess_mode=True,
            )
            prediction = result["final_reply"]

            # 评测隔离：每个实例独立 repo，模型 patch 就是 repo 的 git diff
            model_patch = _git("diff", cwd=repo_dir)

            # 判定：真实测试（Docker 环境需 --run-tests 或官方 harness，不启用则如实记为未测）
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
    parser.add_argument("--ids", default="", help="逗号分隔的 instance_id 白名单（如 --ids a__a-1,b__b-2）")
    parser.add_argument("--augment", default=None, help="注入合成负例（synth_negatives.jsonl）做前后对比")
    args = parser.parse_args()
    ids = [x.strip() for x in args.ids.split(",") if x.strip()] if args.ids else None
    run_swebench(args.max, args.run_tests, args.lite, args.name, ids, args.augment)
