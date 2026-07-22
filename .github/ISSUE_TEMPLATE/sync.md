---
name: 上游同步
about: 追踪上游 earendil-works/pi 的新版本
title: "[Sync] 上游 vX.Y.Z"
labels: upstream, sync
assignees: ""
---

## 上游版本

<!-- 新的上游版本号 -->

## 变更清单

<!--
建议用以下命令列出两个 minor 之间的全部 commit：
gh api repos/earendil-works/pi/compare/<old>...<new> --jq '.commits[] | "\(.sha[:10]) \(.commit.message | split("\n")[0])"'
-->

## 同步检查表

- [ ] 更新 `UPSTREAM_VERSION`
- [ ] 更新 `SYNC.md` 同步历史
- [ ] pi-ai types 变更评估
- [ ] pi-agent-core agent_loop 变更评估
- [ ] pi-coding-agent tools 变更评估
- [ ] 更新各包 `PORTING.md`
- [ ] 测试通过
- [ ] 更新 `CHANGELOG.md`
