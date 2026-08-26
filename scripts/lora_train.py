#!/usr/bin/env python3
"""小规模 LoRA 微调：用真实代码语料（codeparrot）对 Qwen2.5-Coder-1.5B 做补全式 SFT。

数据集：scripts 外层生成的 /tmp/opencode/ft/train.jsonl，字段 {"text": code}。
损失：标准 causal LM loss（补全式）。
评估：http 端不跑，用 scripts/ft_eval.py 对 base vs lora 做 HumanEval pass@1 前后对比。

用法：
  /home/yuanxiaohu/anaconda3/envs/pytorch/bin/python scripts/lora_train.py \
      --train /tmp/opencode/ft/train.jsonl --out /tmp/opencode/ft/out --epochs 2 --device cuda:6
"""

from __future__ import annotations

import argparse
import json
import os

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import torch
from datasets import Dataset
from peft import LoraConfig, TaskType, get_peft_model
from transformers import (AutoModelForCausalLM, AutoTokenizer, TrainingArguments,
                          Trainer)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", default="/tmp/opencode/ft/train.jsonl")
    ap.add_argument("--base", default="Qwen/Qwen2.5-Coder-1.5B")
    ap.add_argument("--out", default="/tmp/opencode/ft/out")
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--max-len", type=int, default=512)
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--r", type=int, default=8)
    ap.add_argument("--alpha", type=int, default=16)
    ap.add_argument("--device", default="cuda:6")
    args = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(args.base)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    texts = [json.loads(line)["text"] for line in open(args.train, encoding="utf-8") if line.strip()]
    print(f"[train] 加载 {len(texts)} 条代码文本，max_len={args.max_len}")

    def tokenize(text: str):
        ids = tok(text, truncation=True, max_length=args.max_len,
                  return_attention_mask=True, return_tensors="pt")
        ids["labels"] = ids["input_ids"].clone()
        return {k: v[0] for k, v in ids.items()}

    # Dataset.from_dict 组装，避免为张量化每条都建临时对象
    data = {"input_ids": [], "attention_mask": [], "labels": []}
    for t in texts:
        enc = tokenize(t)
        data["input_ids"].append(enc["input_ids"])
        data["attention_mask"].append(enc["attention_mask"])
        data["labels"].append(enc["labels"])
    ds = Dataset.from_dict(data)
    print("[train] tokenize 完成")
    ds = ds.train_test_split(test_size=200, seed=0)
    train_ds, eval_ds = ds["train"], ds["test"]

    base = AutoModelForCausalLM.from_pretrained(args.base, torch_dtype=torch.bfloat16)
    cfg = LoraConfig(
        task_type=TaskType.CAUSAL_LM, r=args.r, lora_alpha=args.alpha,
        target_modules="all-linear", lora_dropout=0.05, bias="none",
    )
    model = get_peft_model(base, cfg)
    model.print_trainable_parameters()
    model = model.to(args.device)

    targs = TrainingArguments(
        output_dir=args.out, num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch, per_device_eval_batch_size=args.batch,
        gradient_accumulation_steps=2, learning_rate=args.lr, weight_decay=0.01,
        bf16=True, logging_steps=20, eval_strategy="epoch", save_strategy="epoch",
        lr_scheduler_type="cosine", warmup_ratio=0.05,
    )
    def pad_collate(batch):
        import torch as _t
        ids = [feature["input_ids"] for feature in batch]
        mask = [feature["attention_mask"] for feature in batch]
        labels = [feature["labels"] for feature in batch]
        maxlen = max(len(x) for x in ids)
        pad = tok.pad_token_id or tok.eos_token_id
        def _pad(seq, p, target):
            return _t.cat([seq, _t.full((target - len(seq),), p, dtype=seq.dtype)])
        idx = _t.stack([_pad(_t.tensor(x), pad, maxlen) for x in ids])
        atn = _t.stack([_pad(_t.tensor(x), 0, maxlen) for x in mask])
        lab = _t.stack([_pad(_t.tensor(x), -100, maxlen) for x in labels])
        return {"input_ids": idx, "attention_mask": atn, "labels": lab}

    trainer = Trainer(model=model, args=targs, train_dataset=train_ds, eval_dataset=eval_ds,
                      tokenizer=tok, data_collator=pad_collate)

    print("[train] 开始训练 ...")
    trainer.train()
    model.save_pretrained(args.out)
    tok.save_pretrained(args.out)
    print(f"[train] 完成，LoRA 保存至 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
