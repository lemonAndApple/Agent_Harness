# Agent_Harness.py — AI 编程助手（Coding Agent）完整实现

单个 Python 文件（约 3100 行）实现的端到端 AI 编码代理系统：
主循环、工具系统、权限管道、钩子、跨会话记忆、持久化任务板、
多 Agent 团队协作、Cron 定时调度、后台任务、Git Worktree 任务隔离、
MCP 外部工具接入等能力。

## 快速开始

```sh
pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`：

```env
ANTHROPIC_API_KEY=sk-ant-xxx   # 必填，或使用兼容端点
MODEL_ID=claude-sonnet-4-6     # 必填，也可填兼容模型（见 .env.example 列表）
# ANTHROPIC_BASE_URL=...       # 可选，走代理/内网/第三方兼容服务时配置
```

启动：

```sh
python agents/Agent_Harness.py
```

启动后出现 `agent >>` 提示符即表示就绪，直接输入任务即可，例如：

```
agent >> 搜索仓库里所有 TODO 注释，汇总到 todo_list.md
```

## 能力总览

| 模块 | 说明 |
|---|---|
| Agent 主循环 | 调 API → 执行工具 → 返回结果，循环直到模型给出最终回答 |
| 基础工具 | `bash` / 读 / 写 / 编辑 / 检索 文件，全部经路径安全检查 |
| 待办清单 | TodoManager，短期内存任务清单，长期不动会提醒 |
| 子代理 | `task` 工具派生一次性子 Agent，独立上下文处理子任务 |
| 技能加载 | SkillLoader 按需加载领域知识 |
| 上下文压缩 | 超长输出落盘 + 预览标记，token 超限自动摘要 |
| 权限管道 | 危险命令黑名单 + 黑白名单规则 + 询问用户，三种模式 |
| 钩子系统 | PreToolUse / PostToolUse / SessionStart 自定义脚本 |
| 跨会话记忆 | 四种类型持久化记忆，重启后仍可用 |
| 系统提示词 | 按核心规则 / 记忆 / 工具偏好 / 技能列表分层组装 |
| 错误恢复 | API 错误指数退避重试、max_tokens 续写、超长输入压缩 |
| 持久化任务 | TaskManager 磁盘任务板，支持依赖与认领 |
| 后台任务 | BackgroundManager 异步执行长命令，不阻塞主循环 |
| 定时任务 | CronScheduler 按 cron 表达式在后台自动触发 |
| 队友系统 | MessageBus 文件邮箱 + TeammateManager 多 Agent 生命周期 |
| 团队协议 | 关闭请求、计划审批等协调协议 |
| 自主代理 | 队友自认领任务、自主运行与退出 |
| Worktree 隔离 | Git Worktree 创建并行工作线，事件日志可审计 |
| MCP 接入 | 外部 MCP 服务器工具合并进统一工具池，复用同一权限门（见「MCP 接入」） |
| 第三方兼容 | 通过 ANTHROPIC_BASE_URL 对接任意 Anthropic 兼容端点（非 MCP） |

## MCP 接入

程序启动时通过 `PluginLoader` 扫描工作目录下的 `.claude-plugin/plugin.json`，按清单拉起 MCP 服务器进程（stdio 传输），拉取工具清单并注册进统一工具池：

- 外部工具统一命名为 `mcp__{server}__{tool}`，与原生工具共存于同一 `TOOLS` 分发表
- 调用时由 `MCPToolRouter` 剥离前缀、路由回对应服务器，同样经过权限管道（`PermissionManager`）与结果规范化
- REPL 中可用 `/mcp` 查看已连接的 MCP 服务器及其工具数量

仓库内置两个可接入的示例服务器（见 `examples/mcp/`）：

```sh
python examples/mcp/echo_server.py   # 极简 stdio MCP 服务器（echo/add/upper）
python examples/mcp/db_server.py     # 基于 SQLite（只读 SQL、建表语句、笔记写入）
```

将 `examples/mcp/.claude-plugin/plugin.json` 复制到工作目录后启动，即可通过 `mcp__echo__add`、`mcp__db__query_db` 等工具调用外部能力。

## 权限模式

`/mode <mode>` 切换权限模式，`PERMISSION_MODES` 共三种：

- `default` — 危险操作（bash、写文件等）需要用户确认
- `plan` — 只读模式，只允许查询类操作
- `auto` — 读取自动放行，写入仍需确认

## 提供的工具

模型可调用的工具分发表 `TOOL_HANDLERS` / `TOOLS` 共 37 个：

**文件操作**：`bash`、`read_file`、`write_file`、`edit_file`、`search_files`

**任务与规划**：`TodoWrite`（内存待办）、`task_create` / `task_get` / `task_update` / `task_list`（磁盘任务板）、`claim_task`

**执行扩展**：`task`（子代理）、`load_skill`、`compress`（手动压缩）、`background_run` / `check_background`、`cron_create` / `cron_delete` / `cron_list`

**团队协作**：`spawn_teammate`、`list_teammates`、`send_message`、`read_inbox`、`broadcast`、`shutdown_request`、`plan_approval`、`idle`

**记忆**：`save_memory`

**Worktree**：`worktree_create` / `worktree_list` / `worktree_enter` / `worktree_status` / `worktree_run` / `worktree_closeout` / `worktree_keep` / `worktree_remove` / `worktree_events`

## REPL 交互命令

| 命令 | 说明 |
|---|---|
| `/compact` | 手动压缩对话历史 |
| `/tasks` | 查看任务板 |
| `/team` | 查看队友状态 |
| `/inbox` | 查看领导收件箱 |
| `/cron` | 查看定时任务列表 |
| `/memories` | 查看跨会话记忆 |
| `/worktrees` | 查看 Git 工作树 |
| `/mcp` | 查看已连接的 MCP 服务器及工具 |
| `/search <pattern> [path] [include]` | 检索文件内容，如 `/search 'def run_' *.py` |
| `/mode <mode>` | 切换权限模式，如 `/mode plan` 切到只读 |
| `q` / `exit` / 空行 | 退出程序 |

## 运行时目录结构

程序会在工作目录下按需创建：

```text
.team/                # 团队配置与队友消息（inbox/requests）
.tasks/               # 持久化任务文件
.task_outputs/        # 超长工具输出落盘 + 预览
.memory/              # 跨会话记忆（MEMORY.md 索引）
.claude/              # cron 任务定义、锁文件、信任标记
.worktrees/           # Git 工作树与事件日志
.transcripts/         # 上下文压缩备份
```

## 代码结构

文件按模块组织，实现按以下顺序递进：

1. 持久化输出 → 基础工具 → Todo（最小核心）
2. 安全（Bash 扫描 / 权限）→ 子代理 / 技能 / 压缩 / 记忆
3. 任务 / 后台 / 消息 / 队友 / 钩子 / Cron
4. Worktree；MCP（mcp_plugin.py）；系统提示词、工具分发表
5. `agent_loop` 主循环与 REPL 入口（系统"大脑"与"界面"）

## 注意

- `bash` 中长时间 `sleep`（≥5 秒）会被拦截，延迟任务请用 `cron_create`（后台执行，不阻塞）。
- 所有文件操作限制在工作目录内（`safe_path` 校验），模型无法逃逸读取系统文件。
- 自定义 `ANTHROPIC_BASE_URL` 时，程序会自动移除 `ANTHROPIC_AUTH_TOKEN`（仅官方端点有效）。
- Worktree 相关工具需要处于 Git 仓库内，否则会返回错误提示。
