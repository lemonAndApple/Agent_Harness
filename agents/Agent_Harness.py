#!/usr/bin/env python3
"""
Agent_Harness.py - AI 编程助手（Agent）的完整实现


程序结构速查：
  持久化输出     maybe_persist_output()             超长输出写入磁盘+预览
  基础工具       safe_path/run_bash/run_read/run_write/run_edit/run_grep
  待办列表       TodoManager                         短期内存任务清单
  Bash安全扫描   BashSecurityValidator               命令危险模式检测
  权限管道       PermissionManager                   黑/白名单+模式+询问用户
  子代理         run_subagent()                      派生一次性子Agent
  技能加载       SkillLoader                         按需加载领域知识
  上下文压缩     microcompact/auto_compact           轻量/智能摘要
  记忆系统       MemoryManager                       跨会话持久化记忆
  任务板         TaskManager                         持久化文件任务管理
  后台任务       BackgroundManager                   异步执行长命令
  消息总线       MessageBus                          队友文件邮箱通信
  队友管理       TeammateManager                     多Agent生命周期管理
  钩子系统       HookManager                         工具前后自定义脚本
  定时任务       CronScheduler                       按cron表达式自动触发
  工作树         WorktreeManager                     Git worktree隔离
  工具函数       normalize/claim_task/backoff_delay
  全局实例       各管理器的单例对象
  系统提示词     build_system_prompt()               构建发给模型的说明书
  关闭/审批      handle_shutdown_request/handle_plan_review
  工具分发表     TOOL_HANDLERS/TOOLS                 工具路由与API定义
  主循环         agent_loop()                        整个系统的大脑
  REPL入口       命令行交互界面

REPL 交互命令：
  /compact   - 手动压缩对话历史
  /tasks     - 查看任务板
  /team      - 查看队友状态
  /inbox     - 查看领导收件箱
  /cron      - 查看定时任务列表
  /memories  - 查看跨会话记忆
  /worktrees - 查看Git工作树
  /mcp       - 查看已连接的MCP服务器及工具
  /search    - 检索文件内容 (如 /search 'def run_' *.py)
  /mode      - 切换权限模式 (/mode plan 只读, /mode build 正常读写)
  q/exit/空  - 退出程序
"""


import json           # 处理 JSON 数据
import logging        # 日志
import os             # 环境变量、文件路径
import random         # 随机数（退避 jitter）
import re             # 正则表达式
import select         # 非阻塞监听 stdin
import shlex          # 命令行解析
import sys            # 系统参数
import subprocess     # 执行系统命令
import threading      # 多线程
import time           # 时间
import uuid           # 唯一 ID
from datetime import datetime, timedelta  # 日期时间
from fnmatch import fnmatch               # Unix 通配符匹配
from pathlib import Path                  # 文件路径
from queue import Queue, Empty            # 线程安全队列

from anthropic import Anthropic, APIError

from dotenv import load_dotenv

from mcp_plugin import MCPClient, PluginLoader, MCPToolRouter

load_dotenv(override=True)

# 自定义 API 地址（如代理/内网）时，移除认证令牌（仅官方地址有效）
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("agent")

# ═══════════════════════════════════════════════════════════════
# 【全局常量】全大写 = 常量，程序运行期间不应改变
# ═══════════════════════════════════════════════════════════════

# 工作目录：所有文件操作都限制在此目录内
WORKDIR = Path.cwd()

# Anthropic 客户端：与 Claude 通信的唯一通道
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))

# 模型名称，从 .env 读取
MODEL = os.environ["MODEL_ID"]

# 目录路径
TEAM_DIR = WORKDIR / ".team"                # 团队配置与队友消息
INBOX_DIR = TEAM_DIR / "inbox"              # 队友收件箱（jsonl）
TASKS_DIR = WORKDIR / ".tasks"              # 持久化任务文件
SKILLS_DIR = WORKDIR / "skills"             # 技能定义目录
TRANSCRIPT_DIR = WORKDIR / ".transcripts"   # 上下文压缩备份

# ---- 运行参数 ----
# 对话 token 数超过此值触发自动压缩（约 7.5 万中文字）
TOKEN_THRESHOLD = 100000
# 分块摘要时每个块的字符数上限
CHUNK_CHARS = 80000

# 队友空闲时轮询间隔（秒）
POLL_INTERVAL = 5
# 队友空转超时自动退出（秒）
IDLE_TIMEOUT = 60

# ---- 大输出持久化 ----
# bash 命令输出可能几万行，塞回对话会撑爆上下文，超过阈值写入磁盘并返回预览
TASK_OUTPUT_DIR = WORKDIR / ".task_outputs"
TOOL_RESULTS_DIR = TASK_OUTPUT_DIR / "tool-results"

PERSIST_OUTPUT_TRIGGER_CHARS_DEFAULT = 50000  # 默认触发阈值（字符）
PERSIST_OUTPUT_TRIGGER_CHARS_BASH = 30000     # bash 输出更常超出，用更低阈值
CONTEXT_TRUNCATE_CHARS = 50000                # 返回给模型的最多字符数

PERSISTED_OPEN = "<persisted-output>"
PERSISTED_CLOSE = "</persisted-output>"
PERSISTED_PREVIEW_CHARS = 2000                 # 预览只保留前 2000 字符

# 压缩时保留最近几次工具结果不压缩
KEEP_RECENT = 3

# 这些工具的输出重要，永不压缩
PRESERVE_RESULT_TOOLS = {"read_file"}

# ---- 队友间消息类型 ----
VALID_MSG_TYPES = {"message", "broadcast", "shutdown_request",
                   "shutdown_response", "plan_approval", "plan_approval_response"}

# ---- 记忆系统配置 ----
MEMORY_DIR = WORKDIR / ".memory"             # 记忆文件存储目录
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"      # 记忆索引文件
# 四种记忆类型: user=用户偏好 feedback=用户纠正 project=项目知识 reference=外部资源
MEMORY_TYPES = ("user", "feedback", "project", "reference")

# ---- Cron 定时任务配置 ----
SCHEDULED_TASKS_FILE = WORKDIR / ".claude" / "scheduled_tasks.json"
CRON_LOCK_FILE = WORKDIR / ".claude" / "cron.lock"

# ---- 钩子系统配置 ----
HOOK_EVENTS = ("PreToolUse", "PostToolUse", "SessionStart")
HOOK_TIMEOUT = 30  # 钩子脚本最长执行 30 秒
TRUST_MARKER = WORKDIR / ".claude" / ".claude_trusted"  # 信任标记文件

# ---- 权限模式 ----
# build=正常读写、危险操作需确认 plan=只读 eval=评测模式（免审批）
PERMISSION_MODES = ("plan", "build", "eval")

# ---- 错误恢复参数 ----
MAX_RECOVERY_ATTEMPTS = 3  # 最多重试 3 次
# 指数退避：1s,2s,4s... 但不超过 30s，加随机抖动避免惊群
BACKOFF_BASE_DELAY = 1.0
BACKOFF_MAX_DELAY = 30.0

# 输出被 max_tokens 截断时注入此消息让模型继续
CONTINUATION_MESSAGE = (
    "Output limit hit. Continue directly from where you stopped -- "
    "no recap, no repetition. Pick up mid-sentence if needed."
)

# ---- 自主代理配置 ----
REQUESTS_DIR = TEAM_DIR / "requests"
CLAIM_EVENTS_PATH = TASKS_DIR / "claim_events.jsonl"

# 线程锁：多个线程同时认领任务时保证互斥
_claim_lock = threading.Lock()

# ---- Worktree 工作树配置 ----
# Git Worktree 在同一个仓库创建多个独立工作目录（"平行宇宙"），互不影响
try:
    # 获取 Git 仓库根目录（含 .git 的最外层目录）
    _git_root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=WORKDIR, capture_output=True, text=True, timeout=10,
    )
    # 命令成功则用返回路径，否则用当前目录
    REPO_ROOT = Path(_git_root.stdout.strip()) if _git_root.returncode == 0 else WORKDIR
except Exception:
    # 不在 Git 仓库内时用当前目录
    REPO_ROOT = WORKDIR

WORKTREES_DIR = REPO_ROOT / ".worktrees"              # 工作树存储目录
WORKTREE_EVENTS_LOG = WORKTREES_DIR / "events.jsonl"  # 工作树事件日志


# ═══════════════════════════════════════════════════════════════════════════════
# 持久化输出机制
# ═══════════════════════════════════════════════════════════════════════════════
# 大模型上下文窗口有限，bash 输出几万行会立即"撑爆"上下文。
# 策略：超长输出 → 写入磁盘文件 → 只给模型看预览标记。
#
# 函数分工：
#   _persist_tool_result() → 把内容写入磁盘
#   _preview_slice()       → 截取前 N 字符做预览
#   _build_persisted_marker→ 组装成模型能理解的标记文本
#   maybe_persist_output()  → 决策入口：要不要存？存了返回什么？
# ═══════════════════════════════════════════════════════════════════════════════

def _persist_tool_result(tool_use_id: str, content: str) -> Path:
    """将单个工具调用的输出写入磁盘文件，返回相对 WORKDIR 的路径。

    文件名用工具 ID 生成。tool_use_id 可能含特殊字符（如 /、..），
    用正则替换为非字母数字字符，防止路径穿越攻击。
    """
    # parents=True 自动创建父目录，exist_ok=True 目录已存在时不报错
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # 只保留字母数字 ._ -，其余替换为下划线
    safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", tool_use_id or "unknown")

    path = TOOL_RESULTS_DIR / f"{safe_id}.txt"

    # 防止覆盖同名文件（极少发生）
    if not path.exists():
        path.write_text(content)

    # 返回相对路径，方便在各目录下查看
    return path.relative_to(WORKDIR)


def _format_size(size: int) -> str:
    """把字节数格式化为人类可读的大小字符串（如 1.5KB、2.0MB）。"""
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / (1024 * 1024):.1f}MB"


def _preview_slice(text: str, limit: int) -> tuple[str, bool]:
    """截取文本前 limit 个字符作为预览，尽量在换行符处截断。

    返回 (预览文本, 是否有更多内容)。在前 limit 字符内找最后一个换行符，
    若其位置超过 limit 的一半则在换行处截断，否则按 limit 硬截。
    """
    if len(text) <= limit:
        return text, False

    idx = text[:limit].rfind("\n")

    # 换行符太靠前（不足 limit 一半）就硬截，否则在换行符截断
    cut = idx if idx > (limit * 0.5) else limit
    return text[:cut], True


def _build_persisted_marker(stored_path: Path, content: str) -> str:
    """构建"输出已持久化到文件"的标记，替代完整输出返回给模型。

    标记包含：大小信息 + 文件路径 + 前 PERSISTED_PREVIEW_CHARS 字符预览。
    """
    preview, has_more = _preview_slice(content, PERSISTED_PREVIEW_CHARS)

    marker = (
        f"{PERSISTED_OPEN}\n"
        f"Output too large ({_format_size(len(content))}). "
        f"Full output saved to: {stored_path}\n\n"
        f"Preview (first {_format_size(PERSISTED_PREVIEW_CHARS)}):\n"
        f"{preview}"
    )
    if has_more:
        marker += "\n..."
    marker += f"\n{PERSISTED_CLOSE}"
    return marker


def maybe_persist_output(tool_use_id: str, output: str, trigger_chars: int = None) -> str: # type: ignore
    """判断工具输出是否太大，若太大则写入磁盘并返回预览标记（门面函数）。

    - 输出长度 <= 阈值 → 原样返回
    - 输出长度 > 阈值  → 写入磁盘，返回预览标记

    不同工具可设不同阈值：bash 用 30000，其余默认 50000。
    无法提前知道哪个工具会产大输出，统一过一遍最稳妥。
    """
    # 非字符串类型直接转换返回（没有"大小"概念）
    if not isinstance(output, str):
        return str(output)

    trigger = PERSIST_OUTPUT_TRIGGER_CHARS_DEFAULT if trigger_chars is None else int(trigger_chars)

    if len(output) <= trigger:
        return output

    stored_path = _persist_tool_result(tool_use_id, output)
    return _build_persisted_marker(stored_path, output)


