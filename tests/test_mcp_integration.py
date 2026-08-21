from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = ROOT / "agents"
sys.path.insert(0, str(AGENTS_DIR))

from mcp_plugin import MCPClient, MCPToolRouter, PluginLoader  # noqa: E402

ECHO_SERVER = ROOT / "examples" / "mcp" / "echo_server.py"
DB_SERVER = ROOT / "examples" / "mcp" / "db_server.py"
PLUGIN_DIR = ROOT / "examples" / "mcp"


@pytest.fixture()
def echo_client():
    """启动 echo MCP 服务器并返回已连接的 MCPClient，测试结束自动断开。"""
    client = MCPClient(
        "test__echo",
        sys.executable,
        [str(ECHO_SERVER)],
    )
    assert client.connect(), "echo MCP server should connect"
    yield client
    client.disconnect()


def test_connect_and_list_tools(echo_client):
    tools = echo_client.list_tools()
    names = {t["name"] for t in tools}
    assert names == {"echo", "add", "upper"}


def test_agent_tool_prefixing(echo_client):
    echo_client.list_tools()
    agent_tools = echo_client.get_agent_tools()
    assert {t["name"] for t in agent_tools} == {
        "mcp__test__echo__echo",
        "mcp__test__echo__add",
        "mcp__test__echo__upper",
    }


def test_call_echo_tool(echo_client):
    out = echo_client.call_tool("echo", {"text": "hello world"})
    assert out == "hello world"


def test_call_add_tool(echo_client):
    out = echo_client.call_tool("add", {"a": 3, "b": 4})
    assert out == "7"


def test_call_upper_tool(echo_client):
    out = echo_client.call_tool("upper", {"text": "mcp works"})
    assert out == "MCP WORKS"


def test_unknown_tool_returns_error(echo_client):
    out = echo_client.call_tool("nope", {})
    assert out.startswith("MCP Error:")


def test_router_routes_calls(echo_client):
    echo_client.list_tools()
    router = MCPToolRouter()
    router.register_client(echo_client)

    assert router.is_mcp_tool("mcp__test__echo__add")
    assert not router.is_mcp_tool("bash")

    out = router.call("mcp__test__echo__add", {"a": 10, "b": 32})
    assert out == "42"

    assert {t["name"] for t in router.get_all_tools()} == {
        "mcp__test__echo__echo",
        "mcp__test__echo__add",
        "mcp__test__echo__upper",
    }


def test_plugin_loader_scans_example_manifest():
    loader = PluginLoader(search_dirs=[PLUGIN_DIR])
    found = loader.scan()
    assert "echo-demo" in found

    servers = loader.get_mcp_servers()
    assert "echo-demo__echo" in servers
    cfg = servers["echo-demo__echo"]
    assert cfg["command"] == "python"
    assert str(ECHO_SERVER) in cfg["args"]


@pytest.fixture()
def db_client(tmp_path):
    """启动 SQLite MCP 服务器并返回已连接的 MCPClient，测试结束自动断开。

    通过 DB_FILE 环境变量把数据库文件重定向到临时目录，
    避免污染示例目录内/仓库工作区的 sample.db。
    """
    client = MCPClient(
        "test__db",
        sys.executable,
        [str(DB_SERVER)],
        env={"DB_FILE": str(tmp_path / "sample.db")},
    )
    assert client.connect(), "db MCP server should connect"
    yield client
    client.disconnect()


def test_db_connect_and_list_tools(db_client):
    tools = db_client.list_tools()
    names = {t["name"] for t in tools}
    assert names == {"list_tables", "get_schema", "query_db", "insert_note"}


def test_db_list_tables(db_client):
    out = db_client.call_tool("list_tables", {})
    assert "users" in out
    assert "products" in out
    assert "orders" in out


def test_db_get_schema(db_client):
    out = db_client.call_tool("get_schema", {"table": "users"})
    assert "CREATE TABLE users" in out


def test_db_query_select(db_client):
    out = db_client.call_tool("query_db", {"sql": "SELECT name FROM users ORDER BY id"})
    assert "Alice" in out
    assert "Bob" in out


def test_db_query_blocks_non_select(db_client):
    out = db_client.call_tool("query_db", {"sql": "DROP TABLE users"})
    assert out.startswith("Error: Only SELECT queries allowed")


def test_db_insert_note(db_client):
    out = db_client.call_tool("insert_note", {"content": "hello from test"})
    assert out.startswith("Inserted note #")

    rows = db_client.call_tool("query_db", {"sql": "SELECT content FROM notes"})
    assert "hello from test" in rows
