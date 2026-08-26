#!/usr/bin/env bash
# finetune_lora.sh — 小规模 SFT/LoRA 微调（Phase 6 可选）
#
# 作用：把 synth_negatives.jsonl（取 good patch 为正例 / bad patch 综述为收敛指引）整理成
#       (instruction → corrected patch) 指令数据，然后对基础模型做一次小规模 LoRA 微调。
#
# 诚实边界：
#   - 本仓库**未运行**微调（当前环境无 GPU/预算）。此脚本仅作为"可复现入口"。
#   - 必须先用同一评测工具做"微调前 vs 微调后"前后对比再下结论；不调就说没调，绝不虚称。
#
# 依赖（可选）：
#   pip install torch peft transformers datasets accelerate
#   并需一块 GPU；无 GPU 时脚本会自检并给出提示后退出。
#
# 用法：
#   bash scripts/finetune_lora.sh                                  # 自检 + 打印运行计划
#   GPU=1 bash scripts/finetune_lora.sh                            # 真正执行（需 GPU）
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
SYNTH="${1:-benchmarks/results/synth_negatives.jsonl}"
DATA_JSONL=".tmp_synth_instructions.jsonl"

log()  { printf '\033[1;34m[finetune]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[finetune][error]\033[0m %s\n' "$*" >&2; exit 1; }

# 1) 检查模型 / 数据可达
[[ -f "$SYNTH" ]] || die "未找到合成数据 $SYNTH（先跑 benchmarks/synth_negatives.py）"
python - <<PY
import json, sys
rows=[json.loads(l) for l in open("$SYNTH") if l.strip()]
good=[r for r in rows if r.get("revised_good_patch")]
print(f"[finetune] 合成样本 {len(rows)} 条，含 good patch 的 {len(good)} 条（作为 SFT 正例）")
if not good:
    sys.exit("没有可用的 good patch，请先做数据质检")
print(f"[finetune] 示例 error_type 分布:", sorted({r.get('error_type') for r in rows}))
PY

# 2) 环境自检（无 GPU / 缺库则只打印计划，不执行）
command -v nvidia-smi >/dev/null 2>&1 || HAS_GPU=0 || true; HAS_GPU=${HAS_GPU:-0}
python -c "import torch,peft,transformers" 2>/dev/null || HAS_LIBS=0 || true; HAS_LIBS=${HAS_LIBS:-0}
if [[ "${GPU:-0}" != "1" ]] || [[ "$HAS_GPU" == "0" ]] || [[ "$HAS_LIBS" == "0" ]]; then
  log "当前环境未检测到可用 GPU 或未装 torch/peft/transformers，跳过实际微调。"
  log "以下为运行计划（条件满足后执行）："
  log "  1. pip install torch peft transformers datasets accelerate"
  log "  2. 用下面的转换逻辑把 $SYNTH 整理成 $DATA_JSONL"
  log "  3. 对基础模型（如 deepseek-chat 的开源对齐版）做 LoRA（r=8, alpha=16, target=all-linear）"
  log "  4. 用 benchmarks/swebench_eval.py 对同一子集做'微调前 vs 微调后'对比，报告 Δ"
  log "注：本仓库明确未运行微调，仅交付数据构建 + 评测流程 + 可复现入口。"
  exit 0
fi

# 3) 整理指令数据（有 GPU 时才执行到这里）
python - <<PY
import json
rows=[json.loads(l) for l in open("$SYNTH") if l.strip()]
out=[]
for r in rows:
    gp=(r.get("revised_good_patch") or "").strip()
    if not gp: continue
    out.append({
        "instruction": "Given the following issue, produce a correct patch.\n\n" + r.get("problem_statement",""),
        "patch": gp,
        "source_instance": r.get("source_instance"),
        "error_type": r.get("error_type"),
    })
with open("$DATA_JSONL","w") as f:
    for o in out:
        f.write(json.dumps(o, ensure_ascii=False)+"\n")
print(f"[finetune] 已写出指令数据 $DATA_JSONL ({len(out)} 条)")
PY

log "GPU 就绪，可在此处接入 peft 训练脚本（本仓库未内置训练代码）。"
log "训练后务必用 benchmarks/swebench_eval.py 做前后对比，切勿跳过评测。"
