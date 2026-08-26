#!/usr/bin/env python3
"""
mcp_plugin.py - MCP（Model Context Protocol）接入与插件系统

外部进程通过标准协议暴露工具，宿主 Agent 经过统一规范化后，
将外部工具与原生工具合并进同一个工具池，经由同一套权限与
结果规范化管道执行。

核心流程：
  1. 启动 MCP 服务器进程（stdio 传输）
  2. 拉取服务器声明的工具清单
  3. 为每个工具添加 mcp__{server}__{tool} 前缀并注册
  4. 将工具调用路由回对应服务器

插件机制在此基础上增加发现能力：插件清单（.claude-plugin/plugin.json）
声明需要启动的外部服务器，宿主据此自动拉起并注册。

设计原则：外部工具进入同一工具管道——共享权限检查、共享工具池、
共享规范化的 tool_result 负载，避免形成与原生工具割裂的第二世界。

模块构成：
  CapabilityPermissionGate  原生与外部工具共用的风险分级权限门
  MCPClient                 stdio MCP 客户端（连接 / 工具 / 调用）
  PluginLoader              插件清单扫描与服务器配置发现
  MCPToolRouter / build_tool_pool  工具池合并与路由
  agent_loop                统一原生 + MCP 工具池的执行循环
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
PERMISSION_MODES = ("default", "auto")

# client / MODEL 依赖环境变量（MODEL_ID），延迟到 main() 再创建。
# 这样本模块被 import 时没有任何副作用，Agent_Harness 等宿主
# 可以直接复用下面的纯类（MCPClient / PluginLoader / MCPToolRouter）。
client: Any = None
MODEL: Any = None


class CapabilityPermissionGate:
    """
    原生工具和外部能力的共享权限门。

    核心原则：MCP 不绕过控制平面。
    原生工具和 MCP 工具统一归一化为 capability intent，
    然后通过相同的 allow/ask 策略审批。
    """

    READ_PREFIXES = ("read", "list", "get", "show", "search", "query", "inspect")
    HIGH_RISK_PREFIXES = ("delete", "remove", "drop", "shutdown")

    def __init__(self, mode: str = "default"):
        self.mode = mode if mode in PERMISSION_MODES else "default"

    '''
    这是核心方法，输入工具名和参数，输出一个字典，包含：

     - source: "mcp" 还是 "native"（本地）
     - server: 如果是 MCP 工具，是哪个服务器
     - tool: 实际的工具名
     - risk: 风险等级 "read" / "write" / "high"
    '''
    def normalize(self, tool_name: str, tool_input: dict) -> dict:
        if tool_name.startswith("mcp__"):
            # 去掉 "mcp__" 前缀后，按最后一个 "__" 切分：
            # 前半是服务器名（可能是 plugin__server 两层），后半是真实工具名
            parts = tool_name.split("__")
            if len(parts) < 3:
                server_name = tool_name
                actual_tool = ""
            else:
                server_name = "__".join(parts[1:-1])
                actual_tool = parts[-1]
            source = "mcp"
        else:
            server_name = None
            actual_tool = tool_name
            source = "native"

        lowered = actual_tool.lower()
        if actual_tool == "read_file" or lowered.startswith(self.READ_PREFIXES):
            risk = "read"
        elif actual_tool == "bash":
            command = tool_input.get("command", "")
            risk = "high" if any(
                token in command for token in ("rm -rf", "sudo", "shutdown", "reboot")
            ) else "write"
        elif lowered.startswith(self.HIGH_RISK_PREFIXES):
            risk = "high"
        else:
            risk = "write"

        return {
            "source": source,
            "server": server_name,
            "tool": actual_tool,
            "risk": risk,
        }

    def check(self, tool_name: str, tool_input: dict) -> dict:
        intent = self.normalize(tool_name, tool_input)

        if intent["risk"] == "read":
            return {"behavior": "allow", "reason": "Read capability", "intent": intent}

        if self.mode == "auto" and intent["risk"] != "high":
            return {
                "behavior": "allow",
                "reason": "Auto mode for non-high-risk capability",
                "intent": intent,
            }

        if intent["risk"] == "high":
            return {
                "behavior": "ask",
                "reason": "High-risk capability requires confirmation",
                "intent": intent,
            }

        return {
            "behavior": "ask",
            "reason": "State-changing capability requires confirmation",
            "intent": intent,
        }

    def ask_user(self, intent: dict, tool_input: dict) -> bool:
        preview = json.dumps(tool_input, ensure_ascii=False)[:200]
        source = (
            f"{intent['source']}:{intent['server']}/{intent['tool']}"
            if intent.get("server")
            else f"{intent['source']}:{intent['tool']}"
        )
        print(f"\n  [Permission] {source} risk={intent['risk']}: {preview}")
        try:
            answer = input("  Allow? (y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in ("y", "yes")


permission_gate = CapabilityPermissionGate()


class MCPClient:
    """
    通过 stdio 的最小化 MCP 客户端。

    覆盖最小协议路径（initialize / tools/list / tools/call），
    HTTP/SSE 传输、认证流程等能力预留为后续扩展。
    """

    def __init__(self, server_name: str, command: str, args: list = None, env: dict = None):
        self.server_name = server_name
        self.command = command
        self.args = args or []
        self.env = {**os.environ, **(env or {})}
        self.process = None
        self._request_id = 0
        self._tools: list = []  # 缓存的工具列表

    def connect(self):
        """启动MCP服务器进程。"""
        try:
            self.process = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                text=True,
            )
            # 发送初始化请求
            self._send({"method": "initialize", "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "agent-harness", "version": "1.0"},
            }})
            response = self._recv()
            if response and "result" in response:
                # 发送initialized通知
                self._send({"method": "notifications/initialized"})
                return True
        except FileNotFoundError:
            print(f"[MCP] Server command not found: {self.command}")
        except Exception as e:
            print(f"[MCP] Connection failed: {e}")
        return False

    def list_tools(self) -> list:
        """从服务器获取可用工具。"""
        self._send({"method": "tools/list", "params": {}})
        response = self._recv()
        if response and "result" in response:
            self._tools = response["result"].get("tools", [])
        return self._tools

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """在服务器上执行一个工具。"""
        self._send({"method": "tools/call", "params": {
            "name": tool_name,
            "arguments": arguments,
        }})
        response = self._recv()
        if response and "result" in response:
            content = response["result"].get("content", [])
            return "\n".join(c.get("text", str(c)) for c in content)
        if response and "error" in response:
            return f"MCP Error: {response['error'].get('message', 'unknown')}"
        return "MCP Error: no response"

    def get_agent_tools(self) -> list:
        """
        将MCP工具转换为agent工具格式。

        工具命名采用前缀约定：
        mcp__{server_name}__{tool_name}
        """
        agent_tools = []
        for tool in self._tools:
            prefixed_name = f"mcp__{self.server_name}__{tool['name']}"
            agent_tools.append({
                "name": prefixed_name,
                "description": tool.get("description", ""),
                "input_schema": tool.get("inputSchema", {"type": "object", "properties": {}}),
                "_mcp_server": self.server_name,
                "_mcp_tool": tool["name"],
            })
        return agent_tools

    def disconnect(self):
        """关闭服务器进程。"""
        if self.process:
            try:
                self._send({"method": "shutdown"})
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                self.process.kill()
            self.process = None

    def _send(self, message: dict):
        if not self.process or self.process.poll() is not None:
            return
        self._request_id += 1
        envelope = {"jsonrpc": "2.0", "id": self._request_id, **message}
        line = json.dumps(envelope) + "\n"
        try:
            self.process.stdin.write(line)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def _recv(self) -> dict | None:
        if not self.process or self.process.poll() is not None:
            return None
        try:
            line = self.process.stdout.readline()
            if line:
                return json.loads(line)
        except (json.JSONDecodeError, OSError):
            pass
        return None


class PluginLoader:
    """
    从.claude-plugin/目录加载插件。

    实现最小可用的插件发现流程：
    读取清单，发现 MCP 服务器配置，并注册它们。
    """

    def __init__(self, search_dirs: list = None):
        self.search_dirs = search_dirs or [WORKDIR]
        self.plugins: dict = {}  # 名称 -> manifest
        self._plugin_dirs: dict = {}  # 名称 -> 插件所在目录（用于解析相对路径）

    def scan(self) -> list:
        """扫描目录中的.claude-plugin/plugin.json清单文件。"""
        found = []
        for search_dir in self.search_dirs:
            search_dir = Path(search_dir)
            plugin_dir = search_dir / ".claude-plugin"
            manifest_path = plugin_dir / "plugin.json"
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text())
                    name = manifest.get("name", plugin_dir.parent.name)
                    self.plugins[name] = manifest
                    self._plugin_dirs[name] = plugin_dir.parent
                    found.append(name)
                except (json.JSONDecodeError, OSError) as e:
                    print(f"[Plugin] Failed to load {manifest_path}: {e}")
        return found

    def get_mcp_servers(self) -> dict:
        """
        从已加载的插件中提取MCP服务器配置。
        返回 {server_name: {command, args, env}}。

        插件清单中 args 的相对路径会基于插件所在目录解析为绝对路径，
        避免在不同工作目录下运行时找不到脚本。
        """
        servers = {}
        for plugin_name, manifest in self.plugins.items():
            for server_name, config in manifest.get("mcpServers", {}).items():
                server_cfg = dict(config)
                # 将 args 中的相对路径解析为插件目录下的绝对路径
                resolved_args = []
                for arg in server_cfg.get("args", []):
                    arg_path = Path(arg)
                    if not arg_path.is_absolute():
                        arg_path = (self._plugin_dirs[plugin_name] / arg).resolve()
                    resolved_args.append(str(arg_path))
                server_cfg["args"] = resolved_args
                servers[f"{plugin_name}__{server_name}"] = server_cfg
        return servers


class MCPToolRouter:
    """
    将工具调用路由到正确的MCP服务器。

    MCP工具以mcp__{server}__{tool}为前缀，与原生工具共存于同一工具池。
    路由器剥离前缀并分发到正确的MCPClient。
    """

    def __init__(self):
        self.clients = {}  # 服务器名称 -> MCPClient

    def register_client(self, client: MCPClient):
        self.clients[client.server_name] = client

    def is_mcp_tool(self, tool_name: str) -> bool:
        return tool_name.startswith("mcp__")

    def call(self, tool_name: str, arguments: dict) -> str:
        """将MCP工具调用路由到正确的服务器。"""
        # 与 normalize 一致：去掉 "mcp__" 后按最后一个 "__" 切分，
        # 兼容 plugin__server 两层命名的服务器名
        parts = tool_name.split("__")
        if len(parts) < 3:
            return f"Error: Invalid MCP tool name: {tool_name}"
        server_name = "__".join(parts[1:-1])
        actual_tool = parts[-1]
        client = self.clients.get(server_name)
        if not client:
            return f"Error: MCP server not found: {server_name}"
        return client.call_tool(actual_tool, arguments)

    def get_all_tools(self) -> list:
        """收集所有已连接MCP服务器的工具。"""
        tools = []
        for client in self.clients.values():
            tools.extend(client.get_agent_tools())
        return tools


# -- 原生工具实现（与 Agent_Harness 保持一致）--
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str) -> str:
    try:
        return safe_path(path).read_text()[:50000]
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


NATIVE_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"]),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

NATIVE_TOOLS = [
    {"name": "bash", "description": "执行 shell 命令。",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "读取文件内容。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "将内容写入文件。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "在文件中精确替换文本。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
]


# -- MCP工具路由器（全局）--
mcp_router = MCPToolRouter()
plugin_loader = PluginLoader()


def build_tool_pool() -> list:
    """
    组装完整的工具池：原生 + MCP工具。

    原生工具在名称冲突时优先，以确保本地核心在添加外部工具后仍然可预测。
    """
    all_tools = list(NATIVE_TOOLS)
    mcp_tools = mcp_router.get_all_tools()

    native_names = {t["name"] for t in all_tools}
    for tool in mcp_tools:
        if tool["name"] not in native_names:
            all_tools.append(tool)

    return all_tools


def handle_tool_call(tool_name: str, tool_input: dict) -> str:
    """分发到原生处理器或MCP路由器。"""
    if mcp_router.is_mcp_tool(tool_name):
        return mcp_router.call(tool_name, tool_input)
    handler = NATIVE_HANDLERS.get(tool_name)
    if handler:
        return handler(**tool_input)
    return f"Unknown tool: {tool_name}"


def normalize_tool_result(tool_name: str, output: str, intent: dict | None = None) -> str:
    intent = intent or permission_gate.normalize(tool_name, {})
    status = "error" if "Error:" in output or "MCP Error:" in output else "ok"
    payload = {
        "source": intent["source"],
        "server": intent.get("server"),
        "tool": intent["tool"],
        "risk": intent["risk"],
        "status": status,
        "preview": output[:500],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def agent_loop(messages: list):
    """统一原生 + MCP工具池的agent循环。"""
    tools = build_tool_pool()

    while True:
        system = (
            f"You are a coding agent at {WORKDIR}. Use tools to solve tasks.\n"
            "You have both native tools and MCP tools available.\n"
            "MCP tools are prefixed with mcp__{server}__{tool}.\n"
            "All capabilities pass through the same permission gate before execution."
        )
        response = client.messages.create(
            model=MODEL, system=system, messages=messages,
            tools=tools, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            decision = permission_gate.check(block.name, block.input or {})
            try:
                if decision["behavior"] == "deny":
                    output = f"Permission denied: {decision['reason']}"
                elif decision["behavior"] == "ask" and not permission_gate.ask_user(
                    decision["intent"], block.input or {}
                ):
                    output = f"Permission denied by user: {decision['reason']}"
                else:
                    output = handle_tool_call(block.name, block.input or {})
            except Exception as e:
                output = f"Error: {e}"
            print(f"> {block.name}: {str(output)[:200]}")
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": normalize_tool_result(
                    block.name,
                    str(output),
                    decision.get("intent"),
                ),
            })

        messages.append({"role": "user", "content": results})


# ────────────────────────────────────────────────────────────────────────────
# TODO (MCP 后续升级) — 扩展路径，按优先级排列
#
# TODO: HTTP / SSE 传输层
#   现状: MCPClient 只支持 stdio(subprocess.Popen + stdin/stdout 行协议)。
#   方案: 抽象 Transport 接口(send/connect/close) → StdioTransport 保留现有逻辑，
#         StreamableHttpTransport 用 httpx/requests 调 POST {base}/message (SSE 订阅
#         GET {base}/sse)；initialize/tools/list/tools/call 的 JSON-RPC 协议不变，
#         配置在 plugin.json 的 mcpServers 加 "type":"http", "url": "..."。
#
# TODO: OAuth 授权流程
#   现状: 无认证，initialize 后直接可用。
#   方案: 连接状态机增加 needs-auth；本地 http://localhost 起 OAuth 回调服务，
#         按标准授权码流换 token 存 ~/.mcp-auth/{server}.json，请求带
#         Authorization: Bearer；401 时用 refresh_token 刷新并重放一次。
#
# TODO: 服务器重连与生命周期管理
#   现状: connect() 失败/进程退出即失败，disconnect() 直接 terminate()。
#   方案: 心跳 ping + 指数退避重连(1s/2s/4s/8s...)，重连成功后重发
#         initialize/initialized 并重新 tools/list；关闭时先发 shutdown 等 exit。
#
# TODO: resources / prompts 能力层
#   现状: 只暴露工具(tools/list)。
#   方案: 实现 resources/list、resources/read、prompts/list、prompts/get，
#         方案A: 注册为 mcp__{server}__resource_read 等工具走同一权限管道;
#         方案B: 在系统提示词注入资源目录(类似 SKILLS.descriptions)，按需加载。
#
# TODO: 工具到达模型前的过滤
#   现状: 所有 MCP 工具原样合并进 TOOLS。
#   方案: 按风险/前缀/白名单在 build_tool_pool() 中过滤，敏感工具不暴露给模型。
#
# TODO: 更丰富的插件安装与更新
#   现状: 只扫描 .claude-plugin/plugin.json。
#   方案: 支持多搜索目录/全局配置、版本与依赖声明、enable/disable 开关、热重载。
#
# TODO: QQ 助手接入（QQ_MODE 收发员驱动）
#   目标: 让 QQ 聊天界面里的官方机器人成为用户的 AI 助手 —— 用户在 QQ 里像
#         和助手对话一样: 下命令 → agent 反问/确认 → 用户回答 → agent 执行 →
#         结果汇报回同一聊天窗口。多轮交互等价于本地 REPL，但输入输出走 QQ。
#   架构: 官方QQ机器人(云端身份, bot.q.qq.com 拿 AppID/Token) + 服务器上
#         stdio+env 的 QQ-MCP 服务器(现有 MCPClient 已支持) + 收发员驱动。
#         注意: 机器人是独立身份，非个人号；本地仅需 QQ 聊天客户端，免隧道。
#   任务:
#     1) 新增 agents/qq_channel.py —— QQ 收发员(QQChannel):
#        - 后台线程每 5~10s 调 QQ-MCP 取消息工具(如 get_new_messages，名称可配)
#        - 按消息 id 去重(last_seen_id 持久化到文件)，同一条绝不重复处理
#        - 新消息追加进 Agent_Harness 的 history(等价于 REPL 输入)
#        - agent_loop 结束后，把最终文字输出经 send_private_msg/send_group_msg
#          发回同一聊天窗口(提问/确认/汇报都靠这一步)
#     2) Agent_Harness 接线:
#        - QQ_MODE=1 且连上 QQ 插件时启动 QQChannel
#        - _has_pending_notifications() 纳入 QQ 新消息 → 复用自动唤醒
#        - 权限: 该 QQ 服务器的 get_*(收) 与 send_*(发) 默认放行(无人值守需要)，
#          其余工具照旧走 PERMS 管道
#     3) 新增 examples/mcp/qq-bot.plugin.json(stdio + env 注入 APPID/TOKEN 占位,
#        注明勿提交凭据)
#     4) 测试: 造一个 fake QQ-MCP 服务器跑通闭环 ——
#        用户发消息 → agent 反问 → 用户回答 → agent 执行 → 汇报; 覆盖去重与回复目标
#   待定: 取/发消息工具名各家略有差异(默认 get_new_messages/send_private_msg/
#         send_group_msg)，在配置中可改。
# ────────────────────────────────────────────────────────────────────────────


def main():
    """独立 REPL 入口：初始化环境相关依赖（client/MODEL）后运行交互循环。"""
    global client, MODEL
    client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
    MODEL = os.environ["MODEL_ID"]

    # 扫描插件
    found = plugin_loader.scan()
    if found:
        print(f"[Plugins loaded: {', '.join(found)}]")
        for server_name, config in plugin_loader.get_mcp_servers().items():
            mcp_client = MCPClient(server_name, config.get("command", ""), config.get("args", []))
            if mcp_client.connect():
                mcp_client.list_tools()
                mcp_router.register_client(mcp_client)
                print(f"[MCP] Connected to {server_name}")

    tool_count = len(build_tool_pool())
    mcp_count = len(mcp_router.get_all_tools())
    print(f"[Tool pool: {tool_count} tools ({mcp_count} from MCP)]")

    history = []
    while True:
        try:
            query = input("\033[36mmcp >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        if query.strip() == "/tools":
            for tool in build_tool_pool():
                prefix = "[MCP] " if tool["name"].startswith("mcp__") else "       "
                print(f"  {prefix}{tool['name']}: {tool.get('description', '')[:60]}")
            continue

        if query.strip() == "/mcp":
            if mcp_router.clients:
                for name, c in mcp_router.clients.items():
                    tools = c.get_agent_tools()
                    print(f"  {name}: {len(tools)} tools")
            else:
                print("  (no MCP servers connected)")
            continue

        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()

    # 清理MCP连接
    for c in mcp_router.clients.values():
        c.disconnect()


if __name__ == "__main__":
    main()
