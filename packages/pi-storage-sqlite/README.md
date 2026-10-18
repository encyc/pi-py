# pi-storage-sqlite —— 【已废弃】

> ⚠️ **本包已废弃并向 [`pi-durable`](../pi-durable) 合并。**
> 新代码请使用 [`pi_durable.storage.sqlite`](../pi-durable/src/pi_durable/storage/sqlite)。

## 为什么废弃

本包原对齐上游 `@earendil-works/pi-session-backend-sqlite-node`
（`packages/session-backends/sqlite-node`）。该包已在**上游 v1.2.0 被整体删除**
（连同 `session-backends`），且旧的数据模型在新实现里**没有对应物**：

- 旧 `SqliteSessionRepo` / `SqliteSessionStorage` 与新 `Storage` 接口（15 个方法）
  **唯一重合的方法名是 `close`**。
- 旧 schema（`sessions`/`entries`/`scalar_values`/…）与新 schema
  （**9 表 / 16 索引 + 独立 `durable_schema` 版本表**）完全不同。

## 本包现在是什么

一个**转发壳**：把 `pi_durable.storage.sqlite` 的公开面重导出，
并对已移除的旧名字抛出**带迁移指引的 `AttributeError` + `DeprecationWarning`**。

我们**故意不转发旧名字**——提供「看起来能用、语义却不同」的替代品，比明确失败更危险。

```python
# 旧（已失效，会得到带指引的 AttributeError）
from pi_storage_sqlite import SqliteSessionStorage

# 新
from pi_durable.storage.sqlite import (
    SqliteStorage,
    SqliteStorageOptions,
    open_node_sqlite_storage,
)

storage = await open_node_sqlite_storage("durable.db")
```

## Install

```bash
pip install pi-py-durable     # 推荐：直接用这个
pip install pi-py-storage-sqlite   # 仅为兼容旧代码而保留的转发壳
```

Import name has no prefix: `from pi_durable.storage.sqlite import ...`

## 迁移

逐项判定、发行名处置理由与 5 步并入顺序见 [`PORTING.md`](./PORTING.md)
与 [`pi-durable/PORTING.md`](../pi-durable/PORTING.md) 的「`pi-storage-sqlite` 并入方案」一节。

新实现已通过上游 `storage-conformance` 套件（**24/24**，内存与 SQLite 两个后端各跑一遍）。

## Links

- [GitHub](https://github.com/encyc/pi-py) | [Issues](https://github.com/encyc/pi-py/issues)
- [PORTING.md](./PORTING.md) — deviations from upstream
