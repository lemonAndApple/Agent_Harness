# Coding Agent Harness

基于 Python 从零实现的端到端 LLM 编程智能体（AI 编程助手）。它运行经典的 Agent 主循环——调用模型、执行工具、回写结果——并扩展到完整的多 Agent 协作平台：持久化任务板、定时调度、Git Worktree 任务隔离、MCP 外部工具接入。

模型负责推理，Harness 为模型提供一个安全、可控的工作环境。

## 核心能力

- **Agent 主循环**：`调用 LLM → 解析 tool_use → 执行工具 → 回写 tool_result`，循环直至模型给出最终回答
- **37 个工具**，由 JSON Schema 注册分发表驱动：bash、文件读/写/精确编辑、正则检索、子代理委派、会话内待办跟踪、持久化任务、后台任务、Cron 定时调度、团队消息
- **上下文管理**：每轮轻量压缩 + 超 10 万 token 触发 LLM 分块智能摘要；超大工具输出自动写入磁盘并替换为预览标记
- **多 Agent 团队**：文件邮箱消息传递、自主任务认领（互斥锁保护）、定向 / 广播消息、计划审批与关闭协议
- **安全体系**：路径沙箱、危险命令扫描、黑白名单 + 询问用户的权限管道（`plan` / `build` / `eval` 三种模式）
- **持久化**：跨会话记忆、带依赖关系的文件任务板、JSONL 消息总线、工作树事件日志
- **MCP 接入**：外部 MCP 服务器工具合并进统一工具池，复用同一权限门
- **Git Worktree 隔离**：按任务创建并行工作线，事件日志全程可审计
- **健壮容错**：指数退避重试（含随机抖动）、max_tokens 续写、输入超限自动压缩
- **无头评测**：`bootstrap()` / `run_episode()` 将交互式 REPL 封装为可编程的一次性会话，`eval` 权限模式免审批，沙箱隔离 + JSONL 会话记录，配套 GAIA / SWE-bench 评测框架

## 架构

| 关注点 | 实现 |
|---|---|
| 主循环 | `agent_loop()` — 权限检查、Pre/Post 钩子、错误恢复、异步通知 |
| 工具 | `TOOL_HANDLERS` / `TOOLS` 注册分发表 + JSON Schema 定义 |
| 权限 | `PermissionManager` + `BashSecurityValidator` |
| 钩子 | `HookManager`（`PreToolUse` / `PostToolUse` / `SessionStart`） |
| 记忆 | `MemoryManager`（user / feedback / project / reference） |
| 任务 | `TaskManager`（持久化任务板）+ `TodoManager`（会话内清单） |
| 团队 | `MessageBus` + `TeammateManager`（生命周期、自主认领） |
| 调度 | `BackgroundManager` + `CronScheduler` |
| 隔离 | `WorktreeManager` + `EventBus` |
| MCP | `agents/mcp_plugin.py` — `MCPClient` / `PluginLoader` / `MCPToolRouter` |
| 评测 | `agents/eval_runner.py` — 无头 `run_episode` / 沙箱子进程 / 会话记录 |

## 快速开始

```sh
pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`：

```env
ANTHROPIC_API_KEY=sk-ant-xxx   # 必填，或使用兼容端点
MODEL_ID=claude-sonnet-4-6     # 必填，也可填兼容模型
# ANTHROPIC_BASE_URL=...       # 可选：代理 / 内网 / 第三方兼容服务
```

运行：

```sh
python agents/Agent_Harness.py
```

出现 `agent >>` 提示符后直接输入任务，例如：

```
agent >> 搜索仓库里所有 TODO 注释，汇总到 todo_list.md
```

### 权限模式

运行期可用 `/mode <mode>` 切换：

- `plan` — 只读模式
- `build` — 正常读写，危险操作（bash、写入等）需用户确认（默认）
- `eval` — 评测模式，免审批直接放行（无头评测自动启用）

## 目录结构

```
agents/
  Agent_Harness.py      # 完整 Harness：主循环、工具、权限、钩子、
                        # 记忆、任务、团队、Cron、后台任务、Worktree
  eval_runner.py        # 无头评测入口：run_episode / 沙箱子进程 / JSONL 会话记录
  mcp_plugin.py         # MCP 客户端 / 插件发现 / 工具路由
  README.md             # 能力与工具详细说明
benchmarks/
  gaia_eval.py          # GAIA 评测（精确匹配 + LLM-as-judge 双评分）
  swebench_eval.py      # SWE-bench 评测（clone@base_commit → patch → test 校验）
  stress_compact.py     # 长会话压缩压测（auto_compact 前后 token 对比）
  results_sink.py       # 结果沉淀（JSONL + BENCHMARK.md 汇总）
  visualize.py          # 结果可视化（dashboard / per_repo / per_instance 图表）
  results/              # 评测结果留档（JSONL + BENCHMARK.md + 可视化图表）
examples/mcp/
  echo_server.py        # 极简 stdio MCP 服务器（echo/add/upper）
  db_server.py          # 基于 SQLite 的 MCP 服务器（只读 SQL、建表语句、笔记写入）
  .claude-plugin/       # 插件清单，接入上述 MCP 服务器
tests/
  test_agents_smoke.py            # 全部 agent 模块的编译冒烟测试
  test_mcp_integration.py         # MCP 客户端 / 路由器 / 插件的端到端测试
  test_eval_runner.py             # 无头评测（Mock 客户端）单元测试
```

