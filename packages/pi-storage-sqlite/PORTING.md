# pi-storage-sqlite 移植注记

对应上游：[`@earendil-works/pi-storage-sqlite-node`](https://github.com/earendil-works/pi/tree/main/packages/storage/sqlite-node)（v0.81.1）

## 有意偏离上游

| 上游 | 本包 | 原因 |
|---|---|---|
| node:sqlite（DatabaseSync） | stdlib `sqlite3` | Node ↔ Python 标准库对等 |
| SQL migration 文件 | Python migration 或同等 SQL | schema 逻辑保持一致 |

## cherry-pick

（暂无）

## 待办

- [ ] SqliteDatabase 接口抽象（包装 sqlite3）
- [ ] migrations（001_initial.sql → schema）
- [ ] session/branch/entries 存储 repo
