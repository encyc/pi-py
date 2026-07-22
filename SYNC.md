# 上游同步机制

本项目追踪上游 [earendil-works/pi](https://github.com/earendil-works/pi)。

当前对齐版本见根目录 [`UPSTREAM_VERSION`](./UPSTREAM_VERSION) 文件 —— 它是"已同步到上游哪个版本"的**唯一事实源**。

## 同步节奏

**仅在上游发布 `0.x.0`(minor)版本时，发起一轮集中同步。** 不追 `0.x.y`(patch)。

理由：上游平均 1.7 天发一个 release，追 patch 不现实；minor 之间通常不引入破坏性 API 变更，集中处理成本可控。

### patch 兼容性口子（例外）

LLM provider 的 API 变动频繁，两个 minor 之间可能积累"上游已修复、我们还在踩"的兼容性坑（如某厂商流式协议变更、错误码迁移）。这类修复**可按需 cherry-pick**，不作为常规同步。

被 cherry-pick 的 patch 记录在对应包的 `PORTING.md` →「cherry-pick」小节，标注上游 commit hash 和原因。

## 同步流程 checklist

当上游发布新 `0.x.0` 时，按以下步骤执行：

1. **拉取变更清单**
   ```bash
   # 列出两个 minor 之间的全部 commit
   gh api repos/earendil-works/pi/compare/<old>...<new> \
     --jq '.commits[] | "\(.sha[:10]) \(.commit.message | split("\n")[0])"'
   ```

2. **更新基线**
   - 修改 `UPSTREAM_VERSION` 为新版本号
   - 在本文件末尾「同步历史」追加一行

3. **逐包 diff 对照**（按依赖顺序，自底向上）
   重点检查三类高频变更文件：
   - `*/src/types.ts` → 对应包的类型定义
   - 核心循环（`agent-loop.ts`、`agent-harness.ts`、`agent-session.ts`）
   - provider 适配（`ai/src/api/*.ts`、`ai/src/providers/*.ts`）

4. **更新各包 `PORTING.md`**
   - 「有意偏离」小节：确认偏离项是否仍成立
   - 「cherry-pick」小节：清理已包含在新 minor 中的旧 patch
   - 新增的偏离项必须写明原因

5. **运行测试**
   ```bash
   uv run pytest
   uv run ruff check
   uv run mypy packages
   ```

6. **更新文档**
   - 更新 `README.md` 顶部进度表
   - 提交：`chore(sync): 同步上游 v0.x.0`

## 同步历史

| 日期 | 上游版本 | 说明 |
|---|---|---|
| 2026-07-22 | 0.81.1 | 推翻重建，建立新基线（对应上游 v0.81.1） |
