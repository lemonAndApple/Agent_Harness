# 设计文档（DESIGN.md）

> 本文正面回答"这个项目与 Claude Code 是什么关系"，并系统记录关键设计取舍。
> 定位一句话：**用 Python 从零实现一个端到端 LLM 编程智能体 + 多 Agent 协作平台，
> 并用 SWE-bench 官方 harness 做了真实可复现的验证——与 Claude Code 是
> "概念自实现 + 独立验证闭环"的关系，而不是它的复刻或依赖。**

---

## 1. 与 Claude Code 的关系

这个问题在面试里一定会被问，这里给出明确口径：

- **学习对象，不是复刻**。Claude Code（Anthropic 的官方 Agent）对外展示的行为窗口：
  agent 主循环（`调用 LLM → 解析 tool_use → 执行工具 → 回写 tool_result`）、
  工具化权限门（bash 需要审批、黑名单）、待办清单（Todos）、技能（Skills）、
  长上下文压缩、多 Agent 协作。本项目把这些**公开可见的交互模式**当作"要理解的哲学"，
  从零在 Python 里独立实现，代码完全自主编写。
- **不依赖其任何内部实现**。没有 import claude-code 的代码、没有逆向其私有协议，
  只用 Anthropic 公开 SDK（甚至可以切换到 DeepSeek/GLM/Kimi/OpenAI 的 Anthropic 兼容端点）。
  因此本项目可以被任何 LLM 驱动，Claude Code 只是"最著名的那个参照系"。
- **有独立的验证闭环**。这是最关键的区别：本项目不只"能跑"，还用自己的代码
  （`benchmarks/swebench_eval.py` + `run_swebench_official.py` + 官方 Docker harness）
  在 **SWE-bench** 上做了真实测试判定，并用 `auto_compact` 压测、GAIA 评估补齐了
  "Agent 产物是否真的解决问题"的证据链。Claude Code 是闭源商业产品，不存在可复现的
  测评分身；本项目把"如何科学评测一个 Agent"这套能力相对刻意地在工程上放大。

**一句话给面试官**：

> 我观察 Claude Code 的交互行为，自己在 Python 里实现了一个同构的 Agent 系统，
> 重点不是为了变成它，而是为了验证我理解它们的核心机制，并独立地证明这套机制
> 在真实 benchmark（SWE-bench 官方判定）上是有效的。

---

## 2. 系统架构总览

```
        ┌──────────── REPL 交互入口 ────────────┐
        │   agent_loop()  /  run_episode()      │
        └────────────────┬──────────────────────┘
                         │ messages（对话历史）
        ┌────────────────▼──────────────────────┐
        │   主循环：调 LLM → 解析 tool_use      │
        │      → 权限检查 → 执行 → 回写结果      │
        └──┬────────────┬────────────┬─────────┘
           │            │            │
   ┌───────▼─────┐ ┌────┴─────┐ ┌────▼─────┐
   │ 工具分发表   │ │ 权限管道  │ │ 上下文压缩│
   │ TOOLS/      │ │ Permission│ │ microcompact
   │ TOOL_HANDLERS│ │ Manager  │ │ /auto_compact
   └──┬──────────┘ └──────────┘ └───────────┘
      │
   ┌──┴───────────────────────────────────────┐
   │ 工具实现：bash/文件/检索/任务/队友/记忆/   │
   │ worktree/子代理/MCP(经 mcp__ 前缀路由)    │
   └──────────────────────────────────────────┘
```

模块划分：主循环（`agent_loop`）、工具分发表（`TOOL_HANDLERS`/`TOOLS`）、
安全（`BashSecurityValidator` + `PermissionManager`）、钩子（`HookManager`）、
记忆（`MemoryManager`）、任务（`TaskManager` + `TodoManager`）、
团队（`MessageBus` + `TeammateManager`）、调度（`BackgroundManager` + `CronScheduler`）、
隔离（`WorktreeManager` + `EventBus`）、MCP（`mcp_plugin.py`）。

---

## 3. 关键设计取舍

每个取舍都按"动机 / 怎么做 / 取舍代价 / 如何验证"来写，便于面试逐条展开。

### 3.1 统一权限门（Unified Permission Gate）

