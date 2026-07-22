"""skills 加载系统测试：frontmatter、校验、两阶段发现、格式化。"""

from __future__ import annotations

import pytest

from pi_agent_core import (
    LoadSkillsOptions,
    Skill,
    format_skill_invocation,
    format_skills_for_prompt,
    load_skill_from_file,
    load_skills,
    load_skills_from_dir,
    parse_frontmatter,
    validate_description,
    validate_name,
)

# ============================================================
# frontmatter 解析
# ============================================================


def test_frontmatter_standard():
    fm, body = parse_frontmatter("---\nname: foo\ndescription: bar\n---\nbody text")
    assert fm["name"] == "foo"
    assert fm["description"] == "bar"
    assert body == "body text"


def test_frontmatter_none():
    """无 frontmatter：返回空 dict + 全文。"""
    fm, body = parse_frontmatter("just plain text")
    assert fm == {}
    assert body == "just plain text"


def test_frontmatter_unclosed():
    """未闭合的 frontmatter：当作无 frontmatter。"""
    fm, body = parse_frontmatter("---\nname: foo\nbody without closing")
    assert fm == {}
    assert "name: foo" in body


def test_frontmatter_crlf():
    """CRLF 换行兼容。"""
    fm, body = parse_frontmatter("---\r\nname: x\r\ndescription: y\r\n---\r\nbody")
    assert fm["name"] == "x"
    assert body == "body"


def test_frontmatter_disable_model_invocation():
    fm, _ = parse_frontmatter("---\ndisable-model-invocation: true\n---\nbody")
    assert fm["disable-model-invocation"] is True


# ============================================================
# 名称校验
# ============================================================


@pytest.mark.parametrize("name", ["good", "my-skill", "a1-b2-c3", "x"])
def test_valid_names(name):
    assert validate_name(name) == []


@pytest.mark.parametrize(
    "name",
    ["Bad", "UPPER", "with_underscore", "a--b", "-leading", "trailing-", "has space", ""],
)
def test_invalid_names(name):
    errors = validate_name(name)
    assert len(errors) > 0


def test_name_too_long():
    errors = validate_name("a" * 65)
    assert any("exceeds" in e for e in errors)


def test_description_required():
    assert len(validate_description(None)) > 0
    assert len(validate_description("")) > 0
    assert len(validate_description("  ")) > 0


def test_description_valid():
    assert validate_description("a valid description") == []


def test_description_too_long():
    errors = validate_description("x" * 1025)
    assert any("exceeds" in e for e in errors)


# ============================================================
# 单文件加载
# ============================================================


def test_load_skill_from_file(tmp_path):
    """完整的单文件加载。"""
    f = tmp_path / "my-skill.md"
    f.write_text("---\nname: my-skill\ndescription: A test skill\n---\n# Instructions\nDo stuff.")
    skill, diags = load_skill_from_file(f)
    assert skill is not None
    assert skill.name == "my-skill"
    assert skill.description == "A test skill"
    assert "# Instructions" in skill.content
    assert skill.disable_model_invocation is False
    assert diags == []


def test_load_skill_name_from_parent_dir(tmp_path):
    """无 name 时从父目录名推断。"""
    skill_dir = tmp_path / "inferred-name"
    skill_dir.mkdir()
    f = skill_dir / "SKILL.md"
    f.write_text("---\ndescription: desc\n---\nbody")
    skill, _ = load_skill_from_file(f)
    assert skill is not None
    assert skill.name == "inferred-name"


def test_load_skill_invalid_name(tmp_path):
    """名称无效：返回 None + 诊断。"""
    f = tmp_path / "x.md"
    f.write_text("---\nname: BadName\ndescription: d\n---\nbody")
    skill, diags = load_skill_from_file(f)
    assert skill is None
    assert any("invalid characters" in d.message for d in diags)


def test_load_skill_missing_description(tmp_path):
    """缺 description：返回 None + 诊断。"""
    f = tmp_path / "x.md"
    f.write_text("---\nname: valid-name\n---\nbody")
    skill, diags = load_skill_from_file(f)
    assert skill is None
    assert any("description is required" in d.message for d in diags)


# ============================================================
# 两阶段目录发现
# ============================================================


def test_skill_md_shortcut(tmp_path):
    """阶段一：SKILL.md 立即加载，不递归子目录。"""
    (tmp_path / "SKILL.md").write_text("---\nname: root-skill\ndescription: d\n---\nroot body")
    (tmp_path / "other.md").write_text("---\nname: other\ndescription: d\n---\nother")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.md").write_text("---\nname: deep\ndescription: d\n---\ndeep")

    result = load_skills_from_dir(tmp_path)
    assert len(result.skills) == 1
    assert result.skills[0].name == "root-skill"


def test_root_md_files(tmp_path):
    """阶段二：根目录 .md 文件加载（include_root_files=True）。"""
    (tmp_path / "a.md").write_text("---\nname: skill-a\ndescription: da\n---\na")
    (tmp_path / "b.md").write_text("---\nname: skill-b\ndescription: db\n---\nb")

    result = load_skills_from_dir(tmp_path)
    assert len(result.skills) == 2
    names = {s.name for s in result.skills}
    assert names == {"skill-a", "skill-b"}


