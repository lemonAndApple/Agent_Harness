#!/usr/bin/env bash
# reproduce_benchmark.sh — 一键复现 SWE-bench 评测（Phase 4 加分项）
#
# 作用：走通"克隆仓库@base_commit → 喂 issue 给 Agent → git diff 为模型 patch →
#       官方 harness（Docker+swebench）判定 FAIL_TO_PASS/PASS_TO_PASS → 沉淀 BENCHMARK" 全链路。
#
# 前置：
#   - 已 `pip install -r requirements.txt -r requirements-dev.txt`
#   - `.env` 有可用的 `ANTHROPIC_API_KEY`（Anthropic 兼容端点，如 DeepSeek）
#   - 国内网络：`HF_ENDPOINT=https://hf-mirror.com`（脚本默认设置，可覆盖）；git clone 自动镜像
#   - 官方判定一步需 Docker + `pip install swebench`（本机已具备）
#
# 用法：
#   bash scripts/reproduce_benchmark.sh                                    # 全部步骤
#   bash scripts/reproduce_benchmark.sh --steps patch                      # 只出模型 patch
#   bash scripts/reproduce_benchmark.sh --steps official --skip-patch      # 复用已缓存 patch，只跑官方判定
#   bash scripts/reproduce_benchmark.sh --ids psf__requests-1142,pallets__flask-4045
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# 示例实例：6 个仓库（与 README/结果口径一致）
DEFAULT_IDS="psf__requests-1142,django__django-10087,pallets__flask-4045,pytest-dev__pytest-10051,sphinx-doc__sphinx-10021,sympy__sympy-11232"

# 两个可运行"run 到官方判定"的请求依赖 cached 本地数据集；sphinx/sympy 未产出有效 diff 故可仅跑 patch
REPORT_DIR="benchmarks/results/swebench_reports"
RUN_ID="reproduce_$(date +%Y%m%d_%H%M%S)"

# 国内网络默认值（可用环境变量覆盖）
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export GIT_MIRROR="${GIT_MIRROR:-}"

log()  { printf '\033[1;34m[reproduce]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[reproduce][warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[reproduce][error]\033[0m %s\n' "$*" >&2; exit 1; }

check_prereqs() {
  command -v python >/dev/null 2>&1 || die "需要 python"
  [[ -f .env ]] || warn "未找到 .env（模型评测需要 ANTHROPIC_API_KEY）"
  if [[ ! -s .env ]] && [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    warn "未检测到 ANTHROPIC_API_KEY，patch 生成步骤会失败；可先 cp .env.example .env 并填入 key"
  fi
  python -c "import datasets,swebench" 2>/dev/null || warn "建议 pip install datasets swebench（官方判定需要 swebench）"
  command -v docker >/dev/null 2>&1 || warn "未安装 Docker，官方判定步骤会失败（--steps official 需要）"
}

step_patch() {
  local ids="$1"
  log "步骤1：生成模型 patch（clone→issue→agent→git diff）"
  log "      实例: $ids"
  python benchmarks/swebench_eval.py --ids "$ids" --name swebench
}

step_official() {
  local ids="$1" name="$2" skip_patch="$3"
  local data_file="benchmarks/results/swebench_local.json"
  local pred_file="benchmarks/results/predictions_swebench.json"
  mkdir -p "$(dirname "$data_file")" "$REPORT_DIR"

  if [[ "$skip_patch" != "1" ]]; then
    # 由本次生成的 preds 落地为官方 harness 所需 predictions 文件
    log "步骤2：把 agent 产出 patch 写入 predictions_${name}.json"
    python - "$name" <<'PY'
import json, sys
name = sys.argv[1]
recs = [json.loads(l) for l in open(f"benchmarks/results/{name}.jsonl") if l.strip()]
out = []
for r in recs:
    pid = r.get("id")
    patch = r.get("model_patch", r.get("patch", ""))
    out.append({"instance_id": pid, "model_patch": patch or "", "model_name_or_path": "self-harness"})
json.dump(out, open(f"benchmarks/results/predictions_{name}.json", "w"), ensure_ascii=False, indent=2)
print(f"  wrote predictions_{name}.json ({len(out)} predictions)")
PY
    pred_file="benchmarks/results/predictions_${name}.json"
  fi

  log "步骤3：官方 harness 判定（Docker + swebench，跑 FAIL_TO_PASS/PASS_TO_PASS）"
  log "      run_id=$RUN_ID  data=$data_file  preds=$pred_file  ids=$ids"
  python benchmarks/run_swebench_official.py \
    -d "$data_file" -p "$pred_file" -i ${ids//,/ } \
    --max_workers "${MAX_WORKERS:-2}" -t "${TIMEOUT:-1800}" -n swebench \
    --cache_level "${CACHE_LEVEL:-instance}" -id "$RUN_ID" --report_dir "$REPORT_DIR"
}

step_report() {
  local name="$2"
  log "步骤4：沉淀 BENCHMARK_${name}.md + 可视化"
  python benchmarks/results_sink.py --name "$name" --model "${MODEL_NAME:-deepseek-chat}" \
    --config "reproduce run_id=$RUN_ID"
  python benchmarks/visualize.py --name "$name" --no-show || warn "可视化生成失败（缺 matplotlib?）"
}

STEP="all"; IDS="$DEFAULT_IDS"; NAME="swebench"; SKIP_PATCH="0"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --steps) STEP="$2"; shift 2;;
    --ids)   IDS="$2"; shift 2;;
    --name)  NAME="$2"; shift 2;;
    --skip-patch) SKIP_PATCH="1"; shift;;
    --max-workers) MAX_WORKERS="$2"; shift 2;;
    --timeout) TIMEOUT="$2"; shift 2;;
    --cache-level) CACHE_LEVEL="$2"; shift 2;;
    -h|--help) sed -n '1,30p' "$0"; exit 0;;
    *) die "未知参数: $1（用 -h 看用法）";;
  esac
done

check_prereqs
case "$STEP" in
  patch)    step_patch "$IDS";;
  official) step_official "$IDS" "$NAME" "$SKIP_PATCH";;
  report)   step_report "$IDS" "$NAME";;
  all)      step_patch "$IDS" && step_official "$IDS" "$NAME" "$SKIP_PATCH" && step_report "$IDS" "$NAME";;
  *) die "--steps 只支持 patch|official|report|all";;
esac

log "完成。结果见 benchmarks/results/（BENCHMARK_${NAME}.md、JSONL、可视化、swebench_reports/）"
