# pi-storage-sqlite 移植注记

## 🗄️ 本包已废弃（2026-10-18）

**本包已并入 [`pi-durable`](../pi-durable)，现在是一个转发壳。**

| | |
|---|---|
| 新实现 | `pi_durable.storage.sqlite`（`SqliteStorage` / `open_node_sqlite_storage` / …） |
| 验收 | 上游 `storage-conformance` 套件 **24/24**，内存与 SQLite 两个后端各跑一遍 |
| 旧 API | **不转发**——已移除的 5 个名字（`SqliteSessionRepo` / `SqliteSessionStorage` / `SqliteRunResult` / `open_database` / `apply_migrations`）抛出带迁移指引的 `AttributeError` + `DeprecationWarning` |
| 被删文件 | `database.py`、`storage.py`、`migrations.sql`（旧 session/entry 模型，git 历史可回溯） |

**为什么不转发旧名字**：旧 `SqliteSessionRepo`/`SqliteSessionStorage` 与新 `Storage` 接口
（15 个方法）**唯一重合的方法名是 `close`**；旧 schema（`sessions`/`entries`/…）与新 schema
（9 表 / 16 索引）完全不同。转发它们会给出「看起来能用但语义不同」的名字，比明确失败更危险。

**逐项判定**（task-23 时曾判断「`database.py` 骨架约 100 行可留」，SQLite 后端完成后结论更彻底——
那 100 行**也不值得留**，因为 `pi-durable` 已有一份按上游契约写的、经一致性套件验证的等价实现，
保留旧骨架只会形成第二套方言）见 [`pi-durable/PORTING.md`](../pi-durable/PORTING.md) 的
「`pi-storage-sqlite` 并入方案」一节。

---

## 历史注记（废弃前）

对应上游：~~`@earendil-works/pi-storage-sqlite-node`~~ → [`@earendil-works/pi-session-backend-sqlite-node`](https://github.com/earendil-works/pi/tree/v0.85.1/packages/session-backends/sqlite-node)（v0.84.1–v0.85.1，本仓基线版本）

> ⚠️ **上游包名沿革 / 现状（2026-10-18 核实，勿再引用旧名）**
>
> - v0.83.0：`packages/storage/sqlite-node`，包名 `@earendil-works/pi-storage-sqlite-node`
> - v0.84.1–v0.99.2：`packages/session-backends/sqlite-node`，包名 `@earendil-works/pi-session-backend-sqlite-node`
> - **v1.0.0 起**：`session-backends` 目录在上游已删除（`contents` API 404），SQLite 存储实现移入 `packages/durable`（`src/storage`、`src/session`）
> - 本包自此没有一一对应的上游包，追赶时以上游 `durable` 的存储层为参照（映射见 `SYNC.md`）

## 有意偏离上游

| 上游 | 本包 | 原因 |
|---|---|---|
| node:sqlite（DatabaseSync） | stdlib `sqlite3` | Node ↔ Python 标准库对等 |
| SQL migration 文件 | Python migration 或同等 SQL | schema 逻辑保持一致 |

## cherry-pick

（暂无）

## v0.85.1 同步说明

- 上游本轮 SQLite 改动位于 harness v3 session 存储（session-backends 迁回
  agent 包内部、ownership 对齐 session workers）；本端 ``storage.py`` 为独立
  精简实现，schema/查询结构不同，无映射的运行时变更。
- 本包仅同步版本、上游引用与内部依赖约束（``>=0.85.1,<0.86``）。

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