def test_deep_md_not_loaded_without_skill_md(tmp_path):
    """子目录的深层 .md（非 SKILL.md）不被加载。"""
    (tmp_path / "root.md").write_text("---\nname: root\ndescription: d\n---\nr")
    (tmp_path / "sub").mkdir()
    # 子目录里有普通 .md，不应被加载（深层 include_root_files=False）
    (tmp_path / "sub" / "deep.md").write_text("---\nname: deep\ndescription: d\n---\ndeep")

    result = load_skills_from_dir(tmp_path)
    names = {s.name for s in result.skills}
    assert "root" in names
    assert "deep" not in names


def test_subdir_skill_md_loaded(tmp_path):
    """子目录的 SKILL.md 被加载。"""
    (tmp_path / "sub1").mkdir()
    (tmp_path / "sub1" / "SKILL.md").write_text("---\nname: sub1\ndescription: d\n---\ns1")

    result = load_skills_from_dir(tmp_path)
    assert len(result.skills) == 1
    assert result.skills[0].name == "sub1"


def test_hidden_files_skipped(tmp_path):
    """隐藏文件被跳过。"""
    (tmp_path / ".hidden.md").write_text("---\nname: h\ndescription: d\n---\nh")
    (tmp_path / "visible.md").write_text("---\nname: v\ndescription: d\n---\nv")

    result = load_skills_from_dir(tmp_path)
    names = {s.name for s in result.skills}
    assert "v" in names
    assert "h" not in names


# ============================================================
# 多位置加载
# ============================================================


def test_load_skills_multiple_locations(tmp_path):
    """用户级 + 项目级 + 显式路径。"""
    agent_dir = tmp_path / "agent"
    (agent_dir / "skills").mkdir(parents=True)
    (agent_dir / "skills" / "SKILL.md").write_text(
        "---\nname: user-skill\ndescription: ud\n---\nuser"
    )

    cwd = tmp_path / "project"
    (cwd / ".pi" / "skills").mkdir(parents=True)
    (cwd / ".pi" / "skills" / "SKILL.md").write_text(
        "---\nname: project-skill\ndescription: pd\n---\nproject"
    )

    explicit = tmp_path / "explicit"
    explicit.mkdir()
    (explicit / "SKILL.md").write_text("---\nname: explicit-skill\ndescription: ed\n---\nexplicit")

    result = load_skills(
        LoadSkillsOptions(
            cwd=str(cwd),
            agent_dir=str(agent_dir),
            skill_paths=[str(explicit)],
        )
    )
    names = {s.name for s in result.skills}
    assert {"user-skill", "project-skill", "explicit-skill"}.issubset(names)


def test_load_skills_name_collision(tmp_path):
    """重名冲突：先加载的胜出，产生诊断。"""
    agent_dir = tmp_path / "agent"
    (agent_dir / "skills").mkdir(parents=True)
    (agent_dir / "skills" / "SKILL.md").write_text("---\nname: dup\ndescription: d\n---\nuser")

    cwd = tmp_path / "project"
    (cwd / ".pi" / "skills").mkdir(parents=True)
    (cwd / ".pi" / "skills" / "SKILL.md").write_text("---\nname: dup\ndescription: d\n---\nproject")

    result = load_skills(LoadSkillsOptions(cwd=str(cwd), agent_dir=str(agent_dir)))
    assert len(result.skills) == 1  # 用户级先加载，胜出
    assert result.skills[0].content == "user"
    assert any(d.code == "collision" for d in result.diagnostics)


# ============================================================
# 格式化
# ============================================================


def test_format_skills_xml():
    skills = [
        Skill(name="a", description="desc a", content="x", file_path="/p/a"),
        Skill(name="b", description="desc b", content="y", file_path="/p/b"),
    ]
    xml = format_skills_for_prompt(skills)
    assert "<available_skills>" in xml
    assert "</available_skills>" in xml
    assert "<name>a</name>" in xml
    assert "<name>b</name>" in xml


def test_format_skills_excludes_disabled():
    """disable_model_invocation 的技能不进 XML。"""
    skills = [
        Skill(name="visible", description="d", content="x", file_path="/p"),
        Skill(
            name="hidden",
            description="d",
            content="y",
            file_path="/p2",
            disable_model_invocation=True,
        ),
    ]
    xml = format_skills_for_prompt(skills)
    assert "visible" in xml
    assert "hidden" not in xml


def test_format_skills_empty():
    assert format_skills_for_prompt([]) == ""


def test_format_skill_invocation():
    skill = Skill(name="my", description="d", content="do this", file_path="/path/SKILL.md")
    block = format_skill_invocation(skill)
    assert '<skill name="my"' in block
    assert "do this" in block
    assert "/path" in block  # dirname 引用


def test_format_skill_invocation_with_instructions():
    skill = Skill(name="my", description="d", content="body", file_path="/p/SKILL.md")
    block = format_skill_invocation(skill, "extra instructions")
    assert "extra instructions" in block
