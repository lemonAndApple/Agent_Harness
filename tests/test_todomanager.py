from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = ROOT / "agents"
sys.path.insert(0, str(AGENTS_DIR))

# 导入 Agent_Harness 需要环境变量
os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test")

import Agent_Harness as A  # noqa: E402


def _todo(content: str, status: str = "pending", activeForm: str = "task") -> dict:
    return {"content": content, "status": status, "activeForm": activeForm}


def test_todo_update_and_render():
    t = A.TodoManager()
    out = t.update([_todo("a"), _todo("b", "in_progress", "working"), _todo("c", "completed")])
    assert "[x] c" in out
    assert "[>] b <- working" in out
    assert "[ ] a" in out
    assert "(1/3 completed)" in out
    assert len(t.items) == 3


def test_todo_empty():
    t = A.TodoManager()
    assert t.update([]) == "No todos."
    assert t.items == []


def test_todo_requires_content():
    t = A.TodoManager()
    with pytest.raises(ValueError, match="content required"):
        t.update([_todo("   ")])


def test_todo_invalid_status():
    t = A.TodoManager()
    with pytest.raises(ValueError, match="invalid status"):
        t.update([_todo("a", "doing")])


def test_todo_requires_active_form():
    t = A.TodoManager()
    with pytest.raises(ValueError, match="activeForm required"):
        t.update([{"content": "a", "status": "pending", "activeForm": ""}])


def test_todo_max_20_items():
    t = A.TodoManager()
    items = [_todo(f"task {i}") for i in range(20)]
    assert t.update(items) is not None  # 20 项合法
    with pytest.raises(ValueError, match="Max 20 todos"):
        t.update([_todo(f"task {i}") for i in range(21)])


def test_todo_only_one_in_progress():
    t = A.TodoManager()
    with pytest.raises(ValueError, match="Only one in_progress allowed"):
        t.update([_todo("a", "in_progress"), _todo("b", "in_progress")])


def test_todo_status_normalized_to_lower():
    t = A.TodoManager()
    t.update([_todo("a", "COMPLETED")])
    assert t.items[0]["status"] == "completed"


def test_mcp_aggregate_listing():
    """`/mcp` 命令展示"server: N tools"，此处复用同一数据路径验证聚合逻辑。"""
    from mcp_plugin import MCPToolRouter

    server = MCPToolRouter()
    server.register_client(_FakeClient("echo", ["mcp__echo__echo", "mcp__echo__add"]))

    listing = []
    for name, c in server.clients.items():
        tools = c.get_agent_tools()
        listing.append(f"{name}: {len(tools)} tools")

    assert any("echo: 2 tools" in line for line in listing)


class _FakeClient:
    """最小客户端摹本：只模拟 get_agent_tools 返回前缀化工具名。"""

    def __init__(self, name: str, tool_names: list):
        self.server_name = name
        self._tool_names = tool_names

    def get_agent_tools(self):
        return [{"name": n, "description": ""} for n in self._tool_names]
