# LLM Agent 评测全流程技术报告

> 面向**第一次接触"评测 LLM Agent"**的读者：以本仓库（Coding Agent Harness）为例，
> 把你从"如何把 Agent 跑起来并科学地拿到分数"，一路讲到"如何判断这分数是不是真的提升"。
> 对关键名词我给足了通俗解释，读完应能对整个流程有深入、完整的认识。

---

## 目录
1. [什么是"评测一个 Agent"](#1-什么是评测一个-agent)
2. [前置：把交互式 Agent 变成"可评测的程序"](#2-前置把交互式-agent-变成可评测的程序)
3. [项目做了什么评测](#3-项目做了什么评测)
4. [评测一：SWE-bench（真实工程问题修复）](#4-评测一swe-bench真实工程问题修复)
5. [评测二：GAIA（通用问答/推理）](#5-评测二gaia通用问答推理)
6. [评测三：长会话压测 auto_compact](#6-评测三长会话压测-auto_compact)
7. [评测之外：数据合成（从失败里长出新数据）](#7-评测之外数据合成从失败里长出新数据)
8. [工程可信度：让评测"可复现、可自动跑"](#8-工程可信度让评测可复现可自动跑)
9. [两套口径与诚实原则](#9-两套口径与诚实原则)
10. [如何自己复现](#10-如何自己复现)
11. [术语表](#11-术语表)

---

## 1. 什么是"评测一个 Agent"

**一句话**：评测，就是给 Agent 一批"已知标准答案"的题目，让它做，然后**客观地判它对不对**，最后给出一个可比较的分数。

为什么要费劲评测？
- **不评测 = 不知道它到底行不行**。一个 Agent 能"聊得很像那么回事"，不代表它真能把代码改对。
- **没评测 = 无法迭代**。改了一个参数，到底变好了还是变坏了？只能靠分数判断。
- **评测证据 = 可信度**。"我做了个 Agent"没有说服力；"我在 SWE-bench 官方测试上 resolve 了 1/4"才是硬证据。

本报告覆盖的评测维度（由浅入深）：

| 评测 | 考什么 | 怎么判对错 |
|---|---|---|
| SWE-bench | 读真实 GitHub issue，改真实仓库代码 | 跑官方单元测试（FAIL_TO_PASS / PASS_TO_PASS） |
| GAIA | 通用常识/推理问答（非工程） | 精确匹配 + LLM-as-judge |
| auto_compact 压测 | 会话太长时，压缩省多少 token | 前后 token 对比 |
| 数据合成 | 从真实失败案例生成"反例/修正"数据 | 规范 + 质检 + LLM-as-judge 判分 |

**贯穿本报告的一条主线：区分"真的能行"和"看着行"**。我会反复强调两套口径，以及"提升是真还是碰运气"。

---

## 2. 前置：把交互式 Agent 变成"可评测的程序"

Agent 平时是**人机对话**（人在终端里打字，它调工具干活）。评测时没人打字，所以要把它改造成**headless、无交互、可批量、可留痕**的形态。

### 2.1 `bootstrap()` 与 `run_episode()`
原始代码里，Agent 初始化（读记忆、起定时任务、连 MCP、执行会话开始钩子）和主循环都写在 `__main__`。
- 抽出 `bootstrap()`：统一初始化，让"交互式 REPL"和"评测"共用同一套初始化。
- 抽出 `run_episode(prompt)`：把"人给一句话"变成"程序给一句话"，跑完返回 `(对话历史, 最终回答)`。

**`run_episode` 内部就是 Agent 主循环**：
```
调 LLM → 模型返回 tool_use（"我要调用 X 工具，参数是 Y"）
  → 解析 → 权限检查 → 真正执行工具 → 拿到 tool_result
  → 把 tool_result 回填给模型 → 再调 LLM → … 直到模型给出最终回答（end_turn）
```

### 2.2 `eval` 权限模式：免审批
平时 Agent 想 `bash` 会停下来问用户（`PermissionManager.ask_user`）。评测时没人回答，会卡死。
所以加了 **`eval` 模式**：权限检查直接放行（黑名单如 `sudo rm -rf` 仍拒绝，保证不胡跑），headless 运行不阻塞。

> 这其实是安全性的取舍：评测时**能自动跑**，但**危险命令黑名单依旧生效**。

### 2.3 沙箱隔离：`--workdir` 子进程
评测最怕**污染**：一次任务改了 `.env`、写了 `.memory/`、动了 `.tasks/`，下一题就会受影响，甚至把仓库弄脏。
解法：给 `run_episode` 传入 `workdir`，它在**独立临时目录（子进程）**里跑，所有 `.team/.tasks/.memory/` 都建在沙箱内，跑完即弃。

### 2.4 JSONL 会话留痕：每步都可回放
`run_episode` 把每轮完整对话写成**一行一个 JSON** 的 `.jsonl` 文件。这样：
- 失败了能**回放**：看到模型哪一步走错；
- 也是后面"失败复盘"和"数据合成"的原材料。

### 2.5 Mock 客户端：测试不依赖真实 API
单元测试不能每次都花钱调 API。于是用 **mock（假客户端）**：提前预设模型会返回什么，验证主循环、工具分发、权限判定等逻辑是否正确。这样 CI（持续集成）跑测试**不花钱、不联网、可重复**。

---

## 3. 项目做了什么评测

- **结果来源**：模型为 `deepseek-chat`（Anthropic 兼容端点，见 `.env.example`）。
- **四种评测**：SWE-bench、GAIA、auto_compact 压测、数据合成（失败案例 → 合成数据）。
- **全程纪律（诚信底线）**：**严禁把 gold patch / test patch 注入模型输入**（否则评测毫无意义，等于作弊）。
  这是整个报告反复强调的"防泄漏"。

---

## 4. 评测一：SWE-bench（真实工程问题修复）

### 4.1 SWE-bench 是什么
SWE-bench 是当前评测"会写代码的 Agent"最主流的基准。它收集了真实 GitHub 仓库上的 **issue（问题）→ 修复（gold patch）→ 验收测试（test patch）** 三元组。
每一条叫一个 **instance（实例）**，例如 `django__django-10087`。

### 4.2 术语解释（这里最关键）
- **base_commit**：问题发生时的仓库起点。评测要先把仓库 `checkout` 到这个提交。
- **gold patch**：官方给出的**正确答案补丁**（人类修复）。
- **test patch**：官方给出的**验收测试**，用于判断 Agent 是否修对了。
- **FAIL_TO_PASS（F2P）**：修复前必挂、修复后必须通过的测试。**这是核心**——没修好就挂。
- **PASS_TO_PASS（P2P）**：修复前后都必须通过的测试。**防回归**——修 A 却把 B 弄坏就挂。
- **resolved**：F2P **全部**通过且 P2P **全部**通过 → 才算"真正解决"。
- **patch_valid（文件级命中）**：模型 diff **碰触了** gold patch 涉及的**文件**。只能说明"方向对了"，不代表"做对了"（后面详说）。

### 4.3 评测流程（step by step）
```
1. 克隆仓库 @ base_commit
2. 把 issue 文本作为 prompt 喂给 Agent（run_episode）
3. Agent 在沙箱里读代码/改代码/跑命令，最终我们取 git diff 作为"模型 patch"
4a. 近似判定 patch_valid：模型 patch 是否触及 gold 涉及的文件
4b. 官方判定：用官方 swebench harness + 预构建 Docker 镜像，套上 test_patch
       跑 F2P / P2P，得到 resolved / fail / error
5. 写结果 JSONL + BENCHMARK.md
```

> 注意：**4a 不依赖测试环境、省钱**；**4b 才是真判**。本项目**两套都做了，且分开标注**。

### 4.4 受限网络下的工程解法（SWE-bench 联调实录）
评测真实仓库要联网：Docker Hub（拉镜像）、GitHub raw（拉 requirements）。本机网络受限，逐一解决：
- **Docker Hub 不可达** → 探测镜像代理，预拉官方镜像并本地打 tag，让 harness 本地命中、不触发拉取。
- **harness 构造测试规格要 raw.githubusercontent 拉 requirements 会卡死** → monkeypatch 改为从本地 worktree 仓库读取（**只改数据来源，不改判定逻辑**）。
- **FAIL_TO_PASS 双重 JSON 编码导致判定全错** → 归一化数据类型后修复。

### 4.5 结果（deepseek-chat，6 仓库 6 实例）
| instance | 文件级命中 | 官方判定 | F2P / P2P |
|---|---|---|---|
| `psf__requests-1142` | 是 | **RESOLVED（1/4）** | F2P 1/1；P2P 5/5 |
| `django__django-10087` | 是 | ERROR | patch 应用失败（模型补丁缺末尾换行） |
| `pallets__flask-4045` | 是 | FAIL | F2P 0/2；P2P 50/50 |
| `pytest-dev__pytest-10051` | 是 | FAIL | F2P 0/1；P2P 14/15（1 回归） |
| `sphinx-doc__sphinx-10021` | 否 | – | 未产出有效 diff，未提交 |
| `sympy__sympy-11232` | 否 | – | 子进程超时，未产出有效 diff |

**大盘**：文件级命中 4/6 = 66.7%；**官方 resolved 1/4**（提交的 4 条中 requests 真正通过全部 F2P/P2P）；平均耗时 223.9s。

### 4.6 四个失败案例复盘（最有价值的部分）
失败比成功更说明问题——它暴露了自研 Agent 的真实短板：

1. **django：补丁缺末尾换行**（ERROR）。模型改了正确文件，但 diff 格式不合法（无末尾换行），官方 `git apply` 失败。→ 教训：**该加"提交前预检"**（`git apply --check`）。
2. **flask：F2P 0/2**（FAIL）。P2P 50/50 全过（没搞坏），但新增测试没过 → **理解问题不够**，改的方向不对。
3. **pytest：F2P 0/1 + 1 回归**（FAIL）。既没修对，又引入了一个回归 → **"最小改动"与"不破坏既有约束"的平衡没做好**。
4. **sphinx / sympy：无 patch / 超时**。前者只探索没收敛；后者陷入长时间工具循环被超时终止。→ 教训：**应该加"必须产出 patch"的收敛约束 + 最大轮数**。

> 这四类根因被后续"数据合成"直接当作**负例来源**（见第 7 节）。

---

## 5. 评测二：GAIA（通用问答/推理）

### 5.1 GAIA 是什么
GAIA 是**通用问答基准**，考 Agent 的常识、推理、多步检索。比 SWE-bench 简单，适合快速验证工作流。
- **level**：难度分 1/2/3，越高越难。
- **附件**：有些题附带 pdf/mp3/png 等文件（需文件工具）。
- 本项目**只取 level1 纯文本子集**（无附件），避免额外工具依赖。

### 5.2 "gated 数据集" + HF token（通俗解释）
GAIA 数据集在 Hugging Face 上是 **`gated="auto"`**：元数据公开，但**真实数据要授权**才能下。你要：
- 在 HF 页面**同意条款**（Agree and access dataset）；
- 生成一个 **`HF_TOKEN`**（个人访问令牌）；
- 把它写入环境/`.env`（本项目从 `agents/.env` 读取）。

我们实测：官方 `huggingface.co` 不通，走镜像 **`hf-mirror.com`** 可用；但**没有授权 token 时直接 401**（`DatasetNotFoundError`），这就是 `gated` 的含义。

### 5.3 评测流程
```
1. 设 HF_ENDPOINT=镜像 + 提供 HF_TOKEN
2. load_dataset("gaia-benchmark/GAIA", "2023_level1", split="test")
   只保留 level1 且无附件的样本
3. 对每条：把问题喂给 Agent，取它最终回答
4. 双评分：
   - exact（精确匹配）：规范化后与标准答案完全一致（要求极严）
   - judge（LLM 判定）：用模型当裁判，判"语义等价即可"
5. 写 JSONL + BENCHMARK
```

**为什么用双评分？** 精确匹配太苛刻——模型答得"其实对，但措辞不同"就会被判错；但 LLM 当裁判又可能"自身偏乐观"。所以**两套分开看**，不混为一谈。

### 5.4 结果（2023_level1 纯文本，5 条第 4 条完成）
| 指标 | 值 |
|---|---|
| 精确匹配（exact） | **0 / 4** |
| LLM-as-judge 通过 | **3 / 4（75%）** |
| 平均轮次 | 1.25 |
| 平均耗时(s) | 21.65 |

**解读（诚实）**：exact=0 说明没拿到逐字一致的答案（GAIA 极严）；judge 75% 更像"语义对"的真实水平；
但 judge 与候选同源（都是 deepseek），**偏乐观**。第 5 条的问题引导 Agent 探测本机网络/代理配置（systemd-resolved/proxy），
Agent **陷入无界探测被超时中断**——这本身是个真实的失败样本（未收敛）。

---

## 6. 评测三：长会话压测 auto_compact

### 6.1 为什么要压缩上下文
LLM 有上下文上限，且输入越长越贵、越慢。长会话里塞满工具输出，模型很快"忘记前面"或触发超限报错。
所以要有**上下文压缩**。

- **microcompact（轻量）**：把**旧的工具结果**替换成简短标记，不调 LLM，便宜。`read_file` 等关键结果不压缩。
- **auto_compact（重量）**：超长后让 LLM **分块**切段→逐段摘要→合并成一份"续接摘要"，失败则回退原文。
- 配合 **bash 超长输出落盘 + 预览标记**：超过阈值（30k/50k 字符）就写入磁盘、只回传一个「已落盘，共 N 字符」的标记，从源头防溢出。

### 6.2 结果（合成 50.9 万字符长会话）
| 指标 | 值 |
|---|---|
| 压缩前 | 50.9 万字符 / 12.7 万 token（64 条消息） |
| 压缩后 | 2842 字符 / 710 token（1 条续接消息） |
| **token 减少** | **99.4%** |
| 耗时 | 27.2s（7 分块 + 1 合并，共 8 次 LLM 调用） |

> 意义：**几乎不损失关键决策/约束的前提下**，把后续会话成本压到最低。这是工程价值，不是玄学。

---

## 7. 评测之外：数据合成（从失败里长出新数据）

### 7.1 为什么要"合成数据"
给 JD匹配"训练/数据"这条腿，光会评测不够，要会**基于失败**做**数据**。这里把上面 SWE-bench 的**失败案例**变成可复用数据。

### 7.2 流程
1. **负例来源**：官方判定的失败实例（django/flask/pytest/sphinx/sympy）的 **agent 失败 patch + 失败证据**。
2. **`synth_negatives.py`**：让 LLM 分析"错在哪一步"，并给出一个**修正后的 patch**，产出"错误对比对"：
   `(bad patch ⇄ 修正 patch) + error_type（失败类型）`，存 JSONL。
3. **`synth_rubric.py`**：复用 GAIA 的 LLM-as-judge 思路，为每个样本生成 **rubric（判分标准）**并判定 GOOD/BAD。
4. **数据规范与质检**：统一 schema（id / 来源实例 / 错误类型 / reward / revised / verifier 判定），**可追溯**（每行指向具体 instance）、**去重**、**抽样质检**。
5. **评测纪律（防泄漏）**：只用 agent 自己产出的失败 patch；**gold / test patch 零注入**，且每条样本都标记 `gold_patch_used=False`。

### 7.3 结果与警示
- 产出了真实 `synth_negatives.jsonl`（3 条）、`synth_rubric.jsonl`、`synth_rubric_qc.json`。
- **一个诚实的发现**：合成与判定用同一个模型（deepseek），**3 条全判 GOOD、置信≈0.87**——这正是"**同模型自说自话**"偏差的体现。
  所以正确做法是：**人工抽样复核 + 跨模型交叉判定 + 优先用官方测试做最终裁决**，而非只信 LLM 判分。

---

## 8. 工程可信度：让评测"可复现、可自动跑"

评测要可信，工程必须跟得上：
- **`pyproject.toml`**：`ruff`（行宽 110、py311）、`mypy`（ignore_missing_imports、implicit_optional）、`pytest`（testpaths）。
- **`requirements-dev.txt`**：`pytest` / `ruff` / `mypy`（开发依赖，与运行时分离）。
- **CI（`.github/workflows/test.yml`）**：Python 3.11 → 装依赖 → **`ruff check .` → `mypy agents/` → `pytest tests/`** 三件套。
- **测试**：36 个单测（编译冒烟、TodoManager 校验、MCP 端到端、headless 评测 Mock），**用 mock client 不依赖真实 API**。

> 只有 CI 绿+可复现，别人（和未来的你）才能相信你贴的分数。

---

## 9. 两套口径与诚实原则

评测里最容易"自欺"的就是**口径含糊**。本项目刻意区分三套口径，绝不混用：

| 口径 | 含义 | 可信度 |
|---|---|---|
| **文件级命中（patch_valid）** | 模型 diff 碰到了 gold 涉及的文件 | 弱（方向对≠做对） |
| **官方 resolved** | 官方 Docker 跑通全部 F2P/P2P | 强 |
| **合成数据 / judge 通过** | LLM 合成或 LLM 判分 | 中（需防同源偏差） |

**三条诚实底线**：
1. **严禁 gold / test patch 注入**输入或合成数据（否则等于作弊）。
2. **区分"命中"与"resolve"**，分开标注。
3. **提分要证明**是"真提升"（配对+方差、可执行测试），不然就如实写"无提升"——哪怕打脸。

---

## 10. 如何自己复现

```sh
# 依赖
pip install -r requirements.txt -r requirements-dev.txt

# 工程可信度三件套
python -m pytest tests/ -q && ruff check . && mypy agents/

# SWE-bench（patch 生成 → 官方判定 → 沉淀）
bash scripts/reproduce_benchmark.sh --steps all

# GAIA（需已授权 HF_TOKEN + 镜像）
export HF_ENDPOINT=https://hf-mirror.com
python benchmarks/gaia_eval.py --max 5 --judge

# 长会话压缩压测
python benchmarks/stress_compact.py --target-chars 500000

# 数据合成 + 判分
python benchmarks/synth_negatives.py --max 4 --name synth_negatives
python benchmarks/synth_rubric.py --name synth_negatives --out synth_rubric

```
# 数据合成已属"数据准备"，不在本报告复现（见 docs/DATA_PIPELINE.md）
```

原始数据都在 `benchmarks/results/`：SWE-bench 的 `swebench.jsonl`/`swebench_local.json`/`predictions_swebench.json`，
GAIA 的 `gaia.jsonl`/`BENCHMARK_gaia.md`，压测的 `stress_compact.jsonl`，合成的 `synth_*.jsonl`。

---

## 11. 术语表

| 术语 | 通俗解释 |
|---|---|
| Agent / Agent 主循环 | 能自主调用工具解决问题的程序；主循环=「提问→模型→工具→回结果→再提问→…直到答」 |
| tool_use / tool_result | 模型"请求调用工具" / 工具"执行后的结果" |
| headless | 无需人类交互、由脚本驱动、可批量跑的模式 |
| 沙箱（sandbox） | 隔离环境，任务在独立临时目录跑，跑完即弃，不污染主仓库 |
| JSONL | 每行一个 JSON 的文本格式，适合逐条记录会话/结果 |
| SWE-bench | 用真实 GitHub issue 考代码修复的基准 |
| instance | SWE-bench 里的一个题目（某仓库某 issue） |
| base_commit | 问题发生时的代码起点 |
| gold patch | 官方标准答案补丁 |
| test patch | 官方验收测试 |
| F2P / P2P | 修前必挂/修后必须过 / 修前后都须过的测试 |
| resolved | F2P+P2P 全过 = 真正解决 |
| patch_valid | 模型 diff 恰好碰到 gold 涉及文件（弱近似） |
| GAIA | 通用问答/推理基准，分 level1/2/3 |
| gated 数据集 | 需授权/同意条款才能下载的数据集 |
| HF_TOKEN | Hugging Face 个人访问令牌 |
| exact / LLM-as-judge | 精确匹配 / 用 LLM 当裁判判语义等价 |
| prompt / prompt 工程 | 输入给模型的话 / 如何把话说好 |
| auto_compact / microcompact | 重量级(LLM 摘要)/轻量级(替换旧结果) 上下文压缩 |
| 合成数据 / 负例 / 正例 | 用 LLM 造的数据 / 错误样例 / 正确样例 |
| rubric / LLM-as-judge | 判分标准 / 用模型判分 |
| CI（持续集成） | 每次 push 自动跑 lint/类型/测试 |
| mock / Mock 客户端 | 假客户端，提前预设返回，让测试不花钱不联网 |

---

*最后一句总结*：**评测不是打分，而是"用确定性证据，回答『它到底行不行、到底是真变强还是碰运气』"。
这个仓库把这套从"headless 化"到"官方判定"再到"诚实归因"的完整链路做出来了，证据全部可复现、可核验。*
