"""仓库级不变量：所有 workspace 包版本锁步，且**版本号即上游对齐点**。

为什么放在 pytest 里
--------------------
这条不变量此前只由 CI 的 ``.github/scripts/check_release_metadata.py`` 在
**发布那一刻**校验，于是「``pyproject`` 与 ``__version__`` 不一致」可以在两次
发布之间长期存在 —— ``pi-ai`` 就曾经 ``pyproject = 0.85.1`` 而
``__version__ = 1.3.0``，两边都"有版本号"，却都不是对方的版本。

放进 pytest 意味着**每次提交**都会校验，而不是等下次发布才发现。

版本号约定（见 README「版本号约定」）
------------------------------------
``版本号 == 镜像的上游版本``。所以 ``__upstream_ref__`` 必须与版本号一致 ——
**升版本号而不升对齐点（或反之）会在这里失败**。这条断言把约定从文档变成了机制。
"""

from __future__ import annotations

import tomllib
from importlib import import_module
from importlib.metadata import version as dist_version
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 允许 ``__upstream_ref__ is None`` 的包（其上游对应物已被删除）。
#:
#: 新增 ``None`` 必须改这里 —— 目的是让「本包没有上游」成为一个**刻意**的决定，
#: 而不是「忘了填」。``pi-storage-sqlite`` 的上游
#: ``@earendil-works/pi-session-backend-sqlite-node`` 已于上游 v1.2.0 删除。
NO_UPSTREAM_ALLOWED = frozenset({"pi-storage-sqlite"})


def _packages() -> list[tuple[str, str, str]]:
    """返回 ``[(包目录名, 发行名, 模块名)]``，直接从 workspace 里读出来。

    刻意**不硬编码**包清单：新增包若忘了登记，硬编码的守卫会静默漏掉它。
    """
    out: list[tuple[str, str, str]] = []
    for pyproject in sorted((ROOT / "packages").glob("*/pyproject.toml")):
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        directory = pyproject.parent.name
        modules = [
            p.name for p in (pyproject.parent / "src").iterdir() if (p / "__init__.py").is_file()
        ]
        assert len(modules) == 1, f"{directory}: src 下有 {len(modules)} 个可导入包，预期 1 个"
        out.append((directory, data["project"]["name"], modules[0]))
    assert out, "没有发现任何 workspace 包 —— 守卫失去意义"
    return out


def test_all_packages_share_one_version() -> None:
    """锁步：所有 workspace 包必须是同一个版本号。

    单包升版会发出一个**混合版本**的 release（依赖上界随即失真），
    而 PyPI 上的包版本不可撤回。
    """
    versions = {
        directory: tomllib.loads(
            (ROOT / "packages" / directory / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["version"]
        for directory, _, _ in _packages()
    }
    assert len(set(versions.values())) == 1, f"版本未锁步：{versions}"


def test_version_matches_metadata_and_upstream_ref() -> None:
    """每个包的 ``__version__``、发行元数据版本、``__upstream_ref__`` 三者必须一致。"""
    problems: list[str] = []
    for directory, dist, module in _packages():
        pkg = import_module(module)
        declared = dist_version(dist)

        if pkg.__version__ != declared:
            problems.append(
                f"{directory}: __version__={pkg.__version__!r} 但元数据={declared!r}"
                f"（pyproject 与 __version__ 漂移）"
            )

        ref = pkg.__upstream_ref__
        if ref is None:
            if directory not in NO_UPSTREAM_ALLOWED:
                problems.append(
                    f"{directory}: __upstream_ref__ 为 None，但不在 NO_UPSTREAM_ALLOWED 里"
                    f"—— 若确实没有上游对应物，请显式加入该名单"
                )
        elif ref != f"earendil-works/pi@v{declared}":
            problems.append(
                f"{directory}: __upstream_ref__={ref!r} 与版本号 {declared!r} 不符"
                f"（约定：版本号 == 镜像的上游版本）"
            )

    assert not problems, "版本约定失守：\n  - " + "\n  - ".join(problems)
