#!/usr/bin/env python3
"""官方 SWE-bench harness 本地化运行包装。

背景：swebench 4.x 在构造 TestSpec 时会从 raw.githubusercontent.com 拉取
requirements.txt / environment.yml（本机对 GitHub raw 网络不通，会卡死）。
但我们用预构建镜像（--namespace swebench，env 脚本不会真正执行），
因此这里把 get_requirements_by_commit / get_environment_yml_by_commit 打补丁，
改为优先从本地已克隆的工作树仓库读取，其余逻辑完全保持官方不变。

用法（参数与官方 harness 完全一致）：
  python benchmarks/run_swebench_official.py -d benchmarks/results/swebench_local.json \
      -p benchmarks/results/predictions_swebench.json -i <ids...> --namespace swebench \
      --cache_level instance -id <run_id> --report_dir ...
"""

from __future__ import annotations

import functools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# repo -> 本地工作树目录（swebench_work/<instance_id>/src）
REPO_TO_WORKTREE = {
    "django/django": "django__django-10087",
    "pallets/flask": "pallets__flask-4045",
    "pytest-dev/pytest": "pytest-dev__pytest-10051",
    "psf/requests": "psf__requests-1142",
}
WORK_ROOT = Path(__file__).resolve().parents[1] / ".worktrees" / "swebench_work"


def _local_file(repo: str, commit: str, req_path: str):
    wid = REPO_TO_WORKTREE.get(repo)
    if not wid:
        return None
    base = WORK_ROOT / wid / "src"
    if not (base / ".git").exists():
        return None
    f = base / req_path
    return f.read_text(encoding="utf-8", errors="ignore") if f.exists() else None


def _install_local_requirements_patch():
    from swebench.harness.test_spec import python as py_mod
    from swebench.harness.constants import MAP_REPO_TO_REQS_PATHS

    orig_reqs = py_mod.get_requirements_by_commit
    orig_yml = py_mod.get_environment_yml_by_commit

    @functools.lru_cache(maxsize=None)
    def get_requirements_by_commit_local(repo: str, commit: str) -> str:
        for req_path in MAP_REPO_TO_REQS_PATHS[repo]:
            text = _local_file(repo, commit, req_path)
            if text is None:
                continue
            # 递归展开 "-r xxx.txt" 引用
            req_dir = "/".join(req_path.split("/")[:-1])
            out = []
            for line in text.split("\n"):
                if line.strip().startswith("-r"):
                    sub = line.split("-r", 1)[1].strip()
                    sub_text = _local_file(repo, commit, f"{req_dir}/{sub}")
                    out.append(sub_text if sub_text is not None else line)
                else:
                    out.append(line)
            return "\n".join(out)
        # 本地缺失 → 退回官方网络实现
        return orig_reqs(repo, commit)

    @functools.lru_cache(maxsize=None)
    def get_environment_yml_by_commit_local(repo: str, commit: str, env_name: str) -> str:
        yml = _local_file(repo, commit, "environment.yml")
        if yml is not None:
            return yml
        return orig_yml(repo, commit, env_name)

    py_mod.get_requirements_by_commit = get_requirements_by_commit_local
    py_mod.get_environment_yml_by_commit = get_environment_yml_by_commit_local
    print("[localpatch] get_requirements_by_commit / get_environment_yml_by_commit -> local worktree")


def main() -> int:
    _install_local_requirements_patch()
    import runpy

    # 以 __main__ 方式执行官方 harness 模块，使其 argparse 消费透传参数
    sys.argv = ["run_evaluation"] + sys.argv[1:]
    runpy.run_module("swebench.harness.run_evaluation", run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
