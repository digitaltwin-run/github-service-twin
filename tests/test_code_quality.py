"""Code quality guard: keep all src modules <= 500 lines and functions CC <= 14."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_LINES = 500
MAX_CC = 14


def _cc(node: ast.AST) -> int:
    score = 1
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp, ast.With, ast.AsyncWith)):
            score += 1
        elif isinstance(child, ast.BoolOp):
            score += len(child.values) - 1
        elif isinstance(child, ast.comprehension):
            score += 1 + len(child.ifs)
    return score


def test_source_modules_meet_line_and_cc_budgets() -> None:
    violations: list[str] = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        lines = len(src.splitlines())
        if lines > MAX_LINES:
            violations.append(f"{path.relative_to(ROOT)}: {lines} lines > {MAX_LINES}")
        tree = ast.parse(src, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                val = _cc(node)
                if val > MAX_CC:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno} {node.name} CC={val} > {MAX_CC}")
    assert not violations, "\n".join(violations)
