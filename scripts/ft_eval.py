#!/usr/bin/env python3
"""HumanEval pass@1 客观评测：加载任意模型（基座 或 base+LoRA），生成补全并跑官方 test。

用途：作为"微调前后对比"的 out-of-distribution、可跑测试的客观指标。
  - 训练用 codeparrot 真实代码（与 HumanEval 不同分布），评测用 HumanEval（held-out、可执行测试）。
  - pass@1 = 能通过全部官方 test 的比例（贪心解码）。

用法：
  python scripts/ft_eval.py --model Qwen/Qwen2.5-Coder-1.5B --n 20            # 基座
  python scripts/ft_eval.py --model Qwen/Qwen2.5-Coder-1.5B --lora .tmp_ft/out --n 20  # LoRA
"""

from __future__ import annotations

import argparse
import os

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import json
import subprocess
import sys
import tempfile

from datasets import load_dataset
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer


def _stop_cut(completion: str) -> str:
    """在 HumanEval 里，补全应在下一个新函数/类定义处停止。"""
    for marker in ("\nclass ", "\ndef ", "\n#"):
        idx = completion.find(marker)
        if idx != -1:
            return completion[:idx]
    # 控制语句出现即视为超出函数体
    for marker in ("\nif __name__", "\nprint(", "\nassert ", "\nimport ", "\nfrom "):
        idx = completion.find(marker)
        if idx != -1:
            return completion[:idx]
    return completion


def run_problem(code: str, test: str, entry_point: str, timeout: int = 15) -> bool:
    """在子进程里执行 candidate_prompt+completion + test，返回是否全部通过。

    HumanEval 的 test 字段只定义 `def check(candidate):`，需再追加
    `check(<entry_point>)` 才能真正执行断言（否则定义未调用、恒通过）。
    """
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "solve.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(code + "\n" + test + f"\n\ncheck({entry_point})\n")
        try:
            r = subprocess.run([sys.executable, path], capture_output=True, text=True,
                               timeout=timeout, cwd=d, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        except subprocess.TimeoutExpired:
            return False
        return r.returncode == 0


def generate(model, tok, prompt: str, max_new: int = 512, device: str = "cuda") -> str:
    ids = tok(prompt, return_tensors="pt", truncation=True).to(device)
    with torch.no_grad():
        out = model.generate(**ids, max_new_tokens=max_new, do_sample=False,
                             pad_token_id=tok.eos_token_id, repetition_penalty=1.0)
    gen = out[0][ids["input_ids"].shape[1]:]
    return tok.decode(gen, skip_special_tokens=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen2.5-Coder-1.5B")
    ap.add_argument("--lora", default=None, help="可选：加载 LoRA adapter 路径")
    ap.add_argument("--n", type=int, default=20, help="评测条数（从 164 里取前 n，可配 seed 打乱）")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--device", default="cuda:6")
    ap.add_argument("--out", default=None, help="写结果 jsonl 路径")
    ap.add_argument("--max-new", type=int, default=512)
    args = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(args.model)
    base = AutoModelForCausalLM.from_pretrained(args.model, torch_dtype=torch.bfloat16)
    if args.lora:
        from peft import PeftModel
        base = PeftModel.from_pretrained(base, args.lora)
        print(f"[eval] loaded LoRA from {args.lora}")
    model = base.to(args.device).eval()

    ds = load_dataset("openai/openai_humaneval", split="test")
    ds = ds.shuffle(seed=args.seed) if args.seed else ds
    ds = ds.select(range(min(args.n, len(ds))))

    passed = 0
    results = []
    for i, row in enumerate(ds):
        prompt = row["prompt"]
        test = row["test"]
        completion = generate(model, tok, prompt, args.max_new, args.device)
        code = prompt + _stop_cut(completion)
        ok = run_problem(code, test, row["entry_point"])
        results.append({"task_id": row["task_id"], "passed": ok, "completion": completion})
        passed += ok
        print(f"  [{i+1}/{len(ds)}] {row['task_id']} pass={ok}")

    pass_at_1 = passed / max(1, len(ds))
    summary = {"model": args.model, "lora": args.lora, "n": len(ds),
               "passed": passed, "pass_at_1": round(pass_at_1, 4)}
    print(json.dumps(summary))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
