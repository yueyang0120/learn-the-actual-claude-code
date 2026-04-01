"""
Smoke tests for all session reimplementations.

Verifies that every session's reimplementation.py is syntactically valid Python
and can be imported without errors (excluding API calls).
"""

import ast
import sys
from pathlib import Path

import pytest

SESSIONS_DIR = Path(__file__).resolve().parents[1] / "sessions"


def get_session_files():
    """Find all reimplementation.py files across sessions."""
    files = sorted(SESSIONS_DIR.glob("s*/reimplementation.py"))
    return [(f.parent.name, f) for f in files]


@pytest.mark.parametrize(
    "session_name,filepath",
    get_session_files(),
    ids=[name for name, _ in get_session_files()],
)
def test_session_compiles(session_name, filepath):
    """Verify that each session's reimplementation.py is valid Python."""
    source = filepath.read_text()
    try:
        ast.parse(source, filename=str(filepath))
    except SyntaxError as e:
        pytest.fail(f"{session_name}/reimplementation.py has syntax error: {e}")


@pytest.mark.parametrize(
    "session_name,filepath",
    get_session_files(),
    ids=[name for name, _ in get_session_files()],
)
def test_session_has_main(session_name, filepath):
    """Verify that each session's reimplementation.py has a runnable entry point."""
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
        f"{session_name}/reimplementation.py should have "
        f"an if __name__ == '__main__' guard or a main() function"
    )


def test_all_sessions_have_readme():
    """Verify that every session directory has a README.md."""
    session_dirs = sorted(
        d for d in SESSIONS_DIR.iterdir() if d.is_dir() and d.name.startswith("s")
    )
    for d in session_dirs:
        assert (d / "README.md").exists(), f"{d.name} is missing README.md"


def test_all_sessions_have_source_analysis():
    """Verify that every session directory has a SOURCE_ANALYSIS.md."""
    session_dirs = sorted(
        d for d in SESSIONS_DIR.iterdir() if d.is_dir() and d.name.startswith("s")
    )
    for d in session_dirs:
        assert (d / "SOURCE_ANALYSIS.md").exists(), f"{d.name} is missing SOURCE_ANALYSIS.md"


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
