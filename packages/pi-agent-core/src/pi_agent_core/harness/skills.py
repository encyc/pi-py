"""技能（Skill）加载系统。

对应上游 ``packages/agent/src/harness/skills.ts``（库层加载器）+
``system-prompt.ts``（格式化）。

发现算法（两阶段，与上游一致）：
1. 阶段一：遍历目录条目，任何名为 ``SKILL.md`` 的文件 → 加载并立即返回（不递归）。
2. 阶段二（无 SKILL.md 时）：按字典序排序，跳过隐藏文件，递归子目录
   （``include_root_files=False``）；只有树的根级才加载直接 ``.md`` 文件，
   深层目录仅通过 ``SKILL.md`` 贡献。

加载位置：
- 用户级：``~/.pi/agent/skills/``
- 项目级：``<cwd>/.pi/skills/``
- 显式路径

遵循 https://agentskills.io 标准。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

import yaml

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
CONFIG_DIR_NAME = ".pi"

#: 名称校验：kebab-case（小写字母、数字、连字符）。
_NAME_RE = re.compile(r"^[a-z0-9-]+$")


# ============================================================
# 数据类型
# ============================================================


@dataclass
class Skill:
    """一个已加载的技能。"""

    name: str
    description: str
    content: str
    file_path: str
    disable_model_invocation: bool = False


@dataclass
class SkillDiagnostic:
    """技能加载诊断信息。"""

    code: str  # parse_failed / invalid_metadata / read_failed / list_failed
    message: str
    path: str


@dataclass
class SkillLoadResult:
    """load_skills 的返回。"""

    skills: list[Skill] = field(default_factory=list)
    diagnostics: list[SkillDiagnostic] = field(default_factory=list)


# ============================================================
# frontmatter 解析
# ============================================================


def parse_frontmatter(content: str) -> tuple[dict[str, object], str]:
    """解析 ``---`` frontmatter。

    返回 (frontmatter_dict, body)。无 frontmatter 时 frontmatter 为空 dict，
    body 为全文。
    """
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---"):
        return {}, normalized
    end_index = normalized.find("\n---", 3)
    if end_index == -1:
        return {}, normalized
    yaml_str = normalized[4:end_index]
    body = normalized[end_index + 4 :].strip()
    try:
        parsed = yaml.safe_load(yaml_str)
    except yaml.YAMLError:
        return {}, normalized
    fm = parsed if isinstance(parsed, dict) else {}
    return fm, body


# ============================================================
# 校验
# ============================================================


def validate_name(name: str) -> list[str]:
    """校验技能名称（kebab-case）。返回错误消息列表（空=通过）。"""
    errors: list[str] = []
    if len(name) > MAX_NAME_LENGTH:
        errors.append(f"name exceeds {MAX_NAME_LENGTH} characters ({len(name)})")
    if not _NAME_RE.match(name):
        errors.append("name contains invalid characters (must be lowercase a-z, 0-9, hyphens only)")
    if name.startswith("-") or name.endswith("-"):
        errors.append("name must not start or end with a hyphen")
    if "--" in name:
        errors.append("name must not contain consecutive hyphens")
    return errors


def validate_description(description: str | None) -> list[str]:
    """校验技能描述。"""
    errors: list[str] = []
    if not description or not description.strip():
        errors.append("description is required")
    elif len(description) > MAX_DESCRIPTION_LENGTH:
        errors.append(
            f"description exceeds {MAX_DESCRIPTION_LENGTH} characters ({len(description)})"
        )
    return errors


# ============================================================
# 单文件加载
# ============================================================


def load_skill_from_file(file_path: str | Path) -> tuple[Skill | None, list[SkillDiagnostic]]:
    """从单个文件加载技能。返回 (skill|None, diagnostics)。"""
    path = str(file_path)
    diagnostics: list[SkillDiagnostic] = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        diagnostics.append(SkillDiagnostic(code="read_failed", message=str(e), path=path))
        return None, diagnostics

    fm, body = parse_frontmatter(text)
    description = fm.get("description")
    name = fm.get("name") or Path(path).parent.name

    if not isinstance(name, str):
        diagnostics.append(
            SkillDiagnostic(code="invalid_metadata", message="name must be a string", path=path)
        )
        return None, diagnostics

    name_errors = validate_name(name)
    desc_errors = validate_description(description if isinstance(description, str) else None)
    if name_errors or desc_errors:
        for err in name_errors + desc_errors:
            diagnostics.append(SkillDiagnostic(code="invalid_metadata", message=err, path=path))
        return None, diagnostics

    if not isinstance(description, str):
        description = ""

    skill = Skill(
        name=name,
        description=description,
        content=body,
        file_path=os.path.abspath(path),
        disable_model_invocation=fm.get("disable-model-invocation") is True,
    )
    return skill, diagnostics


# ============================================================
# 目录加载（两阶段发现算法）
# ============================================================


def load_skills_from_dir(
    dir_path: str | Path,
    include_root_files: bool = True,
) -> SkillLoadResult:
    """从目录递归加载技能。

    两阶段算法（与上游一致）：
    1. 先找 SKILL.md，找到立即返回。
    2. 否则字典序扫描：根目录（include_root_files=True 时）加载 .md 文件；
       子目录递归（include_root_files=False）只通过 SKILL.md 贡献。
    """
    root = Path(dir_path)
    result = SkillLoadResult()
    if not root.is_dir():
        return result
    _load_dir_recursive(root, root, include_root_files, result)
    return result


def _load_dir_recursive(
    current: Path,
    root: Path,
    include_root_files: bool,
    result: SkillLoadResult,
) -> None:
    """递归加载目录。"""
    try:
        entries = sorted(current.iterdir(), key=lambda p: p.name)
    except OSError as e:
        result.diagnostics.append(
            SkillDiagnostic(code="list_failed", message=str(e), path=str(current))
        )
        return

    # 阶段一：SKILL.md 快捷方式
    for entry in entries:
        if entry.name == "SKILL.md" and entry.is_file():
            skill, diags = load_skill_from_file(entry)
            result.diagnostics.extend(diags)
            if skill:
                result.skills.append(skill)
            return  # 不递归子目录

    # 阶段二：通用扫描
    for entry in entries:
        name = entry.name
        # 跳过隐藏文件和 node_modules
        if name.startswith(".") or name == "node_modules":
            continue
        if entry.is_dir():
            _load_dir_recursive(entry, root, include_root_files=False, result=result)
        elif include_root_files and name.endswith(".md"):
            skill, diags = load_skill_from_file(entry)
            result.diagnostics.extend(diags)
            if skill:
                result.skills.append(skill)


# ============================================================
# 多位置加载
# ============================================================


@dataclass
class LoadSkillsOptions:
    """load_skills 的配置。"""

    cwd: str = ""
    agent_dir: str = ""
    skill_paths: list[str] = field(default_factory=list)
    include_defaults: bool = True


def load_skills(options: LoadSkillsOptions) -> SkillLoadResult:
    """从多个位置加载技能。

    加载顺序（先加载的优先，重名产生冲突诊断）：
    1. 用户级：``<agent_dir>/skills``（默认 ``~/.pi/agent/skills``）
    2. 项目级：``<cwd>/.pi/skills``
    3. 显式路径
    """
    cwd = options.cwd or os.getcwd()
    agent_dir = options.agent_dir or _default_agent_dir()
    result = SkillLoadResult()
    seen_names: dict[str, str] = {}  # name -> file_path
    seen_paths: set[str] = set()

    def _add(load_result: SkillLoadResult, source: str) -> None:
        for skill in load_result.skills:
            real = os.path.realpath(skill.file_path)
            if real in seen_paths:
                continue
            seen_paths.add(real)
            if skill.name in seen_names:
                result.diagnostics.append(
                    SkillDiagnostic(
                        code="collision",
                        message=f"skill name '{skill.name}' collision: "
                        f"'{seen_names[skill.name]}' vs '{skill.file_path}' ({source})",
                        path=skill.file_path,
                    )
                )
                continue  # 先加载的胜出
            seen_names[skill.name] = skill.file_path
            result.skills.append(skill)
        result.diagnostics.extend(load_result.diagnostics)

    if options.include_defaults:
        # 用户级
        user_skills_dir = os.path.join(agent_dir, "skills")
        _add(load_skills_from_dir(user_skills_dir), "user")
        # 项目级
        project_skills_dir = os.path.join(cwd, CONFIG_DIR_NAME, "skills")
        _add(load_skills_from_dir(project_skills_dir), "project")

    # 显式路径
    for p in options.skill_paths:
        full = p if os.path.isabs(p) else os.path.join(cwd, p)
        if os.path.isdir(full):
            _add(load_skills_from_dir(full), "path")
        elif full.endswith(".md") and os.path.isfile(full):
            skill, diags = load_skill_from_file(full)
            sub = SkillLoadResult(skills=[skill] if skill else [], diagnostics=diags)
            _add(sub, "path")

    return result


def _default_agent_dir() -> str:
    """默认 agent 目录：``$PI_CODING_AGENT_DIR`` 或 ``~/.pi/agent``。"""
    env_dir = os.environ.get("PI_CODING_AGENT_DIR")
    if env_dir:
        return os.path.expanduser(env_dir)
    return os.path.join(os.path.expanduser("~"), CONFIG_DIR_NAME, "agent")


# ============================================================
# 格式化进 system prompt
# ============================================================


def format_skills_for_prompt(skills: list[Skill]) -> str:
    """把技能列表渲染成 ``<available_skills>`` XML，注入 system prompt。

    ``disable_model_invocation=True`` 的技能被排除（仍可通过显式调用）。
    对应上游 ``formatSkillsForSystemPrompt``。
    """
    visible = [s for s in skills if not s.disable_model_invocation]
    if not visible:
        return ""
    lines = [
        "The following skills provide specialized instructions for specific tasks.",
        "Read the full skill file when the task matches its description.",
        "When a skill file references a relative path, resolve it against the skill "
        "directory (parent of SKILL.md / dirname of the path).",
        "",
        "<available_skills>",
    ]
    for skill in visible:
        lines.append("  <skill>")
        lines.append(f"    <name>{escape(skill.name)}</name>")
        lines.append(f"    <description>{escape(skill.description)}</description>")
        lines.append(f"    <location>{escape(skill.file_path)}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)


def format_skill_invocation(skill: Skill, additional_instructions: str = "") -> str:
    """显式调用某个技能时的提示词块。对应上游 ``formatSkillInvocation``。"""
    block = (
        f'<skill name="{skill.name}" location="{skill.file_path}">\n'
        f"References are relative to {os.path.dirname(skill.file_path)}.\n\n"
        f"{skill.content}\n</skill>"
    )
    return f"{block}\n\n{additional_instructions}" if additional_instructions else block


__all__ = [
    "MAX_NAME_LENGTH",
    "MAX_DESCRIPTION_LENGTH",
    "CONFIG_DIR_NAME",
    "Skill",
    "SkillDiagnostic",
    "SkillLoadResult",
    "LoadSkillsOptions",
    "parse_frontmatter",
    "validate_name",
    "validate_description",
    "load_skill_from_file",
    "load_skills_from_dir",
    "load_skills",
    "format_skills_for_prompt",
    "format_skill_invocation",
]