- **动机**：Agent 最危险的不是"模型不会写代码"，而是"模型会执行它不该执行的命令"。
  需要一个不依赖模型自觉的、确定性的安全闸门。
- **怎么做**：把权限检查抽成**唯一的** `PermissionManager`，所有工具在真正执行前都过这道门：
  1. 静态黑名单（`sudo`、`rm -rf` 等）直接 `deny`；
  2. 规则表按工具名/输入匹配（白/黑名单）；
  3. 命中"需确认"则走 `ask_user`（REPL 里问用户 / 评测模式 `eval` 直接放行）。
  三种模式：`plan`（只读）、`build`（默认，危险操作需确认）、`eval`（评测免审批）。
- **为什么要统一**：如果每个工具各写各的校验，很容易漏检（这是 Agent 框架最常见的漏洞）。
  所有工具（含外部 MCP 工具）走同一条门，意味着"模型调 `mcp__db__insert_note`"
  和调 `bash rm -rf /` 得到同样的对待——外部工具默认挂"只读 + 需审批"，无法绕过。
- **代价**：统一门意味着要维护一份工具权限元数据；部分工具（如只读查询）会显得"过度检查"。
- **验证**：单测 `test_eval_runner.py` 的 `test_eval_mode_auto_allow` /
  `test_eval_mode_deny_rules_still_enforced` 直接断言 `sudo rm -rf /` 在 `eval` 下仍被 `deny`。

### 3.2 全量 TodoWrite（session-scoped to-do）

- **动机**：长会话里模型容易"忘记走到哪一步"，尤其当上下文被压缩后。需要一个持久但轻量的任务清单。
- **怎么做**：`TodoManager` 维护**会话内**的待办清单，模型可用 `TodoWrite` 工具随时增删改，
  超过一段时间（KEEP_RECENT 窗口）未更新的清单会被系统反复提醒，防止模型"推进但没记录"。
- **取舍**：是"会话内内存清单"而非"磁盘任务板"——因为 Todo 是短期执行计划，丢了可以重推；
  真正需要长期持久化、带依赖关系的任务才用 `TaskManager`（磁盘任务板）。分层避免"过度设计"。
- **验证**：`docs/eval-stress-roadmap.md` 阶段1 验收 + 主循环内对 Todo 的注入测试。

### 3.3 子进程沙箱（Subprocess Sandbox for eval）

- **动机**：评测 Agent 时最怕污染：一次 episode 改了 `.env`、写了 `.memory/`、动了 `.tasks/`，
  会污染下一次评测，甚至把仓库搞脏。评测必须可重复、可隔离。
- **怎么做**：`eval_runner.run_episode` 当给到 `--workdir` 时，不在当前进程跑，而是
  **起一个子进程**（`_run_episode_subprocess`），把 cwd 指到独立临时目录/worktree；
  所有 `.team/.tasks/.memory/.transcripts/` 都建在沙箱内，跑完即弃。
  `--subprocess-child` + stdout 打一行 JSON 实现父/子进程通信（继承 `.env` 的 API key）。
- **代价**：子进程无法共享内存中的全局单例，所以每次都要重新 `bootstrap()`；好处是彻底隔离，
  且进程崩溃/超时不拖垮评测驱动脚本。另一个代价是评测只能拿到 stdout JSON，无法做进程内断言。
- **验证**：测试 `test_eval_runner_result_shape` 与 `test_run_episode_*` 用 mock client 跑通闭环；
  真实 SWE-bench 评测时每个实例都在独立目录重建，避免相互污染。

### 3.4 MCP 前缀路由（`mcp__{server}__{tool}`）

- **动机**：要接入外部能力（SQLite 查询、文档检索…）而不破坏"统一工具池 + 统一权限門"的抽象。
- **怎么做**：外部 MCP 服务器经 `MCPToolRouter` 把工具名映射为 `mcp__{server}__{tool}`，
  与原生工具共存于同一个 `TOOLS` 分发表；调用时剥前缀路由回对应服务器，再统一走权限与结果规范化。
  插件机制（`.claude-plugin/plugin.json`）负责自动发现并拉起服务器（stdio 传输，JSON-RPC over stdio）。