# ═══════════════════════════════════════════════════════════════════════════════
# 基础工具函数
# ═══════════════════════════════════════════════════════════════════════════════
# Agent 与操作系统交互的底层"手脚"：
#   safe_path() → 路径安全检查（所有路径操作都要过它）
#   run_bash()  → 执行 shell 命令
#   run_read()  → 读取文件
#   run_write() → 写入/覆盖文件
#   run_edit()  → 查找替换文件中的文本
#   run_grep()  → 检索文件内容
#
# 所有操作经 safe_path() 校验，确保只能访问工作目录内的文件，
# 即使模型试图读 /etc/passwd 也会被拦截。
# ═══════════════════════════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    """确保给定路径不会逃逸到工作目录之外，返回安全的绝对 Path。

    三步：拼接绝对路径 → resolve() 解析 .. 和软链接 → 校验仍在 WORKDIR 内。
    例如 safe_path("src/main.py") 正常通过，safe_path("../../etc/passwd") 抛出 ValueError。
    """
    path = (WORKDIR / p).resolve()

    # is_relative_to 检查是否仍在工作目录下（Python 3.9+）
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def blocking_sleep_check(command: str) -> str:
    """检测命令中是否含长时间 sleep（>=5 秒），返回引导提示文本。

    bash 是同步执行的：`sleep 60` 会阻塞整个 agent_loop 60 秒。
    延迟/定时任务应改用 cron_create 后台执行。
    无 sleep 或 <5 秒时返回空字符串（放行）。
    """
    sleep_match = re.search(r"\bsleep\s+(\d+)", command)
    if sleep_match and int(sleep_match.group(1)) >= 5:
        return ("Error: blocking sleep detected. For delayed/scheduled work use cron_create "
                "(runs in background, agent stays interactive). Never use bash 'sleep N' to wait.")
    return ""


def run_bash(command: str, tool_use_id: str = "") -> str:
    """执行 shell 命令并返回输出。

    三道防护：危险命令黑名单 → 长时间 sleep 检测 → 大输出持久化。
    命令在 WORKDIR 下执行，120 秒超时，stdout 与 stderr 合并返回。
    """
    # 第一道防护：危险命令黑名单
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"

    # 第二道防护：长时间 sleep 阻塞检测（延迟任务应走 cron_create）
    sleep_blocked = blocking_sleep_check(command)
    if sleep_blocked:
        return sleep_blocked

    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)

        out = (r.stdout + r.stderr).strip()

        if not out:
            return "(no output)"

        # 第三道防护：大输出持久化（bash 阈值比默认低）
        out = maybe_persist_output(tool_use_id, out, trigger_chars=PERSIST_OUTPUT_TRIGGER_CHARS_BASH)

        return out[:CONTEXT_TRUNCATE_CHARS] if isinstance(out, str) else str(out)[:CONTEXT_TRUNCATE_CHARS]

    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, tool_use_id: str = "", limit: int = None) -> str: # type: ignore
    """读取文件内容并返回。

    limit 参数可只读取前 N 行（用于超大文件），并提示剩余行数。
    文件太大时返回持久化标记（路径+预览）。
    任何错误返回 "Error: ..." 而不是崩溃。
    """
    try:
        lines = safe_path(path).read_text().splitlines()

        # 指定 limit 且行数超出时，只保留前 limit 行并提示剩余行数
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]

        out = "\n".join(lines)

        # 文件也可能特别大，走一遍持久化检查
        out = maybe_persist_output(tool_use_id, out)

        return out[:CONTEXT_TRUNCATE_CHARS] if isinstance(out, str) else str(out)[:CONTEXT_TRUNCATE_CHARS]

    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    """写入内容到文件，父目录不存在时自动创建。返回 "Wrote {n} bytes to {path}"。

    注意这是覆盖写入：文件已存在时原内容会被清空替换，需要改小段用 run_edit。
    """
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """精确替换文件中的文本：找到第一个 old_text，替换为 new_text。

    只替换第一次出现的位置。old_text 需精确匹配（含缩进、空格、换行），
    找不到时返回错误而非静默失败。返回 "Edited {path}"。
    """
    try:
        fp = safe_path(path)

        c = fp.read_text()

        if old_text not in c:
            return f"Error: Text not found in {path}"

        # count=1 表示只替换第一次出现的位置
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"Edited {path}"

    except Exception as e:
        return f"Error: {e}"


def run_grep(pattern: str, path: str = ".", include: str = "*") -> str:
    """在文件内容中检索匹配的行（类似 grep），返回 "文件路径:行号: 匹配行"。

    比让模型拼 bash grep 更安全、输出更可控。
    - path 经 safe_path 校验，杜绝搜索工作目录以外
    - 最多返回 200 条，超长自动写入磁盘并返回预览
    - 跳过隐藏文件/目录、.pyc 缓存、超过 1MB 的文件
    """
    try:
        root = safe_path(path)
        if not root.is_dir():
            root = root.parent

        pattern_re = re.compile(pattern)
        results = []
        MAX_GREP_RESULTS = 200  # 最多返回 200 条，防止输出爆炸

        for dirpath, dirnames, filenames in os.walk(root):
            # 跳过隐藏目录和 Python 缓存目录
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d != "__pycache__"]
            for fname in filenames:
                # 跳过隐藏文件
                if fname.startswith("."):
                    continue
                # 跳过 Python 字节码缓存
                if fname.endswith((".pyc", ".pyo")):
                    continue
                # include 通配过滤（如 "*.py"）
                if not fnmatch(fname, include):
                    continue

                fp = Path(dirpath) / fname
                try:
                    # 跳过超大文件（>1MB），防止读取卡死
                    if fp.stat().st_size > 1_000_000:
                        continue
                    for lineno, line in enumerate(fp.read_text(errors="replace").splitlines(), 1):
                        if pattern_re.search(line):
                            results.append(f"{fp.relative_to(WORKDIR)}:{lineno}: {line[:300]}")
                            if len(results) >= MAX_GREP_RESULTS:
                                break
                except (UnicodeDecodeError, OSError):
                    continue  # 二进制文件或无法读取，跳过
                if len(results) >= MAX_GREP_RESULTS:
                    break
            if len(results) >= MAX_GREP_RESULTS:
                break

        if not results:
            return "(no matches)"

        out = "\n".join(results)
        if len(results) >= MAX_GREP_RESULTS:
            out += f"\n... (truncated, showing first {MAX_GREP_RESULTS} matches)"
        return maybe_persist_output("", out)
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════════════════════════
# TodoManager — 短期任务清单
# ═══════════════════════════════════════════════════════════════════════════════
#
# 【TodoManager 和 TaskManager 的区别】
#   TodoManager (本节): 内存中，程序关了就没，辅助模型记住当前在做什么
#   TaskManager: 磁盘上，程序关了还在，正式的"项目任务工单"
#
# 【Todo 状态机】
#   [ ] pending ──→ [>] in_progress ──→ [x] completed
#
# 【key 规则】
#   1. 每项必须有 content/status/activeForm
#   2. 最多 20 项
#   3. 同时只能有 1 项 in_progress
# ═══════════════════════════════════════════════════════════════════════════════

class TodoManager:
    """管理当前对话中的"短期待办事项"。

    self.items 是列表，每个元素为
    {"content": 任务名, "status": pending/in_progress/completed, "activeForm": 当前动作}
    """

    def __init__(self):
        """初始化一个空的待办列表。"""
        self.items = []

    def update(self, items: list) -> str:
        """用新列表全量替换旧列表（模型每次调用 TodoWrite 都是全量更新）。

        模型自己维护待办列表最准确，全量替换简单不易出 bug。
        校验：content 非空、status 合法、activeForm 非空、最多 20 项、最多 1 项 in_progress。
        """
        validated = []  # 校验通过后的项
        ip = 0          # in_progress 计数器
        for i, item in enumerate(items):
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "pending")).lower()
            af = str(item.get("activeForm", "")).strip()

            # 校验 1: content 不能为空
            if not content:
                raise ValueError(f"Item {i}: content required")

            # 校验 2: status 必须合法
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {i}: invalid status '{status}'")

            # 校验 3: activeForm 不能为空
            if not af:
                raise ValueError(f"Item {i}: activeForm required")

            # 校验 5: 统计 in_progress 数量
            if status == "in_progress":
                ip += 1

            validated.append({"content": content, "status": status, "activeForm": af})

        # 校验 4: 最多 20 项
        if len(validated) > 20:
            raise ValueError("Max 20 todos")

        # 校验 5: in_progress 最多 1 项
        if ip > 1:
            raise ValueError("Only one in_progress allowed")

        self.items = validated
        return self.render()

    def render(self) -> str:
        """把待办列表格式化为人类可读文本：
        [ ] pending  [>] in_progress（附 activeForm）  [x] completed  (n/N completed)
        """
        if not self.items:
            return "No todos."

        lines = []
        for item in self.items:
            m = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}.get(item["status"], "[?]")
            suffix = f" <- {item['activeForm']}" if item["status"] == "in_progress" else ""
            lines.append(f"{m} {item['content']}{suffix}")

        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")

        return "\n".join(lines)

    def has_open_items(self) -> bool:
        """是否还有未完成的待办项（agent_loop 据此触发"更新 Todo"提醒）。"""
        return any(item.get("status") != "completed" for item in self.items)


# ═══════════════════════════════════════════════════════════════════════════════
# BashSecurityValidator — Bash 命令安全扫描器
# ═══════════════════════════════════════════════════════════════════════════════
# 在命令执行前用正则扫描危险模式（rm -rf /、sudo、$(恶意代码) 等）。
# 这只是"命令语法层面"的第一道防线；策略层面的权限控制由 PermissionManager 负责。
# ═══════════════════════════════════════════════════════════════════════════════

class BashSecurityValidator:
    """Bash 命令安全检查器：用正则扫描命令，找出危险模式。

    VALIDATORS 是 (规则名, 正则) 列表，正则用 \b 表示单词边界防误报。
    用列表而非字典，因为可能有同一个规则名的多个变体需要分别定义。
    """

    # ── 5 个安全规则 ──
    VALIDATORS = [
        # 规则1: Shell特殊字符 ; & | ` $（可拼接多条命令，常见攻击手法）
        ("shell_metachar", r"[;&|`$]"),

        # 规则2: sudo（管理员权限，绕过权限限制）
        ("sudo", r"\bsudo\b"),

        # 规则3: rm -r*（递归删除，rm -rf 强制删除不询问）
        ("rm_rf", r"\brm\s+(-[a-zA-Z]*)?r"),

        # 规则4: $(...) 命令替换（动态取路径，绕过硬编码黑名单）
        ("cmd_substitution", r"\$\("),

        # 规则5: IFS= 环境变量注入（改变 shell 分隔符绕过检查）
        ("ifs_injection", r"\bIFS\s*="),
    ]

    def validate(self, command: str) -> list:
        """扫描命令，返回所有触发的违规规则（(规则名, 正则) 元组列表）。空列表=安全。"""
        failures = []

        # 遍历所有5个规则，逐个用 re.search() 匹配
        for name, pattern in self.VALIDATORS:
            if re.search(pattern, command):
                failures.append((name, pattern))

        return failures

    def is_safe(self, command: str) -> bool:
        """快捷判断：validate() 返回空列表即安全。"""
        return len(self.validate(command)) == 0

    def describe_failures(self, command: str) -> str:
        """生成人类可读的违规描述，如 "Security flags: sudo (pattern: \bsudo\b), ..."。"""
        failures = self.validate(command)
        if not failures:
            return "No issues detected"

        # 格式化为 "name (pattern: ...)"
        parts = [f"{name} (pattern: {pattern})" for name, pattern in failures]
        return "Security flags: " + ", ".join(parts)


# 创建全局安全检查器实例（单例）
bash_validator = BashSecurityValidator()


# ── 默认权限规则（程序启动即生效）──
DEFAULT_RULES = [
    {"tool": "bash", "content": "rm -rf /", "behavior": "deny"},    # 黑名单：禁止删除根目录
    {"tool": "bash", "content": "sudo *",   "behavior": "deny"},    # 黑名单：禁止 sudo
    {"tool": "read_file", "path": "*",       "behavior": "allow"},   # 白名单：允许读文件
    # 白名单：允许内容检索。与 read_file 同为只读操作，直接放行，
    # 保证 Agent 检索代码不需要人工确认。
    {"tool": "search_files", "path": "*",    "behavior": "allow"},
]

# ═══════════════════════════════════════════════════════════════════════════════
# PermissionManager — 权限管道
# ═══════════════════════════════════════════════════════════════════════════════
#
# 【权限管道执行流程】
#   1. deny_rules  ← 黑名单命中 → 直接拒绝
#   2. bash_scan   ← Bash 危险命令 → 直接拒绝
#   3. mode_check  ← plan 模式禁止写入
#   4. allow_rules ← 白名单命中 → 直接放行
#   5. ask_user    ← 无规则匹配 → 询问用户
#
# 【两种权限模式】
#   build: 正常模式，读写均可，危险操作询问用户
#   plan:  只能读不能写，最安全
# ═══════════════════════════════════════════════════════════════════════════════

class PermissionManager:
    """权限管理器：控制 Agent 能做哪些操作。

    黑名单明确拒绝、白名单直接放行、无规则时询问用户。
    self.consecutive_denials 记录连续拒绝次数，连续 3 次会建议切换 plan 模式。
    """
    def __init__(self, mode: str = "build", rules: list = None): # type: ignore
        # 校验模式是否合法
        if mode not in PERMISSION_MODES:
            raise ValueError(f"Unknown mode: {mode}. Choose from {PERMISSION_MODES}")

        self.mode = mode                      # 当前模式（plan/build）

        # list() 拷贝，防止修改实例规则时影响 DEFAULT_RULES
        self.rules = rules or list(DEFAULT_RULES)

        self.consecutive_denials = 0          # 连续拒绝计数器
        self.max_consecutive_denials = 3      # 达到上限后警告

    def check(self, tool_name: str, tool_input: dict) -> dict:
        """权限管道核心入口：判断工具调用是否被允许，返回
        {"behavior": "allow"/"deny"/"ask", "reason": "原因"}。

        五层检查：Bash 安全扫描 → deny 黑名单 → 模式检查 → allow 白名单 → ask。
        """
        # 第1层：Bash 命令专项安全检查
        if tool_name == "bash":
            command = tool_input.get("command", "")
            failures = bash_validator.validate(command)

            if failures:
                # 严重违规（sudo/rm_rf）直接拒绝，普通违规询问用户
                severe = {"sudo", "rm_rf"}
                severe_hits = [f for f in failures if f[0] in severe]

                if severe_hits:
                    desc = bash_validator.describe_failures(command)
                    return {"behavior": "deny", "reason": f"Bash validator: {desc}"}

                desc = bash_validator.describe_failures(command)
                return {"behavior": "ask", "reason": f"Bash validator flagged: {desc}"}

        # 第2层：deny 黑名单规则匹配
        for rule in self.rules:
            if rule["behavior"] != "deny":
                continue  # 只看 deny 规则
            if self._matches(rule, tool_name, tool_input):
                return {"behavior": "deny", "reason": f"Blocked by deny rule: {rule}"}

        # 第3层：模式检查
        # plan 模式只能读不能写
        if self.mode == "plan":
            if tool_name in ("write_file", "edit_file", "bash"):
                return {"behavior": "deny", "reason": "Plan mode: write operations are blocked"}
            return {"behavior": "allow", "reason": "Plan mode: read-only allowed"}

        # eval 模式：评测免审批，除 deny 黑名单外一律放行（不卡 input()）
        if self.mode == "eval":
            return {"behavior": "allow", "reason": "Eval mode: auto-allow"}

        # build 模式：读写均可，继续走白名单/询问流程（危险操作询问用户）

        # 第4层：allow 白名单规则匹配
        for rule in self.rules:
            if rule["behavior"] != "allow":
                continue  # 只看 allow 规则
            if self._matches(rule, tool_name, tool_input):
                self.consecutive_denials = 0  # 放行即重置拒绝计数
                return {"behavior": "allow", "reason": f"Matched allow rule: {rule}"}

        # 第5层：无规则匹配 → 需要用户确认
        return {"behavior": "ask", "reason": f"No rule matched for {tool_name}, asking user"}

    def ask_user(self, tool_name: str, tool_input: dict) -> bool:
        """询问用户是否允许本次操作，返回 True=允许 False=拒绝。

        用户选择 "always" 时会追加一条 allow 规则到 self.rules，
        下次同类操作在白名单中直接命中，无需再问。
        """
        # 评测模式免审批：直接放行，避免在 input() 处卡死无头评测
        if self.mode == "eval":
            return True

        # 生成简短预览，防止参数太长刷屏
        preview = json.dumps(tool_input, ensure_ascii=False)[:200]
        log.info(f"Permission check: {tool_name} -> {preview}")

        try:
            answer = input("  Allow? (y/n/always): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            # Ctrl+D / Ctrl+C 视为拒绝
            return False

        # 用户选择 "always"（永久允许）：追加 allow 规则
        if answer == "always":
            self.rules.append({"tool": tool_name, "path": "*", "behavior": "allow"})
            self.consecutive_denials = 0
            return True

        # 用户选择 y/yes（本次允许）
        if answer in ("y", "yes"):
            self.consecutive_denials = 0
            return True

        # 用户拒绝
        self.consecutive_denials += 1

        # 连续拒绝过多 → 建议切换 plan 模式
        if self.consecutive_denials >= self.max_consecutive_denials:
            log.warning(f"{self.consecutive_denials} consecutive denials -- consider switching to plan mode")

        return False

    def _matches(self, rule: dict, tool_name: str, tool_input: dict) -> bool:
        """判断权限规则是否匹配给定工具和参数。

        三个维度 AND 关系：tool（精确）、path（fnmatch）、content（fnmatch）。
        维度未指定或为 "*" 时跳过该维度检查。
        """
        # 维度1: tool（工具名）
        if rule.get("tool") and rule["tool"] != "*":
            if rule["tool"] != tool_name:
                return False

        # 维度2: path（文件路径）
        if "path" in rule and rule["path"] != "*":
            path = tool_input.get("path", "")
            if not fnmatch(path, rule["path"]):
                return False

        # 维度3: content（命令内容）
        if "content" in rule:
            command = tool_input.get("command", "")
            if not fnmatch(command, rule["content"]):
                return False

        # 所有约束维度都匹配，命中规则
        return True


# ═══════════════════════════════════════════════════════════════════════════════
# Subagent — 子代理机制
# ═══════════════════════════════════════════════════════════════════════════════
# 主 Agent 派生子 Agent 独立完成子任务，做完返回结果即消失。
# 与队友的区别：队友持久在线、有自己的线程和收件箱。
#
# 两种类型：
#   - "Explore":        只有 bash+read_file，只读，适合调研
#   - "general-purpose": 额外有写入/编辑工具，可改文件，适合编码
# ═══════════════════════════════════════════════════════════════════════════════

def run_subagent(prompt: str, agent_type: str = "Explore") -> str:
    """创建并运行一个子 Agent，独立完成指定任务，返回最终文本总结。

    流程：初始化为用户 prompt → 调 API → 若返回工具调用则执行并把结果加回
    对话 → 重复，直到模型返回最终文本（stop_reason != "tool_use"），最多 30 轮。
    """
    # 基础工具：所有子代理都有 bash + read_file
    sub_tools = [
        {"name": "bash", "description": "执行 shell 命令。",
         "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
        {"name": "read_file", "description": "读取文件。",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    ]
    # 非 Explore 类型额外获得写入和编辑工具
    if agent_type != "Explore":
        sub_tools += [
            {"name": "write_file", "description": "写入文件。",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "精确替换文件文本。",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
        ]

    # 工具名 → 执行函数映射
    sub_handlers = {
        "bash": lambda **kw: run_bash(kw["command"]),
        "read_file": lambda **kw: run_read(kw["path"]),
        "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
        "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    }

    sub_msgs = [{"role": "user", "content": prompt}]
    resp = None

    # 代理循环：最多 30 轮，防止无限调用工具卡死
    for _ in range(30):
        resp = client.messages.create(model=MODEL, messages=sub_msgs, tools=sub_tools, max_tokens=8000) # type: ignore
        sub_msgs.append({"role": "assistant", "content": resp.content}) # type: ignore
        # stop_reason != "tool_use" → 模型给了最终回答，任务完成
        if resp.stop_reason != "tool_use":
            break

        # 逐个执行工具，输出截断到 5 万字符防止撑爆子代理上下文
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                h = sub_handlers.get(b.name, lambda **kw: "Unknown tool")
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": str(h(**b.input))[:50000]})
        sub_msgs.append({"role": "user", "content": results}) # type: ignore

    # 提取所有 text block 的文本拼接成最终结果
    if resp:
        return "".join(b.text for b in resp.content if hasattr(b, "text")) or "(no summary)" # type: ignore
    return "(subagent failed)"


# ═══════════════════════════════════════════════════════════════════════════════
# SkillLoader — 技能加载器
# ═══════════════════════════════════════════════════════════════════════════════
# 技能是预先写好的"领域知识指令"，存放在 skills/ 目录。
# 不能把所有技能都塞进 system prompt（会撑爆上下文），所以做成按需加载。
#
# 文件格式：skills/ 下每个子目录含一个 SKILL.md：
#   ---
#   name: git-helper
#   description: 帮助处理 Git 命令
#   ---
#   # Git 操作指南（具体指令内容...）
# ═══════════════════════════════════════════════════════════════════════════════

class SkillLoader:
    """扫描 skills 目录，管理所有可用技能的加载。"""

    def __init__(self, skills_dir: Path):
        """用 rglob 递归查找所有 SKILL.md 并解析 YAML 头 + 正文。

        解析正则：r"^---\n(.*?)\n---\n(.*)"，--- 之间是元数据，之后是正文。
        存入 self.skills: {技能名: {"meta": {...}, "body": "指令正文"}}。
        rglob 递归搜索所有子目录，glob 只搜当前目录。
        """
        self.skills = {}
        if skills_dir.exists():
            # sorted() 保证按名称排序，结果可预测
            for f in sorted(skills_dir.rglob("SKILL.md")):
                text = f.read_text()

                # re.DOTALL 让 . 匹配换行符；group(1)=YAML 头，group(2)=正文
                match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
                meta, body = {}, text

                if match:
                    # 逐行解析 key: value 格式
                    for line in match.group(1).strip().splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)  # 只分割第一个冒号
                            meta[k.strip()] = v.strip()
                    body = match.group(2).strip()

                # 技能名优先用 YAML 声明的 name，否则用父目录名
                name = meta.get("name", f.parent.name)
                self.skills[name] = {"meta": meta, "body": body}

    def descriptions(self) -> str:
        """生成所有可用技能的清单（只含名称+描述，不含正文）。

        放入 system prompt 当"目录"，模型需要时再调 load_skill() 取完整内容。
        """
        if not self.skills:
            return "(no skills)"
        return "\n".join(
            f"  - {n}: {s['meta'].get('description', '-')}"
            for n, s in self.skills.items()
        )

    def load(self, name: str) -> str:
        """加载指定技能的完整指令内容，用 <skill> 标签包裹返回。

        标签帮助模型区分"系统指令"和"对话内容"。找不到时返回可用技能列表。
        """
        s = self.skills.get(name)  # dict.get 找不到返回 None
        if not s:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        return f"<skill name=\"{name}\">\n{s['body']}\n</skill>"


# ═══════════════════════════════════════════════════════════════════════════════
# 上下文压缩机制
# ═══════════════════════════════════════════════════════════════════════════════
# 模型每次对话有上下文窗口限制，对话越来越长会挤掉最老的内容。
# 压缩 = 把旧的对话"浓缩"成摘要。
#
# 【三级策略——由轻到重】
#   第1级: microcompact  — 只压缩旧工具结果（不调API，极快）
#   第2级: auto_compact  — 调 LLM 对整个对话做智能摘要（较慢但有深度）
#   第3级: 手动 /compact — 用户主动在 REPL 触发
# ═══════════════════════════════════════════════════════════════════════════════

def estimate_tokens(messages: list) -> int:
    """粗略估算消息列表的 token 数量。

    真正的 tokenizer 计算要调 API，慢且有成本；JSON 序列化长度除以 4
    是经验公式（1 个英文 token ≈ 4 个字符），精度够用。
    """
    return len(json.dumps(messages, default=str)) // 4


def microcompact(messages: list):  # type: ignore
    """轻量级上下文压缩：把旧工具结果替换为简短摘要，不调 API。

    第1步: 收集所有 user 消息中的 tool_result 块
    第2步: 保留最近 KEEP_RECENT(3) 个完整结果（最近的更重要）
    第3步: 更早的结果超过 100 字符则替换为 "[Previous: used {工具名}]"
    read_file 的结果永不压缩（读到的代码内容不能丢）。

    原地修改 messages 而非返回新列表（复制几千条很费内存）。
    """
    # 第1步：收集所有 tool_result 块（tool_result 出现在 user 角色的 list 内容中）
    tool_results = []
    for msg in messages:
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for part in msg["content"]:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    tool_results.append(part)

    # 结果不足保留数 → 不用压缩
    if len(tool_results) <= KEEP_RECENT:
        return

    # 第2步：建立 tool_use_id → tool_name 映射
    # 工具结果里只有 id 没有名字，需从 assistant 的 tool_use block 找对应关系
    tool_name_map = {}
    for msg in messages:
        if msg["role"] == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if hasattr(block, "type") and block.type == "tool_use":
                        tool_name_map[block.id] = block.name

    # 第3步：替换旧的工具结果为摘要
    # tool_results[:-KEEP_RECENT] = 除最后 3 个之外的所有结果
    for part in tool_results[:-KEEP_RECENT]:
        # 太短的内容（<=100字符）不需要压缩
        if not isinstance(part.get("content"), str) or len(part["content"]) <= 100:
            continue

        tool_id = part.get("tool_use_id", "")
        tool_name = tool_name_map.get(tool_id, "unknown")

        # read_file 等关键工具不压缩
        if tool_name in PRESERVE_RESULT_TOOLS:
            continue

        # 替换为简短标记，保留"用了什么工具"的信息
        part["content"] = f"[Previous: used {tool_name}]"


def _chunk_messages(messages: list, max_chars: int) -> list[list]:
    """将消息列表按字符数切分成多个块，每条消息保持完整不拆分。"""
    chunks = []
    current_chunk = []
    current_size = 0

    for msg in messages:
        msg_size = len(json.dumps(msg, default=str))
        # 当前块满了（且已有消息）→ 开始新块
        if current_size + msg_size > max_chars and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            current_size = 0
        current_chunk.append(msg)
        current_size += msg_size

    # 最后一个块（可能没满）
    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def _summarize_chunk(chunk: list, chunk_index: int, total_chunks: int,
                     focus: str = None, is_first: bool = False) -> str: # type: ignore
    """调用 LLM 对单个消息块生成摘要。

    第一块用完整的长 prompt 做结构化摘要，后续块用简短 prompt 只做续接式摘要。
    """
    conv_text = json.dumps(chunk, default=str)[:CHUNK_CHARS]
    position = f"Part {chunk_index + 1} of {total_chunks} of the conversation."

    if is_first:
        # 第一块：完整结构化摘要
        prompt = (
            f"{position}\n"
            "Summarize this conversation PART. Structure your summary:\n"
            "1) Task overview: core request, success criteria, constraints\n"
            "2) Current state: completed work, files touched, artifacts created\n"
            "3) Key decisions and discoveries: constraints, errors, failed approaches\n"
            "4) Next steps: remaining actions, blockers, priority order\n"
            "5) Context to preserve: user preferences, domain details, commitments\n"
            "Be concise but preserve critical details.\n"
        )
    else:
        # 后续块：精简摘要
        prompt = (
            f"{position} "
            "Summarize the key new developments, decisions, errors, file changes, "
            "and context changes in this part. Be concise.\n"
        )

    if focus:
        prompt += f"\nPay special attention to: {focus}\n"

    try:
        resp = client.messages.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt + "\n\n" + conv_text}],
            max_tokens=2000,
        )
        return resp.content[0].text or "(no summary)" # type: ignore
    except Exception as e:
        log.error(f"Chunk summary error: {e}")
        return f"(chunk {chunk_index + 1} summary failed)"


def auto_compact(messages: list, focus: str = None) -> list: # type: ignore
    """重量级上下文压缩：用 LLM 对整个对话做智能摘要，支持分块处理长对话。

    与 microcompact 的区别：microcompact 只按长度压缩工具结果，
    auto_compact 让 LLM 理解整段对话后生成结构化摘要。

    流程：备份到 .transcripts/ → 切块 → 逐块摘要 → 合并为总摘要 → 返回续接消息。
    若消息 ≤ CHUNK_CHARS 则走单次摘要（不切块，简单场景不浪费 API 调用）。
    """
    # 第1步：备份旧对话
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")

    # 第2步：判断是否需要分块
    total_chars = len(json.dumps(messages, default=str))
    if total_chars <= CHUNK_CHARS:
        # 对话不长，走单次摘要
        conv_text = json.dumps(messages, default=str)[:CHUNK_CHARS]
        prompt = (
            "Summarize this conversation for continuity. Structure your summary:\n"
            "1) Task overview: core request, success criteria, constraints\n"
            "2) Current state: completed work, files touched, artifacts created\n"
            "3) Key decisions and discoveries: constraints, errors, failed approaches\n"
            "4) Next steps: remaining actions, blockers, priority order\n"
            "5) Context to preserve: user preferences, domain details, commitments\n"
            "Be concise but preserve critical details.\n"
        )
        if focus:
            prompt += f"\nPay special attention to: {focus}\n"
        resp = client.messages.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt + "\n" + conv_text}],
            max_tokens=4000,
        )
        summary = resp.content[0].text # type: ignore
    else:
        # 第3步：分块摘要（长对话优化）
        chunks = _chunk_messages(messages, CHUNK_CHARS)
        total_chunks = len(chunks)
        log.info(f"Chunked compact: {total_chunks} chunks ({total_chars} chars)")

        chunk_summaries = []
        for i, chunk in enumerate(chunks):
            log.info(f"  Summarizing chunk {i + 1}/{total_chunks} ({len(chunk)} msgs)")
            s = _summarize_chunk(chunk, i, total_chunks, focus=focus, is_first=(i == 0))
            chunk_summaries.append(f"## Part {i + 1} Summary\n{s}")

        # 第4步：合并摘要
        combined = "\n\n".join(chunk_summaries)
        merge_prompt = (
            f"The following are summaries of {total_chunks} parts of a long conversation. "
            "Merge them into a single coherent summary. "
            "Structure the final summary:\n"
            "1) Task overview: core request, success criteria, constraints\n"
            "2) Current state: completed work, files touched, artifacts created\n"
            "3) Key decisions and discoveries: constraints, errors, failed approaches\n"
            "4) Next steps: remaining actions, blockers, priority order\n"
            "5) Context to preserve: user preferences, domain details, commitments\n"
            "Be concise but preserve critical details.\n"
        )
        try:
            resp = client.messages.create(
                model=MODEL,
                messages=[{"role": "user", "content": merge_prompt + "\n\n" + combined}],
                max_tokens=4000,
            )
            summary = resp.content[0].text # type: ignore
        except Exception as e:
            log.error(f"Merge summary failed: {e}, using concatenated summaries")
            summary = combined

    # 第5步：构建"续接"消息
    continuation = (
        "This session is being continued from a previous conversation that ran out "
        "of context. The summary below covers the earlier portion of the conversation.\n\n"
        f"{summary}\n\n"
        "Please continue the conversation from where we left it off without asking "
        "the user any further questions."
    )

    return [
        {"role": "user", "content": continuation},
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# MemoryManager — 跨会话记忆系统
# ═══════════════════════════════════════════════════════════════════════════════
# 记忆不同于对话历史：它是有意保存的重要信息，多轮对话后仍存在。
#
# 四种记忆类型：
#   user      = 用户偏好 ("我喜欢 Python")
#   feedback  = 用户纠正 ("别用 print，用 logging")
#   project   = 项目知识 ("这个项目用了 FastAPI")
#   reference = 外部资源 ("文档在 https://...")
#
# 存储格式：.memory/{name}.md —— Markdown + YAML frontmatter
#   ---
#   name: logging-preference
#   description: User prefers structured logging
#   type: feedback
#   ---
#   User said to always use logging module instead of print().
# ═══════════════════════════════════════════════════════════════════════════════

class MemoryManager:
    """管理跨会话的持久化记忆。"""

    def __init__(self, memory_dir: Path = None): # type: ignore
        self.memory_dir = memory_dir or MEMORY_DIR
        self.memories = {}  # {记忆名: {"description":..., "type":..., "content":..., "file":...}}

    def load_all(self):
        """从 .memory/ 目录加载所有记忆文件（MEMORY.md 索引文件除外）。

        每个 .md 文件 = 一个记忆，解析 frontmatter 获取元数据，正文是记忆内容。
        """
        self.memories = {}
        if not self.memory_dir.exists():
            return

        for md_file in sorted(self.memory_dir.glob("*.md")):
            if md_file.name == "MEMORY.md":
                continue  # 跳过索引文件
            parsed = self._parse_frontmatter(md_file.read_text())
            if parsed:
                name = parsed.get("name", md_file.stem)  # stem=无后缀文件名
                self.memories[name] = {
                    "description": parsed.get("description", ""),
                    "type": parsed.get("type", "project"),
                    "content": parsed.get("content", ""),
                    "file": md_file.name,
                }

        count = len(self.memories)
        if count > 0:
            log.info(f"Loaded {count} memories from {self.memory_dir}")

    def load_memory_prompt(self) -> str:
        """把所有记忆格式化为 system prompt 可用的文本，按类型分组用 Markdown 分层。"""
        if not self.memories:
            return ""

        sections = ["# Memories (persistent across sessions)", ""]
        # 按 MEMORY_TYPES 顺序遍历（user → feedback → project → reference）
        for mem_type in MEMORY_TYPES:
            # 字典推导式筛选出当前类型的记忆
            typed = {k: v for k, v in self.memories.items() if v["type"] == mem_type}
            if not typed:
                continue

            sections.append(f"## [{mem_type}]")
            for name, mem in typed.items():
                sections.append(f"### {name}: {mem['description']}")
                if mem["content"].strip():
                    sections.append(mem["content"].strip())
                sections.append("")
        return "\n".join(sections)

    def save_memory(self, name: str, description: str, mem_type: str, content: str) -> str:
        """保存一条新记忆（或覆盖同名记忆）。

        步骤：校验类型 → 清理名称 → 写 .memory/{safe_name}.md → 更新内存 → 重建索引。
        """
        # 校验记忆类型
        if mem_type not in MEMORY_TYPES:
            return f"Error: type must be one of {MEMORY_TYPES}"

        # 清理名称：只保留安全字符，转为小写
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", name.lower())
        if not safe_name:
            return "Error: invalid memory name"

        self.memory_dir.mkdir(parents=True, exist_ok=True)

        # 构建 frontmatter + content
        frontmatter = f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n{content}\n"
        file_name = f"{safe_name}.md"
        file_path = self.memory_dir / file_name
        file_path.write_text(frontmatter)

        # 更新内存缓存并重建索引
        self.memories[name] = {"description": description, "type": mem_type, "content": content, "file": file_name}
        self._rebuild_index()

        log.info(f"Saved memory '{name}' [{mem_type}]")
        return f"Saved memory '{name}' [{mem_type}] to {file_path.relative_to(WORKDIR)}"

    def _rebuild_index(self):
        """重建 MEMORY.md 索引文件，便于用户快速浏览所有记忆摘要。"""
        lines = ["# Memory Index", ""]
        for name, mem in self.memories.items():
            lines.append(f"- {name}: {mem['description']} [{mem['type']}]")
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        MEMORY_INDEX.write_text("\n".join(lines) + "\n")

    def _parse_frontmatter(self, text: str) -> dict | None:
        """解析记忆文件的 YAML frontmatter 和正文，返回
        {"name":..., "description":..., "type":..., "content":...}，无 frontmatter 返回 None。
        """
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
        if not match:
            return None
        header, body = match.group(1), match.group(2)
        result = {"content": body.strip()}
        # 逐行解析 key: value 格式
        for line in header.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                result[key.strip()] = value.strip()
        return result


# ═══════════════════════════════════════════════════════════════════════════════
# TaskManager — 持久化文件任务系统
# ═══════════════════════════════════════════════════════════════════════════════
# 把任务存到磁盘 JSON 文件，支持状态跟踪、认领分配、依赖关系。
# 不同于 TodoManager（内存便签纸），这是正式的"项目任务工单"。
#
# 【任务状态流转】
#   pending ──认领──► in_progress ──完成──► completed
#                              └──强制──► deleted（删除文件）
#
# 【文件结构】.tasks/task_1.json、.tasks/task_2.json ...
#
# 【依赖关系】
#   blockedBy = "我依赖谁"（task3 blockedBy task1 = task1 完成后 task3 才能开始）
#   blocks    = "我阻塞谁"（双向引用）
#   任务完成时自动清理所有被它阻塞任务的 blockedBy 引用。
# ═══════════════════════════════════════════════════════════════════════════════

class TaskManager:
    """管理持久化的任务板。

    与 TodoManager 的区别：Todo 在内存（关了就没），Task 在磁盘（重启还在）。
    """

    def __init__(self):
        """确保 .tasks/ 目录存在。"""
        TASKS_DIR.mkdir(exist_ok=True)

    def _next_id(self) -> int:
        """扫描 task_*.json 提取最大 ID + 1。删除不会导致 ID 重复（只增不减）。"""
        ids = [int(f.stem.split("_")[1]) for f in TASKS_DIR.glob("task_*.json")]
        # max 的 default=0 表示目录为空时从 1 开始
        return max(ids, default=0) + 1

    def _load(self, tid: int) -> dict:
        """从磁盘加载指定 ID 的任务字典；文件不存在时抛出 ValueError。"""
        p = TASKS_DIR / f"task_{tid}.json"
        if not p.exists():
            raise ValueError(f"Task {tid} not found")
        return json.loads(p.read_text())

    def _save(self, task: dict):
        """把任务保存到磁盘。indent=2 让 JSON 人类可读。"""
        (TASKS_DIR / f"task_{task['id']}.json").write_text(json.dumps(task, indent=2))

    def create(self, subject: str, description: str = "") -> str:
        """创建新任务，状态初始为 pending，返回 JSON 详情。"""
        task = {"id": self._next_id(), "subject": subject, "description": description,
                "status": "pending", "owner": None, "blockedBy": [], "blocks": []}
        self._save(task)
        return json.dumps(task, indent=2)

    def get(self, tid: int) -> str:
        """获取指定 ID 的任务详情（JSON 格式）。"""
        return json.dumps(self._load(tid), indent=2)

    def update(self, tid: int, status: str = None, # type: ignore
               add_blocked_by: list = None, add_blocks: list = None) -> str: # type: ignore
        """更新任务的状态或依赖关系。

        - status="completed" → 解锁所有被本任务阻塞的任务
        - status="deleted"   → 删除磁盘文件
        - set() 去重添加依赖，避免重复 ID
        """
        task = self._load(tid)

        # 状态更新
        if status:
            task["status"] = status

            # completed 特殊处理：解锁被阻塞的任务
            if status == "completed":
                for f in TASKS_DIR.glob("task_*.json"):
                    t = json.loads(f.read_text())
                    # 该任务被 tid 阻塞 → 从 blockedBy 移除 tid
                    if tid in t.get("blockedBy", []):
                        t["blockedBy"].remove(tid)
                        self._save(t)

            # deleted 特殊处理：删除磁盘文件
            if status == "deleted":
                (TASKS_DIR / f"task_{tid}.json").unlink(missing_ok=True)
                return f"Task {tid} deleted"

        # 依赖关系更新
        if add_blocked_by:
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        if add_blocks:
            task["blocks"] = list(set(task["blocks"] + add_blocks))

        self._save(task)
        return json.dumps(task, indent=2)

    def list_all(self) -> str:
        """列出所有任务，格式化为人类可读文本（含状态、owner、依赖）。"""
        tasks = [json.loads(f.read_text()) for f in sorted(TASKS_DIR.glob("task_*.json"))]
        if not tasks:
            return "No tasks."

        lines = []
        for t in tasks:
            m = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
            owner = f" @{t['owner']}" if t.get("owner") else ""
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            lines.append(f"{m} #{t['id']}: {t['subject']}{owner}{blocked}")

        return "\n".join(lines)

    def claim(self, tid: int, owner: str) -> str:
        """认领任务：标记为 in_progress 并设置 owner。任何 agent 都可以认领未分配任务。"""
        task = self._load(tid)
        task["owner"] = owner
        task["status"] = "in_progress"
        self._save(task)
        return f"Claimed task #{tid} for {owner}"


# ═══════════════════════════════════════════════════════════════════════════════
# BackgroundManager — 后台任务管理器
# ═══════════════════════════════════════════════════════════════════════════════
# 有些命令可能执行很久（npm install、docker build），同步等待会让 Agent 卡住。
# 把耗时命令放到独立线程执行，完成后通过 Queue 通知主循环。
#
# 通信方式：后台线程完成 → 通知 put 进 Queue → 主循环 drain 取走通知。
# daemon 线程在主程序退出时自动结束。
# ═══════════════════════════════════════════════════════════════════════════════

class BackgroundManager:
    """管理后台异步执行的命令。

    数据流：主循环 run("npm install") → 新线程执行 → 结果放入通知队列
    → 主循环 drain() 取出注入对话历史。
    """

    def __init__(self):
        self.tasks = {}                       # {task_id: {"status":"running", ...}}
        self.notifications = Queue()          # 线程安全队列，通知主循环

    def run(self, command: str, timeout: int = 120) -> str:
        """启动一个后台任务，返回任务 ID。

        daemon=True: 守护线程，主程序退出时自动结束。
        """
        tid = str(uuid.uuid4())[:8]  # 随机 ID 取前 8 位
        self.tasks[tid] = {"status": "running", "command": command, "result": None}

        threading.Thread(target=self._exec, args=(tid, command, timeout), daemon=True).start()

        return f"Background task {tid} started: {command[:80]}{'...' if len(command) > 80 else ''}"

    def _exec(self, tid: str, command: str, timeout: int):
        """在后台线程中执行命令（内部方法，由 Thread 调用），完成后放入通知队列。"""
        try:
            r = subprocess.run(command, shell=True, cwd=WORKDIR,
                               capture_output=True, text=True, timeout=timeout)
            output = (r.stdout + r.stderr).strip()[:50000]  # 截断到 5 万字符
            self.tasks[tid].update({"status": "completed", "result": output or "(no output)"})
        except Exception as e:
            self.tasks[tid].update({"status": "error", "result": str(e)})

        # 通知队列附带 task_id，方便主循环识别
        self.notifications.put({
            "task_id": tid,
            "status": self.tasks[tid]["status"],
            "result": self.tasks[tid]["result"][:500]  # 通知也截断到 500 字符
        })

    def check(self, tid: str = None) -> str: # type: ignore
        """查询后台任务状态；tid 为 None 时列出所有任务。"""
        if tid:
            t = self.tasks.get(tid)
            return f"[{t['status']}] {t.get('result') or '(running)'}" if t else f"Unknown: {tid}"

        # 列出所有任务
        if not self.tasks:
            return "No bg tasks."
        return "\n".join(f"{k}: [{v['status']}] {v['command'][:60]}" for k, v in self.tasks.items())

    def drain(self) -> list:
        """一次性清空通知队列，返回所有积压通知。

        用 get_nowait 而非 get：get 会阻塞主循环，get_nowait 队列空时抛 Empty 立即返回。
        """
        notifs = []
        while True:
            try:
                notifs.append(self.notifications.get_nowait())
            except Empty:
                break  # 队列空了
        return notifs

    def has_notifications(self) -> bool:
        """是否有已完成但尚未被消费的后台任务通知（REPL 用来自动唤醒 agent_loop）。"""
        return not self.notifications.empty()


# ═══════════════════════════════════════════════════════════════════════════════
# MessageBus — 消息总线
# ═══════════════════════════════════════════════════════════════════════════════
# 队友之间通过"文件邮箱"通信：
#   发送 = 往接收者的 .team/inbox/{name}.jsonl 末尾追加一行 JSON
#   收取 = 读取文件内容并清空文件（读完即删，防止重复处理）
# JSONL = 每行一个独立的 JSON 对象，方便追加和批量处理。
#
# 消息类型：
#   message / broadcast / shutdown_request / shutdown_response
#   plan_approval / plan_approval_response
# ═══════════════════════════════════════════════════════════════════════════════

class MessageBus:
    """队友之间的消息传递系统。

    不用线程间共享变量：队友不一定是线程（也可能是独立进程/远程服务），
    文件邮箱方式"最笨但最可靠"——只要文件系统正常，消息就能送达。
    """

    def __init__(self):
        """确保收件箱目录存在。"""
        INBOX_DIR.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str: # type: ignore
        """发送一条消息给指定队友（追加模式写入目标文件一行 JSON）。

        extra 为额外字段字典（如 {"request_id":"abc123"}），会合并到消息中。
        """
        msg = {"type": msg_type, "from": sender, "content": content,
               "timestamp": time.time()}
        # 追加额外字段
        if extra:
            msg.update(extra)

        # "a" 模式 = append 追加，文件不存在则创建
        with open(INBOX_DIR / f"{to}.jsonl", "a") as f:
            f.write(json.dumps(msg) + "\n")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list:
        """读取并清空指定队友的收件箱。

        读而不清会导致每轮循环重复处理同一条消息；读完立即清空是原子操作。
        """
        path = INBOX_DIR / f"{name}.jsonl"
        if not path.exists():
            return []

        # 逐行解析 JSON（跳过空行）
        msgs = [json.loads(l) for l in path.read_text().strip().splitlines() if l]
        path.write_text("")  # 清空文件
        return msgs

    def has_pending(self, name: str) -> bool:
        """指定收件人是否有未读消息（文件有非空内容即代表有未读消息）。"""
        path = INBOX_DIR / f"{name}.jsonl"
        if not path.exists():
            return False
        return bool(path.read_text().strip())

    def broadcast(self, sender: str, content: str, names: list) -> str:
        """向多个队友广播同一条消息（循环调用 send，跳过自己）。"""
        count = 0
        for n in names:
            if n != sender:  # 不给自己发
                self.send(sender, n, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"


# ====================================================================
# 关闭协议 & 计划审批追踪
#
# shutdown_requests: {request_id: {"target": "队友名", "status": "pending/acknowledged"}}
# plan_requests:     {request_id: {"from": "队友名", "description": "...", "status": "..."}}
# ====================================================================

shutdown_requests = {}
plan_requests = {}


# ═══════════════════════════════════════════════════════════════════════════════
# TeammateManager — 队友管理器
# ═══════════════════════════════════════════════════════════════════════════════
# 队友(Teammate) = 持久在后台运行的 Agent 线程，有自己的循环和收件箱。
# 子代理(Subagent) = 一次性任务，执行完就消失。
#
# 【队友状态机——生命周期】
#   spawn → WORKING → IDLE → SHUTDOWN
#   IDLE ←─────────────┘（有新任务回到 WORKING）
#
# 【两个阶段】
#   1. WORK PHASE: 调 API 做任务，最多 50 轮；模型调 idle 或不再调工具则结束
#   2. IDLE PHASE: 每 5 秒轮询收件箱和未认领任务，IDLE_TIMEOUT(60s) 内无事则退出
# ═══════════════════════════════════════════════════════════════════════════════

class TeammateManager:
    """管理多个队友 Agent 的生命周期。

    配置文件 .team/config.json：
      {"team_name": "编码团队",
       "members": [{"name": "bob", "role": "后端开发", "status": "working"}, ...]}

    设计：每个队友 = 一个独立 daemon 线程；通过 MessageBus 收消息；
    可自主扫描任务板认领未分配任务。
    """
    def __init__(self, bus: MessageBus, task_mgr: TaskManager):
        TEAM_DIR.mkdir(exist_ok=True)
        self.bus = bus
        self.task_mgr = task_mgr
        self.config_path = TEAM_DIR / "config.json"
        self.config = self._load()  # 加载团队配置
        self.threads = {}  # 存放线程引用（预留扩展）

    def _load(self) -> dict:
        """从 .team/config.json 加载配置；文件不存在时返回默认配置。"""
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save(self):
        """保存团队配置到文件（JSON 格式化输出）。"""
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def _find(self, name: str) -> dict:
        """在成员列表中查找指定名称的队友，返回成员字典或 None。"""
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None  # type: ignore

    def spawn(self, name: str, role: str, prompt: str) -> str:
        """生成（创建/唤醒）一个队友并启动工作线程。

        三种情况：
        1. 队友不存在 → 创建新成员
        2. 队友空闲/已关闭 → 重新激活为 working
        3. 队友工作中 → 返回错误（不能同时做两件事）
        """
        member = self._find(name)
        if member:
            # 已存在队友，检查状态
            if member["status"] not in ("idle", "shutdown"):
                return f"Error: '{name}' is currently {member['status']}"
            # 重新激活队友
            member["status"] = "working"
            member["role"] = role
        else:
            # 新建队友
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)

        self._save()

        # daemon=True: 主程序退出时自动结束队友线程
        threading.Thread(target=self._loop, args=(name, role, prompt), daemon=True).start()
        return f"Spawned '{name}' (role: {role})"

    def _set_status(self, name: str, status: str):
        """更新队友状态并保存到配置文件。"""
        member = self._find(name)
        if member:
            member["status"] = status
            self._save()

    def _loop(self, name: str, role: str, prompt: str):
        """队友主循环（在独立线程中运行）。

        状态机：WORKING ──(idle/完成)──► IDLE ──(新消息/任务)──► WORKING
                IDLE ──(60s 超时)──► SHUTDOWN；任意 ──(shutdown_request)──► SHUTDOWN

        WORK PHASE 最多 50 轮，防止模型无限循环调工具；
        IDLE PHASE 每 5 秒轮询一次，最多 IDLE_TIMEOUT//POLL_INTERVAL 轮。
        """
        # 初始化：系统提示词 + 消息历史
        team_name = self.config["team_name"]
        sys_prompt = (f"You are '{name}', role: {role}, team: {team_name}, at {WORKDIR}. "
                      f"Use idle when done with current work. You may auto-claim tasks.")

        messages = [{"role": "user", "content": prompt}]

        # 队友工具列表（比主 Agent 少，因为队友只管干活）
        tools = [
            {"name": "bash", "description": "执行 shell 命令。", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
            {"name": "read_file", "description": "读取文件。", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            {"name": "write_file", "description": "写入文件。", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "精确替换文件文本。", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
            {"name": "send_message", "description": "向队友发送消息。", "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}}, "required": ["to", "content"]}},
            {"name": "idle", "description": "通知没有更多工作要做。", "input_schema": {"type": "object", "properties": {}}},
            {"name": "claim_task", "description": "按 ID 从任务板认领任务。", "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
        ]

        # 外层循环：WORK PHASE ↔ IDLE PHASE 来回切换
        while True:
            # ────── WORK PHASE ──────
            silent_rounds = 0          # 连续只调工具、未产出文本的轮数
            MAX_SILENT_ROUNDS = 8      # 超过则注入收敛提示，防止无限 bash 刷屏
            for _ in range(50):  # 最多 50 轮
                # ① 检查收件箱
                inbox = self.bus.read_inbox(name)
                for msg in inbox:
                    # shutdown_request 最高优先级——立即退出
                    if msg.get("type") == "shutdown_request":
                        self._set_status(name, "shutdown")
                        return
                    messages.append({"role": "user", "content": json.dumps(msg)})

                # ② 调用 API
                try:
                    response = client.messages.create(
                        model=MODEL, system=sys_prompt, messages=messages, # type: ignore
                        tools=tools, max_tokens=8000) # type: ignore
                except Exception:
                    # API 错误 → 队友无法正常工作 → 退出
                    self._set_status(name, "shutdown")
                    return

                messages.append({"role": "assistant", "content": response.content}) # type: ignore

                # ③ 判断是否继续
                if response.stop_reason != "tool_use":
                    break  # 模型给了最终答案 → 进入 IDLE

                # 模型只调工具没给文本 → 累计空转轮数；给出了文本则清零
                has_text = any(getattr(b, "text", None) for b in response.content)
                silent_rounds = 0 if has_text else silent_rounds + 1

                # ④ 执行工具调用
                results = []
                idle_requested = False
                for block in response.content:
                    if block.type == "tool_use":
                        # idle 工具：模型主动说"活干完了"
                        if block.name == "idle":
                            idle_requested = True
                            output = "Entering idle phase."
                        # claim_task：认领任务板上的任务
                        elif block.name == "claim_task":
                            output = self.task_mgr.claim(block.input["task_id"], name) # type: ignore
                        # send_message：给其他人发消息
                        elif block.name == "send_message":
                            output = self.bus.send(name, block.input["to"], block.input["content"]) # type: ignore
                        # 文件操作工具
                        else:
                            dispatch = {"bash": lambda **kw: run_bash(kw["command"]),
                                        "read_file": lambda **kw: run_read(kw["path"]),
                                        "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
                                        "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"])}
                            output = dispatch.get(block.name, lambda **kw: "Unknown")(**block.input)

                        print(f"  [{name}] {block.name}: {str(output)[:120]}")
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})

                messages.append({"role": "user", "content": results}) # type: ignore

                # 连续空转过多 → 注入收敛提示，让模型停下并总结，而不是无限 bash
                if silent_rounds >= MAX_SILENT_ROUNDS:
                    messages.append({"role": "user", "content":
                        "You have been calling tools repeatedly without producing a summary. "
                        "Stop now: give one concise final answer describing what you did."})
                    silent_rounds = 0

                # idle 被触发 → 跳出工作阶段，进入 IDLE
                if idle_requested:
                    break

            # ────── IDLE PHASE ──────
            self._set_status(name, "idle")
            resume = False

            # 轮询循环：最多 IDLE_TIMEOUT//POLL_INTERVAL = 60//5 = 12 轮
            for _ in range(IDLE_TIMEOUT // max(POLL_INTERVAL, 1)):
                time.sleep(POLL_INTERVAL)

                # 轮询 A: 检查收件箱
                inbox = self.bus.read_inbox(name)
                if inbox:
                    for msg in inbox:
                        if msg.get("type") == "shutdown_request":
                            self._set_status(name, "shutdown")
                            return
                        messages.append({"role": "user", "content": json.dumps(msg)})
                    resume = True
                    break

                # 轮询 B: 扫描任务板
                unclaimed = []
                for f in sorted(TASKS_DIR.glob("task_*.json")):
                    t = json.loads(f.read_text())
                    # 三个条件同时满足：pending + 无人认领 + 无依赖阻塞
                    if t.get("status") == "pending" and not t.get("owner") and not t.get("blockedBy"):
                        unclaimed.append(t)

                if unclaimed:
                    task = unclaimed[0]
                    self.task_mgr.claim(task["id"], name)

                    # 身份重注入：防止对话被压缩后队友"忘了自己是谁"
                    if len(messages) <= 3:
                        messages.insert(0, {"role": "user", "content":
                            f"<identity>You are '{name}', role: {role}, team: {team_name}.</identity>"})
                        messages.insert(1, {"role": "assistant", "content": f"I am {name}. Continuing."})

                    messages.append({"role": "user", "content":
                        f"<auto-claimed>Task #{task['id']}: {task['subject']}\n{task.get('description', '')}</auto-claimed>"})
                    messages.append({"role": "assistant", "content": f"Claimed task #{task['id']}. Working on it."})
                    resume = True
                    break

            # 超时：无事可做 → 退出
            if not resume:
                self._set_status(name, "shutdown")
                return

            self._set_status(name, "working")

    def list_all(self) -> str:
        """列出所有队友及其状态。"""
        if not self.config["members"]:
            return "No teammates."
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        """返回所有队友的名称列表。"""
        return [m["name"] for m in self.config["members"]]


# ═══════════════════════════════════════════════════════════════════════════════
# HookManager — 钩子系统
# ═══════════════════════════════════════════════════════════════════════════════
# 钩子 = 在特定事件发生时自动执行的脚本（类似事件监听器）。
#
# 三种钩子事件：
#   PreToolUse   = 工具调用前触发，可拦截阻止（returncode=1）
#   PostToolUse  = 工具调用后触发，记录日志/发通知（returncode=2）
#   SessionStart = 会话启动时触发，做初始化
#
# 钩子脚本返回值约定：0=通过，1=阻止执行，2=发送消息
#
# 工作区信任机制：钩子有任意代码执行能力，只有存在 .claude_trusted
# 标记文件的受信工作区才执行，防止恶意 .hooks.json 植入危险脚本。
# ═══════════════════════════════════════════════════════════════════════════════

class HookManager:
    """管理工具调用前后的钩子脚本。

    配置文件 .hooks.json：
      {"hooks": {
        "PreToolUse": [{"matcher": "bash", "command": "./hooks/check-bash.sh"}],
        "PostToolUse": [{"matcher": "*", "command": "./hooks/log-tool.sh"}]
      }}
    matcher 匹配工具名，"*" 表示匹配所有工具。
    """

    def __init__(self, config_path: Path = None, sdk_mode: bool = False): # type: ignore
        # 初始化三种事件的钩子列表
        self.hooks = {"PreToolUse": [], "PostToolUse": [], "SessionStart": []}

        # SDK 模式不需要信任标记（已由环境验证）
        self._sdk_mode = sdk_mode

        config_path = config_path or (WORKDIR / ".hooks.json")
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text())
                for event in HOOK_EVENTS:
                    self.hooks[event] = config.get("hooks", {}).get(event, [])
                log.info(f"Hooks loaded from {config_path}")
            except Exception as e:
                log.error(f"Hook config error: {e}")

    def _check_workspace_trust(self) -> bool:
        """检查工作区是否可信（存在 .claude_trusted 标记文件）。"""
        if self._sdk_mode:
            return True  # SDK 模式不需要检查
        return TRUST_MARKER.exists()

    def run_hooks(self, event: str, context: dict = None) -> dict: # type: ignore
        """运行指定事件的所有钩子脚本，返回 {"blocked":..., "messages":[...], ...}。

        钩子通过环境变量获取上下文：HOOK_EVENT / HOOK_TOOL_NAME /
        HOOK_TOOL_INPUT（JSON，最多 10000 字符）/ HOOK_TOOL_OUTPUT（仅 PostToolUse）。
        """
        result = {"blocked": False, "messages": []}

        # 安全检查：工作区不可信就不执行钩子
        if not self._check_workspace_trust():
            return result

        hooks = self.hooks.get(event, [])
        for hook_def in hooks:
            # matcher 匹配：钩子只对特定工具有效
            matcher = hook_def.get("matcher")
            if matcher and context:
                tool_name = context.get("tool_name", "")
                if matcher != "*" and matcher != tool_name:
                    continue  # matcher 不匹配，跳过

            command = hook_def.get("command", "")
            if not command:
                continue

            # 设置环境变量，传递上下文给钩子脚本
            env = dict(os.environ)
            if context:
                env["HOOK_EVENT"] = event
                env["HOOK_TOOL_NAME"] = context.get("tool_name", "")
                env["HOOK_TOOL_INPUT"] = json.dumps(context.get("tool_input", {}), ensure_ascii=False)[:10000]
                if "tool_output" in context:
                    env["HOOK_TOOL_OUTPUT"] = str(context["tool_output"])[:10000]

            try:
                # 执行钩子脚本（shell=True 支持管道和变量）
                r = subprocess.run(command, shell=True, cwd=WORKDIR, env=env,
                                   capture_output=True, text=True, timeout=HOOK_TIMEOUT)

                if r.returncode == 0:
                    # 钩子成功通过
                    if r.stdout.strip():
                        log.info(f"[hook:{event}] {r.stdout.strip()[:100]}")

                elif r.returncode == 1:
                    # 钩子阻止执行（PreToolUse用）
                    result["blocked"] = True
                    result["block_reason"] = r.stderr.strip() or "Blocked by hook"
                    log.warning(f"[hook:{event}] BLOCKED: {result['block_reason'][:200]}")

                elif r.returncode == 2:
                    # 钩子发送消息通知（PostToolUse用）
                    msg = r.stderr.strip()
                    if msg:
                        result["messages"].append(msg)

            except subprocess.TimeoutExpired:
                log.warning(f"[hook:{event}] Timeout ({HOOK_TIMEOUT}s)")
            except Exception as e:
                log.error(f"[hook:{event}] Error: {e}")

        return result


# ═══════════════════════════════════════════════════════════════════════════════
# CronScheduler — 定时任务调度
# ═══════════════════════════════════════════════════════════════════════════════
# Cron 表达式（5字段）：分钟 小时 日 月 星期
#   例: "0 9 * * 1-5" = 每个工作日早上 9 点
#   例: "*/30 * * * *" = 每 30 分钟
#
# 单次 vs 重复 vs 持久化：
#   recurring=True:  重复执行（如每天提醒）
#   recurring=False: 只执行一次
#   durable=True:    持久化到磁盘（重启后仍存在）
# ═══════════════════════════════════════════════════════════════════════════════

def cron_matches(expr: str, dt: datetime) -> bool:
    """判断给定时间是否匹配 cron 表达式（5字段）。"""
    fields = expr.strip().split()
    if len(fields) != 5:
        return False

    # 时间值按顺序：分钟, 小时, 日, 月, 星期（0=周日，这里转换后用0-6）
    values = [dt.minute, dt.hour, dt.day, dt.month, (dt.weekday() + 1) % 7]
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]

    for field, value, (lo, hi) in zip(fields, values, ranges):
        if not _field_matches(field, value, lo, hi):
            return False
    return True


def _field_matches(field: str, value: int, lo: int, hi: int) -> bool:
    """判断单个 cron 字段是否匹配。支持：* 通配、/ 步长、- 范围、, 并列。"""
    if field == "*":
        return True
    for part in field.split(","):  # 多个值逗号分隔
        step = 1
        if "/" in part:
            part, step_str = part.split("/", 1)
            step = int(step_str)
        if part == "*":
            if (value - lo) % step == 0:
                return True
        elif "-" in part:
            start, end = int(part.split("-", 1)[0]), int(part.split("-", 1)[1])
            if start <= value <= end and (value - start) % step == 0:
                return True
        else:
            if int(part) == value:
                return True
    return False


class CronScheduler:
    """定时任务调度器：在后台线程中按 cron 表达式触发任务。

    调度粒度：每分钟检查一次。重复任务超过 7 天未触发自动过期删除。
    通过 Queue 向主循环发送通知，无需加锁。
    """
    AUTO_EXPIRY_DAYS = 7  # 重复任务自动过期天数

    def __init__(self):
        self.tasks = []                      # 任务列表
        self.queue = Queue()                 # 通知队列（触发时放入）
        self._stop_event = threading.Event() # 停止信号
        self._thread = None                  # 后台检查线程
        self._last_check_minute = -1         # 上次检查的分钟编号（防重复触发）

    def start(self):
        """启动后台检查线程。"""
        self._load_durable()  # 加载持久化任务
        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()
        if self.tasks:
            log.info(f"Cron: Loaded {len(self.tasks)} scheduled tasks")

    def stop(self):
        """停止后台检查线程（退出前调用）。"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def create(self, cron_expr: str, prompt: str, recurring: bool = True, durable: bool = False) -> str:
        """创建一个定时任务。"""
        task_id = str(uuid.uuid4())[:8]
        task = {"id": task_id, "cron": cron_expr, "prompt": prompt,
                "recurring": recurring, "durable": durable, "createdAt": time.time()}
        self.tasks.append(task)
        if durable:
            self._save_durable()
        return f"Created task {task_id} (cron={cron_expr})"

    def delete(self, task_id: str) -> str:
        before = len(self.tasks)
        self.tasks = [t for t in self.tasks if t["id"] != task_id]
        if len(self.tasks) < before:
            self._save_durable()
            return f"Deleted task {task_id}"
        return f"Task {task_id} not found"

    def list_tasks(self) -> str:
        if not self.tasks:
            return "No scheduled tasks."
        lines = []
        for t in self.tasks:
            age_hours = (time.time() - t["createdAt"]) / 3600
            lines.append(f"  {t['id']}  {t['cron']}  ({age_hours:.1f}h): {t['prompt'][:60]}")
        return "\n".join(lines)

    def drain_notifications(self) -> list:
        """清空通知队列，返回积压的通知列表。"""
        notifications = []
        while True:
            try:
                notifications.append(self.queue.get_nowait())
            except Empty:
                break
        return notifications

    def has_pending(self) -> bool:
        """是否有已触发但尚未被消费的定时任务通知（REPL 用来自动唤醒 agent_loop）。"""
        return not self.queue.empty()

    def _check_loop(self):
        """后台检查循环：每 1 秒检查一次，分钟变化时才触发 _check_tasks。"""
        while not self._stop_event.is_set():
            now = datetime.now()
            current_minute = now.hour * 60 + now.minute  # 分钟编号 (0-1439)
            if current_minute != self._last_check_minute:
                self._last_check_minute = current_minute
                self._check_tasks(now)
            self._stop_event.wait(timeout=1)

    def _check_tasks(self, now: datetime):
        """检查所有任务，触发匹配的、清理过期的。"""
        expired, fired_oneshots = [], []
        for task in self.tasks:
            age_days = (time.time() - task["createdAt"]) / 86400
            if task["recurring"] and age_days > self.AUTO_EXPIRY_DAYS:
                expired.append(task["id"])
                continue
            if cron_matches(task["cron"], now):
                self.queue.put(f"[Scheduled task {task['id']}]: {task['prompt']}")
                log.info(f"Cron fired: {task['id']}")
                if not task["recurring"]:
                    fired_oneshots.append(task["id"])
        if expired or fired_oneshots:
            remove_ids = set(expired) | set(fired_oneshots)
            self.tasks = [t for t in self.tasks if t["id"] not in remove_ids]
            self._save_durable()

    def _load_durable(self):
        """从磁盘加载持久化任务。"""
        if not SCHEDULED_TASKS_FILE.exists():
            return
        try:
            self.tasks = [t for t in json.loads(SCHEDULED_TASKS_FILE.read_text()) if t.get("durable")]
        except Exception as e:
            log.error(f"Cron load error: {e}")

    def _save_durable(self):
        """保存持久化任务到磁盘。"""
        durable = [t for t in self.tasks if t.get("durable")]
        SCHEDULED_TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SCHEDULED_TASKS_FILE.write_text(json.dumps(durable, indent=2) + "\n")


