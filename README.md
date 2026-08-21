# Coding Agent Harness

基于 Python 从 0 实现的端到端 LLM 编程智能体（AI 编程助手）。它运行经典的 Agent 主循环——调用模型、执行工具、回填结果——并扩展到完整的多 Agent 协作平台：持久化任务板、定时调度、Git Worktree 任务隔离、MCP 外部工具接入。

模型负责推理，Harness 为模型提供一个安全、可控的工作环境。

## 核心能力

- **Agent 主循环**：`调用 LLM → 解析 tool_use → 执行工具 → 回填 tool_result`，循环直至模型给出最终回答
- **37 个工具**，由 JSON Schema 分发表驱动：bash、文件读/写/精确编辑、正则检索、子代理委派、会话内待办跟踪、持久化任务、后台任务、Cron 定时调度、团队消息
- **上下文管理**：每轮轻量压缩 + 超 10 万 token 触发 LLM 分块智能摘要；超大工具输出自动落盘并替换为预览标记
- **多 Agent 团队**：文件邮箱消息传递、自主任务认领（互斥锁保护）、定向 / 广播消息、计划审批与关闭协议
- **安全体系**：路径沙箱、危险命令扫描、黑白名单 + 询问用户的权限管道（`default` / `plan` / `auto` 三种模式）
- **持久化**：跨会话记忆、带依赖关系的文件任务板、JSONL 消息总线、工作树事件日志
- **MCP 接入**：外部 MCP 服务器工具合并进统一工具池，复用同一权限门
- **Git Worktree 隔离**：按任务创建并行工作线，事件日志全程可审计
- **健壮容错**：指数退避重试（含随机抖动）、max_tokens 续写、输入超限自动压缩

## 架构

| 关注点 | 实现 |
|---|---|
| 主循环 | `agent_loop()` — 权限检查、Pre/Post 钩子、错误恢复、异步通知 |
| 工具 | `TOOL_HANDLERS` / `TOOLS` 分发表 + JSON Schema 定义 |
| 权限 | `PermissionManager` + `BashSecurityValidator` |
| 钩子 | `HookManager`（`PreToolUse` / `PostToolUse` / `SessionStart`） |
| 记忆 | `MemoryManager`（user / feedback / project / reference） |
| 任务 | `TaskManager`（持久化任务板）+ `TodoManager`（会话内清单） |
| 团队 | `MessageBus` + `TeammateManager`（生命周期、自主认领） |
| 调度 | `BackgroundManager` + `CronScheduler` |
| 隔离 | `WorktreeManager` + `EventBus` |
| MCP | `agents/mcp_plugin.py` — `MCPClient` / `PluginLoader` / `MCPToolRouter` |

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

- `default` — 危险操作（bash、写入等）需用户确认
- `plan` — 只读模式
- `auto` — 读取自动放行，写入仍需确认

## 目录结构

```
agents/
  Agent_Harness.py      # 完整 Harness：主循环、工具、权限、钩子、
                        # 记忆、任务、团队、Cron、后台任务、Worktree
  mcp_plugin.py         # MCP 客户端 / 插件发现 / 工具路由
  README.md             # 能力与工具详细说明
examples/mcp/
  echo_server.py        # 极简 stdio MCP 服务器（echo/add/upper）
  db_server.py          # 基于 SQLite 的 MCP 服务器（只读 SQL、建表语句、笔记写入）
  .claude-plugin/       # 插件清单，接入上述 MCP 服务器
tests/
  test_agents_smoke.py            # 全部 agent 模块的编译冒烟测试
  test_mcp_integration.py         # MCP 客户端 / 路由器 / 插件的端到端测试
```

## 示例

仓库内置两个可直接接入 Harness 的 MCP 示例服务器：

- `echo_server.py` — 极简 stdio MCP 服务器，实现 `initialize` / `tools/list` / `tools/call`
- `db_server.py` — 基于 SQLite 的 MCP 服务器，暴露 `list_tables`、`get_schema`、`query_db`（强制只读 `SELECT`）、`insert_note`；首次运行自动创建示例数据库

接入方式：把 `examples/mcp/.claude-plugin/plugin.json` 复制到工作目录，启动 Harness 后即可通过 `mcp__echo__*`、`mcp__db__*` 工具调用外部能力；REPL 中可用 `/mcp` 查看已连接的服务器及工具。

## 测试

```sh
python -m pytest tests/test_agents_smoke.py tests/test_mcp_integration.py -q
```

## License

MIT
