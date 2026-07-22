"""compaction 真实 LLM 集成测试。

验证 generate_summary 和 compact 的真实调用路径（DeepSeek）。
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

from pi_agent_core import (  # noqa: E402
    CompactionSettings,
    compact,
    generate_summary,
)
from pi_ai import AssistantMessage, Model, ModelCost, TextContent, UserMessage  # noqa: E402

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


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextContent(text=text)], api="x", provider="x", model="x")


@pytest.mark.skipif(not DEEPSEEK_AVAILABLE, reason="未设置 DEEPSEEK_API_KEY")
async def test_generate_summary_real():
    """真实调用：generate_summary 生成对话摘要。"""
    model = _deepseek_model()
    api_key = os.environ["DEEPSEEK_API_KEY"]

    messages = [
        UserMessage(content="我叫小明，今年25岁"),
        _assistant("你好小明！"),
        UserMessage(content="我喜欢打篮球和编程"),
        _assistant("很好的爱好！篮球锻炼身体，编程锻炼思维。"),
    ]

    summary = await generate_summary(model, messages, api_key=api_key)

    # 摘要应非空
    assert len(summary) > 0, "摘要为空"
    # 摘要应包含关键信息（名字或爱好）
    summary_lower = summary.lower()
    has_key_info = any(k in summary for k in ["小明", "篮球", "编程", "25"]) or any(
        k in summary_lower for k in ["basketball", "programming", "ming"]
    )
    assert has_key_info, f"摘要未包含关键信息: {summary}"
    print(f"\n[generate_summary] 摘要: {summary[:200]}")


@pytest.mark.skipif(not DEEPSEEK_AVAILABLE, reason="未设置 DEEPSEEK_API_KEY")
async def test_compact_real():
    """真实调用：compact 执行上下文压缩。

    构造一个足够长的对话，用小 keep_recent_tokens 强制产生切割点，
    验证 summary + retained_tail 的完整性。
    """
    model = _deepseek_model()
    api_key = os.environ["DEEPSEEK_API_KEY"]

    # 构造多轮对话
    messages = [
        UserMessage(content="第一步：创建一个 Python 项目"),
        _assistant("好的，首先创建项目目录，然后初始化 pyproject.toml"),
        UserMessage(content="第二步：添加依赖"),
        _assistant("在 pyproject.toml 里加入 pydantic 和 openai"),
        UserMessage(content="第三步：写主逻辑"),
        _assistant("创建 main.py，导入需要的模块，实现核心功能"),
        UserMessage(content="第四步：写测试"),
        _assistant("创建 tests 目录，用 pytest 写单元测试"),
        UserMessage(content="最后一步：提交代码"),
        _assistant("用 git add 和 git commit 提交所有改动"),
    ]

    # 用很小的 keep_recent_tokens，强制压缩大部分
    settings = CompactionSettings(enabled=True, keep_recent_tokens=30, reserve_tokens=1000)
    result = await compact(model, messages, settings, api_key=api_key)

    # summary 应非空
    assert len(result.summary) > 0, "摘要为空"
    print(f"\n[compact] 摘要: {result.summary[:200]}")

    # retained_tail 应有内容（保留的最近消息）
    assert len(result.retained_tail) > 0, "retained_tail 为空"
    print(f"[compact] 保留消息数: {len(result.retained_tail)}")

    # removed_count > 0（有消息被压缩了）
    assert result.removed_count > 0, f"removed_count={result.removed_count}，未压缩任何消息"
    print(f"[compact] 压缩了 {result.removed_count} 条消息")

    # 验证总消息数一致
    total = result.removed_count + len(result.retained_tail)
    assert total == len(messages), f"消息数不一致: {total} != {len(messages)}"


@pytest.mark.skipif(not DEEPSEEK_AVAILABLE, reason="未设置 DEEPSEEK_API_KEY")
async def test_compact_no_cut_needed():
    """对话很短时，compact 全部保留，不发 LLM 请求。"""
    model = _deepseek_model()
    api_key = os.environ["DEEPSEEK_API_KEY"]

    messages = [UserMessage(content="hi"), _assistant("hello")]
    settings = CompactionSettings(enabled=True, keep_recent_tokens=100000)
    result = await compact(model, messages, settings, api_key=api_key)

    # 全部保留
    assert result.summary == ""
    assert len(result.retained_tail) == 2
    assert result.removed_count == 0
    print("\n[compact no-cut] 全部保留，未调用 LLM ✓")
