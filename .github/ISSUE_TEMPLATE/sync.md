---
name: 上游同步
about: 追踪上游 earendil-works/pi 的新版本
title: "[Sync] 上游 vX.Y.Z"
labels: upstream, sync
assignees: ""
---

## 上游版本

<!-- 本轮同步到的上游版本号（收尾时写入 UPSTREAM_VERSION） -->

## 扫描结果

<!-- 先用扫描器取增量（触发源是上游各包的 CHANGELOG.md，不是 Release Notes）：
     python3 scripts/upstream_scan.py
     python3 scripts/upstream_scan.py --json
     把输出粘贴到下面代码块 -->

```
<paste upstream_scan output>
```

## 破坏性变更处置

<!-- 对每个 ⚠ 命中项给出结论：跟随，或登记为「有意不跟」并写明理由 -->

- [ ] 扫描器报出的 Breaking 条目已逐条确认（跟 / 不跟都有结论）
- [ ] 「有意不跟」的条目已写入对应包 `PORTING.md`（含理由与上游 `文件:行号`）

## 同步检查表

- [ ] 重新确认「上游包 → 本仓包」映射（上游会拆包/并包/删包，映射不是恒定的）
- [ ] 逐包 diff 对照，按依赖顺序：`pi-ai → pi-chord → pi-durable → { pi-mcp, pi-codemode } → pi-coding-agent`
- [ ] 重点评估 `*/src/types.ts`、核心循环（`agent-loop.ts` / `agent.ts`）、provider 适配（`ai/src/api/*.ts`、`ai/src/providers/*.ts`）
- [ ] 更新各包 `PORTING.md`（「有意偏离」确认仍成立；「cherry-pick」清理旧 patch）
- [ ] 更新 `UPSTREAM_VERSION`
- [ ] 更新 `SYNC.md` 同步历史
- [ ] 更新 `CHANGELOG.md` 与 `README.md` 顶部状态表

## 质量门（与 `.github/workflows/ci.yml` 一致）

- [ ] `uv sync`
- [ ] `uv run ruff check packages/ .github/scripts/`
- [ ] `uv run ruff format --check packages/ .github/scripts/`
- [ ] `uv run mypy`（**不要**加 `packages` 参数，会触发同名测试模块冲突）
- [ ] `uv run pytest -q`
- [ ] `uv run pytest -m integration`（DeepSeek 通道，需 API key、消耗额度）

## 发布

- [ ] 若本轮需要发版：打 tag **并**创建 GitHub Release（两件事都要做；只推 tag 不会触发 `publish.yml`，v0.85.1 就是这样漏发的）
- [ ] 发布后确认 PyPI 上每个包的 latest 等于本次 tag（`publish.yml` 已内置回读断言）
