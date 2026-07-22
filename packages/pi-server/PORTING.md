# pi-server 移植注记

对应上游：[`@earendil-works/pi-server`](https://github.com/earendil-works/pi/tree/main/packages/server)（v0.81.1）

## 有意偏离上游

| 上游 | 本包 | 原因 |
|---|---|---|
| Unix socket + JSONL RPC | asyncio + Unix socket + JSONL | Node ↔ Python 对等 |
| 子进程 supervisor | asyncio 子进程 | 同上 |

> 注：本包为**可选**。SDK-only 形态下用户可直接 import pi-coding-agent 使用，server 的价值在于进程隔离与跨语言 RPC。最后评估是否值得实现。

## cherry-pick

（暂无）

## 待办

- [ ] ipc（server / client / protocol）
- [ ] supervisor、rpc-process
- [ ] serve / spawn / status / stop / rpc 命令
