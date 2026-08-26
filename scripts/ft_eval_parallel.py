#!/usr/bin/env python3
"""HumanEval pass@1 并行评估：把问题分片，跨多块 GPU 各载一份模型并行评测。

相比单卡，这能在不同 seed 下（base vs lora）快速产出完整方差带。
用法：
  /home/yuanxiaohu/anaconda3/envs/pytorch/bin/python scripts/ft_eval_parallel.py \
      --model Qwen/Qwen2.5-Coder-1.5B --n 25 --seeds 0 1 2 3 \
      --gpus 2 3 5 6 7 --out /tmp/opencode/ft
  # 对比 lora 时额外 --lora /tmp/opencode/ft/out
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import multiprocessing as mp

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from datasets import load_dataset  # noqa: E402

_BASE = "Qwen/Qwen2.5-Coder-1.5B"


# ---------- 子进程内执行测试（安全隔离，跑模型产出的代码） ----------
def _run_code(code: str, entry_point: str, timeout: int = 15) -> bool:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "solve.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(code + "\n" + entry_point + "\n")  # entry_point 含 test + check()
        try:
            r = subprocess.run([sys.executable, path], capture_output=True, text=True,
                               timeout=timeout, cwd=d,
                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        except subprocess.TimeoutExpired:
            return False
        return r.returncode == 0


def _stop_cut(completion: str) -> str:
    for marker in ("\nclass ", "\ndef ", "\n#"):
        idx = completion.find(marker)
        if idx != -1:
            return completion[:idx]
    for marker in ("\nif __name__", "\nprint(", "\nassert ", "\nimport ", "\nfrom "):
        idx = completion.find(marker)
        if idx != -1:
            return completion[:idx]
    return completion


# ---------- 每个 GPU 一个 worker 进程 ----------
def _eval_worker(gpu, model, lora, tasks, outfile):
    os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu)
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(model)
    m = AutoModelForCausalLM.from_pretrained(model, torch_dtype=torch.bfloat16)
    if lora:
        from peft import PeftModel
        m = PeftModel.from_pretrained(m, lora)
    m = m.to("cuda:0").eval()
    pad = tok.eos_token_id or tok.pad_token_id

    with open(outfile, "w", encoding="utf-8") as f:
        for t in tasks:
            prompt = t["prompt"]
            ids = tok(prompt, return_tensors="pt", truncation=True).to("cuda:0")
            with torch.no_grad():
                out = m.generate(**ids, max_new_tokens=512, do_sample=False,
                                 pad_token_id=pad, repetition_penalty=1.0)
            gen = out[0][ids["input_ids"].shape[1]:]
            completion = tok.decode(gen, skip_special_tokens=True)
            code = prompt + _stop_cut(completion)
            ok = _run_code(code + "\n" + t["test"], f"check({t['entry_point']})")
            f.write(json.dumps({"task_id": t["task_id"], "passed": ok}) + "\n")


def _evaluate_once(model, lora, tasks, gpus, outdir, tag):
    """用 len(gpus) 个 worker 并行评估这批任务，返回 (passed, total)。"""
    shards = [[] for _ in gpus]
    for i, t in enumerate(tasks):
        shards[i % len(gpus)].append(t)
    procs = []
    for gpu, shard in zip(gpus, shards):
        if not shard:
            continue
        outfile = os.path.join(outdir, f"{tag}_gpu{gpu}.jsonl")
        p = mp.Process(target=_eval_worker, args=(gpu, model, lora, shard, outfile))
        p.start()
        procs.append(p)
    for p in procs:
        p.join()

    passed = 0
    total = 0
    for gpu in gpus:
        outfile = os.path.join(outdir, f"{tag}_gpu{gpu}.jsonl")
        if not os.path.exists(outfile):
            continue
        for line in open(outfile, encoding="utf-8"):
            if line.strip():
                total += 1
                passed += json.loads(line)["passed"]
        os.unlink(outfile)
    return passed, total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=_BASE)
    ap.add_argument("--lora", default=None)
    ap.add_argument("--n", type=int, default=25)
    ap.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2, 3])
    ap.add_argument("--gpus", type=int, nargs="+", default=[2, 3, 5, 6, 7])
    ap.add_argument("--out", default="/tmp/opencode/ft")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    ds = load_dataset("openai/openai_humaneval", split="test")
    rows = []
    for seed in args.seeds:
        sub = ds.shuffle(seed=seed).select(range(min(args.n, len(ds))))
        passed, total = _evaluate_once(
            args.model, args.lora,
            [{"task_id": r["task_id"], "prompt": r["prompt"], "test": r["test"],
              "entry_point": r["entry_point"]} for r in sub],
            args.gpus, args.out, tag=f"{args.model.split('/')[-1]}_s{seed}",
        )
        rows.append({"seed": seed, "passed": passed, "total": total,
                     "pass_at_1": round(passed / total, 4)})
        print(f"  seed {seed}: pass={passed}/{total} pass@1={passed/total:.3f}")

    import statistics as st
    pass1 = [r["pass_at_1"] for r in rows]
    summary = {
        "model": args.model, "lora": args.lora, "n": args.n, "seeds": args.seeds,
        "mean_pass_at_1": round(st.mean(pass1), 4),
        "std_pass_at_1": round(st.pstdev(pass1), 4),
        "runs": rows,
    }
    print(json.dumps(summary))
    with open(os.path.join(args.out, f"{args.model.split('/')[-1]}{'_lora' if args.lora else ''}.summary.json"), "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    mp.set_start_method("spawn")
    raise SystemExit(main())
