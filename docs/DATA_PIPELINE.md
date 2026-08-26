# 数据流水线（DATA_PIPELINE.md）

> **目标**：为本项目补齐 JD 里"数据 / 评测"相关的能力——基于真实评测失败案例做
> **数据合成 → 质检 → 迭代验证** 的闭环，并把"诚实边界"讲清楚。
>
> 定位一句话：**数据不是拍脑袋编的，而是从 SWE-bench 官方判定的失败案例里反推出来的；
> 我们明确区分"已做 / 未做"，绝不虚称数据有效或提升。**

---

## 0. 一张图看懂全链路

```
SWE-bench 失败实例（官方判定 FAIL）
        │  (agent 失败 patch + 失败证据，gold patch 零注入)
        ▼
任务1  synth_negatives.py ──► 构造错误对比对 (bad patch ⇄ 修正后的 good patch)
        │                        + error_type 分类
        ▼
任务2  synth_rubric.py    ──► 生成 rubric + LLM-as-judge 判定 (GOOD/BAD) + 质控统计
        │
        ▼
质量检查（schema 校验 / 去重 / 可追溯 / 抽样质检）
        │
        ▼
迭代验证（基线 vs 增广提示 前后对比，报告 Δ + 方差）        ── 已设计，待预算运行
```

---

## 1. 数据规范（统一 schema）

合成样本统一使用如下字段（`results/synth_negatives.jsonl` 的每行）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | str | 样本唯一 id（`synth_neg_<instance 后缀>`） |
| `source_instance` | str | **可追溯来源**：对应 SWE-bench 实例 id（如 `django__django-10087`） |
| `error_type` | str | 错误类型（`patch_format_malformed` / `behavior_unmatched` / `regression_introduced` / `no_patch_produced` / `subprocess_timeout`） |
| `problem_statement` | str | 原始 issue 文本（喂给模型的输入，**不含 gold patch**） |
| `agent_bad_patch` | str | **负例**：Agent 真实产出的失败 patch |
| `revised_good_patch` | str | **正例**：LLM 修正后的 patch（合成，非 gold） |
| `reasoning` | str | 失败根因分析 |
| `reward_neg` / `reward_pos` | int | 正/反例 reward（0 / 1），供偏好学习/DPO 使用 |
| `verifier` | str | 判分器判定（`pending` / GOOD / BAD），由任务2 回填 |
| `gold_patch_used` | bool | **恒为 False**：明确记录"未使用 gold patch" |
| `created_at` | str | 时间戳 |

**三条硬性红线**：
1. **零 gold / test patch 注入**：`swebench_local.json` 里的 `patch`（gold）与 `test_patch`
   从不写进 prompt 或输出；只在工程里读取 `problem_statement`。
2. **可追溯**：每条样本必须指向一个真实失败实例 `source_instance`，杜绝"无中生有"。
3. **正例是"合成"而非"gold"**：同步在 `docs/BENCHMARK.md` 口径里区分
   `file-level hit` / `official resolved` / `synthetic positive`，三套口径分开列。

---

## 2. 质检规则

`synth_rubric.py` 输出 `results/synth_rubric_qc.json`，含以下质控维度：

- **判分可靠性**：`avg_confidence`、`good/bad 比例`；`low_confidence_sample_ids`
  （置信度 < 0.7 的样本需人工复查）。
- **抗刷分 / 数据泄漏检查**：`gold_patch_used=False` 全量断言；人工抽查样本中是否出现
  "模型照抄 gold patch"或"训练集泄漏"特征，若出现则该样本弃用并记为泄漏。
- **去重**：按 `id` + `source_instance` 去重；同一实例的正例若与已有样本高度相似，仅保留分数最高的一条。

**已知偏差（如实标注）**：当前合成与判定用的是**同一个模型**（deepseek-chat），
存在"自说自话"乐观偏差——本次实测 3 条全部判 GOOD、avg_confidence≈0.87，正是该偏差的体现。
因此：**judge 不应只相信同模型**，建议（a）人工抽样复核；（b）用另一个更强的模型做交叉 judge；
（c）用官方测试（SWE-bench 的 PASS_TO_PASS/FAIL_TO_PASS）做最终裁决，而非纯 LLM 判分。

---

## 3. 迭代验证方法（"合成数据是否真的有用"）

这是证明"数据不是摆设"的关键，**必须做前后对比并报告方差**，不做就明确说"未测"。

**方法**（用已有评测工具）：
- **基线**：`swebench_eval.py` 对某一小集合跑 `--ids <集合>`（不注入合成负例）。
- **增强**：在**同一集合**上，把合成负例（`error_type → 失败样例 → 修正思路`）注入系统提示，
  再跑一遍。记录同一集合的 Δ 通过率 / 文件级命中率变化。
- **防偶然波动**：小样本用**多次重复**（如 n≥3）报告均值与方差/std，区分"真实提升"与"运气"。

```sh
# 基线（某 2-3 个实例）
python benchmarks/swebench_eval.py --ids django__django-10087,pallets__flask-4045 --name baseline_subset
# 增强（同一批实例，注入合成负例提示——需在 eval 侧加 --augment from synth_negatives.jsonl）
python benchmarks/swebench_eval.py --ids django__django-10087,pallets__flask-4045 \
    --augment benchmarks/results/synth_negatives.jsonl --name augmented_subset
```

> **现状**：本轮的对比表**尚未跑**（真实前后对比需多次重复跑 SWE-bench，预算/时间受限）。
> 为避免"虚假提升"，这里**只给方法与命令，不填伪造的数字**；拿到预算后补跑并回填 Δ 表。

---

## 4. 复现

```sh
# 任务1：从失败实例构造错误对比对
python benchmarks/synth_negatives.py --max 4 --name synth_negatives
# 任务2：生成 rubric + LLM-as-judge 判定 + 质控
python benchmarks/synth_rubric.py --name synth_negatives --out synth_rubric
```

产物：`benchmarks/results/synth_negatives.jsonl`、`synth_rubric.jsonl`、`synth_rubric_qc.json`。
这些文件均已纳入版本控制（数据可追溯），但**绝不包含任何 gold patch**。