- **取舍**：名字前缀是"约定"，不是"命名空间隔离"。代价是工具名变长、需避免前缀冲突；
  收益是模型/权限/持久化全部复用，不必为外部工具另写一套执行+审批逻辑。
- **验证**：`test_mcp_integration.py` 对 echo/db 两个自写服务器做端到端测试（`mcp__echo__add` 等）。

### 3.5 会话回放（JSONL transcript replay）

- **动机**：一次评测失败，若只看到最终判定（NO），没法知道"模型哪一步走错"。
  这既是调试手段，也是做**失败复盘**（fail-cases analysis）的原材料——恰恰是评测岗位看重的"分析能力"。
- **怎么做**：`run_episode` 每次把完整 `messages` 写成一行一个 JSON 的 `.transcripts/` 文件
  （或用户指定的 `--transcript`），记录 role / content / tool_use / tool_result，失败后可重放。
- **价值**：SWE-bench 的失败案例复盘（见 `docs/BENCHMARK.md`）正是从这个 transcript 里定性分析
  "patch 缺换行 / F2P 没过 / 子进程超时"等失败模式，而不是只报一个数字。
- **代价**：落盘开销 + 隐私（需要 redact 掉绝对路径，仓库已做权限裁剪）。

---

## 4. 其他值得说明的设计

- **两级上下文压缩**（`microcompact` / `auto_compact`）：轻量级只把旧工具结果替换成简短标记；
  重量的用 LLM 分块智能摘要（按 `CHUNK_CHARS` 切块 + 合并），失败回退原文。
  配合 **bash 超长输出落盘 + 预览标记**（30k/50k 阈值），从源头防止上下文被工具输出撑爆。
- **文件消息总线（at-most-once）**：跨 Agent 通信用文件邮箱而非 Redis/MQ——零基础设施、落盘可审计、
  同目录可扩展跨进程；已知边界是"处理中崩溃会丢消息"（at-most-once），改进路径是消费确认 + 重放日志。
- **全局单例重置**：MEMORY / BUS / TASK_MGR / BG / CRON 是进程级单例，`reset_runtime_state()` 每次
  episode 重建，避免跨任务状态污染（这一点在评测里是硬要求，否则结果不可信）。
- **诚实边界**：不把"文件级命中"说成"resolved"。SWE-bench 里模型 diff 触碰了 gold patch 涉及文件
  （file-level hit），和真正通过官方 FAIL_TO_PASS/PASS_TO_PASS（resolved）是两回事，
  本项目在 README / design / benchmark 三处都分开口径、分开标注。

---

## 5. 与主流框架（LangGraph / LangChain）的对比

本项目刻意**不依赖**任何编排框架（核心主循环只依赖标准库 + Anthropic SDK），原因：

- 框架把"图 / 状态 / 工具"作为一等公民，抽象高、上手快；但也意味着"模型怎么被调度"
  这类本质问题被框架封装掉了，调试时要钻进框架源码才看得清。
- 自研主循环把 `调用 → 解析 → 执行 → 回写` 摊在明处，每一步可打日志、可单步、可跑无头评测，
  这正好服务于本项目"把 Agent 的机制讲清楚 + 用 benchmark 证明有效"的目标。
- 代价：工具/权限/压缩/多 Agent 都要自己造，工程量集中在系统集成而非算法。
  反向对比时我会说明：在复杂工作流编排、可观测性、社区生态上，LangGraph 等框架明显更强，
  本项目更适合"理解机制 / 教学 / 轻量自托管"场景——这也回答了"为什么不用现成框架"。

---

## 6. 如何复现与验证

```sh
# 单元测试 + lint + 类型
python -m pytest tests/ -q
ruff check .
mypy agents/

# SWE-bench 官方判定（预构建 Docker 镜像 + swebench harness）
python benchmarks/swebench_eval.py --ids psf__requests-1142,pallets__flask-4045
python benchmarks/run_swebench_official.py -d benchmarks/results/swebench_local.json \
    -p benchmarks/results/predictions_swebench.json -i <ids...>

# 长会话压缩压测
python benchmarks/stress_compact.py --target-chars 500000

# GAIA（level1 纯文本子集，精确匹配 + LLM-as-judge）
python benchmarks/gaia_eval.py --max 5 --judge
```

更完整的数据与失败复盘见`docs/BENCHMARK.md`。
