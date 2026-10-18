"""缺口一（config 部分移植 + `docs/codemode.md` 资产）的测试。

关键那条是「**文件存在且非空**」—— 常量指对了但指向空文件，等于没指向。
"""

from __future__ import annotations

from pathlib import Path

from pi_coding_agent.config import get_docs_path


def test_get_docs_path_shape_matches_upstream():
    """``<packageDir>/docs``（上游 ``config.ts:449-451``）。"""
    docs = Path(get_docs_path())
    assert docs.is_absolute()
    assert docs.name == "docs"
    # <packageDir> 的那个目录要真的是本包
    assert (docs.parent / "__init__.py").is_file()
    assert docs.parent.name == "pi_coding_agent"


def test_codemode_doc_exists_and_is_not_empty():
    """**最重要的一条**：模型要读的参考手册必须真的在，而且非空。"""
    doc = Path(get_docs_path()) / "codemode.md"
    assert doc.is_file(), f"缺少 {doc}"
    text = doc.read_text(encoding="utf-8")
    assert len(text) > 5_000, "文档过短，可能是空壳"
    assert text.startswith("# Codemode")


def test_codemode_doc_covers_the_models_api():
    """上游 release notes 说这份文档是 ``models.*`` 能力的参考 —— 它必须真的讲 models。"""
    text = (Path(get_docs_path()) / "codemode.md").read_text(encoding="utf-8")
    for topic in ("models", "store(", "searchTools", "@options"):
        assert topic in text, topic


def test_config_is_a_partial_port():
    """部分移植：只导出 ``get_docs_path``，上游 ``config.ts`` 其余部分不得出现。"""
    import pi_coding_agent.config as config

    assert config.__all__ == ["get_docs_path"]
    for absent in ("get_agent_dir", "get_package_dir", "get_examples_path", "APP_NAME", "VERSION"):
        assert not hasattr(config, absent), absent
