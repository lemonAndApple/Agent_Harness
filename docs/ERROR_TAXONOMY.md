# 通用跨 Benchmark 错误分类法与映射规则（ERROR_TAXONOMY.md）

> **目标**：把当前 `synth_negatives.py` 里"从失败样本反推 + 按 SWE-bench 判定环节枚举 + 硬编码兜底"的错误类型，
> 升级成一套**任务无关、先验定义、可跨 benchmark 复用、可追溯**的错误分类法。
>
> 定位一句话：**分类是"判据驱动"的蓝本，不是"对着这次失败写死"的清单。**
> 它同时服务于"评测失败归因"和"数据合成"两条链路，并与项目一贯的诚实边界（gold 零注入、可追溯、不虚称）对齐。

---

## 目录
1. [为什么需要这套分类法](#1-为什么需要这套分类法)
2. [一句话主张](#2-一句话主张)
3. [错误分类法：任务无关的 6 个维度](#3-错误分类法任务无关的-6-个维度)
4. [判定接口：错误分类器](#4-判定接口错误分类器)
5. [各 Benchmark 的映射规则](#5-各-benchmark-的映射规则)
6. [产出数据规范（统一 schema）](#6-产出数据规范统一-schema)
7. [如何消费这套分类](#7-如何消费这套分类)
8. [质检与诚实边界](#8-质检与诚实边界)
9. [与现有代码的兼容与迁移](#9-与现有代码的兼容与迁移)
10. [分阶段落地计划](#10-分阶段落地计划)

---

## 1. 为什么需要这套分类法

现状的 `ERROR_TYPES`（`synth_negatives.py:58`）和 `_default_error_type`（`synth_negatives.py:138`）有三个根本缺陷：

| 缺陷 | 现状 | 后果 |
|---|---|---|
| **自底向上** | `ERROR_TYPES` 是从 5 个失败实例反推的 | 换一批失败样本，类型就得重写 |
| **绑定单一任务形态** | 类型大多对应 SWE-bench 的 `git apply` / F2P / P2P / 超时 | GAIA、压测等没有这些环节，对不上 |
| **兜底靠硬编码** | `instance_id → error_type` 写死 | 泛化时贴错标签，样本少不具统计意义 |

**核心教训**：错误分类应像"诊断标准"（如医学的 ICD 编码）——**先定义"错误的本质是什么"，再让每个具体失败去对号入座**；而不是反过来，看着这次得了什么病就定义哪几种病。

---

## 2. 一句话主张

> 先用**任务无关的错误维度**定义分类法（蓝本），再为每个 benchmark 提供**把失败证据映射到这些维度**的**判定接口与规则**。

- **分类法**（黑盒外的"标准"）：6 个与任务无关的维度，永远稳定。
- **判定接口**（黑盒内的"判决"）：一个函数，吃"失败证据"，出维度和置信度。
- **映射规则**（针对 benchmark 的"校准"）：说明每个 benchmark 的失败证据怎么填进判定接口的参数。

---

## 3. 错误分类法：任务无关的 6 个维度

错误类型按**错误发生的层面**划分，与具体任务无关，先验定义如下：

| 维度 label | 含义（错误的本质） | 典型触发条件 | SWE-bench 例子 | GAIA 例子 | 压测例子 |
|---|---|---|---|---|---|
| `intent_misunderstanding` | 对任务目标/需求理解错了 | 改的行为与需求不符 | F2P 0/2（改了但方向错） | 答非所问 | — |
| `retrieval_failure` | 该找到的信息没找到 / 找错 | 检索、读文件、查表缺失 | 定位不到要改的函数 | 查不到答案所需事实 | — |
| `tool_misuse` | 工具用错：参数、接口、权限、频率 | 工具调用错误 | 用错命令参数 | 调用工具不当 | 触发到不该走的路径 |
| `plumbing_error` | 产物格式/契约问题，非逻辑问题 | 输出不符合下游解析 | 补丁缺末尾换行（`git apply` 失败） | 输出无法解析 | 输出未落盘/标记异常 |
| `convergence_failure` | 过程没收敛 / 没产出最终结果 | 探索无成果、空转 | 未产出有效 diff | 无界探测兜圈子 | 未触发压缩 |
| `resource_exhaustion` | 成本/资源超限 | 超时、超预算、超轮数 | 子进程超时 | 超时中断 | 上下文爆掉 |

**设计要点**：

1. **互斥且覆盖**：任一失败应可归入且仅归入一个主维度（必要时允许 `+ secondary`）。
2. **与任务无关**：每个维度的"判据"都不引用具体任务环节，只引用"错误本质"。
3. **优先级**：当某失败同时命中多个维度时，用固定优先级裁决，避免摇摆（见 §4.3）。
4. **保留 `unknown`**：判定器确认不了时不硬猜，回退 `unknown`，并计入"判分器可靠性"指标。

**为什么恰好这 6 个**：它们对应一条失败链的四段——理解（intent）、输入（retrieval）、执行（tool）、产出（plumbing / convergence / resource）。任何 agent 任务都绕不开这四段，因此天然可泛化到其它 benchmark。

---

## 4. 判定接口：错误分类器

统一接口，供所有数据合成/失败归因复用：

```python
# agents/error_classifier.py
class ErrorClassifier:
    ERROR_TYPES = [
        "intent_misunderstanding",
        "retrieval_failure",
        "tool_misuse",
        "plumbing_error",
        "convergence_failure",
        "resource_exhaustion",
        "unknown",
    ]

    PRIORITY = [  # 冲突时的裁决顺序
        "resource_exhaustion",
        "convergence_failure",
        "plumbing_error",
        "tool_misuse",
        "retrieval_failure",
        "intent_misunderstanding",
    ]

    def classify(self, evidence: FailureEvidence) -> Classification:
        ...
```

### 4.1 `FailureEvidence`（失败证据，输入）
结构化的失败消息，与 benchmark 无关：

```python
@dataclass
class FailureEvidence:
    benchmark: str            # "swebench" | "gaia" | "stress" | ...
    task_id: str              # 可追溯来源（如 django__django-10087）
    problem_statement: str    # 题干（不含 gold）
    agent_output: str         # 模型答复 / 最终回复
    agent_patch: str | None   # 代码类才有
    verdict: str              # 判分结果：resolved/fail/error/none 等
    test_detail: str          # 判分细节（F2P/P2P/error 描述）
    rounds: int
    elapsed_s: float
    error: str | None = None  # 基础设施异常（超时/setup 失败）
```

#### 4.1.1 `FailureEvidence` 采集来源对照表

`FailureEvidence` 不是凭空生成的，它是**从现有评测结果 JSONL（`benchmarks/results/{name}.jsonl`）逐字段搬运 + 规范化**出来的，
再加两处"评测结果里没存、但判定需要"的补充输入。**它是分类器与已有评测记录之间的一层薄适配器：不是新数据，而是把散落结果字段改写成统一形状。**

**① 直接对应（1:1 搬运，结果文件里已有）**

| `FailureEvidence` 字段 | 来源（结果 JSONL 字段） | 说明 |
|---|---|---|
| `benchmark` | 文件名 | `swebench` / `gaia` / `stress` |
| `task_id` | `id` | 可追溯来源（如 `django__django-10087`） |
| `agent_output` | `prediction` | 模型最终答复 |
| `agent_patch` | `model_patch` | 代码类才有；GAIA / 压测无 → `None` |
| `verdict` | `passed` / `official_resolved` | 判分结果 |
| `test_detail` | `test_detail` 或 `official_test_detail` | 判分细节（F2P/P2P/error 描述） |
| `rounds` | `rounds` | 执行轮数 |
| `elapsed_s` | `elapsed_s` | 耗时 |
| `error` | 记录里的 `error` 字段 | 基础设施异常（超时 / setup 失败） |

实测各结果文件的既有字段（`benchmarks/results/*.jsonl` 首行 key 佐证）：`swebench.jsonl` 已有
`id / model_patch / official_resolved / official_test_detail / test_detail / prediction / rounds / elapsed_s`；
`gaia.jsonl` 有 `id / passed / passed_exact / prediction / rounds / elapsed_s`；
`stress_compact.jsonl` 有 `token_reduction_pct / before_tokens / after_tokens / elapsed_s`。

**② 需要"补采"的（评测结果里没存）**

| `FailureEvidence` 字段 | 补充来源 | 为什么 |
|---|---|---|
| `problem_statement` | 回查数据集（如 `benchmarks/results/swebench_local.json` 的 `problem_statement`，`synth_negatives.py:90` 即此法） | 结果 JSONL 只存了答案 `prediction`，没存题干 |
| `model`（可选） | `.env` 的 `MODEL_ID` | 记录是哪个模型跑的 |

**③ 采集来源链（从哪一层拿到这些）**

```
评测脚本运行 (swebench_eval / gaia_eval / stress_compact)
   ↓ 逐条写出
benchmarks/results/{name}.jsonl        ← 每行一个 JSON，即"失败证据"主体
   ↓ 组装 FailureEvidence（classify_failures.py）
   读取: benchmark + task_id + prediction + model_patch + verdict + test_detail + rounds + elapsed + error
   补齐: problem_statement（回查数据集） + model（MODEL_ID）
   ↓
ErrorClassifier.classify(evidence)  →  Classification
```

**④ 采集的诚实边界（三条）**

1. **只收失败实例**：与 `synth_negatives._load_instances`（`synth_negatives.py:84`）一致，仅挑 `official_resolved` 非 `True` / 未通过的记录，避免对成功案例做无意义归因。
2. **只读不注入**：`problem_statement` 只用于归因上下文，**绝不把 gold patch / test patch 带进来**——二者在数据源（如 `swebench_local.json` 的 `patch` / `test_patch`）就应隔离，采集时跳过。
3. **字段缺失 → 降级不误判**：GAIA / 压测无 `agent_patch`。规则层对 `agent_patch is None` 走"无 patch"分支或跳过相关规则；缺失字段不影响归类，只是少了对应维度的证据。

### 4.2 `Classification`（输出）
```python
@dataclass
class Classification:
    error_type: str              # ERROR_TYPES 之一
    secondary: list[str]         # 可选副维度
    confidence: float            # 0.0-1.0
    rationale: str               # 判定理由（可追溯）
    source: str                  # "llm" | "rule" | "fallback"
    rules_fired: list[str]       # 命中的规则名（便于审计）
```

### 4.3 判定流程（三层，逐级降级）
```
1. 规则层 rule        ← 确定性、可审计，优先（见 §5 映射规则）
    命中即返回，source="rule"
2. LLM 层 llm         ← 规则没命中，让 LLM 按"6 维度判据"归类
    解析失败或不在 ERROR_TYPES → 进入兜底
3. 兜底 fallback      ← 仍归不了
    返回 ("unknown", confidence=0, source="fallback")，绝不硬猜
```

> 关键：**规则层优先于 LLM**，因为它确定、可复现、可审计——这正是"判分器可靠性"和"抗刷分"想要的东西。LLM 只处理规则覆盖不到的"灰区"。

---

## 5. 各 Benchmark 的映射规则

"映射规则"= 把各 benchmark 的失败证据**翻译成规则层判据**，从而不依赖 LLM 就能稳定归类。每条规则有：`rule_name`、`条件`、`命中维度`。

### 5.1 SWE-bench（代码修复）

| rule_name | 条件（对 FailureEvidence） | 命中维度 |
|---|---|---|
| `swb_apply_fail` | `test_detail` 含 "apply" / "patch apply failed" / "malformed" | `plumbing_error` |
| `swb_f2p_zero` | `test_detail` 含 "FAIL_TO_PASS" 且失败数 ≥ 1 但 P2P 全过 | `intent_misunderstanding` |
| `swb_p2p_regress` | `test_detail` 含 "PASS_TO_PASS" 且失败 ≥ 1（有回归） | `retrieval_failure`（改到不该改的）+ secondary `tool_misuse` |
| `swb_no_patch` | `agent_patch` 为空 / `test_detail` 含 "no patch" | `convergence_failure` |
| `swb_timeout` | `error` 含 "timed out" / elapsed 超阈值 | `resource_exhaustion` |
| `swb_setup_error` | `error` 含 "setup failed" / "clone" | `retrieval_failure`（资源/环境获取失败）→ `resource_exhaustion` |

**裁决说明**：
- `swb_apply_fail` 优先于 `swb_f2p_zero`（格式问题先于逻辑问题）。
- `swb_p2p_regress` 若同时 F2P 也失败，主维度取 `intent_misunderstanding`，`retrieval_failure` 作为 secondary。
- 优先级统一走 `ErrorClassifier.PRIORITY`。

### 5.2 GAIA（问答/推理）

| rule_name | 条件 | 命中维度 |
|---|---|---|
| `gaia_unanswered` | 无最终答复 / 答复为空 | `convergence_failure` |
| `gaia_nonsense` | 答复与问题主题无关（判分 exact/judge 均低） | `intent_misunderstanding` |
| `gaia_missing_evidence` | 需要的事实未检索到（judge 指出缺关键信息） | `retrieval_failure` |
| `gaia_uncollapsed` | 输出超长、探测反复、未收敛 | `convergence_failure` |
| `gaia_api_timeout` | `error` 含 timeout / 请求失败 | `resource_exhaustion` |

### 5.3 压测（stress_compact）

| rule_name | 条件 | 命中维度 |
|---|---|---|
| `stress_not_compacting` | 未触发压缩 / token 未显著减少 | `plumbing_error`（契约未达成） |
| `stress_output_spill` | 压缩后仍超限 / 输出未落盘 | `plumbing_error` |
| `stress_slow` | 耗时超阈值 | `resource_exhaustion` |

> 压测的"失败"少（大多是输入超限、输出未落盘），故以 plumbing / resource 为主；这也证明分类法可覆盖非"改代码"任务。

---

## 6. 产出数据规范（统一 schema）

`synth_negatives.py` 产出的每条 record 统一扩展为：

```python
{
  "id": "synth_neg_<instance后缀>",
  "source_instance": "<instance_id>",          # 可追溯
  "benchmark": "swebench",
  "error_type": "plumbing_error",              # 用新 6 维度
  "secondary": [],
  "confidence": 0.9,
  "rationale": "GitHub issue 补丁缺末尾换行，git apply 失败",
  "classify_source": "rule:swb_apply_fail",    # rule/llm/fallback
  "problem_statement": "...",                  # 不含 gold
  "agent_bad_patch": "...",
  "revised_good_patch": "...",
  "reasoning": "...",
  "reward_neg": 0,
  "reward_pos": 1,
  "verifier": "pending",
  "gold_patch_used": False,                    # 恒为 False
  "model": "...",
  "created_at": "...",
}
```

**兼容性**：`error_type` 的取值从旧的 5 个改为新 6 维度；为兼容旧数据的查询，可在 §9 的迁移层做一次 `legacy → new` 映射。

---

## 7. 如何消费这套分类

1. **失败归因**：`docs/BENCHMARK.md` 的失败复盘表，用新维度列"根因"，比"补丁缺换行"这类表象更通用、可跨 benchmark 对比。
2. **数据合成**：`synth_negatives.py` 的 `_default_error_type` 被 `ErrorClassifier` 替代；LLM 兜底只在规则+LLM 都失败时给 `unknown`。
3. **增强（augment）**：`swebench_eval._augment_context`（swebench_eval.py:189）里读 `error_type` 的地方，天然复用新维度（只需保证字段名不变）。
4. **质检（rubric）**：`synth_rubric.py:117` 读回的 `error_type` 也自动升级。

---

## 8. 质检与诚实边界

沿用 `DATA_PIPELINE.md` 的规则，并强化分类本身的可信度：

- **判分可靠性**：报告 `classification.confidence` 分布、`unknown` 比例、`fallback` 比例；`unknown` / `low-confidence` 样本标记 `manual_review_flag`。
- **抗刷分 / 泄漏**：`gold_patch_used=False` 全量断言；人工抽查是否出现"照抄 gold patch"或"训练集泄漏"特征。
- **同源偏差**：LLM 层仍可能有"自说自话"（合成与判定同模型），故**规则层优先**、并鼓励**跨模型交叉 classify**（`--classify-model` 参数，参照 `synth_rubric.py:96` 的 `--judge-model`）。
- **可追溯**：每条样本 `source_instance` 必填、指向真实失败实例；`classify_source` 记录是 rule / llm / fallback，便于审计。
- **不虚称**：多 benchmark 的分布统计只是"描述"，不当作"提升证据"；提升仍需前后对比 + 官方测试（见 `DATA_PIPELINE.md` §3）。

---

## 9. 与现有代码的兼容与迁移

**新增（不改行为）**：
- 新建 `agents/error_classifier.py`：`ErrorClassifier` / `FailureEvidence` / `Classification` + 各 benchmark 映射规则。
- 新建 `benchmarks/classify_failures.py`：读 `results/*.jsonl` → 走 `ErrorClassifier` → 产出/回填 `error_type`（可独立运行，不侵入现有评测脚本）。

**改造（渐进、可回退）**：
- `synth_negatives.py`：把 `ERROR_TYPES` 常量与 `_default_error_type` 替换为对 `ErrorClassifier` 的调用；保留 `_default_error_type` 作为**迁移期兜底**（标记 deprecate）。
- 其余（`swebench_eval._augment_context` / `synth_rubric`）字段名不变，仅值语义变化。

**映射旧→新**（迁移 README 用）：
```
patch_format_malformed   -> plumbing_error
behavior_unmatched       -> intent_misunderstanding
regression_introduced    -> retrieval_failure (+ secondary tool_misuse)
no_patch_produced        -> convergence_failure
subprocess_timeout       -> resource_exhaustion
```

---

## 10. 分阶段落地计划

| 阶段 | 内容 | 产出 | 验收 | 状态 |
|---|---|---|---|---|
| **P0 分类法定稿** | 本设计文档（6 维度 + 判据） | `docs/ERROR_TAXONOMY.md` | 各维度在 SWE-bench/GAIA/压测上都能举 1 例 | ✅ |
| **P1 error_classifier** | `ErrorClassifier` + 规则层 + LLM 层 + 兜底 | `agents/error_classifier.py` | 单测：规则命中、LLM 灰区、未知兜底逐条覆盖 | ✅ |
| **P2 映射规则落库** | 三 benchmark 的规则表 | 规则表 + 单测 | 每条 `rule_name` 有对应条件与断言 | ✅ |
| **P3 接入 synth** | `synth_negatives` 改走 classifier | 新 `error_type` 的 JSONL | 旧数据可经映射；`gold_patch_used` 恒 False | ✅ |
| **P4 质检与报告** | confidence/unknown/fallback 分布 + 人工抽查 | `classify_failures.py` + 报告 | `unknown` 与 `fallback` 比例可统计 | ✅ |
| **P5 跨 benchmark 对比** | 汇总各 benchmark 失败维度分布 | 汇总表 | 分布用于描述，不虚称提升 | ⬜ 未做 |

> 已落地：`agents/error_classifier.py`（+ `tests/test_error_classifier.py` 25 例）、`benchmarks/classify_failures.py`（规则层 CLI，`--llm`/`--legacy-map` 可选）、`synth_negatives.py` 接入新维度 + `classify_source` 字段。P5 仍为"描述"工具，不做性能提升测量。

---

## 一句话记住

**错误分类要像诊断标准一样先验定义"错误的本质"，再用"规则优先 + LLM 灰区 + 不硬猜"的判定接口，让任何 benchmark 的失败都能对号入座、可追溯、可审计；而分类结果只用于归因与数据，不冒充性能提升。**
