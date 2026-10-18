"""pi-agent-core sessions 子包（原名 ``harness``）。

本子包承载**本端自己的**会话持久化、技能加载与上下文压缩设施。

> ⚠️ 命名由来：上游在 v1.0.0 把 ``@earendil-works/pi-agent-core`` 的实验性
> ``harness`` 整体删除，改由全新的 ``@earendil-works/pi-durable`` 承担
> 「durable conversation / task / document runtime」。本端这套精简实现与
> ``pi-durable`` 并非同构（详见 ``PORTING.md``），因此 v1.3.0 同步时把
> ``harness`` 更名为 ``sessions``，避免与上游同名不同物继续混淆。
> 旧导入路径 ``pi_agent_core.harness.*`` 已移除，迁移表见 ``PORTING.md``。
"""
