#!/usr/bin/env python3
"""上游变更扫描器（提案原型，尚未定稿）.

动机
----
现有 SYNC.md 的同步触发条件是「上游发布 0.x.0 minor」。但实测数据表明
**上游会在 patch 版本里发破坏性变更**（pi-ai 1.0.3 改了 provider 名、
pi-durable 1.0.3/1.0.4 各有 4/2 条 Breaking）——按版本号触发会漏。

上游每个包都有 ``CHANGELOG.md``，按 ``## [版本] - 日期`` 分节，节内含
``### Breaking Changes`` 子节。这比 GitHub Release Notes 可靠得多
（后者以 coding-agent/TUI 为主，SDK 变更淹没在 TUI 条目里）。

本脚本以「CHANGELOG 的 Breaking Changes」为触发源，而不是版本号。

用法
----
    python3 recon/upstream_scan.py            # 扫描并打印报告
    python3 recon/upstream_scan.py --json     # 输出 JSON

依赖 ``gh`` CLI（已登录）。离线时可先用 ``--cache-dir`` 指向本地 tarball 解压树。
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = "earendil-works/pi"
ROOT = Path(__file__).resolve().parent.parent

#: pi-py 包 → 上游包（``None`` 表示上游已移除）
#:
#: 这份映射本身就是 1.0 变动的产物，且**每轮同步都要重新确认**——1.0 期间
#: 就发生过拆包/并包：``agent`` 瘦身、``session-backends`` 被删、``durable``
#: 另起炉灶、``server``/``protocol``/``client`` 整体废弃。
#:
#: 注意 ``pi-storage-sqlite`` 与 ``pi-durable`` 都指向上游 ``durable``：
#: 前者按计划将并入后者（其 schema 与上游新 ``Storage`` 接口唯一重合的方法名
#: 只有 ``close``，属作废重写）。合并完成前两条都保留，便于看清进度。
PACKAGE_MAP: dict[str, str | None] = {
    "pi-ai": "ai",
    "pi-agent-core": "agent",
    "pi-chord": "chord",
    "pi-durable": "durable",
    "pi-storage-sqlite": "durable",
    "pi-coding-agent": "coding-agent",
}

#: 上游存在、pi-py 尚未建包的 SDK 相关包（用于评估扩展面）
UPSTREAM_ONLY = ["mcp", "codemode", "env"]

SECTIONS = ("Breaking Changes", "Added", "Changed", "Removed", "Deprecated", "Fixed")


def parse_version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def read_baseline() -> str:
    """读 ``UPSTREAM_VERSION`` 里的基线版本号。

    容忍两种格式，便于逐步迁移而不断工具：

    * 旧：``0.85.1``（整文件就是一个版本号）
    * 新：``baseline = 0.85.1``（key = value，可扩展 target 等字段）
    """
    f = ROOT / "UPSTREAM_VERSION"
    if not f.exists():
        return "0.0.0"
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("baseline"):
            _, _, value = line.partition("=")
            return value.strip()
        return line
    return "0.0.0"


def fetch_changelog(pkg: str, cache_dir: Path | None) -> str | None:
    """取上游某包的 CHANGELOG.md 原文."""
    if cache_dir:
        for cand in sorted(cache_dir.glob(f"*/packages/{pkg}/CHANGELOG.md")):
            return cand.read_text(encoding="utf-8")
    try:
        out = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{REPO}/contents/packages/{pkg}/CHANGELOG.md",
                "--jq",
                ".content",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout.strip()
        return base64.b64decode(out).decode("utf-8")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"  ! 拉取 {pkg} 失败: {exc}", file=sys.stderr)
        return None


@dataclass
class VersionEntry:
    version: str
    date: str
    sections: dict[str, list[str]] = field(default_factory=dict)

    @property
    def breaking(self) -> list[str]:
        return self.sections.get("Breaking Changes", [])


def parse_changelog(text: str) -> list[VersionEntry]:
    """把 CHANGELOG 切成版本条目."""
    entries: list[VersionEntry] = []
    for chunk in re.split(r"^## \[", text, flags=re.M)[1:]:
        header, _, body = chunk.partition("\n")
        m = re.match(r"([0-9][^\]]*)\]\s*-\s*([0-9-]+)", header)
        if not m:
            continue  # [Unreleased] 等
        entry = VersionEntry(version=m.group(1), date=m.group(2))
        for name in SECTIONS:
            sm = re.search(
                rf"^### {re.escape(name)}\s*$(.*?)(?=^### |\Z)",
                body,
                flags=re.M | re.S,
            )
            if sm:
                items = [
                    ln[2:].strip() for ln in sm.group(1).split("\n") if ln.strip().startswith("- ")
                ]
                if items:
                    entry.sections[name] = items
        entries.append(entry)
    return entries


def scan(cache_dir: Path | None) -> dict:
    baseline = read_baseline()
    base_t = parse_version(baseline)
    report: dict = {"baseline": baseline, "packages": {}, "missing": []}

    # 多个本仓包可能映射到同一个上游包（如 pi-durable 与 pi-storage-sqlite
    # 都在追 durable）。按**上游包**去重，否则同一份 CHANGELOG 会被重复计数。
    targets: dict[str, list[str]] = {}
    for label, upkg in PACKAGE_MAP.items():
        if upkg is None:
            report["missing"].append(label)
            continue
        targets.setdefault(upkg, []).append(label)
    for p in UPSTREAM_ONLY:
        targets.setdefault(p, []).append(f"(未建包) {p}")

    for upkg, labels in sorted(targets.items()):
        text = fetch_changelog(upkg, cache_dir)
        if text is None:
            continue
        newer = [e for e in parse_changelog(text) if parse_version(e.version) > base_t]
        report["packages"][upkg] = {
            "local_packages": labels,
            "versions_behind": len(newer),
            "versions_with_breaking": sum(1 for e in newer if e.breaking),
            "breaking_items": sum(len(e.breaking) for e in newer),
            "breaking": [
                {"version": e.version, "date": e.date, "items": e.breaking}
                for e in newer
                if e.breaking
            ],
        }
    return report


def print_report(rep: dict) -> None:
    print(f"上游基线 (UPSTREAM_VERSION) = {rep['baseline']}")
    print("=" * 72)
    total_breaking = 0
    for upkg, info in rep["packages"].items():
        n_v = info["versions_behind"]
        n_b = info["breaking_items"]
        total_breaking += n_b
        flag = "⚠" if n_b else " "
        local = " ".join(info["local_packages"])
        print(f"{flag} {upkg:<14} ← {local:<38} {n_v:>2} 个版本, {n_b:>2} 条 Breaking")
        for grp in info["breaking"]:
            print(f"      [{grp['version']}] {grp['date']}")
            for it in grp["items"]:
                print(f"        - {it[:110]}")
    if rep["missing"]:
        print()
        print("上游已移除、但 pi-py 仍有包：")
        for m in rep["missing"]:
            print(f"    ✗ {m}")
    print("=" * 72)
    print(f"合计待处理 Breaking 条目: {total_breaking}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--cache-dir", type=Path, help="本地上游源码树目录（离线）")
    args = ap.parse_args()

    rep = scan(args.cache_dir)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print_report(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
