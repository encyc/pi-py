# pi-server 移植注记

对应上游：[`@earendil-works/pi-server`](https://github.com/earendil-works/pi/tree/main/packages/server)（v0.84.1）

## 有意偏离上游

| 上游 | 本包 | 原因 |
|---|---|---|
| Unix socket + JSONL RPC | asyncio + Unix socket + JSONL | Node ↔ Python 对等 |
| 子进程 supervisor | asyncio 子进程 | 同上 |

> 注：本包为**可选**。SDK-only 形态下用户可直接 import pi-coding-agent 使用，server 的价值在于进程隔离与跨语言 RPC。最后评估是否值得实现。

## cherry-pick

（暂无）

## v0.84.1 同步说明（破例同步 patch）

- 上游本轮 server 重写（composable protocol server + Unix transport）依赖未移植的 ``protocol`` / ``client`` / ``telemetry`` 包。本端精简 RPC/supervisor 维持现状，无映射的运行时变更。
- 本包仅同步版本、上游引用与内部依赖约束（``pi-coding-agent`` → ``>=0.84.1,<0.85``）。

## v0.83.0 同步说明

- 上游 server 包在本轮没有落入当前 Python RPC/supervisor 范围的行为变更。
- 本包仅同步版本、上游引用和内部依赖约束。

## 待办

- [ ] ipc（server / client / protocol）
- [ ] supervisor、rpc-process
- [ ] serve / spawn / status / stop / rpc 命令
