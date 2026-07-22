"""pi-coding-agent 真实 LLM 集成测试。

用 DeepSeek 验证 CodingAgent 的完整工具调用链路。
默认跳过，-m integration 运行。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

_load_path = Path(__file__).resolve().parents[3] / ".env"
if _load_path.exists():
    load_dotenv(_load_path)

from pi_ai import Model, ModelCost  # noqa: E402
from pi_coding_agent import CodingAgent  # noqa: E402

pytestmark = pytest.mark.integration

DEEPSEEK_AVAILABLE = bool(os.environ.get("DEEPSEEK_API_KEY"))


def _deepseek_model() -> Model:
    return Model(
        id="deepseek-chat",
        name="DeepSeek",
        api="openai-completions",
        provider="deepseek",
        base_url="https://api.deepseek.com/v1",
        reasoning=False,
        input=["text"],
        cost=ModelCost(input=0.14, output=0.28),
        context_window=64000,
        max_tokens=8192,
    )


@pytest.mark.skipif(not DEEPSEEK_AVAILABLE, reason="未设置 DEEPSEEK_API_KEY")
async def test_agent_reads_file(tmp_path):
    """Agent 真实读取文件：读 → 报告内容。"""
    api_key = os.environ["DEEPSEEK_API_KEY"]
    (tmp_path / "target.txt").write_text("The answer is 42.\n")

    agent = CodingAgent(
        model=_deepseek_model(),
        api_key=api_key,
        cwd=str(tmp_path),
        tool_names=["read", "bash"],
    )
    await agent.prompt("读取 target.txt 的内容，然后告诉我答案是什么。")

    # 最后一条 assistant 消息应含 42
    msgs = agent.state.messages
    last = msgs[-1]
    assert "42" in last.content[0].text if last.content else False
    print(f"\n[coding-agent read] 回复: {last.content[0].text[:200] if last.content else '(空)'}")


@pytest.mark.skipif(not DEEPSEEK_AVAILABLE, reason="未设置 DEEPSEEK_API_KEY")
async def test_agent_writes_file(tmp_path):
    """Agent 真实写文件：prompt → write → 验证文件。"""
    api_key = os.environ["DEEPSEEK_API_KEY"]

    agent = CodingAgent(
        model=_deepseek_model(),
        api_key=api_key,
        cwd=str(tmp_path),
        tool_names=["write", "read"],
    )
    await agent.prompt("创建一个文件 hello.py，内容是一个打印 'Hello World' 的 Python 脚本。")

    # 验证文件被创建
    hello = tmp_path / "hello.py"
    assert hello.exists(), "文件未被创建"
    content = hello.read_text()
    assert "Hello World" in content or "hello world" in content.lower()
    print(f"\n[coding-agent write] hello.py:\n{content}")


@pytest.mark.skipif(not DEEPSEEK_AVAILABLE, reason="未设置 DEEPSEEK_API_KEY")
async def test_agent_edits_file(tmp_path):
    """Agent 真实编辑文件：read → edit → 验证修改。"""
    api_key = os.environ["DEEPSEEK_API_KEY"]
    (tmp_path / "config.py").write_text("DEBUG = False\nTIMEOUT = 30\n")

    agent = CodingAgent(
        model=_deepseek_model(),
        api_key=api_key,
        cwd=str(tmp_path),
        tool_names=["read", "edit"],
    )
    await agent.prompt("读取 config.py，然后把 DEBUG 改成 True。")

    content = (tmp_path / "config.py").read_text()
    assert "DEBUG = True" in content
    print(f"\n[coding-agent edit] config.py:\n{content}")


@pytest.mark.skipif(not DEEPSEEK_AVAILABLE, reason="未设置 DEEPSEEK_API_KEY")
async def test_agent_bash_and_ls(tmp_path):
    """Agent 用 bash/ls 探索目录。"""
    api_key = os.environ["DEEPSEEK_API_KEY"]
    (tmp_path / "a.py").write_text("print('a')")
    (tmp_path / "b.py").write_text("print('b')")
    (tmp_path / "c.txt").write_text("text")

    agent = CodingAgent(
        model=_deepseek_model(),
        api_key=api_key,
        cwd=str(tmp_path),
        tool_names=["ls", "bash"],
    )
    await agent.prompt("列出当前目录下有哪些文件？有几个 Python 文件？")

    msgs = agent.state.messages
    last = msgs[-1]
    text = last.content[0].text if last.content else ""
    # 应提到文件名
    assert "a.py" in text or "2" in text
    print(f"\n[coding-agent ls/bash] 回复: {text[:200]}")
