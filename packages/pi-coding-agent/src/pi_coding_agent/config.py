"""包内路径解析（**部分移植**）。

> ⚠️ 上游 ``config.ts`` **未移植**；这里只提供 codemode 需要的那一个函数。
> 上游 ``config.ts``（约 460 行）还含 ``getAgentDir`` / ``getPackageDir`` / ``getReadmePath`` /
> ``getExamplesPath`` / ``APP_NAME`` / ``VERSION`` 等 —— **均未移植**。
> 需要 agent 目录的模块按既有惯例就地提供（例如 ``extensions/mcp/oauth.py`` 的 ``agent_dir()``）。

上游出处：``src/config.ts:449-451``
::

    export function getDocsPath(): string {
        return resolve(join(getPackageDir(), "docs"));
    }

**``<packageDir>`` 在 Python 侧的对应物是「可导入包目录」**（``src/pi_coding_agent/``），
而不是仓库里的 ``packages/pi-coding-agent/``：上游的 ``<packageDir>`` 是 npm 包根，
语义是「这个包自己携带的目录」。放在包内还有一个实际好处 ——
``hatchling`` 会**自动**把 ``src/pi_coding_agent/**`` 打进 wheel，于是
**源码树与 wheel 用同一段解析代码都能找到文档**；若照搬 npm 的目录层级
（``packages/pi-coding-agent/docs/``），wheel 里就得 ``force-include`` 到别处，
运行期还得写双路径查找，更脆。
"""

from __future__ import annotations

from pathlib import Path


def get_docs_path() -> str:
    """包内 ``docs/`` 目录的绝对路径（对应上游 ``getDocsPath``）。

    解析依据是**本模块自身的位置**，因此源码运行与安装后运行结果一致。
    """
    return str(Path(__file__).resolve().parent / "docs")


__all__ = ["get_docs_path"]
