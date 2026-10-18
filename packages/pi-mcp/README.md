# pi-mcp

Python port of [`@earendil-works/pi-mcp`](https://github.com/earendil-works/pi/tree/main/packages/mcp) **v1.3.0** —
a standalone [Model Context Protocol](https://modelcontextprotocol.io) client.

不依赖 pi 运行时，也不依赖 pi-py 的其它包：**唯一的第三方依赖是 `httpx`**（上游唯一的
外部依赖是 `cross-spawn`，Python 侧对应 `asyncio.create_subprocess_exec`）。

## 安装

```bash
pip install pi-py-mcp
```

## 快速上手：连接一个 stdio MCP 服务端

```python
import asyncio

from pi_mcp import McpClient, McpClientOptions, StdioTransport, StdioTransportOptions, to_llm_content


async def main() -> None:
    transport = StdioTransport(StdioTransportOptions(command="npx", args=("-y", "@modelcontextprotocol/server-everything")))
    client = McpClient(McpClientOptions(name="my-app", version="1.0.0"))

    await client.connect(transport)
    print(client.server_info, client.protocol_version)

    for tool in await client.list_tools():
        print(tool["name"], "-", tool.get("description"))

    result = await client.call_tool("echo", {"text": "hello"})
    for block in to_llm_content(result):
        print(block)

    await client.close()


asyncio.run(main())
```

## 三种传输

| 传输 | 用途 | 上游对应 |
|---|---|---|
| `StdioTransport` | 以**换行分帧**的 JSON-RPC 驱动子进程服务端；POSIX 下自建进程组，关闭时按组终止 | `transports/stdio.ts` |
| `StreamableHttpTransport` | `POST` + 可选 SSE 响应、`Mcp-Session-Id` 会话、服务端→客户端 GET 流与断线续传 | `transports/streamable-http.ts` |
| `InMemoryTransport` | 进程内一对互连传输（测试/内嵌 server），经 `pi_mcp.testing` 也可导入 | `transports/in-memory.ts` |

## 取消与超时

沿用本仓约定，用 `asyncio.Event` 当取消信号（上游是 `AbortSignal`）：

```python
import asyncio

from pi_mcp import McpRequestOptions

cancel = asyncio.Event()
task = asyncio.create_task(client.call_tool("slow", {}, McpRequestOptions(signal=cancel, timeout_ms=5_000)))
cancel.set()          # 服务端会收到 notifications/cancelled（initialize 除外，规范禁止取消它）
```

进度回调会**重置超时**：

```python
await client.call_tool("long", {}, McpRequestOptions(timeout_ms=30_000, on_progress=print))
```

## 内容转换

`to_llm_content()` 把工具结果转成模型可接受的文本/图像块（与 pi-ai 的 `TextContent` /
`ImageContent` 同形）：文本与图像原样通过，内嵌文本资源变文本、内嵌图像资源变图像，
音频/资源链接/二进制资源变成一句短文本占位；没有内容块但有 `structuredContent` 时用它的 JSON。

## 范围（第一期）

✅ 已提供：`McpClient`、JSON-RPC 与内容类型、三种传输、SSE 解析与重连、`to_llm_content`、
认证交接所需的类型（`AuthProvider` / `UnauthorizedContext`）。

🕓 **第二期**：OAuth（上游 `src/oauth/` 7 个文件：flow / discovery / provider / callback /
errors / types / index）。理由与当前状态见 [`PORTING.md`](./PORTING.md)。

## 链接

- 上游上游包：`@earendil-works/pi-mcp`
- 移植注记：[`PORTING.md`](./PORTING.md)
