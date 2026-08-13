# pi-storage-sqlite 移植注记

对应上游：[`@earendil-works/pi-storage-sqlite-node`](https://github.com/earendil-works/pi/tree/main/packages/storage/sqlite-node)（v0.84.1）

## 有意偏离上游

| 上游 | 本包 | 原因 |
|---|---|---|
| node:sqlite（DatabaseSync） | stdlib `sqlite3` | Node ↔ Python 标准库对等 |
| SQL migration 文件 | Python migration 或同等 SQL | schema 逻辑保持一致 |

## cherry-pick

（暂无）

## v0.84.1 同步说明（破例同步 patch）

- 上游本轮 SQLite 改动（查询优化、SQL 模板查询、分支缓存等）位于 ``agent`` harness v2 session 内部；上游 ``packages/session-backends/src/`` 在 v0.84.1 为空。本端 ``storage.py`` 为独立精简实现，schema/查询结构不同，无映射的运行时变更。
- 本包仅同步版本、上游引用与内部依赖约束（``pi-ai``/``pi-agent-core`` → ``>=0.84.1,<0.85``）。

## v0.83.0 同步说明

- 上游 sqlite storage 包在本轮没有运行时行为变更。
- 本包仅同步版本、上游引用和内部依赖约束。

## 待办

- [ ] SqliteDatabase 接口抽象（包装 sqlite3）
- [ ] migrations（001_initial.sql → schema）
- [ ] session/branch/entries 存储 repo