# ═══════════════════════════════════════════════════════════════════════════════
# EventBus + WorktreeManager — 工作树隔离
# ═══════════════════════════════════════════════════════════════════════════════
# Git Worktree 在同一个仓库创建多个独立"工作目录"（不同分支），互不影响，
# 让队友在各自分支上同时干活而不踩到对方代码。
#
# EventBus: 记录工作树生命周期事件到 events.jsonl（用于审计和调试）。
# 两种关闭方式：keep=保留工作树，remove=删除工作树（放弃改动）。
# ═══════════════════════════════════════════════════════════════════════════════

class EventBus:
    """事件总线：记录工作树生命周期事件到 JSONL 文件。

    emit("event", task_id=..., wt_name=..., error=..., **extra) 追加一行 JSON，
    task_id/wt_name/error 为可选关键字段，**extra 使调用灵活。
    """

    def __init__(self, event_log_path: Path):
        self.path = event_log_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("")  # 创建空文件

    def emit(self, event: str, task_id=None, wt_name=None, error=None, **extra):
        """记录一个事件到日志文件。"""
        payload = {"event": event, "ts": time.time()}
        if task_id is not None:
            payload["task_id"] = task_id
        if wt_name:
            payload["worktree"] = wt_name
        if error:
            payload["error"] = error
        payload.update(extra)
        # "a" 模式追加到文件末尾，不覆盖已有内容
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")

    def list_recent(self, limit: int = 20) -> str:
        """列出最近 limit 条事件记录（limit 限制在 1-200）。"""
        n = max(1, min(int(limit or 20), 200))
        lines = self.path.read_text(encoding="utf-8").splitlines()
        items = []
        for line in lines[-n:]:  # 只取最后 n 行
            try:
                items.append(json.loads(line))
            except Exception:
                items.append({"event": "parse_error", "raw": line})
        return json.dumps(items, indent=2)


