#!/usr/bin/env python3
"""echo_server.py - 极简 stdio MCP 服务器（示例实现）。

仅实现 MCP 最小协议，供 Agent_Harness 的 MCP 集成做端到端测试：
  initialize / notifications/initialized / tools/list / tools/call

暴露三个工具：
  echo   -> 原样返回输入文本
  add    -> 两个整数相加
  upper  -> 文本转大写

运行方式：
  python examples/mcp/echo_server.py
（作为 MCP 服务器由客户端以子进程方式拉起，一般不直接交互）
"""

import json
import sys


def echo(text: str) -> str:
    return text


def add(a: int, b: int) -> int:
    return a + b


def upper(text: str) -> str:
    return text.upper()


TOOLS = [
    {"name": "echo", "description": "原样返回输入文本。",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "add", "description": "对两个整数求和。",
     "inputSchema": {"type": "object",
                     "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                     "required": ["a", "b"]}},
    {"name": "upper", "description": "将文本转为大写。",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
]

HANDLERS = {
    "echo": lambda args: echo(args.get("text", "")),
    "add": lambda args: add(int(args.get("a", 0)), int(args.get("b", 0))),
    "upper": lambda args: upper(args.get("text", "")),
}


def handle_request(msg: dict) -> dict | None:
    """处理一条 JSON-RPC 请求，返回响应；通知（无 id）返回 None。"""
    request_id = msg.get("id")

    if msg.get("method") == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "echo-server", "version": "1.0.0"},
        }}

    if msg.get("method") == "notifications/initialized":
        return None  # 通知：不需要响应

    if msg.get("method") == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}

    if msg.get("method") == "tools/call":
        params = msg.get("params", {})
        name = params.get("name")
        args = params.get("arguments", {})
        handler = HANDLERS.get(name)
        if handler is None:
            return {"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32601, "message": f"Unknown tool: {name}"}}
        try:
            result = handler(args)
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": str(result)}]}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32603, "message": str(e)}}

    return {"jsonrpc": "2.0", "id": request_id, "error": {
        "code": -32601, "message": f"Method not found: {msg.get('method')}"}}


def main() -> None:
    """从 stdin 逐行读取 JSON-RPC 消息，响应写入 stdout（行分隔）。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle_request(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