## 示例

仓库内置两个可直接接入 Harness 的 MCP 示例服务器：

- `echo_server.py` — 极简 stdio MCP 服务器，实现 `initialize` / `tools/list` / `tools/call`
- `db_server.py` — 基于 SQLite 的 MCP 服务器，暴露 `list_tables`、`get_schema`、`query_db`（强制只读 `SELECT`）、`insert_note`；首次运行自动创建示例数据库

接入方式：把 `examples/mcp/.claude-plugin/plugin.json` 复制到工作目录，启动 Harness 后即可通过 `mcp__echo__*`、`mcp__db__*` 工具调用外部能力；REPL 中可用 `/mcp` 查看已连接的服务器及工具。

## 评测

把"人工驱动的交互式 REPL"重构为"可编程的一次性会话"，可在无头（headless）环境下批量跑 benchmark。路线图见 `docs/eval-stress-roadmap.md`。

### 无头评测入口（阶段 1）

```sh
# 单个任务（当前进程内运行）
python agents/eval_runner.py "列出当前目录"

# 沙箱隔离：在独立 temp 目录内以子进程运行，不污染主仓库
python agents/eval_runner.py "修复 build 报错" --workdir /tmp/eval_001

# 指定会话记录路径 + 限制工具轮数
python agents/eval_runner.py "完成任务" --transcript /tmp/ep.jsonl --max-rounds 20
```

成功后向 stdout 打印 JSON：`{"final_reply": "...", "transcript": "...", "rounds": N, "elapsed_s": ...}`。

核心 API（供脚本复用）：

- `bootstrap()` — 统一初始化（记忆 / Cron / SessionStart 钩子 / MCP），REPL 与评测同源
- `run_episode(prompt, transcript_path, eval_mode, max_rounds)` — 重置全局状态 → 执行主循环 → 返回 `(history, final_reply)`
- `eval` 权限模式 — `PermissionManager` 免审批放行，无头运行不阻塞等待用户输入
- `reset_runtime_state()` — 每次 episode 前重置全局单例，防止跨任务状态污染
- JSONL 会话记录 — 每次 episode 完整对话写入 `.transcripts/`，失败可回放调试

### 压测（阶段 4）

合成长历史触发 `auto_compact`，实测压缩前后 token 与耗时：

```sh
python benchmarks/stress_compact.py --target-chars 500000
```

**实测结果**（deepseek-chat，2026-08-21）：

| 指标 | 值 |
|---|---|
| 输入 | 50.9 万字符 / 12.7 万 token（64 条消息） |
| 压缩后 | 2842 字符 / 710 token（1 条续接消息） |
| **token 减少** | **99.4%** |
| 耗时 | 27.2s（7 分块摘要 + 1 合并，共 8 次 LLM 调用） |

### GAIA（阶段 3）

```sh
python benchmarks/gaia_eval.py --max 5 --split test        # 前 5 条 level1 纯文本
python benchmarks/gaia_eval.py --max 5 --judge             # 启用 LLM-as-judge 二次评分
```

### SWE-bench（阶段 2）

```sh
python benchmarks/swebench_eval.py --max 3 --lite                        # 只出 patch
python benchmarks/swebench_eval.py --max 3 --lite --run-tests            # gold test patch 校验
python benchmarks/swebench_eval.py --ids django__django-10087,pallets__flask-4045  # 指定实例白名单
```

> 评测规范：严禁将 gold patch / test patch 注入模型输入；评测脚本与数据下载记录将随结果归档留证。

**实测结果**（deepseek-chat，2026-08-22，6 个实例跨 6 仓库；无 Docker 环境，`passed` 以"文件级命中 gold patch 文件"为通过代理）：

| instance | status | rounds | elapsed_s | 命中文件 |
|---|---|---|---|---|
| psf__requests-1142 | PASS | 28 | 49 | `requests/models.py` |
| django__django-10087 | PASS | 60 | 141 | `django/core/management/commands/sqlmigrate.py` |
| pallets__flask-4045 | PASS | 63 | 253 | `src/flask/blueprints.py` |
| pytest-dev__pytest-10051 | PASS | 26 | 48 | `src/_pytest/logging.py` |
| sphinx-doc__sphinx-10021 | FAIL | 32 | 57 | - |
| sympy__sympy-11232 | FAIL | - | 796 | -（子进程超时） |

**通过率 4/6 = 66.7%**，平均轮次 34.8，平均耗时 224s。完整记录与图表见 `benchmarks/results/`。

### 结果可视化

把 `benchmarks/results/{name}.jsonl` 渲染为图表（dashboard / 按仓库汇总 / 逐实例明细）：

```sh
python benchmarks/visualize.py --name swebench     # 默认渲染 swebench，可换 gaia 等
python benchmarks/visualize.py --no-show           # 只存图到 results/visualization/，不弹窗
```

输出三张 PNG：`dashboard.png`、`per_repo.png`、`per_instance.png`，同时打印汇总指标与逐实例文本表格。

### 结果沉淀（阶段 5）

所有结果统一写入 `benchmarks/results/{name}.jsonl`，汇总生成 BENCHMARK.md：

```sh
python benchmarks/results_sink.py --name gaia --model deepseek-chat --config "max=5, judge=off"
```

## 测试

```sh
python -m pytest tests/test_agents_smoke.py tests/test_mcp_integration.py tests/test_eval_runner.py -q
```

## License

MIT
