#!/usr/bin/env python3
"""压测：合成长历史触发 auto_compact，实测压缩前后 token 与耗时（阶段 4）。

对应 docs/eval-stress-roadmap.md 阶段4 的 stress_compact，及
agents/resume.md 里"[待填，长会话输入 token 减少约 ×%]"缺口的填充。

用法：
  python benchmarks/stress_compact.py --target-chars 500000
  python benchmarks/stress_compact.py --target-chars 500000 --no-record
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

import Agent_Harness as A  # noqa: E402
from results_sink import append_result, write_benchmark_md  # noqa: E402


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def _tool_use_block(id_: str, name: str, input_: dict) -> dict:
    return {"type": "tool_use", "id": id_, "name": name, "input": input_}


def _tool_result(id_: str, content: str) -> dict:
    return {"type": "tool_result", "tool_use_id": id_, "content": content}


def _code_dump(n: int) -> str:
    """合成一段"大工具输出"（模拟 read_file / bash 输出的代码清单）。"""
    lines = []
    for i in range(n):
        lines.append(f"def func_{i}(a: int, b: int) -> int:")
        lines.append(f"    \"\"\"Function {i} adds two numbers.\"\"\"")
        lines.append(f"    result = a + b + {i}")
        lines.append(f"    if result > 1000:")
        lines.append(f"        log.debug(f'large result: {{result}}')")
        lines.append(f"    return result")
    return "\n".join(lines)


def synthesize_history(target_chars: int) -> list:
    """构造一条接近真实多轮会话的长历史（含大工具输出），触发 auto_compact。"""
    messages = [
        {"role": "user", "content": "帮我实现一个数据处理管线，包含清洗、聚合、导出。", }
    ]
    chars = 0
    round_i = 0
    while chars < target_chars:
        round_i += 1
        tid = f"tool_{round_i}"
        # assistant: 计划 + 一个工具调用
        messages.append({
            "role": "assistant",
            "content": [
                _text_block(f"第 {round_i} 轮：先读取模块源码，确认现有接口。"),
                _tool_use_block(tid, "read_file", {"path": f"src/module_{round_i % 20}.py", "limit": 500}),
            ],
        })
        # user: 大工具输出回写
        dump = _code_dump(120)
        messages.append({"role": "user", "content": [_tool_result(tid, dump)]})
        # assistant: 简短进展
        messages.append({
            "role": "assistant",
            "content": [_text_block(f"已读取第 {round_i} 个模块，接下来处理清洗逻辑。")],
        })
        chars = len(json.dumps(messages))
    return messages


def run_stress(target_chars: int, record: bool = True) -> dict:
    """跑一轮压缩压测，返回前后 token 与耗时统计。"""
    A.bootstrap()

    messages = synthesize_history(target_chars)
    before_tokens = A.estimate_tokens(messages)
    before_chars = len(json.dumps(messages, default=str))

    t0 = time.time()
    compacted = A.auto_compact(messages)
    elapsed = time.time() - t0

    after_tokens = A.estimate_tokens(compacted)
    after_chars = len(json.dumps(compacted, default=str))
    reduction_pct = (1 - after_tokens / before_tokens) * 100

    result = {
        "name": "stress_compact",
        "before_tokens": before_tokens,
        "after_tokens": after_tokens,
        "before_chars": before_chars,
        "after_chars": after_chars,
        "token_reduction_pct": round(reduction_pct, 2),
        "elapsed_s": round(elapsed, 2),
        "msgs_before": len(messages),
        "msgs_after": len(compacted),
        "model": A.MODEL,
    }
    if record:
        append_result(result, "stress_compact")
        write_benchmark_md("stress_compact", model=A.MODEL,
                           config=f"target_chars={target_chars}")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="auto_compact 压缩压测")
    parser.add_argument("--target-chars", type=int, default=500000,
                        help="合成长历史目标字符数（默认 500000，约触发分块压缩）")
    parser.add_argument("--no-record", action="store_true", help="不写入结果文件")
    args = parser.parse_args()
    run_stress(args.target_chars, record=not args.no_record)
