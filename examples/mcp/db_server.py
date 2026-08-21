#!/usr/bin/env python3
"""db_server.py - 基于 SQLite 的 MCP 示例服务器（stdio）。

与 echo_server.py 相同的极简 MCP 最小协议：
  initialize / notifications/initialized / tools/list / tools/call

暴露四个数据库工具：
  list_tables  -> 列出库中所有表
  get_schema   -> 查看某张表的建表语句
  query_db     -> 只读执行 SELECT 查询（仅允许 SELECT，防止写操作）
  insert_note  -> 向 notes 表写入一条笔记（演示带权限意识的写工具）

首次运行自动在同目录创建示例数据库 sample.db 并灌入种子数据
（users / products / orders 三张演示表），方便直接联调。

运行方式：
  python examples/mcp/db_server.py
（作为 MCP 服务器由客户端以子进程方式拉起，一般不直接交互）
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

# 默认数据库文件与脚本同目录；可用环境变量 DB_FILE 覆盖（便于测试隔离）
_DEFAULT_DB = Path(__file__).resolve().parent / "sample.db"
DB_PATH = Path(os.environ["DB_FILE"]).resolve() if os.environ.get("DB_FILE") else _DEFAULT_DB

# 展示给模型的查询结果行数上限，防止超大结果撑爆上下文
MAX_ROWS = 100
# 单条输出字符上限
MAX_OUTPUT_CHARS = 20000


def _connect() -> sqlite3.Connection:
    """打开数据库连接（每次调用独立连接，简单可靠）。"""
    return sqlite3.connect(DB_PATH)


def _ensure_database():
    """若 sample.db 不存在，则建表并写入种子数据。"""
    if DB_PATH.exists():
        return
    conn = _connect()
    try:
        conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE products (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                price REAL NOT NULL,
                stock INTEGER DEFAULT 0
            );

            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                product_id INTEGER NOT NULL REFERENCES products(id),
                quantity INTEGER NOT NULL,
                total REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE notes (
                id INTEGER PRIMARY KEY,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );
            """
        )
        conn.execute("INSERT INTO users (name, email) VALUES ('Alice', 'alice@example.com')")
        conn.execute("INSERT INTO users (name, email) VALUES ('Bob', 'bob@example.com')")
        conn.execute("INSERT INTO products (name, price, stock) VALUES ('Keyboard', 99.9, 42)")
        conn.execute("INSERT INTO products (name, price, stock) VALUES ('Mouse', 49.9, 100)")
        conn.execute(
            "INSERT INTO orders (user_id, product_id, quantity, total, status) "
            "VALUES (1, 1, 2, 199.8, 'completed')"
        )
        conn.commit()
    finally:
        conn.close()


def _format_rows(rows: list, colnames: list) -> str:
    """把查询结果格式化为可读文本：首行列名 + 数据行。"""
    header = " | ".join(colnames)
    lines = [header, "-" * len(header)]
    lines.extend(" | ".join(str(c) for c in row) for row in rows)
    return "\n".join(lines)


def list_tables() -> str:
    """列出数据库中所有表。"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    return "\n".join(r[0] for r in rows) if rows else "(no tables)"


def get_schema(table: str) -> str:
    """返回指定表的建表语句。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else f"Error: table '{table}' not found"


def query_db(sql: str) -> str:
    """只读执行一条 SELECT 查询并返回结果。

    出于安全考虑仅放行 SELECT，拒绝任何写操作（INSERT/UPDATE/DELETE/DROP 等）。
    """
    stmt = sql.strip()
    if not stmt.upper().startswith("SELECT"):
        return "Error: Only SELECT queries allowed (read-only access)"

    conn = _connect()
    try:
        cur = conn.execute(stmt)
        colnames = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()[:MAX_ROWS]
    except sqlite3.Error as e:
        return f"Error: {e}"
    finally:
        conn.close()

    if not colnames:
        return "(no result)"
    out = _format_rows(rows, colnames)
    return out[:MAX_OUTPUT_CHARS]


def insert_note(content: str) -> str:
    """向 notes 表写入一条笔记（写工具示例，供权限管道演示用）。"""
    if not content or len(content) > 2000:
        return "Error: content must be 1-2000 chars"
    conn = _connect()
    try:
        cur = conn.execute("INSERT INTO notes (content) VALUES (?)", (content,))
        conn.commit()
        return f"Inserted note #{cur.lastrowid}"
    except sqlite3.Error as e:
        return f"Error: {e}"
    finally:
        conn.close()


TOOLS = [
    {"name": "list_tables", "description": "列出示例 SQLite 数据库中的所有表。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_schema", "description": "查看指定表的建表语句（CREATE TABLE）。",
     "inputSchema": {"type": "object", "properties": {"table": {"type": "string"}},
                     "required": ["table"]}},
    {"name": "query_db", "description": "对示例 SQLite 数据库执行只读 SELECT 查询，返回列名与数据行；仅允许 SELECT。",
     "inputSchema": {"type": "object", "properties": {"sql": {"type": "string"}},
                     "required": ["sql"]}},
    {"name": "insert_note", "description": "向 notes 表写入一条笔记（写操作）。",
     "inputSchema": {"type": "object", "properties": {"content": {"type": "string"}},
                     "required": ["content"]}},
]

HANDLERS = {
    "list_tables": lambda args: list_tables(),
    "get_schema": lambda args: get_schema(str(args.get("table", ""))),
    "query_db": lambda args: query_db(str(args.get("sql", ""))),
    "insert_note": lambda args: insert_note(str(args.get("content", ""))),
}


def handle_request(msg: dict) -> dict | None:
    """处理一条 JSON-RPC 请求，返回响应；通知（无 id）返回 None。"""
    request_id = msg.get("id")

    if msg.get("method") == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "db-server", "version": "1.0.0"},
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
    """确保数据库存在，然后从 stdin 逐行读取 JSON-RPC 消息，响应写入 stdout。"""
    _ensure_database()
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
