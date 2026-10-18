# pi-agent-core 移植注记

对应上游：[`@earendil-works/pi-agent-core`](https://github.com/earendil-works/pi/tree/main/packages/agent)（v1.3.0）

> 上游在 v1.0.0 把本包的实验性 ``harness`` 整体**删除**（commit `7fd478a2`：
> removed 290 / added 0 / renamed 0），改由全新的 `@earendil-works/pi-durable`
> 承担会话/持久任务/文档运行时。侦察证据见
> [`../../recon/02-upstream-runtime-restructure.md`](../../recon/02-upstream-runtime-restructure.md)。
> v1.3.0 时上游本包只剩 6 个 `.ts` 文件：`agent.ts` / `agent-loop.ts` / `proxy.ts` /
> `stream-fn.ts` / `types.ts` / `index.ts`。

## 有意偏离上游

| 上游 | 本包 | 原因 |
|---|---|---|
| typebox 类型 | Pydantic v2 | 详见 pi-ai/PORTING.md |
| Promise / ReadableStream | asyncio + AsyncGenerator | 同上 |
| 标准日志 | stdlib `logging` | 不引入私有日志包 |
| storage 直接耦合 | 通过抽象接口，storage 为可选后端 | 保持 agent-core 与存储解耦 |
| 上游 v1.0.0 起**不再含**会话/技能/压缩 | 保留为自己的 `pi_agent_core.sessions`（技能 / 会话 / 压缩） | 本端是精简 SDK，尚无 `pi-durable` 等价物；能力保留，但**明确登记为本地扩展**，不再声称对应上游同名物 |
| `AgentContext.system_prompt`（上游已把 system prompt 交给 transcript 的 `SystemMessage` 承载） | 仍由 `AgentContext.system_prompt` 承载 | 上游 `SystemMessage` / `sections` / `TranscriptContext` 的移植在 pi-ai 侧，未完成前不在此包自造 |

## cherry-pick

（暂无）

## v1.3.0 同步说明

本包对齐上游 v1.3.0 的公开 API（`/tmp/pi-up/v1.3.0/packages/agent/src/`）。

### ⚠️ BREAKING 1：`should_stop_after_turn` → `finish_turn`

上游 v0.87.0 移除了 `AgentOptions.shouldStopAfterTurn` 与
`AgentLoopConfig.shouldStopAfterTurn`，改为 `finishTurn`。本端同步移除，**无兼容别名**。

| 旧（v0.85.1，本端已移除） | 新（v1.3.0） |
|---|---|
| `AgentOptions(should_stop_after_turn=fn)` | `AgentOptions(finish_turn=fn)` |
| `AgentLoopConfig(should_stop_after_turn=fn)` | `AgentLoopConfig(finish_turn=fn)` |
| `fn(turn) -> bool`，`True` 即终止 | `fn(turn, cancel_event) -> "end" \| "continue" \| None` |
| —— | 也接受上游原样 `{"action": "end" / "continue"}` |
| —— | 也接受 `True` / `False`（迁移便利，见下） |

时序（对齐上游 `agent-loop.ts`）：

1. `finish_turn` 在 assistant 消息**与全部工具结果**定稿之后、`turn_end` **之前**运行；
2. 其决策在 `turn_end` **之后**生效；
3. **error / aborted 响应也会调用它**（此时 `turn.tool_results == []`），但那两类是硬退出，
   **决策被忽略**；
4. `{"action": "end"}`：本次 run 在 `turn_end` 后立即结束，不再轮询 steering / follow-up；
5. `{"action": "continue"}`：保证再发起**一次** provider 请求。若工具结果 / steering /
   follow-up 已经自然选中下一轮，则由它们满足，不额外追加；
6. `None`：保持默认调度。

迁移示例（与上游 CHANGELOG 的 `shouldStopAfterTurn` 迁移段一致）：

```python
# 旧
def should_stop(turn):            # turn 当时是 dict
    return finished(turn["message"])

AgentOptions(should_stop_after_turn=should_stop)

# 新（注意对 error / aborted 返回 None，否则会改变原谓词「仅普通响应」的语义）
async def finish_turn(turn, cancel_event):
    if turn.message.stop_reason in ("error", "aborted"):
        return None
    return {"action": "end"} if finished(turn.message) else None

AgentOptions(finish_turn=finish_turn)
```

> 迁移便利：`finish_turn` 也接受 `True`（等价 `{"action": "end"}`）与 `False`/`None`
> （等价「不干预」），便于把旧谓词直接搬过来。上游没有这个宽松形式。

### ⚠️ BREAKING 2：`harness/` → `sessions/`

`pi_agent_core.harness.*` 全部公开导入路径已移除。原因：上游的「harness」在 v1.0.0 之后
指向 `pi-durable` 里一个 25k 行的**不同东西**（Conversation / Task / Document / 原子提交），
本端这套精简实现与它并非同构，继续同名会持续误导。

| 旧导入（已移除） | 新导入 |
|---|---|
| `from pi_agent_core.harness import compaction` | `from pi_agent_core.sessions import compaction` |
| `from pi_agent_core.harness.session import SessionEntry` | `from pi_agent_core.sessions.session import SessionEntry` |
| `from pi_agent_core.harness.session import _deserialize_message` | `from pi_agent_core.sessions.session import _deserialize_message` |
| `from pi_agent_core.harness.skills import ...` | `from pi_agent_core.sessions.skills import ...` |

**根包导出不变**：`from pi_agent_core import Session, SessionEntry, SessionStorage,
InMemorySessionStorage, JsonlSessionStorage, Skill, load_skills, compact, ...`
仍然可用（`__init__.py` 只改了内部 import 路径）。

### 新增 API

| 上游 v1.3.0 | 本端 |
|---|---|
| `AgentLoopConfig.prepareRequest` | `AgentLoopConfig.prepare_request`（`AgentOptions.prepare_request`） |
| `PrepareRequestContext` | `PrepareRequestContext(context, model, thinking_level)` dataclass |
| `AgentRequestUpdate` | `AgentRequestUpdate(context=None, model=None, thinking_level=None)`；也接受等价 dict |
| `AgentTurnContext` | `AgentTurnContext(message, tool_results, context, new_messages)` dataclass，同时支持属性与下标访问 |
| `AgentTurnDecision` / `FinishTurn` | `AgentTurnDecision`（`Literal["continue","end"]`）/ `FinishTurn` |
| `AgentTool.outputSchema` | `AgentTool.output_schema`（`dict \| None`） |
| `AgentToolResult.structuredContent` | `AgentToolResult.structured_content` |
| `AgentToolResult.isError` | `AgentToolResult.is_error` |
| `AgentToolResult.usage` | `AgentToolResult.usage`（并透传到 `ToolResultMessage.usage`） |
| `AgentState.systemPrompt` 变 `readonly` | `AgentState.system_prompt` 改为只读 property |

- `prepare_request` 在**每次**会话型 provider 请求前运行（**含第一次**），且在待处理
  （steering）消息已追加并发出之后。
- `structured_content` **不写入 transcript**（对齐上游「Not sent to the model」），
  只经 `tool_execution_end` 事件与 `after_tool_call` 钩子交给程序调用方。
- `AgentState.system_prompt` 只在构造 / `initial_state` 时写入，之后赋值抛
  `AttributeError`。
- 附带修正：`AgentToolResult.added_tool_names` / `usage` 此前被丢弃，现透传到
  `ToolResultMessage`。

### 仍未移植（本轮裁剪范围，明示）

- 上游 `SystemMessage` / `sections` / `TranscriptContext`（**pi-ai 侧**，不在本包自造）；
- `AgentTool.replay`、`AgentToolCallOutcome.durationMs`（依赖 durable / pi-ai 侧改动）；
- `prepareNextTurn` 的 `messages` 字段（上游 `AgentLoopTurnUpdate.messages`）；
- `@earendil-works/pi-durable` 的会话/任务/文档运行时（后续批次，见侦察报告 §6 方案 C）。

## v0.85.1 同步说明

- **``prepare_next_turn`` 时机对齐**：移至 turn_end 之后、**下一轮 turn_start 之前**
  执行，且仅当循环继续时调用；可返回 ``{"context", "model", "thinkingLevel"}``
  替换下一轮状态（``"off"`` 清除 reasoning）。准备期间（如压缩）排队的 steering
  消息在下一轮注入前补拉。
- ~~``should_stop_after_turn`` 先于 ``prepare_next_turn`` 看到已完成轮次上下文。~~
  **已在 v1.3.0 同步中移除**（见上「BREAKING 1」）。
- ``agent_loop_continue`` 路径补发首个 ``turn_start``（对齐上游两个入口均在
  runLoop 外发首个 turn_start）。
- 工具准备阶段（参数校验 + before 钩子）完成后若已取消，不再执行工具，产出
  ``"Operation aborted"`` 错误结果（覆盖并行批次内尚未轮到的调用）。
- skills：根目录普通 ``.md`` 文件必须带非空 ``description`` frontmatter 才算
  技能，否则静默跳过（不产生诊断）；``SKILL.md`` 照常报诊断。
- **未移植**（裁剪范围，v0.85.1 时的判断）：上游本轮 agent 包重写为 harness v3 /
  durable drive（storage-backed sessions、branches/lanes/values、runtime2、
  invocation context 贯穿、``AgentTool.replay``、``BranchSummaryMessage.fromId``
  可空化、conformance/benchmark 测试设施、session JSONL v4 与 legacy v3 导入）。
  → **v1.3.0 已验证：这套东西并没有「后来搬进本包」，而是随 harness 一起被上游删除**
  （见 `recon/02-upstream-runtime-restructure.md` §3.1）。因此该裁剪是**永久性**的，
  不是暂缓。

## v0.84.1 同步说明（破例同步 patch）

- ``should_stop_after_turn`` 钩子接入 ``_run_loop``（每轮 ``TurnEnd`` 后询问，返回真则终止），并在 ``Agent`` / ``AgentOptions`` 上公开。（**v1.3.0 已移除，见上「BREAKING 1」**）
- ``before_tool_call`` 返回 ``block + terminate`` 时，被 block 的工具调用可终止后续轮次。
- ``Agent.reset()`` 在活跃运行期间拒绝并抛错。
- 上游 harness v2（reducer / telemetry / JSONL codec / session 仓库化 / conformance）仍属裁剪范围，未追逐。
- 本包同步版本、上游引用与对 ``pi-ai>=0.84.1,<0.85`` 的依赖约束。

## v0.83.0 同步说明

- 上游 agent 包在本轮没有落入当前精简 Python runtime 的行为变更。
- 本包仅同步版本、上游引用和对 `pi-ai>=0.83.0,<0.84` 的依赖约束。

## v0.82.1 同步说明

- Compaction/summary 请求使用独立 routing session，并强制
  `cache_retention="none"`，避免污染主会话缓存。
- Agent tools 会将 `constrained_sampling` 透传到 pi-ai。
- 上游新增的 Harness execution tools 与本仓库 `pi-coding-agent` 工具集职责重叠；
  当前精简 Harness 尚未公开 `ExecutionEnv`/`toolContext`，因此未引入不完整兼容层。

## 待办

- [x] agent-loop.ts（无状态循环引擎）
- [x] agent.ts（有状态 Agent 封装）
- [x] sessions/（skills / session / compaction，原名 harness/）
- [x] proxy（修复旧版导入位置错误）
- [x] v1.3.0 API 对齐（finish_turn / prepare_request / output_schema / system_prompt 只读）
- [ ] `prepareNextTurn` 的 `messages` 字段（上游 `AgentLoopTurnUpdate.messages`）
- [ ] `AgentTool.replay` / `AgentToolCallOutcome.durationMs`（依赖 durable / pi-ai 侧改动）
- [ ] `pi-durable` 等价物（若决定追上游 durable，见 `recon/02-upstream-runtime-restructure.md` §6 方案 C）
