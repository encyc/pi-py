# pi-mcp 移植注记

对应上游：[`@earendil-works/pi-mcp`](https://github.com/earendil-works/pi/packages/mcp) **v1.3.0**
（`packages/mcp/`：3,277 行 TS / 18 文件，外部依赖只有 `cross-spawn`）。

---

## 期次划分（本包按**依赖**而不是按目录分期）

| 期 | 范围 | 状态 |
|---|---|---|
| **第一期** | `client.ts`、`protocol/`（content/jsonrpc/types）、`transports/`（transport/stdio/streamable-http/in-memory）、`testing/`、`index.ts`、**以及 `auth-provider.ts` 的类型声明** | ✅ 本批次 |
| **第二期** | `oauth/`（`errors` / `types` / `flow` / `discovery` / `provider` / `callback` / `index`，共 7 文件 1,349 行） | ✅ 已落地（见下「第二期交付」） |

### 为什么 `auth-provider.ts` 的**类型**属于第一期

上游任务书按**目录**把它划入第二期，但 `transports/streamable-http.ts` **硬依赖**它的三个类型
（`AuthProvider` / `McpFetch` / `UnauthorizedContext`）来实现 401/403 的授权交接
（`streamable-http.ts:295-317` 的 `authorizedFetch`）。

**这是「按目录分期」而不是「按依赖分期」会踩的坑** —— 与 `pi-durable` 批次里
「E 依赖 C/D」是同一类错误。若把类型也推到第二期，第一期 transport 就缺一条分支，
第二期还得回头改第一期文件。

因此本包的切法：

- 第一期提供 `pi_mcp.auth_provider`：**只有类型**（`McpFetch` 除外，见下）+ 一个非交互的
  最简实现 `BearerAuthProvider`（注入现成令牌），以及 transport 里那段 401/403 交接逻辑；
- 第二期只做 OAuth **实体**（发现、动态客户端注册、PKCE 流程、回调服务器、令牌存储），
  对第一期是**纯新增**。

### 为什么最终把 OAuth 一起做了（第二期交付）

原本的理由是：**OAuth 交互式登录（浏览器回调服务器 + 页面渲染）属于「凭据获取的用户界面」，
不属于调用协议**，与 pi-py 在 `pi-ai` 里已确立的口径一致。

**该理由被证据推翻**：`pi-coding-agent` 的 `extensions/mcp/oauth.ts`（被 `src/index.ts`
re-export 的公开 API）直接 import 了 `authorizeMcp`、`OAuthCallbackServer`、`OAuthCallback`、
`stepUpScope` —— 只做 discovery + provider 会让下游**仍然编译不过**。lead 据此改为
「一起做完」，并记录了区分：pi-ai 排除的是 **provider 的交互式登录**，而 MCP OAuth 是
**接入第三方 MCP 服务端所必需的协议流程**。

于是第二期**一次交付**：`errors` + `types` + `discovery` + `provider` + `flow` + `callback` + `index`。

---

## 模块对应表

| 上游 | 本包 | 上游行数 |
|---|---|---:|
| `src/index.ts` | `pi_mcp/__init__.py` | 72 |
| `src/client.ts` | `pi_mcp/client.py` | 621 |
| `src/protocol/jsonrpc.ts` | `pi_mcp/protocol/jsonrpc.py` | 113 |
| `src/protocol/content.ts` | `pi_mcp/protocol/content.py` | 118 |
| `src/protocol/types.ts` | `pi_mcp/protocol/types.py` | 131 |
| `src/transports/transport.ts` | `pi_mcp/transports/transport.py` | 64 |
| `src/transports/in-memory.ts` | `pi_mcp/transports/in_memory.py` | 51 |
| `src/transports/stdio.ts` | `pi_mcp/transports/stdio.py` | 216 |
| `src/transports/streamable-http.ts` | `pi_mcp/transports/streamable_http.py` | 521 |
| `src/testing/index.ts` | `pi_mcp/testing/__init__.py` | 1 |
| `src/auth-provider.ts` | `pi_mcp/auth_provider.py`（类型 + 401/403 交接） | 20 |
| `src/oauth/errors.ts` | `pi_mcp/oauth/errors.py` | 55 |
| `src/oauth/types.ts` | `pi_mcp/oauth/types.py` | 209 |
| `src/oauth/discovery.ts` | `pi_mcp/oauth/discovery.py` | 215 |
| `src/oauth/flow.ts` | `pi_mcp/oauth/flow.py` | 482 |
| `src/oauth/provider.ts` | `pi_mcp/oauth/provider.py` | 168 |
| `src/oauth/callback.ts` | `pi_mcp/oauth/callback.py` | 164 |
| `src/oauth/index.ts` | `pi_mcp/oauth/__init__.py` | 56 |

`package.json` 的 `exports` → 模块路径：

| 上游 subpath | 本包 |
|---|---|
| `@earendil-works/pi-mcp` | `pi_mcp` |
| `@earendil-works/pi-mcp/testing` | `pi_mcp.testing` |
| `@earendil-works/pi-mcp/oauth` | `pi_mcp.oauth` |

---

## 有意偏离上游

| # | 上游 | 本包 | 理由 |
|---|---|---|---|
| D1 | `cross-spawn`（`stdio.ts:3,95`） | `asyncio.create_subprocess_exec` | 语言对等。POSIX 下 `detached: true` → `start_new_session=True`，`process.kill(-pid)` → `os.killpg` |
| D2 | `process.once("exit")` 兜底（`stdio.ts:46`） | `atexit.register` | 同上 |
| D3 | `fetch` + `ReadableStream`（`streamable-http.ts:211`） | `httpx.AsyncClient` + `send(stream=True)` + `aiter_bytes()` | Python 侧唯一成熟方案；已在 `pyproject.toml` 显式声明 `httpx>=0.27` |
| D4 | `AbortSignal` + `.reason` | `asyncio.Event`（`cancel_event`）+ `McpRequestOptions.cancel_reason` | 本仓既有约定（`pi-ai`/`pi-agent-core` 都用 `asyncio.Event`）；Event 没有 reason，故单列一个字段 |
| D5 | `AbortController`（transport 级，`streamable-http.ts:198`） | `asyncio.Event` + 关闭时 `aclose()` 在途响应 | httpx 不接受外部 signal；见「已知简化」 |
| D6 | `McpFetch`（可注入的 WHATWG fetch） | 名字**保留导出**，但等价于 `httpx.AsyncClient`；真正的注入点是 `StreamableHttpTransportOptions.client` / 各 OAuth 函数的 `client=` | 注入 fetch 的动机是「替换传输实现」，注入客户端同样达成，且不必自造 WHATWG `Request`/`Response` 子集。名字保留是为了让下游（`pi-coding-agent` 的 mcp 扩展）import 形状与上游一致 |
| D7 | `structuredClone`（`in-memory.ts:23`） | `copy.deepcopy` | 语言对等 |
| D8 | `queueMicrotask`（`in-memory.ts:24`） | `loop.call_soon` + 让出一次事件循环 | **行为对齐**：Node 里 `await send()` 返回时微任务已跑过，上游测试因此能在 `connect()` 后立即断言服务端收到的消息；Python 的 `call_soon` 要等下一轮循环，故补 `await asyncio.sleep(0)` |
| D9 | `interface` → `interface` | `TypedDict`（线上形状）与 `@dataclass`（配置对象） | 线上对象必须**原样保留未知键**（上游到处 `{ ...item }`），dataclass 会丢键；配置对象用 dataclass 更符合 Python 习惯 |
| D10 | 错误类 `extends Error` | `Exception` 子类 | 语言对等；`McpAbortError.name` 仍保留上游的 `"AbortError"` |
| D11 | `JSON-RPC id` 允许 number | `str \| int \| float`，且 `bool` 不算（`jsonrpc.py:is_json_rpc_id`） | Python 里 `bool` 是 `int` 子类，不排除会让 `True` 被当成合法 id |

---

## `OAuthCallbackServer` 的安全边界（本端逐条登记）

它会在本机**回环地址**上绑定一个 TCP 端口、接收浏览器带来的 `?code=&state=`，并把一个页面
渲染给浏览器。逐条差异与处置：

| # | 关注点 | 上游（`callback.ts`） | 本端 | 处置 |
|---|---|---|---|---|
| S1 | 绑定的地址 | `host ?? "127.0.0.1"` | 同 | **只绑回环**，不接受 `0.0.0.0`；调用方显式传 `host` 才能改 |
| S2 | `redirect_url` 的主机名 | `redirectHost ?? host`（例如注册用 `localhost` 而实际监听 `127.0.0.1`） | 同 | 保留；IPv6 字面量会加方括号（`[::1]`） |
| S3 | `state` 校验 | 必须匹配某个在等的 `state`，否则 400 | 同 | 不匹配时**不区分**「不存在」与「已过期」，避免探测 |
| S4 | 路径白名单 | `paths = [path, ...extraPaths]`；不在内 → 404 | 同 | — |
| S5 | 服务端专属路径（RFC 9700 §4.4.2.2） | `waitForCallback(state, path)`：响应落在别的路径则失败 | 同（`wait_for_callback(state, path)`） | 防止「另一个 redirect URI 的 code」被误用 |
| S6 | 页面内容 | 默认**纯文本**；`renderPage` 才出 HTML | 同 | 默认路径无 XSS 面；用 `render_page` 时**转义是调用方的责任**（上游亦然） |
| S7 | 缓存 | HTML 分支发 `cache-control: no-store` | 同 | 授权码不进浏览器缓存 |
| S8 | 日志 | — | 本端 `log_message` 被覆写成空 | 授权码等在 URL 里，**不写访问日志**（上游 `node:http` 默认也不打） |
| S9 | 超时 | `timeoutMs ?? 5min`，超时 reject | 同（`asyncio.TimerHandle`） | 端口不会一直被占着 |
| S10 | 关闭 | reject 所有在等的回调 + 关服务器 | 同（`close()`） | — |
| S11 | 重复 `state` | 抛「already pending」 | 同 | 防同一 `state` 被两次等待 |
| S12 | HTTP 栈 | `node:http`，单进程事件循环 | `http.server.ThreadingHTTPServer` 跑在**后台线程**，通过 `loop.call_soon_threadsafe` 交回事件循环 | Python 没有内建 async HTTP **服务器**；线程 + 线程安全交接是最小实现 |

### 端口选取：上游踩过的坑与本端的处理

上游在**别处**（`pi-ai` / coding-agent 的浏览器登录，不是 pi-mcp）踩过固定端口的坑：

- `coding-agent/CHANGELOG.md:160` —— Anthropic 浏览器登录在端口 53692 被占用或被
  **Hyper-V/WSL 端口排除名单**拦下时，浏览器报 *"localhost refused to connect"*；
  修法是**回退到空闲回环端口**（#10571）。
- `coding-agent/CHANGELOG.md:258` —— ChatGPT 登录的回调端口被另一个登录占用时，
  会继续跑并让浏览器报 *"OAuth state mismatch"*；修法是**明确以 port-in-use 失败**（#10265）。

`pi-mcp` 的 `OAuthCallbackServer` **默认就是端口 0（内核分配临时端口）** ——
`callback.ts:72` 的 `server.listen(options.port ?? 0, host, ...)`，天生规避该问题。

本端的处理与上游一致，并把两件事写死：

1. **默认端口 0**（`OAuthCallbackServerOptions.port = 0`）；有测试断言两次 `listen()`
   拿到不同端口。
2. **调用方钉死端口且被占用时明确失败**，不静默换端口 —— 换端口会让已注册的
   redirect URI 对不上（正是 #10265 那类「state mismatch」的成因）。本端把
   `OSError` 包一层更清楚的文案再抛（`could not bind 127.0.0.1:<port>`），有测试覆盖。

## 黄金样本：直接跑上游 TypeScript 对答案

`parseWwwAuthenticate` / `selectResource` / `parse*` 这些纯函数的行为细节
（`null`/`""` 算缺席、URL 规范化、错误文案、未知键保留）**靠读代码容易漏**。
本端用 Node ≥ 22.6 的 `--experimental-strip-types` **直接执行上游 `.ts`**，把输出固化成
黄金样本，再在 Python 侧逐条回放：

```
tests/golden/generate_oauth_golden.ts        # 生成器（对上游源码跑 Node，无需 npm install）
tests/golden/oauth_pure_functions.json       # 72 条样本，入库
tests/test_oauth_golden.py                   # 回放断言（不依赖 Node，CI 常驻守护）
```

**当前：72/72 字节级一致，0 处不符。**

它抓到了一个真问题（靠读代码不会发现）：错误文案里上游插的是 **URL 对象**，
`${new URL("https://a.example")}` 渲染成 `https://a.example/`（**补了尾斜杠**），
而本端一开始直接用原始字符串 → 文案差一个 `/`。已在 `_js_href()` 里修正。

刷新样本：

```bash
UPSTREAM=/tmp/pi-up/v1.3.0/packages/mcp \
  node --experimental-strip-types tests/golden/generate_oauth_golden.ts \
  > tests/golden/oauth_pure_functions.json
```

生成器里有一条注释值得记住：`fn() ?? null` 不能省 —— `JSON.stringify` 会把值为
`undefined` 的键**整个丢掉**，那样「返回 undefined」这条规格就静默消失了
（`selectResource` 本来就有 3 例）。

## 〈跨包形状差异清单〉

**这是一份给未来移植者的检查表。** TS 的**结构类型**让上游可以在包与包之间「靠形状搭桥」
（对象字面量、`readonly` 接口、可选方法、鸭子类型），Python 侧没有等价物，于是每一处
「形状相同但类型不同」都会变成下游的 `AttributeError` / `TypeError`。
逐条记：**现象 / 上游为什么没有这个问题 / 消费者该怎么用**。

---

### E1. `to_llm_content()` 返回的元素：**`dict` 子类，支持属性访问**（本端已处置）

**现象.** 下游按属性读 `block.type` / `block.text`（agent 循环与 `_text_of` 就是这么读
`AgentToolResult.content` 的），而 `to_llm_content()` 以前返回**纯 dict** →
`'dict' object has no attribute 'text'`。`upstream-coding-agent` 实测一次踩掉 4 条测试。

**上游为什么没有这个问题.** 上游 `protocol/content.ts:77` 是**对象字面量类型**：

```ts
export type LlmContent = { type: "text"; text: string } | { type: "image"; data: string; mimeType: string };
```

TS 里对象字面量与类实例在**属性访问上结构等价**，所以 `toLlmContent()` 的产物可以直接当
`AgentToolResult.content` 用，一行适配都不需要。而 `pi-mcp` 的 `dependencies` **故意只有
`cross-spawn`**（不依赖 `pi-ai`），它搭桥靠的正是这种结构等价。

**本端处置（选项 B）.** 返回 :class:`pi_mcp.protocol.content.LlmContentBlock`：
`dict` 子类 + `__getattr__` / `__setattr__`。属性名接受**三种拼写**（原键 / snake_case /
camelCase），所以 `block.mimeType` 与 `block.mime_type` 都能读 —— 从 pi-ai 的
`ImageContent`（字段 `mime_type`、alias `mimeType`）机械搬过来的代码也能直接用。

**消费者该怎么用.**

```python
from pi_mcp import to_llm_content

blocks = to_llm_content(result)          # list[LlmContentBlock]
blocks[0].text                           # ✅ 属性访问（上游写法）
blocks[0]["text"]                        # ✅ 下标访问（Pi 既有写法）
isinstance(blocks[0], dict)              # ✅ True —— Mapping / json / ** 语义全在
json.dumps(blocks)                       # ✅ 照常序列化
TextContent(**blocks[0])                 # ✅ 想转成 pi-ai 的模型就这一步
```

> **下游注意**：`pi-coding-agent` 现有的 `_to_pi_content()` 适配器**变成 no-op**
> （它的 `isinstance(block, dict)` 分支仍会命中，转出来的结果一样）。**可以删，也可以留**；
> 但新代码不必再写这种适配器。
>
> **不在本端范围的**：`AgentToolResult.content` 的注解是 pi-ai 的 pydantic 模型，
> 若要**通过 mypy**，仍得 `TextContent(**block)`；`LlmContentBlock` 解决的是**运行时**接缝。

---

### E2. `McpFetch` 是**类型别名**，不是可调用工厂

**现象.** 下游写 `from pi_mcp import McpFetch` 之后，通常会以为它像上游那样是
「可注入的 fetch 函数」（`(input, init) => Promise<Response>`），于是尝试 `McpFetch(...)`
或给它传一个自定义函数。本端它是 `httpx.AsyncClient` 的**类型别名**，实例化它得到的是一个
HTTP 客户端。

**上游为什么没有这个问题.** 上游 `McpFetch = (input: string | URL, init?: RequestInit) => Promise<Response>`
是 WHATWG `fetch` 的签名，Node 与浏览器都有原生实现，所以它就是「一个可调用对象的类型」。
Python 没有 WHATWG fetch，本端的对应物是 **httpx 客户端**（见偏离 D6）。

**消费者该怎么用.**

```python
from pi_mcp import McpFetch                      # 只是类型注解用
from pi_mcp import StreamableHttpTransportOptions

transport = StreamableHttpTransport(StreamableHttpTransportOptions(
    url=..., client=my_httpx_client,             # ← 真正的注入点
))
```

- 要替换传输实现 → 传 `StreamableHttpTransportOptions.client=`
- 要替换 OAuth 流程的 HTTP 调用 → 传各 OAuth 函数的 `client=`
  或 `UnauthorizedContext.client`（它带着**发起 401 那个请求所用的客户端**）
- **不要** `McpFetch(...)`、也不要传一个 `async def fetch(...)` 进去

---

### E3. **所有线上对象都是 `TypedDict`** —— 请用 `[...]` / `.get()`，不要用属性

**现象.** 这条是**系统性**的：`Tool` / `Resource` / `ResourceTemplate` / `ReadResourceResult` /
`ListToolsResult` / `CallToolResult` / `OAuthTokens` / `AuthorizationServerMetadata` /
`McpOAuthState` / `JSON-RPC 消息`…… 全部是 `TypedDict`。对它们写 `tool.name` 会
`AttributeError`；正确写法是 `tool["name"]` / `tool.get("description")`。

**上游为什么没有这个问题.** 上游就是「原样 JSON 对象」——访问方式天然是 `tool.name`（TS 对象）
或 `tool["name"]`（`Record<string, unknown>`），两者都合法，所以上游不会显式区分。
Python 必须二选一：本端选 `TypedDict`，因为**未知键必须保留**（服务端会塞扩展字段，
上游到处 `{ ...item }`），而 dataclass / pydantic 会丢键或报错。

**消费者该怎么用.**

```python
tools = await client.list_tools()
for tool in tools:
    name = tool["name"]                      # ✅
    desc = tool.get("description")           # ✅ 可选键用 .get()
    # name = tool.name                       # ❌ AttributeError
```

**唯一的例外就是 E1** —— `to_llm_content()` 的产物**不是**线上对象，而是「给模型 API 用的
派生值」，它的消费者是 agent 循环（按属性读），所以那里特意做成了属性可访问的 `dict` 子类。
**判断规则：JSON 载荷用下标，派生的 API 值可以按属性。**

---

### E4. 可选成员用「中性默认值」，不是「方法不存在」

**现象.** 上游大量使用可选成员（`provider.discoveryState?.()`、`transport.setProtocolVersion?.()`、
`provider.saveClientInformation?`）。Python 的 `Protocol` / 抽象基类没法表达「方法可能不存在」，
机械照搬会写成 `hasattr(x, "discovery_state")` 之类的探测。

**上游为什么没有这个问题.** TS 的 `?.` 在**调用点**判空，一个接口里没有的方法就是没有。

**消费者该怎么用.** 本端的约定是**可选成员给出中性默认值**：读不到的状态返回 `None`，
可选的副作用是 no-op。所以直接调用即可：

```python
state = await provider.discovery_state()      # 未实现 → None，不用 hasattr 探测
await provider.save_discovery_state(state)    # 未实现 → no-op
```

**唯一的例外**是 `OAuthClientProvider.save_client_information`：它没有安全的默认行为，
基类抛 `NotImplementedError`；「能不能存」用 `provider.can_save_client_information()` 问，
而不是 `hasattr`。

---

### E5. 回调与信号：`asyncio.Event` / `Future`，不是 `AbortSignal` / `Promise`

**现象.** 上游到处传 `AbortSignal`（`fetch(url, { signal })`、`signal.aborted`、
`signal.reason`），本端一律是 `asyncio.Event`；上游 `waitForCallback()` **同步**返回一个
promise，本端返回 `asyncio.Future`。

**上游为什么没有这个问题.** `AbortSignal` 是 Web 标准类型，`fetch` 直接吃它。

**消费者该怎么用.**

```python
signal = asyncio.Event()          # 上游的 AbortController().signal
signal.set()                      # 上游的 controller.abort()
signal.is_set()                   # 上游的 signal.aborted
# 上游的 signal.reason → 本端 McpRequestOptions.cancel_reason（单列字段）

pending = server.wait_for_callback(state)   # 同步返回 Future
callback = await pending                    # 再 await；超时/关闭会以异常结束
```

---

### E6. 配置对象是 `@dataclass`（关键字参数），不是对象字面量

**现象.** 上游到处是 `new StreamableHttpTransport({ url, headers, ... })`、`auth.onUnauthorized?.(...)`，
键是 camelCase 且可随意省略。本端配置对象是 `@dataclass`、字段是 snake_case，构造要写
关键字参数。

**上游为什么没有这个问题.** 对象字面量在 TS 里既能当类型也能当值，多余/缺失的键由类型检查兜底。

**消费者该怎么用.**

```python
from pi_mcp import StreamableHttpTransportOptions, StdioTransportOptions
StreamableHttpTransportOptions(url=..., headers={...}, open_get_stream=False)   # ✅ snake_case
# StreamableHttpTransportOptions({...})                                        # ❌ 不是 dict
```

取值则是**属性**（`options.url`），因为它是 dataclass 而不是线上 JSON。

---

## 关键行为（逐条对齐上游，含上游行号）

| 行为 | 上游位置 | 本包 |
|---|---|---|
| `initialize` 之后发 `notifications/initialized`，只在这之后允许常规请求 | `client.ts:204-250` | `client.py: connect()` |
| 请求 id 从 1 单调递增；`_meta.progressToken` = 请求 id | `client.ts:404-415` | `client.py: _request_internal()` |
| 进度通知**重置**超时 | `client.ts:536-547` | `client.py: _handle_progress()` |
| 取消/超时给服务端发 `notifications/cancelled`，**`initialize` 除外** | `client.ts:424-430,561-575` | `client.py: _cancel_pending()` / `_watch_signal()` |
| 请求结束置位 `sendController`，传输据此不再投递 | `client.ts:582` / `streamable-http.ts:303` | `_remove_pending()` → `send_signal.set()`；`_authorized_fetch()` 派发前检查 |
| **不 await `transport.send()`**（只挂 `.catch`） | `client.ts:440-442` | `client.py: _send_request()` 独立任务 —— 见下「F1」 |
| 传输错误只上报，不使在途请求失败 | `client.ts:209-212` | 同一份监听器接线 |
| 传输关闭才让在途请求全部失败；`on_close` 只回调一次 | `client.ts:592-611` | `_mark_closed()` |
| 分页：`nextCursor` 为 `null`/`""` 视为结束；重复 cursor 报错；上限 1000 页 | `client.ts:94-109,357-376` | `_validate_list_page()` / `_list_all()` |
| 资源缺 `name` 时回退成 `uri`；模板回退成 `uriTemplate` | `client.ts:118-124` | `_to_resource()` / `_to_resource_template()` |
| `tools/call` 缺 `content` 时补空列表；`structuredContent` 可为任意 JSON | `client.ts:148-153` | `_validate_call_tool_result()` |
| stdio 换行分帧：容忍 `\r\n`、跳过空行、超限丢弃并报错 | `stdio.ts:181-207` | `stdio.py: _handle_stdout()` |
| stdio 关闭时序：关 stdin → 宽限 500ms → SIGTERM → 等 `close_timeout_ms` → SIGKILL | `stdio.ts:152-179` | `stdio.py: close()` |
| stderr 只保留尾部 `max_stderr_bytes`（默认 64 KiB） | `stdio.ts:209-215` | `stdio.py: _handle_stderr()` |
| SSE 解析：注释、CRLF、多行 data、`id` 含 `\0` 丢弃、`retry` 仅纯数字 | `streamable-http.ts:67-108` | `streamable_http.py: consume_sse_stream()` |
| SSE 事件无结束空行时的字节上限保护 | `streamable-http.ts:48-50,100-101` | 同一处 `data_bytes` / `buffered` 双检查 |
| GET 流在 `notifications/initialized` 之后打开；405 视为「无 GET 流」 | `streamable-http.ts:248,464-467` | `_start_get_stream()` / `_open_sse_stream()` |
| 响应流中途断开且服务端给了事件 id → `GET` + `Last-Event-ID` 续读 | `streamable-http.ts:382-420` | `_consume_response_stream()` |
| 重连退避：服务端 `retry` 优先，否则指数退避（默认 1000ms→30000ms，5 次） | `streamable-http.ts:489-501` | `_reconnect_delay()` / `_sleep()` |
| `toLlmContent()`：文本/图像直通，其余块变文本占位；无块时回退到结构化 JSON 的字符串 | `content.ts:79-118` | `to_llm_content()`（返回元素见〈跨包形状差异清单〉E1） |
| 401 与 `insufficient_scope` 的 403 交给授权提供者一次后重发 | `streamable-http.ts:295-317,166-170` | `_authorized_fetch()` / `_needs_authorization()` |
| `close()` 复用最后一个请求的令牌，**不问**授权提供者 | `streamable-http.ts:272-283` | `close()` 用 `self._last_token` |
| 受保护资源发现：先试 `/.well-known/oauth-protected-resource<path>`，4xx/502 再退回根路径 | `discovery.ts:70-104` | `discover_protected_resource_metadata()` |
| 授权服务器发现顺序：`oauth-authorization-server<path>` → `openid-configuration<path>` → `<path>/.well-known/openid-configuration` | `discovery.ts:106-117` | `build_authorization_server_discovery_urls()` |
| issuer 校验（去尾斜杠比较）；`skipIssuerValidation` / 配置文档不校验 | `discovery.ts:137-142` | `discover_authorization_server_metadata()` |
| `selectResource`：同源 + 路径前缀匹配，否则抛错 | `discovery.ts:202-215` | `select_resource()` |
| PKCE S256；`offline_access` 时加 `prompt=consent` | `discovery.ts`→`flow.ts:170-200` | `start_authorization()` |
| 客户端认证方式选择（basic / post / none）+ `addClientAuthentication` 覆盖 | `flow.ts:128-161` | `_select_client_auth_method()` / `_apply_client_authentication()` |
| 令牌端点**先看 body 的 `error` 再看状态码** | `flow.ts:234-243` | `_token_request()` |
| 动态注册的 `application_type` 由 redirect_uris 推导（SEP-837） | `flow.ts:113-120,266-268` | `_application_type()` |
| 刷新：响应带新 `refresh_token` 就用新的，否则沿用旧的 | `flow.ts:302` | `refresh_authorization()`（`{refresh_token: old, **tokens}` 的展开顺序） |
| RFC 9207：`iss` 必须指向本次流程的授权服务端；无元数据时任何 `iss` 都拒绝 | `flow.ts:383-389` | `_run_flow()` 的 authorization_code 分支 |
| `invalid_client`/`unauthorized_client` → 作废全部凭据重试一次；`invalid_grant` → 只作废令牌 | `flow.ts:430-444` | `authorize_mcp()` |
| 并发 401 共享一次刷新；令牌已被换掉的请求直接返回 | `flow.ts:452-482` | `adapt_oauth_provider()` 的 `_SharedRefresh` |
| `insufficient_scope` 走 step-up（不刷新、scope 合并） | `flow.ts:457-470` | `adapt_oauth_provider()` + `step_up_scope()` |

---

## 本批次修掉的一个移植陷阱（F1）

第一版 `_request_internal()` 写成了：

```python
try:
    await transport.send(message, ...)     # ← 错
except Exception as error:
    self._cancel_pending(request_id, error, notify_server=False)
return await future
```

上游是 **不 await** 的（`client.ts:440-442` 只挂 `.catch`）。差别在 #10598 那个场景：
一个请求卡在共享的授权刷新里（`on_unauthorized` 还没返回）时，调用方已经取消了它 ——
「请求 promise」应当**立刻** rejection，而不是等 `send()` 自己回来。

await 版本会让 `await client.call_tool(..., signal=...)` 一直挂到刷新结束，
上游的 `streamable-http.test.ts:346` 用例会**超时**（本端实测确认）。
改成独立任务后与上游一致。

---

## 已知简化（记录在案）

1. **transport 级取消（D5）**：上游 `close()` 会 abort 所有在途 HTTP 请求（`controller.signal`
   传给每次 fetch）。本端 `close()` 关闭在途的 SSE 响应（`aclose()`）并置位一个内部 Event，
   使 `_sleep()` 立即返回、后续派发被拒；但**已经在途的 POST** 不会被强杀，而是自然结束
   （httpx 默认 30s 超时兜底）。影响面：`close()` 极少与长 POST 并发；上游对应行为也无测试。
   **OAuth 侧不受此简化影响**：`oauth/` 的每个请求都经 `pi_mcp._cancel.await_or_abort()`
   与信号竞速，在途请求会被真正中止 —— 上游 `oauth.test.ts:537`（「中止即停，不回退到跳转」）
   就是靠这一点通过的。
2. **Windows 进程树**：`taskkill /pid N /T /F` 已按上游实现（`stdio.ts:19-27`），
   但 **CI 与本地实测都在 POSIX**（对应上游用例 `it.skipIf(process.platform === "win32")`），
   Windows 分支未经实测。
3. **`retry` 字段**：本端 `on_retry` 只接受纯数字（与上游 `/^\d+$/` 一致）；上游
   `Number(value)` 允许负号但被正则挡住，行为等价。

---

## 测试与验收

上游 `src/testing/index.ts` **不是**可执行规格 —— 它只有 1 行（把内存传输再导出一遍），
不像 `pi-durable` 的 `env-conformance` / `storage-conformance`。因此本期**以上游 `test/`
目录为验收依据**：

| 上游测试文件 | 行数 | 本端对应 | 覆盖 |
|---|---:|---|---|
| `test/client.test.ts` | 339 | `tests/test_client.py` | 13 个上游用例逐一对应 + 6 个本端补充 |
| `test/streamable-http.test.ts` | 486 | `tests/test_streamable_http.py` | 15 个上游用例逐一对应（1 个 JS 专属显式 skip）+ 2 个补充 |
| `test/stdio.test.ts` | 64 | `tests/test_stdio.py` | 2 个上游用例（**真实子进程**）+ 6 个分帧/生命周期补充 |
| `test/content.test.ts` | 39 | `tests/test_content.py` | 2 个上游用例 + 2 个补充 |
| `test/fixtures/*.mjs` | 2 个 | `tests/fixtures/*.py` | 逐条对齐行为 |
| `test/helpers.ts` | 40 | `tests/helpers.py` | 真实回环 HTTP 服务端（含 HTTP/1.1 分块 SSE 长连接） |
| `test/oauth.test.ts` | 639 | `tests/test_oauth.py` | 12 个上游用例逐一对应 + 8 个本端补充 |
| —（上游无） | — | `tests/test_oauth_golden.py` | 上游 TS 黄金样本回放（72/72） |
| —（上游无） | — | `tests/test_jsonrpc.py` | JSON-RPC 判别边界 |
| —（上游无） | — | `tests/test_smoke.py` | 元数据 / exports / httpx 显式依赖 |

**显式跳过**：上游 `streamable-http.test.ts:426`「calls fetch without a receiver」针对 JS 的
`Illegal invocation`（Cloudflare Workers 对平台 `fetch` 的 `this` 校验）。Python 没有 receiver
绑定，该风险面不存在；本端改为「注入的 httpx 客户端被原样复用、且不被传输关闭」
（`test_injected_http_client_is_used_and_not_closed_by_the_transport`）。

---

## 待办

- [x] `oauth/discovery.ts`（215 行）：受保护资源元数据 + 授权服务器元数据发现
- [x] `oauth/flow.ts`（482 行）：PKCE 授权码流程、动态注册、刷新、`adapt_oauth_provider`
- [x] `oauth/provider.ts`（168 行）：有状态 provider + 内存存储
- [x] `oauth/callback.ts`（164 行）：本地回调服务器
- [x] `oauth/{errors,types,index}.ts`（55+209+56 行）
- [x] `pi_mcp.oauth` 子路径（上游 `./oauth` export）
- [x] `tests/test_smoke.py::test_upstream_exports_subpaths_are_mirrored` 改为正向断言
- [ ] **下游未对接**：`pi-coding-agent` 的 mcp 扩展还需要 `@earendil-works/pi-ai/utils/oauth-page`
      的等价物（`oauthErrorHtml` / `oauthSuccessHtml`）—— 已由 lead 立项（task-29，`pi-ai` 侧），
      **不在本包范围**。本包按上游 `oauth.ts:16` 的 import 形状提供符号，不为绕开它改形状。
