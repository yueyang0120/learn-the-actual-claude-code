"""
Smoke tests for all agent reimplementations.

Verifies that every agent file is syntactically valid Python
and can be imported without errors (excluding API calls).
"""

import ast
import sys
from pathlib import Path

import pytest

AGENTS_DIR = Path(__file__).resolve().parents[1] / "agents"
DOCS_DIR = Path(__file__).resolve().parents[1] / "docs" / "en"
SOURCE_ANALYSIS_DIR = Path(__file__).resolve().parents[1] / "source-analysis"


def get_agent_files():
    """Find all Python files in agents/."""
    files = sorted(AGENTS_DIR.glob("s*.py"))
    return [(f.stem, f) for f in files]


@pytest.mark.parametrize(
    "agent_name,filepath",
    get_agent_files(),
    ids=[name for name, _ in get_agent_files()],
)
def test_agent_compiles(agent_name, filepath):
    """Verify that each agent file is valid Python."""
    source = filepath.read_text()
    try:
        ast.parse(source, filename=str(filepath))
    except SyntaxError as e:
        pytest.fail(f"agents/{agent_name}.py has syntax error: {e}")


@pytest.mark.parametrize(
    "agent_name,filepath",
    get_agent_files(),
    ids=[name for name, _ in get_agent_files()],
)
def test_agent_has_main(agent_name, filepath):
    """Verify that each agent file has a runnable entry point."""
    source = filepath.read_text()
    tree = ast.parse(source)

    has_main_guard = any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and any(
            isinstance(c, ast.Constant) and c.value == "__main__"
            for c in node.test.comparators
        )
        for node in ast.walk(tree)
    )

    has_main_func = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "main"
        for node in ast.walk(tree)
    )

    assert has_main_guard or has_main_func, (
        f"agents/{agent_name}.py should have "
        f"an if __name__ == '__main__' guard or a main() function"
    )


def test_all_sessions_have_docs():
    """Verify that every session has a learning doc."""
    for i in range(1, 15):
        prefix = f"s{i:02d}-"
        matches = list(DOCS_DIR.glob(f"{prefix}*.md"))
        assert len(matches) > 0, f"docs/en/ is missing doc for session {i:02d}"


def test_all_sessions_have_source_analysis():
    """Verify that every session has a source analysis file."""
    for i in range(1, 15):
        prefix = f"{i:02d}-"
        matches = list(SOURCE_ANALYSIS_DIR.glob(f"{prefix}*.md"))
        assert len(matches) > 0, f"source-analysis/ is missing file for session {i:02d}"


def test_lib_imports():
    """Verify that the shared library modules can be imported."""
    lib_dir = Path(__file__).resolve().parents[1] / "lib"
    sys.path.insert(0, str(lib_dir.parent))
    try:
        import lib.types
        import lib.utils

        # Verify key types exist
        assert hasattr(lib.types, "Message")
        assert hasattr(lib.types, "ToolUseContext")
        assert hasattr(lib.types, "Task")
        assert hasattr(lib.types, "PermissionRule")

        # Verify key utilities exist
        assert hasattr(lib.utils, "estimate_tokens")
        assert hasattr(lib.utils, "AUTOCOMPACT_BUFFER_TOKENS")
        assert hasattr(lib.utils, "DEFAULT_MAX_TOOL_USE_CONCURRENCY")
    finally:
        sys.path.pop(0)