class WorktreeManager:
    """管理 Git Worktree 的创建、查询、操作、删除。

    索引文件 .worktrees/index.json 存储所有工作树的元数据。
    """

    def __init__(self, repo_root: Path, tasks, events: EventBus):
        self.repo_root = repo_root
        self.tasks = tasks       # TaskManager 实例（用于绑定任务和工作树）
        self.events = events     # EventBus 实例（记录事件日志）
        self.dir = repo_root / ".worktrees"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.json"
        if not self.index_path.exists():
            self.index_path.write_text(json.dumps({"worktrees": []}, indent=2))
        self.git_available = self._check_git()

    def _check_git(self) -> bool:
        """检测当前目录是否在 Git 仓库内。"""
        try:
            r = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                               cwd=self.repo_root, capture_output=True, text=True, timeout=10)
            return r.returncode == 0
        except Exception:
            return False

    def _run_git(self, args: list[str]) -> str:
        """执行 git 命令（安全封装的子进程调用）。"""
        if not self.git_available:
            raise RuntimeError("Not in a git repository")
        r = subprocess.run(["git", *args], cwd=self.repo_root, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError((r.stdout + r.stderr).strip() or f"git {args[0]} failed")
        return (r.stdout + r.stderr).strip() or "(no output)"

    def _load_index(self) -> dict:
        return json.loads(self.index_path.read_text())

    def _save_index(self, data: dict):
        self.index_path.write_text(json.dumps(data, indent=2))

    def _find(self, name: str) -> dict | None:
        for wt in self._load_index().get("worktrees", []):
            if wt.get("name") == name:
                return wt
        return None

    def _update_entry(self, name: str, **changes) -> dict:
        """更新索引中指定工作树的字段。"""
        idx = self._load_index()
        updated = None
        for item in idx.get("worktrees", []):
            if item.get("name") == name:
                item.update(changes)
                updated = item
                break
        self._save_index(idx)
        if not updated:
            raise ValueError(f"Worktree '{name}' not found in index")
        return updated

    def create(self, name: str, task_id: int = None, base_ref: str = "HEAD") -> str: # type: ignore
        """创建新的 Git 工作树。

        name 限制 1-40 字符（字母数字._-）；base_ref 指定基于哪个分支/提交创建。
        """
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", name or ""):
            raise ValueError("Invalid worktree name. Use 1-40 chars: letters, digits, ., _, -")
        if self._find(name):
            raise ValueError(f"Worktree '{name}' already exists")
        if task_id is not None and not self.tasks.exists(task_id):
            raise ValueError(f"Task {task_id} not found")
        path = self.dir / name
        branch = f"wt/{name}"  # 分支命名: wt/{工作树名}
        self.events.emit("worktree.create.before", task_id=task_id, wt_name=name)
        try:
            # git worktree add -b wt/my-task .worktrees/my-task HEAD
            self._run_git(["worktree", "add", "-b", branch, str(path), base_ref])
            entry = {"name": name, "path": str(path), "branch": branch, "task_id": task_id,
                     "status": "active", "created_at": time.time()}
            idx = self._load_index()
            idx["worktrees"].append(entry)
            self._save_index(idx)
            if task_id is not None:
                self.tasks.bind_worktree(task_id, name)
            self.events.emit("worktree.create.after", task_id=task_id, wt_name=name)
            return json.dumps(entry, indent=2)
        except Exception as e:
            self.events.emit("worktree.create.failed", task_id=task_id, wt_name=name, error=str(e))
            raise

    def list_all(self) -> str:
        """列出所有工作树。"""
        wts = self._load_index().get("worktrees", [])
        if not wts:
            return "No worktrees in index."
        lines = []
        for wt in wts:
            suffix = f" task={wt['task_id']}" if wt.get("task_id") else ""
            lines.append(f"[{wt.get('status', '?')}] {wt['name']} -> {wt['path']} ({wt.get('branch', '-')}){suffix}")
        return "\n".join(lines)

    def enter(self, name: str) -> str:
        """进入一个工作树（记录 last_entered_at 时间戳）。"""
        wt = self._find(name)
        if not wt:
            return f"Error: Unknown worktree '{name}'"
        self._update_entry(name, last_entered_at=time.time())
        self.events.emit("worktree.enter", task_id=wt.get("task_id"), wt_name=name)
        return json.dumps(self._find(name), indent=2)

    def status(self, name: str) -> str:
        """获取工作树的 git status（哪些文件被修改了）。"""
        wt = self._find(name)
        if not wt:
            return f"Error: Unknown worktree '{name}'"
        r = subprocess.run(["git", "status", "--short", "--branch"],
                           cwd=wt["path"], capture_output=True, text=True, timeout=60)
        return (r.stdout + r.stderr).strip() or "Clean worktree"

    def run(self, name: str, command: str) -> str:
        """在指定工作树目录下执行命令（有危险命令检查）。"""
        dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
        if any(d in command for d in dangerous):
            return "Error: Dangerous command blocked"
        wt = self._find(name)
        if not wt:
            return f"Error: Unknown worktree '{name}'"
        try:
            self.events.emit("worktree.run.before", task_id=wt.get("task_id"), wt_name=name, command=command[:120])
            r = subprocess.run(command, shell=True, cwd=wt["path"], capture_output=True, text=True, timeout=300)
            self.events.emit("worktree.run.after", task_id=wt.get("task_id"), wt_name=name)
            return (r.stdout + r.stderr).strip()[:50000] or "(no output)"
        except subprocess.TimeoutExpired:
            self.events.emit("worktree.run.timeout", task_id=wt.get("task_id"), wt_name=name)
            return "Error: Timeout (300s)"

    def remove(self, name: str, force: bool = False, complete_task: bool = False, reason: str = "") -> str:
        """删除一个工作树（git worktree remove），可选同时完成任务。"""
        wt = self._find(name)
        if not wt:
            return f"Error: Unknown worktree '{name}'"
        task_id = wt.get("task_id")
        self.events.emit("worktree.remove.before", task_id=task_id, wt_name=name)
        try:
            args = ["worktree", "remove"]
            if force:
                args.append("--force")
            args.append(wt["path"])
            self._run_git(args)
            if complete_task and task_id is not None:
                self.tasks.update(task_id, status="completed")
            if task_id is not None:
                self.tasks.record_closeout(task_id, "removed", reason, keep_binding=False)
            self._update_entry(name, status="removed", removed_at=time.time())
            self.events.emit("worktree.remove.after", task_id=task_id, wt_name=name)
            return f"Removed worktree '{name}'"
        except Exception as e:
            self.events.emit("worktree.remove.failed", task_id=task_id, wt_name=name, error=str(e))
            raise

    def keep(self, name: str) -> str:
        """标记工作树为保留状态（kept）。"""
        wt = self._find(name)
        if not wt:
            return f"Error: Unknown worktree '{name}'"
        if wt.get("task_id") is not None:
            self.tasks.record_closeout(wt["task_id"], "kept", "", keep_binding=True)
        self._update_entry(name, status="kept", kept_at=time.time())
        self.events.emit("worktree.keep", task_id=wt.get("task_id"), wt_name=name)
        return json.dumps(self._find(name), indent=2)

    def closeout(self, name: str, action: str, reason: str = "", force: bool = False, complete_task: bool = False) -> str:
        """统一关闭接口：action 可以是 'keep' 或 'remove'。"""
        if action == "keep":
            wt = self._find(name)
            if not wt:
                return f"Error: Unknown worktree '{name}'"
            if wt.get("task_id") is not None:
                self.tasks.record_closeout(wt["task_id"], "kept", reason, keep_binding=True)
                if complete_task:
                    self.tasks.update(wt["task_id"], status="completed")
            self._update_entry(name, status="kept", kept_at=time.time())
            self.events.emit("worktree.closeout.keep", task_id=wt.get("task_id"), wt_name=name, reason=reason)
            return json.dumps(self._find(name), indent=2)
        if action == "remove":
            self.events.emit("worktree.closeout.remove", wt_name=name, reason=reason)
            return self.remove(name, force=force, complete_task=complete_task, reason=reason)
        raise ValueError("action must be 'keep' or 'remove'")


# ═══════════════════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════════════════

def normalize_messages(messages: list) -> list:
    """将消息列表格式化为纯 dict 格式（去掉 SDK 对象属性）。

    Anthropic SDK 的 content block 是对象（有 type/id/name 等属性），
    直接 json.dumps 会包含大量私有字段（_ 开头）。此函数提取为纯 dict
    方便序列化传输（如 auto_compact 场景）。
    """
    cleaned = []
    for msg in messages:
        role = msg["role"]
        content = msg.get("content")

        if isinstance(content, str):
            # 字符串内容 → 包装成 text block
            blocks = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            blocks = []
            for blk in content:
                if isinstance(blk, dict):
                    # 过滤掉私有字段（_ 开头的 key）
                    clean_blk = {k: v for k, v in blk.items() if not k.startswith("_")}
                    blocks.append(clean_blk)
                elif hasattr(blk, "type"):
                    # SDK 对象 → 提取属性到字典
                    clean_blk = {"type": blk.type}
                    for attr in ["text", "id", "name", "input", "tool_use_id", "content"]:
                        if hasattr(blk, attr):
                            clean_blk[attr] = getattr(blk, attr)
                    blocks.append(clean_blk)
        else:
            blocks = []

        if blocks:
            cleaned.append({"role": role, "content": blocks})
    return cleaned


def scan_unclaimed_tasks(role: str = None) -> list: # type: ignore
    """扫描任务板上的未认领任务。

    未认领条件：status=pending + 无 owner + 无 blockedBy 依赖。
    role 参数可按任务要求的角色过滤（task["claim_role"]）。
    """
    TASKS_DIR.mkdir(exist_ok=True)
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(f.read_text())
        if task.get("status") == "pending" and not task.get("owner") and not task.get("blockedBy"):
            required_role = task.get("claim_role") or ""
            if not required_role or (role and role == required_role):
                unclaimed.append(task)
    return unclaimed


def claim_task(task_id: int, owner: str, role: str = None, source: str = "manual") -> str: # type: ignore
    """认领一个任务（用线程锁保证互斥）。

    多个队友同时认领同一任务时，_claim_lock 保证只有一个成功。
    source 记录认领来源："manual"（手动）或 "auto"（自动）。
    """
    with _claim_lock:  # 互斥锁保护：同一时刻只有一个线程能进入
        path = TASKS_DIR / f"task_{task_id}.json"
        if not path.exists():
            return f"Error: Task {task_id} not found"
        task = json.loads(path.read_text())
        if task.get("status") != "pending" or task.get("owner"):
            return f"Error: Task {task_id} not claimable"
        task["owner"] = owner
        task["status"] = "in_progress"
        task["claimed_at"] = time.time()
        task["claim_source"] = source
        path.write_text(json.dumps(task, indent=2))
    return f"Claimed task #{task_id} for {owner} via {source}"


def backoff_delay(attempt: int) -> float:
    """计算指数退避延迟时间（含随机抖动）。

    延迟 = min(基础 * 2^attempt, 上限) + jitter。加随机抖动是为了避免
    大量客户端同时重试导致"惊群效应"（同步涌入导致二次崩溃）。
    """
    delay = min(BACKOFF_BASE_DELAY * (2 ** attempt), BACKOFF_MAX_DELAY)
    jitter = random.uniform(0, 1)
    return delay + jitter


# ═══════════════════════════════════════════════════════════════════════════════
# 全局实例 — 单例模式
# ═══════════════════════════════════════════════════════════════════════════════
# 所有管理器只创建一次，全程序引用同一批对象。
# 若创建两个实例，各自状态（如 TodoManager.items）就不同步了。
# ═══════════════════════════════════════════════════════════════════════════════

TODO = TodoManager()                               # 内存待办列表
SKILLS = SkillLoader(SKILLS_DIR)                   # 技能加载器
MCP_ROUTER = MCPToolRouter()                        # MCP 外部工具路由器
# MCP 后续升级项（HTTP/SSE 传输、OAuth、重连、resources/prompts、
#            工具过滤、插件安装）详见 agents/mcp_plugin.py 顶部 TODO 清单
TASK_MGR = TaskManager()                           # 磁盘任务板
BG = BackgroundManager()                           # 后台任务管理器
BUS = MessageBus()                                 # 队友间消息通信
TEAM = TeammateManager(BUS, TASK_MGR)              # 队友生命周期管理
PERMS = PermissionManager(mode="build")           # 权限管理器
HOOKS = HookManager()                               # 钩子系统
MEMORY = MemoryManager()                            # 跨会话记忆
CRON = CronScheduler()                             # 定时任务调度
EVENTS = EventBus(WORKTREE_EVENTS_LOG)              # 工作树事件总线
WORKTREES = WorktreeManager(REPO_ROOT, TASK_MGR, EVENTS)  # Git 工作树管理

# ═══════════════════════════════════════════════════════════════════════════════
# System Prompt 构建 — 发给模型的"说明书"
# ═══════════════════════════════════════════════════════════════════════════════
# 每次调 API 时发给模型系统指令，告诉它：你是谁、在哪工作、
# 有哪些工具/技能可用、哪些记忆需要参考。
#
# 【分层构建策略】核心规则 → 持久化记忆 → 记忆指南 → 工具偏好 → 技能列表
# ═══════════════════════════════════════════════════════════════════════════════

MEMORY_GUIDANCE = """
When to save memories:
- User states a preference ("I like tabs", "always use pytest") -> type: user
- User corrects you ("don't do X") -> type: feedback
- You learn a project fact not easy to infer from code -> type: project
- You learn an external resource (docs URL, ticket board) -> type: reference
When NOT to save: code structure, temp task state, secrets/credentials.
"""


def build_system_prompt() -> str:
    """构建发给模型的系统提示词。

    每次 API 调用都调用此函数，保证拿到最新的系统时间、
    内存中的记忆和技能列表（记忆可能在对话中被更新）。
    """
    parts = []
    # 核心规则
    parts.append("=" * 60)
    parts.append("# CORE RULES")
    parts.append(f"You are a coding agent at {WORKDIR}. Use tools to solve tasks.")
    parts.append("System time: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # 持久化记忆（从 .memory/ 加载）
    memory_section = MEMORY.load_memory_prompt()
    if memory_section:
        parts.append("")
        parts.append("# MEMORIES (DYNAMIC_BOUNDARY)")
        parts.append(memory_section)

    # 记忆指南
    parts.append("")
    parts.append("# MEMORY GUIDANCE (DYNAMIC_BOUNDARY)")
    parts.append(MEMORY_GUIDANCE)

    # 工具偏好
    parts.append("")
    parts.append("# TOOL PREFERENCES")
    parts.append("Multi-step work -> task_create/task_update/task_list (persisted)")
    parts.append("Short checklists -> TodoWrite (in-memory)")
    parts.append("Delegation -> task (subagent spawning)")
    parts.append("Domain knowledge -> load_skill")
    parts.append("Code search -> search_files (regex, safer than shell grep)")
    parts.append("Delayed/scheduled work -> cron_create (BACKGROUND, never blocks; recurring=False for one-shot)")
    parts.append("Long-running commands -> background_run (background thread, non-blocking)")
    parts.append("IMPORTANT: NEVER use bash 'sleep N' to wait for time. Bash runs synchronously and BLOCKS the agent.")
    parts.append("Permissions -> the user controls. Some tools may be denied.")
    parts.append("Parallel risky changes -> task_create + worktree_create")

    # 可用技能列表
    parts.append("")
    parts.append("# AVAILABLE SKILLS (DYNAMIC_BOUNDARY)")
    parts.append(f"Skills: {SKILLS.descriptions()}")

    return "\n".join(parts)


# 构建初始系统提示词（后续每次 API 调用会重新构建）
SYSTEM = build_system_prompt()


# ═══════════════════════════════════════════════════════════════════════════════
# 关闭协议与计划审批处理器
# ═══════════════════════════════════════════════════════════════════════════════

def handle_shutdown_request(teammate: str) -> str:
    """向指定队友发送关闭请求。

    生成唯一 request_id → 记录到 shutdown_requests → 经消息总线发送 shutdown_request。
    """
    req_id = str(uuid.uuid4())[:8]
    shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send("lead", teammate, "Please shut down.", "shutdown_request", {"request_id": req_id})
    return f"Shutdown request {req_id} sent to '{teammate}'"


def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    """审批队友提交的计划。

    查找 plan_requests → 更新状态 approved/rejected → 经消息总线发送审批结果。
    """
    req = plan_requests.get(request_id)
    if not req:
        return f"Error: Unknown plan request_id '{request_id}'"
    req["status"] = "approved" if approve else "rejected"
    BUS.send("lead", req["from"], feedback, "plan_approval_response",
             {"request_id": request_id, "approve": approve, "feedback": feedback})
    return f"Plan {req['status']} for '{req['from']}'"


# ═══════════════════════════════════════════════════════════════════════════════
# 工具分发表
# ═══════════════════════════════════════════════════════════════════════════════
#
# 【两张表的关系】
#   TOOL_HANDLERS (Python 端): 工具名 → 执行函数的映射（内部"电话簿"）
#   TOOLS (API 端): 工具定义列表（JSON Schema），发给 Claude 的"菜单"
#   模型不认识 Python 函数签名，只看 JSON Schema；程序需要 Python 函数执行，
#   两者通过"工具名"关联。
# ═══════════════════════════════════════════════════════════════════════════════

TOOL_HANDLERS = {
    # ---- 文件操作 ----
    "bash":             lambda **kw: run_bash(kw["command"], kw.get("tool_use_id", "")),
    "read_file":        lambda **kw: run_read(kw["path"], kw.get("tool_use_id", ""), kw.get("limit")), # type: ignore
    "write_file":       lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":        lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "search_files":     lambda **kw: run_grep(kw["pattern"], kw.get("path", "."), kw.get("include", "*")),
    # ---- 短期任务清单 ----
    "TodoWrite":        lambda **kw: TODO.update(kw["items"]),
    # ---- 子代理 ----
    "task":             lambda **kw: run_subagent(kw["prompt"], kw.get("agent_type", "Explore")),
    # ---- 技能加载 ----
    "load_skill":       lambda **kw: SKILLS.load(kw["name"]),
    # ---- 上下文管理 ----
    "compress":         lambda **kw: "Compressing...",
    # ---- 后台任务 ----
    "background_run":   lambda **kw: BG.run(kw["command"], kw.get("timeout", 120)),
    "check_background": lambda **kw: BG.check(kw.get("task_id")), # type: ignore
    # ---- 持久化文件任务 ----
    "task_create":      lambda **kw: TASK_MGR.create(kw["subject"], kw.get("description", "")),
    "task_get":         lambda **kw: TASK_MGR.get(kw["task_id"]),
    "task_update":      lambda **kw: TASK_MGR.update(kw["task_id"], kw.get("status"), kw.get("add_blocked_by"), kw.get("add_blocks")), # type: ignore
    "task_list":        lambda **kw: TASK_MGR.list_all(),
    # ---- 团队管理 ----
    "spawn_teammate":   lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    "list_teammates":   lambda **kw: TEAM.list_all(),
    "send_message":     lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox":       lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),
    "broadcast":        lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
    # ---- 控制协议 ----
    "shutdown_request": lambda **kw: handle_shutdown_request(kw["teammate"]),
    "plan_approval":    lambda **kw: handle_plan_review(kw["request_id"], kw["approve"], kw.get("feedback", "")),
    "idle":             lambda **kw: "Lead does not idle.",
    "claim_task":       lambda **kw: claim_task(kw["task_id"], "lead"),
    # ----  记忆 ----
    "save_memory":      lambda **kw: MEMORY.save_memory(kw["name"], kw["description"], kw["type"], kw["content"]),
    # ----  Cron ----
    "cron_create":      lambda **kw: CRON.create(kw["cron"], kw["prompt"], kw.get("recurring", True), kw.get("durable", False)),
    "cron_delete":      lambda **kw: CRON.delete(kw["id"]),
    "cron_list":        lambda **kw: CRON.list_tasks(),
    # ----  Worktree ----
    "worktree_create":  lambda **kw: WORKTREES.create(kw["name"], kw.get("task_id"), kw.get("base_ref", "HEAD")), # type: ignore
    "worktree_list":    lambda **kw: WORKTREES.list_all(),
    "worktree_enter":   lambda **kw: WORKTREES.enter(kw["name"]),
    "worktree_status":  lambda **kw: WORKTREES.status(kw["name"]),
    "worktree_run":     lambda **kw: WORKTREES.run(kw["name"], kw["command"]),
    "worktree_closeout": lambda **kw: WORKTREES.closeout(kw["name"], kw["action"], kw.get("reason", ""), kw.get("force", False), kw.get("complete_task", False)),
    "worktree_keep":    lambda **kw: WORKTREES.keep(kw["name"]),
    "worktree_remove":  lambda **kw: WORKTREES.remove(kw["name"], kw.get("force", False), kw.get("complete_task", False), kw.get("reason", "")),
    "worktree_events":  lambda **kw: EVENTS.list_recent(kw.get("limit", 20)),
}

# 工具定义（发给 API 的 JSON Schema，描述每个工具的名称、用途、参数）
TOOLS = [
    {"name": "bash", "description": "执行 shell 命令。",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "读取文件内容。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "search_files", "description": "按正则表达式检索文件内容（类似 grep），返回匹配行的文件路径与行号。",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "write_file", "description": "将内容写入文件。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "在文件中精确替换文本。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "TodoWrite", "description": "更新任务跟踪清单。",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "activeForm": {"type": "string"}}, "required": ["content", "status", "activeForm"]}}}, "required": ["items"]}},
    {"name": "task", "description": "派生独立的子代理执行探索或任务。",
     "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}, "agent_type": {"type": "string", "enum": ["Explore", "general-purpose"]}}, "required": ["prompt"]}},
    {"name": "load_skill", "description": "按名称加载领域知识。",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "compress", "description": "手动压缩对话上下文。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "background_run", "description": "在后台线程执行命令。",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]}},
    {"name": "check_background", "description": "查询后台任务状态。",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}}},
    {"name": "task_create", "description": "创建持久化文件任务。",
     "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"]}},
    {"name": "task_get", "description": "按 ID 获取任务详情。",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
    {"name": "task_update", "description": "更新任务状态或依赖关系。",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"]}, "add_blocked_by": {"type": "array", "items": {"type": "integer"}}, "add_blocks": {"type": "array", "items": {"type": "integer"}}}, "required": ["task_id"]}},
    {"name": "task_list", "description": "列出所有任务。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "spawn_teammate", "description": "派生一个持久的自主队友。",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}},
    {"name": "list_teammates", "description": "列出所有队友。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "send_message", "description": "向队友发送消息。",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
    {"name": "read_inbox", "description": "读取并清空领导的收件箱。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "broadcast", "description": "向所有队友广播消息。",
     "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
    {"name": "shutdown_request", "description": "请求某个队友关闭。",
     "input_schema": {"type": "object", "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]}},
    {"name": "plan_approval", "description": "审批队友的计划。",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, "required": ["request_id", "approve"]}},
    {"name": "idle", "description": "进入空闲状态。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "claim_task", "description": "从任务板认领任务。",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
    # ----  记忆 ----
    {"name": "save_memory", "description": "保存跨会话的持久化记忆。",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "description": {"type": "string"},
         "type": {"type": "string", "enum": ["user", "feedback", "project", "reference"]},
         "content": {"type": "string"}}, "required": ["name", "description", "type", "content"]}},
    # ----  Cron ----
    {"name": "cron_create", "description": "在后台按 5 字段 cron 表达式（分钟 小时 日 月 星期）调度任务。一次性延迟任务用 recurring=False。任务在后台执行不阻塞，Agent 保持交互。切勿用 bash 'sleep' 等待——请改用本工具。示例：'0 9 * * *' 每天 9 点；'* * * * *' 每分钟。",
     "input_schema": {"type": "object", "properties": {
         "cron": {"type": "string"}, "prompt": {"type": "string"},
         "recurring": {"type": "boolean"}, "durable": {"type": "boolean"}}, "required": ["cron", "prompt"]}},
    {"name": "cron_delete", "description": "按 ID 删除定时任务。",
     "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "cron_list", "description": "列出所有定时任务。",
     "input_schema": {"type": "object", "properties": {}}},
    # ----  Worktree ----
    {"name": "worktree_create", "description": "创建 Git 工作树，可选绑定到某个任务。",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "task_id": {"type": "integer"}, "base_ref": {"type": "string"}}, "required": ["name"]}},
    {"name": "worktree_list", "description": "列出 .worktrees/index.json 中的工作树。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "worktree_enter", "description": "进入工作树工作线后在其内工作。",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "worktree_status", "description": "显示工作树的 git status。",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "worktree_run", "description": "在工作树目录内执行 shell 命令。",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "command": {"type": "string"}}, "required": ["name", "command"]}},
    {"name": "worktree_closeout", "description": "关闭工作树工作线（保留或移除）。",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "action": {"type": "string", "enum": ["keep", "remove"]}, "reason": {"type": "string"}, "force": {"type": "boolean"}, "complete_task": {"type": "boolean"}}, "required": ["name", "action"]}},
    {"name": "worktree_keep", "description": "将工作树标记为保留而不移除。",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "worktree_remove", "description": "移除工作树（可选将任务标记为完成）。",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "force": {"type": "boolean"}, "complete_task": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["name"]}},
    {"name": "worktree_events", "description": "列出最近的工作树生命周期事件。",
     "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
]


# ═══════════════════════════════════════════════════════════════════════════════
# agent_loop — 主循环（整个系统的大脑）
# ═══════════════════════════════════════════════════════════════════════════════
# 不停重复"调 API → 执行工具 → 返回结果"，是 Agent 的"心跳"。
#
# 【每轮 6 步流程】
#   1. microcompact() — 轻量压缩旧工具结果
#   2. 检查 token 数 → 超限则 auto_compact() 摘要
#   3. 收通知 — 后台任务/Cron/队友消息
#   4. 调 API — 带错误恢复（退避重试）
#   5. 执行工具 — 带权限检查 + Pre/Post 钩子
#   6. Todo 提醒 — 3 轮没更新就提醒
#
# 【循环结束条件】stop_reason != "tool_use"（模型给了最终回答）
# 【错误恢复】overlong_prompt→自动压缩；API 错误→退避重试；max_tokens→注入续写消息
# ═══════════════════════════════════════════════════════════════════════════════

def agent_loop(messages: list, max_rounds: int = None): # type: ignore
    """增强版主循环：权限检查 + 钩子系统 + 错误恢复 + 定时任务。

    messages 对话历史列表会被原地修改。
    rounds_without_todo 追踪模型多少轮没更新 Todo（超 3 轮提醒）；
    max_output_recovery_count 追踪 max_tokens 恢复次数（超限放弃）。
    microcompact 每轮执行（轻量零开销），auto_compact 仅超限时执行（重量级）。

    max_rounds: 评测用，限制工具执行轮数上限（None=不限制）。达到上限时
    注入停止提示让模型收尾，防止评测任务失控烧钱。
    """
    rounds_without_todo = 0         # "用了 Todo 才重置"计数器
    max_output_recovery_count = 0    # max_tokens 连续恢复计数器
    tool_rounds = 0                  # 工具执行轮数计数（max_rounds 用）
    stop_prompt_injected = False     # max_rounds 停止提示只注入一次

    while True:
        # 第1步：轻量压缩旧工具结果（极快，不调 API）
        microcompact(messages)

        # 第2步：Token 超限检测（超过阈值触发 LLM 智能摘要）
        if estimate_tokens(messages) > TOKEN_THRESHOLD:
            log.info("auto-compact triggered")
            messages[:] = auto_compact(messages)  # 切片原地替换，不创建新列表

        # 第3步：收通知
        # 3a. 后台任务通知
        notifs = BG.drain()
        if notifs:
            txt = "\n".join(f"[bg:{n['task_id']}] {n['result']}" for n in notifs)
            messages.append({"role": "user", "content": f"<background-results>\n{txt}\n</background-results>"})
            messages.append({"role": "assistant", "content": "Noted background results."})

        # 3b. Cron 定时任务通知
        cron_notifs = CRON.drain_notifications()
        for note in cron_notifs:
            messages.append({"role": "user", "content": note})

        # 3c. 队友消息
        inbox = BUS.read_inbox("lead")
        if inbox:
            messages.append({"role": "user", "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>"})
            messages.append({"role": "assistant", "content": "Noted inbox messages."})

        # 第4步：API 调用（带错误恢复）
        response = None
        for attempt in range(MAX_RECOVERY_ATTEMPTS + 1):  # 总共尝试 4 次
            try:
                system = build_system_prompt()  # 每次重建，获取最新记忆
                response = client.messages.create(
                    model=MODEL, system=system, messages=messages,
                    tools=TOOLS, max_tokens=8000, # type: ignore
                )
                break  # 调用成功 → 跳出重试循环
            except APIError as e:
                error_body = str(e).lower()
                # overlong_prompt（输入过长）→ 触发压缩而不是重试
                if "overlong_prompt" in error_body or ("prompt" in error_body and "long" in error_body):
                    log.warning(f"Prompt too long. Compacting (attempt {attempt + 1})")
                    messages[:] = auto_compact(messages)
                    continue
                # 一般 API 错误 → 指数退避重试
                if attempt < MAX_RECOVERY_ATTEMPTS:
                    delay = backoff_delay(attempt)
                    log.warning(f"API error, retrying in {delay:.1f}s (attempt {attempt + 1})")
                    time.sleep(delay)
                    continue
                log.error(f"API call failed after {MAX_RECOVERY_ATTEMPTS} retries: {e}")
                return
            except (ConnectionError, TimeoutError, OSError) as e:
                # 网络连接错误 → 退避重试
                if attempt < MAX_RECOVERY_ATTEMPTS:
                    delay = backoff_delay(attempt)
                    log.warning(f"Connection error, retrying in {delay:.1f}s (attempt {attempt + 1})")
                    time.sleep(delay)
                    continue
                log.error(f"Connection failed after {MAX_RECOVERY_ATTEMPTS} retries: {e}")
                return

        if response is None:
            log.error("No response received.")
            return

        # 保存模型回复到对话历史
        messages.append({"role": "assistant", "content": response.content})

        # max_tokens 恢复：输出被截断 → 注入 CONTINUATION_MESSAGE 让模型继续
        if response.stop_reason == "max_tokens":
            max_output_recovery_count += 1
            if max_output_recovery_count <= MAX_RECOVERY_ATTEMPTS:
                log.warning(f"max_tokens hit ({max_output_recovery_count}/{MAX_RECOVERY_ATTEMPTS}). Injecting continuation...")
                messages.append({"role": "user", "content": CONTINUATION_MESSAGE})
                continue  # 继续下一轮循环
            else:
                log.error(f"max_tokens recovery exhausted")
                return

        max_output_recovery_count = 0  # 重置计数器

        # 判断是否结束循环
        if response.stop_reason != "tool_use":
            return  # end_turn：模型给了最终回答

        # 第5步：执行工具调用
        results = []
        used_todo = False
        manual_compress = False
        compact_focus = None

        for block in response.content:
            if block.type != "tool_use":
                continue  # 跳过 text block

            # 压缩工具特殊处理：不立即执行，在一轮结束后触发
            if block.name == "compress":
                manual_compress = True
                compact_focus = (block.input or {}).get("focus")

            tool_input = dict(block.input or {})
            tool_input["tool_use_id"] = block.id
            tool_name = block.name

            # PreToolUse 钩子
            hook_ctx = {"tool_name": tool_name, "tool_input": tool_input}
            pre_result = HOOKS.run_hooks("PreToolUse", hook_ctx)
            for msg in pre_result.get("messages", []):
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": f"[Hook]: {msg}"})
            if pre_result.get("blocked"):
                output = f"Blocked by PreToolUse hook: {pre_result.get('block_reason', 'unknown')}"
                log.warning(f"HOOK blocked {tool_name}: {pre_result.get('block_reason', '')[:100]}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
                continue

            # 权限管道检查（管理类工具在列表中，跳过权限检查）
            skip_execution = False
            # bash 长 sleep 提前拦截：在权限询问前就返回引导提示，
            # 避免用户先看到 "Allow?" 再卡住等 sleep 结束。
            if tool_name == "bash":
                sleep_hint = blocking_sleep_check(str(tool_input.get("command", "")))
                if sleep_hint:
                    log.warning("Blocking sleep intercepted before permission check")
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": sleep_hint})
                    continue
            if tool_name not in ("TodoWrite", "task", "load_skill", "compress", "background_run", "check_background",
                                 "spawn_teammate", "list_teammates", "send_message", "read_inbox", "broadcast",
                                 "shutdown_request", "plan_approval", "idle", "claim_task",
                                 "save_memory", "cron_create", "cron_delete", "cron_list",
                                 "worktree_create", "worktree_list", "worktree_enter", "worktree_status",
                                 "worktree_run", "worktree_closeout", "worktree_keep", "worktree_remove", "worktree_events"):
                decision = PERMS.check(tool_name, tool_input)
                if decision["behavior"] == "deny":
                    output = f"Permission denied: {decision['reason']}"
                    log.warning(f"DENIED {tool_name}: {decision['reason'][:100]}")
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
                    skip_execution = True
                elif decision["behavior"] == "ask":
                    if not PERMS.ask_user(tool_name, tool_input):
                        output = f"Permission denied by user for {tool_name}"
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
                        skip_execution = True

            if skip_execution:
                continue

            # 执行工具：MCP 工具走外部路由器，原生工具查表分发
            try:
                if MCP_ROUTER.is_mcp_tool(tool_name):
                    # 剥离内部注入的 tool_use_id，只把真实参数转发给 MCP 服务器
                    mcp_args = {k: v for k, v in tool_input.items() if k != "tool_use_id"}
                    output = MCP_ROUTER.call(tool_name, mcp_args)
                else:
                    handler = TOOL_HANDLERS.get(tool_name)  # 查表找执行函数
                    output = handler(**tool_input) if handler else f"Unknown tool: {tool_name}"
            except Exception as e:
                output = f"Error: {e}"
                log.error(f"Tool {tool_name} error: {e}")

            log.info(f"> {tool_name}: {str(output)[:200]}")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})

            # PostToolUse 钩子
            hook_ctx["tool_output"] = output
            post_result = HOOKS.run_hooks("PostToolUse", hook_ctx)
            for msg in post_result.get("messages", []):
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": f"[Hook note]: {msg}"})

            if block.name == "TodoWrite":
                used_todo = True  # 本轮用了 Todo

        # 第6步：Todo 提醒（3 轮没更新就提醒）
        rounds_without_todo = 0 if used_todo else rounds_without_todo + 1
        if TODO.has_open_items() and rounds_without_todo >= 3:
            results.insert(0, {"type": "text", "text": "<reminder>Update your todos.</reminder>"})

        messages.append({"role": "user", "content": results})

        # 手动压缩（模型调用了 compress 工具）
        if manual_compress:
            log.info("manual compact")
            messages[:] = auto_compact(messages, focus=compact_focus) # type: ignore

        # max_rounds 上限：达到后注入停止提示，让模型尽快给出最终回答
        tool_rounds += 1
        if max_rounds is not None and tool_rounds >= max_rounds and not stop_prompt_injected:
            stop_prompt_injected = True
            log.warning(f"max_rounds={max_rounds} reached. Asking model to conclude.")
            messages.append({"role": "user", "content": "<eval: max_rounds reached. Stop now and give your best final answer.>"})
            continue


# ═══════════════════════════════════════════════════════════════════════════════
# REPL 入口 — 命令行交互界面
# ═══════════════════════════════════════════════════════════════════════════════
# REPL = Read-Eval-Print Loop：读取输入 → agent_loop 处理 → 打印结果 → 再读取。
# 特殊命令见文件顶部"REPL 交互命令"。
# ═══════════════════════════════════════════════════════════════════════════════


def _read_input_or_none(timeout: float = 1.0, prompt: str = ""):
    """非阻塞读取一行用户输入，超时无输入返回 None。

    select 轮询 stdin：有输入返回该行文本，超时返回 None，
    让主循环在无输入的空档期也能检查 Cron/后台任务通知。
    select 对 stdin 的检测仅 POSIX 可用；不支持的环境退化为阻塞 input()。
    """
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None  # 超时无输入
        line = sys.stdin.readline()
        if not line:
            # EOF（Ctrl+D 或输入流结束）：与 input() 的 EOFError 语义一致
            raise EOFError
        return line.strip()
    except (OSError, ValueError, InterruptedError):
        # Windows 等不支持 select(stdin) 的环境退化为阻塞 input()
        try:
            return input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            raise  # 转发给主循环统一处理退出


def _has_pending_notifications() -> bool:
    """是否有需要自动处理的"异步通知"。

    三类来源任一非空即返回 True：
    1. CRON.queue   - 定时任务到点触发
    2. BG           - 后台任务完成
    3. BUS          - 队友发来的消息
    有通知时主循环无需用户输入即可自动唤醒 agent_loop 消费执行。
    """
    # 1) Cron 定时任务触发通知
    if CRON.has_pending():
        return True
    # 2) 后台任务完成通知
    if BG.has_notifications():
        return True
    # 3) 队友消息（领导收件箱有未读）
    if BUS.has_pending("lead"):
        return True
    return False


def extract_final_reply(history: list) -> str:
    """从 agent_loop 处理后的对话历史中提取模型最终回复的纯文本。

    REPL 打印与评测返回都依赖它。返回 "" 表示历史为空或最后一条不是 assistant。
    """
    if not history:
        return ""
    last = history[-1]
    if last["role"] != "assistant":
        return ""
    content = last["content"]
    if isinstance(content, list):
        # 遍历 content，拼接每个 text block 的文本
        return "".join(block.text for block in content if hasattr(block, "text"))
    if isinstance(content, str):
        return content
    return str(content)


def _print_final_reply(history: list) -> None:
    """打印 agent_loop 处理后的模型最终回复。

    用户输入触发的对话和定时任务自动唤醒的对话都要打印最终回复，
    抽取为公共函数避免重复代码。
    """
    text = extract_final_reply(history)
    if text:
        print(text)
    print()  # 末尾补空行，让输出与提示符分开


def reset_runtime_state() -> None:
    """重置进程级全局单例，保证每次评测 episode 互不污染。

    MEMORY / BUS / TASK_MGR / BG / CRON 等都是模块级单例，
    agent_loop 会持续读写它们；评测连续跑多个任务时若不复位，
    上一个任务产生的任务、记忆、通知会泄漏到下一个 episode。
    """
    global TODO, SKILLS, MCP_ROUTER, TASK_MGR, BG, BUS, TEAM
    global PERMS, HOOKS, MEMORY, CRON, EVENTS, WORKTREES
    global shutdown_requests, plan_requests

    # 断开 MCP 客户端，避免残留连接影响下一次评测
    for _c in list(MCP_ROUTER.clients.values()):
        try:
            _c.disconnect()
        except Exception:
            pass

    # 停止 Cron 后台线程（避免跨 episode 触发旧任务）
    try:
        CRON.stop()
    except Exception:
        pass

    TODO = TodoManager()
    SKILLS = SkillLoader(SKILLS_DIR)
    MCP_ROUTER = MCPToolRouter()
    TASK_MGR = TaskManager()
    BG = BackgroundManager()
    BUS = MessageBus()
    TEAM = TeammateManager(BUS, TASK_MGR)
    PERMS = PermissionManager(mode="build")
    HOOKS = HookManager()
    MEMORY = MemoryManager()
    CRON = CronScheduler()
    EVENTS = EventBus(WORKTREE_EVENTS_LOG)
    WORKTREES = WorktreeManager(REPO_ROOT, TASK_MGR, EVENTS)
    shutdown_requests = {}
    plan_requests = {}


def write_transcript(history: list, path: Path) -> Path:
    """把一次 episode 的完整对话历史写成 JSONL 会话记录，失败可回放调试。

    每条消息一行 JSON（经 normalize_messages 转为纯 dict）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for msg in normalize_messages(history):
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    return path


def run_episode(prompt: str, transcript_path: Path = None, eval_mode: bool = True,
                max_rounds: int = None) -> tuple: # type: ignore
    """无头运行一次完整评测 episode。

    等价于一次一次性会话：重置全局状态 → 注入用户 prompt → agent_loop 主循环
    直至模型给出最终回答 → 返回 (history, final_reply)。

    - transcript_path: 非 None 时把完整对话历史写成 JSONL 会话记录
    - eval_mode: 为 True 时切到 eval 权限模式（免审批，不卡 input()）
    - max_rounds: 限制最大工具执行轮数（防超时/烧钱）
    返回的 history 为完整对话历史（含 tool_use / tool_result），final_reply 为最终文本。
    """
    if transcript_path is None:
        transcript_path = TRANSCRIPT_DIR / f"eval_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jsonl"
    transcript_path = Path(transcript_path)

    # 全局状态隔离：每个 episode 前必须重置
    reset_runtime_state()
    if eval_mode:
        PERMS.mode = "eval"

    history = [{"role": "user", "content": prompt}]
    try:
        agent_loop(history, max_rounds=max_rounds)
    finally:
        # 会话记录：无论成功失败都写入磁盘，失败可回放调试
        write_transcript(history, transcript_path)
    final_reply = extract_final_reply(history)
    return history, final_reply


def _connect_mcp_servers() -> int:
    """扫描并连接 MCP 服务器，工具注册进 TOOLS（前缀 mcp__{server}__{tool}）。

    读取 .claude-plugin/plugin.json 的 mcpServers 配置。返回成功连接的服务器数。
    """
    _mcp_loader = PluginLoader(search_dirs=[WORKDIR])
    _mcp_found = _mcp_loader.scan()
    if not _mcp_found:
        return 0
    _existing = {t["name"] for t in TOOLS}
    _connected = 0
    for _srv_name, _cfg in _mcp_loader.get_mcp_servers().items():
        _c = MCPClient(_srv_name, _cfg.get("command", ""), _cfg.get("args", []), _cfg.get("env"))
        if _c.connect():
            _c.list_tools()
            MCP_ROUTER.register_client(_c)
            for _t in _c.get_agent_tools():
                if _t["name"] not in _existing:
                    TOOLS.append(_t)
            log.info(f"[MCP] Connected to {_srv_name} ({len(_c.get_agent_tools())} tools)")
            _connected += 1
        else:
            log.warning(f"[MCP] Failed to connect: {_srv_name}")
    if MCP_ROUTER.clients:
        log.info(f"[MCP] {len(MCP_ROUTER.clients)} server(s) connected, {len(MCP_ROUTER.get_all_tools())} external tools registered")
    return _connected


def bootstrap() -> dict:
    """初始化运行时环境：加载记忆 → 启动 Cron → SessionStart 钩子 → 连接 MCP。

    评测入口与 REPL 共用同一套初始化，保证行为一致。
    返回各阶段统计信息。
    """
    # 加载跨会话记忆
    MEMORY.load_all()
    mem_count = len(MEMORY.memories)
    if mem_count:
        log.info(f"Loaded {mem_count} memories")

    # 启动定时任务调度器（后台线程）
    CRON.start()
    log.info("Cron scheduler started")

    # 执行会话启动钩子
    HOOKS.run_hooks("SessionStart", {"tool_name": "", "tool_input": {}})

    # 连接 MCP 服务器
    mcp_count = _connect_mcp_servers()

    return {"memories": mem_count, "mcp_servers": mcp_count, "tools": len(TOOLS)}


if __name__ == "__main__":
    """程序启动入口：加载记忆 → 启动 Cron → 运行 SessionStart 钩子 → 进入 REPL。"""
    log.info("Starting coding agent...")

    # 统一的运行时初始化（评测入口也复用 bootstrap）
    stats = bootstrap()
    mem_count = stats["memories"]

    # Git 不可用时给出警告
    if not WORKTREES.git_available:
        log.warning("Not in a git repo. worktree_* tools will return errors.")

    print(f"[Agent Ready] coding-agent | mode={PERMS.mode} | tools={len(TOOLS)} | memories={mem_count}")

    # ── REPL 循环 ────────────────────────────────────
    history = []  # 对话历史，每次调用 agent_loop 都会修改它
    prompt_shown = False  # 提示符已显示标志（避免 select 轮询时每 1 秒重复打印刷屏）
    while True:
        # 自动唤醒：定时任务/后台任务/队友消息触发时无需用户输入
        if _has_pending_notifications():
            log.info("Auto-wake: pending notifications detected")
            agent_loop(history)
            _print_final_reply(history)
            prompt_shown = False  # 通知处理后需重新显示提示符
            continue

        # 等待用户输入（非阻塞），无输入时回到自动唤醒检查
        if not prompt_shown:
            # 只打印一次提示符（\033[36m 青色 / \033[0m 重置）
            sys.stdout.write("\033[36magent >> \033[0m")
            sys.stdout.flush()
            prompt_shown = True

        try:
            query = _read_input_or_none(timeout=1.0)
        except EOFError:
            # 输入流结束（Ctrl+D / 管道关闭）：优雅退出
            print()
            CRON.stop()
            for c in MCP_ROUTER.clients.values():
                c.disconnect()
            break
        if query is None:
            continue  # 超时无输入 → 继续检查通知

        prompt_shown = False  # 已拿到输入，下次等待时重新打印提示符

        # 退出命令
        if query.lower() in ("q", "exit", ""):
            CRON.stop()
            for c in MCP_ROUTER.clients.values():
                c.disconnect()
            break

        # 特殊命令（不经过 AI 处理）
        if query == "/compact":
            if history:
                log.info("Manual compact via /compact")
                history[:] = auto_compact(history)
            continue
        if query == "/tasks":
            print(TASK_MGR.list_all())
            continue
        if query == "/team":
            print(TEAM.list_all())
            continue
        if query == "/inbox":
            print(json.dumps(BUS.read_inbox("lead"), indent=2))
            continue
        if query == "/cron":
            print(CRON.list_tasks())
            continue
        if query == "/memories":
            if MEMORY.memories:
                for name, mem in MEMORY.memories.items():
                    print(f"  [{mem['type']}] {name}: {mem['description']}")
            else:
                print("  (no memories)")
            continue
        if query == "/worktrees":
            print(WORKTREES.list_all())
            continue
        if query == "/mcp":
            if MCP_ROUTER.clients:
                for name, c in MCP_ROUTER.clients.items():
                    tools = c.get_agent_tools()
                    print(f"  {name}: {len(tools)} tools")
                    for t in tools:
                        print(f"    - {t['name']}: {t.get('description', '')[:60]}")
            else:
                print("  (no MCP servers connected)")
            continue
        if query.startswith("/search"):
            # shlex.split 正确处理引号：/search 'def run_' *.py
            parts = shlex.split(query)
            if len(parts) < 2:
                print("Usage: /search <pattern> [path] [include]  (e.g. /search 'def run_' *.py)")
                continue
            pattern = parts[1]
            path = parts[2] if len(parts) > 2 else "."
            include = parts[3] if len(parts) > 3 else "*"
            print(run_grep(pattern, path, include))
            continue
        if query.startswith("/mode"):
            parts = query.split()
            if len(parts) == 2 and parts[1] in PERMISSION_MODES:
                PERMS.mode = parts[1]
                log.info(f"Permission mode switched to {parts[1]}")
                print(f"[Switched to {parts[1]} mode]")
            else:
                print(f"Usage: /mode <{'|'.join(PERMISSION_MODES)}>")
            continue

        # 正常对话：交给 agent_loop 处理
        history.append({"role": "user", "content": query})
        agent_loop(history)

        # 打印模型最终回复
        _print_final_reply(history)
