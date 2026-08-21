#!/usr/bin/env python3
"""评测入口：把"人工驱动的交互式 REPL"重构为"可编程的一次性会话"（阶段 1 · 无头评测）。

设计要点（对应 docs/eval-stress-roadmap.md 阶段 1）：
  - bootstrap() / run_episode() 复用 Agent_Harness 的初始化与主循环
  - 全局状态隔离：每个 episode 前 reset_runtime_state()，防跨任务污染
  - 评测免审批：eval 权限模式，ask_user 直接放行，无头不阻塞等待输入
  - 沙箱隔离：--workdir 指定独立 temp 目录/worktree，不污染主仓库
  - 会话记录：transcript JSONL 写入磁盘，失败可回放调试

用法：
  python agents/eval_runner.py "列出当前目录"
  python agents/eval_runner.py "列出当前目录" --workdir /tmp/eval_001 --transcript /tmp/ep.jsonl
  python agents/eval_runner.py "完成 x" --max-rounds 20

成功时向 stdout 打印 JSON：{"final_reply": "...", "transcript": "...", "rounds": N}
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import Agent_Harness as A  # noqa: E402


def run_episode(prompt: str, workdir: Path = None, transcript: Path = None, # type: ignore
                max_rounds: int = None, subprocess_mode: bool = True) -> dict: # type: ignore
    """运行一次评测 episode，返回结果字典。

    workdir 非 None 且 subprocess_mode=True 时，在当前进程外再起一个子进程
    并在指定工作目录内运行，实现严格的沙箱隔离（不污染主仓库全局状态）。
    """
    if workdir is not None and subprocess_mode:
        return _run_episode_subprocess(prompt, Path(workdir), transcript, max_rounds)

    t0 = time.time()
    if transcript is None:
        transcript = A.TRANSCRIPT_DIR / f"eval_{int(time.time())}_{os.getpid()}.jsonl"
    history, final_reply = A.run_episode(
        prompt,
        transcript_path=Path(transcript),
        eval_mode=True,
        max_rounds=max_rounds,
    )
    # 统计轮数：数 user 消息（含初始 prompt 与每轮 tool_result 回写）
    rounds = sum(1 for m in history if m.get("role") == "user")
    return {
        "final_reply": final_reply,
        "transcript": str(transcript),
        "rounds": rounds,
        "elapsed_s": round(time.time() - t0, 2),
    }


def _run_episode_subprocess(prompt: str, workdir: Path,
                            transcript: Path, max_rounds: int) -> dict: # type: ignore
    """沙箱隔离：在独立工作目录内启动子进程运行 episode。

    子进程继承 .env 环境变量（API key / base url），cwd 指向沙箱目录，
    .tasks / .team / .memory 等都创建在沙箱内，评测完即丢弃。
    """
    workdir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(Path(__file__).resolve()),
        prompt,
    ]
    if transcript is not None:
        cmd += ["--transcript", str(Path(transcript).resolve())]
    if max_rounds is not None:
        cmd += ["--max-rounds", str(max_rounds)]
    cmd += ["--subprocess-child"]

    env = dict(os.environ)
    r = subprocess.run(cmd, cwd=str(workdir), env=env,
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(
            f"eval subprocess failed (rc={r.returncode}): {r.stderr[-2000:]}"
        )
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as e:
        raise RuntimeError(f"cannot parse eval output: {e}\nstdout={r.stdout[-2000:]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="无头评测入口（阶段1）")
    parser.add_argument("prompt", nargs="?", help="要执行的评测任务指令")
    parser.add_argument("--workdir", type=Path, default=None, help="沙箱工作目录（默认当前目录）")
    parser.add_argument("--transcript", type=Path, default=None, help="会话记录 JSONL 路径")
    parser.add_argument("--max-rounds", type=int, default=None, help="工具执行轮数上限")
    parser.add_argument("--in-process", action="store_true", help="强制当前进程内运行（不沙箱）")
    parser.add_argument("--subprocess-child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.prompt is None:
        parser.print_help()
        return 2

    # 子进程模式：只执行一次 episode，向 stdout 打一行 JSON
    if args.subprocess_child:
        result = run_episode(
            args.prompt,
            workdir=None, # type: ignore
            transcript=args.transcript,
            max_rounds=args.max_rounds,
            subprocess_mode=False,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0

    # 父进程模式：初始化一次（与 REPL 同源），再跑 episode
    A.bootstrap()
    result = run_episode(
        args.prompt,
        workdir=args.workdir,
        transcript=args.transcript,
        max_rounds=args.max_rounds,
        subprocess_mode=not args.in_process,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
