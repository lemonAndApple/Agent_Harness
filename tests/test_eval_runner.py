from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = ROOT / "agents"
sys.path.insert(0, str(AGENTS_DIR))

# 导入 Agent_Harness 需要环境变量（MODEL_ID 等）
os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test")

import Agent_Harness as A  # noqa: E402
from eval_runner import run_episode as eval_run_episode  # noqa: E402


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(id_: str, name: str, input_: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def _resp(content, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(content=content, stop_reason=stop_reason)


@pytest.fixture()
def fake_client(monkeypatch):
    """把 client.messages.create 换成可编排的假响应。"""

    class FakeMessages:
        responses = []

        def create(self, **kwargs):
            return self.responses.pop(0) if self.responses else _resp([_text_block("done")])

    fake = FakeMessages()
    monkeypatch.setattr(A.client, "messages", fake)
    return fake


def test_extract_final_reply_text(monkeypatch):
    history = [{"role": "user", "content": "hi"},
               {"role": "assistant", "content": [_text_block("hello world")]}]
    assert A.extract_final_reply(history) == "hello world"


def test_extract_final_reply_empty():
    assert A.extract_final_reply([]) == ""


def test_run_episode_simple(monkeypatch, tmp_path, fake_client):
    """验收：无 stdin 下 run_episode 能跑完并返回文本。"""
    fake_client.responses = [_resp([_text_block("目录内容")])]
    transcript = tmp_path / "ep.jsonl"

    history, final = A.run_episode("列出当前目录", transcript_path=transcript)

    assert final == "目录内容"
    assert history[0] == {"role": "user", "content": "列出当前目录"}
    assert history[-1]["role"] == "assistant"
    # 会话记录：JSONL 写入磁盘
    assert transcript.exists()
    lines = transcript.read_text(encoding="utf-8").strip().splitlines()
    assert lines, "transcript should not be empty"
    assert json.loads(lines[0])["role"] == "user"


def test_run_episode_tool_round(monkeypatch, tmp_path, fake_client):
    """工具调用轮：模型先 tool_use 再 end_turn，验证 tool_result 回写。"""
    fake_client.responses = [
        _resp([_tool_use_block("t1", "bash", {"command": "echo hi"})], stop_reason="tool_use"),
        _resp([_text_block("finished")]),
    ]
    transcript = tmp_path / "ep2.jsonl"

    history, final = A.run_episode("run a command", transcript_path=transcript)

    assert final == "finished"
    # 找 tool_result 回写
    tool_results = [
        part for m in history
        if m["role"] == "user" and isinstance(m.get("content"), list)
        for part in m["content"]
        if isinstance(part, dict) and part.get("type") == "tool_result"
    ]
    assert tool_results, "tool_result should be present"
    assert "hi" in tool_results[0]["content"]


def test_run_episode_max_rounds(monkeypatch, tmp_path, fake_client):
    """max_rounds 兜底限制：模型一直调工具也不至于无限循环。"""
    A.MODEL = "test-model"
    calls = {"n": 0}

    def sometimes_tool_use(**kwargs):
        calls["n"] += 1
        if calls["n"] < 5:
            return _resp([_tool_use_block("tx", "bash", {"command": "echo x"})], stop_reason="tool_use")
        return _resp([_text_block("ok stop")])

    fake_client.create = sometimes_tool_use
    transcript = tmp_path / "ep3.jsonl"

    history, final = A.run_episode("loop", transcript_path=transcript, max_rounds=2)

    # 达到上限后注入停止提示并收敛，transcript 写入磁盘
    assert transcript.exists()
    assert any("max_rounds reached" in str(m.get("content", "")) for m in history)


def test_reset_runtime_state(monkeypatch):
    """全局状态隔离：连续两次 episode 不互相污染。"""
    old_task_mgr = A.TASK_MGR
    old_memory = A.MEMORY
    old_cron = A.CRON
    old_perms = A.PERMS

    # 注入一些内存中的过期状态
    A.MEMORY.memories["stale"] = {"description": "x", "type": "project", "content": "x", "file": "x"}
    A.CRON.tasks.append({"id": "stale-cron", "cron": "* * * * *", "prompt": "x", "recurring": True, "durable": False, "createdAt": 0})

    A.reset_runtime_state()

    # 所有单例被重建为新对象（内存状态隔离）
    assert A.MEMORY is not old_memory
    assert A.TASK_MGR is not old_task_mgr
    assert A.CRON is not old_cron
    assert A.PERMS is not old_perms
    assert "stale" not in A.MEMORY.memories
    assert A.CRON.tasks == []
    assert A.PERMS.mode == "build"


def test_eval_mode_auto_allow(monkeypatch):
    """eval 模式下 ask_user 直接放行，check 不返回 ask。"""
    perms = A.PermissionManager(mode="eval")
    assert perms.check("bash", {"command": "echo hi"})["behavior"] == "allow"
    assert perms.ask_user("bash", {"command": "echo hi"}) is True


def test_eval_mode_deny_rules_still_enforced(monkeypatch):
    """eval 模式仍遵守 deny 黑名单（如 sudo）。"""
    perms = A.PermissionManager(mode="eval")
    assert perms.check("bash", {"command": "sudo rm -rf /"})["behavior"] == "deny"


def test_eval_runner_result_shape(monkeypatch, tmp_path, fake_client):
    """eval_runner.run_episode 返回结果字典结构。"""
    fake_client.responses = [_resp([_text_block("ok")])]
    result = eval_run_episode("test", workdir=None, transcript=tmp_path / "r.jsonl")
    assert result["final_reply"] == "ok"
    assert result["rounds"] >= 1
    assert "elapsed_s" in result
