"""Branch-check audit: flag constant tests, redundant else, dup elif, bool returns.

Usage:
    python openzync-core/scripts/audit_branch_checks.py [--root DIR] [--json]
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ALLOWLIST: set[str] = set()

SCAN_SUBDIRS = ("services", "schemas", "core", "repositories", "workers")
SKIP_PARTS = (".venv", "__pycache__", "tests", "migrations")
TERMINATORS = (ast.Return, ast.Raise, ast.Continue, ast.Break)


def _is_skipped(path: Path) -> bool:
    """Return True if path should be excluded from scanning."""
    if path.name == "__init__.py":
        return True
    return any(part in SKIP_PARTS for part in path.parts)


def _snippet(lines: list[str], lineno: int, limit: int = 80) -> str:
    """Return single-line source snippet for a 1-based line number."""
    try:
        text = lines[lineno - 1].strip()
    except IndexError:
        return ""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _bool_returned(stmts: list[ast.stmt]) -> bool | None:
    """Return the bool value if stmts is exactly `return True/False`, else None."""
    if len(stmts) == 1 and isinstance(stmts[0], ast.Return):
        value = stmts[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, bool):
            return value.value
    return None


def _is_negation(a: ast.expr, b: ast.expr) -> bool:
    """Return True if b negates a: `x`/`not x` or `a==b`/`a!=b`."""
    for expr, plain in ((a, b), (b, a)):  # either side may carry the `not`
        if (isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not)
                and ast.dump(expr.operand) == ast.dump(plain)):
            return True
    return (isinstance(a, ast.Compare) and isinstance(b, ast.Compare)
            and len(a.ops) == len(b.ops) == 1
            and {type(a.ops[0]), type(b.ops[0])} == {ast.Eq, ast.NotEq}
            and ast.dump(a.left) == ast.dump(b.left)
            and ast.dump(a.comparators) == ast.dump(b.comparators))


def _elif_findings(head: ast.If, lines: list[str]) -> list[dict]:
    """Flag duplicate or contradictory `elif` tests within one chain."""
    tests: list[tuple[ast.expr, int]] = []
    current: ast.If | None = head
    while current is not None:
        tests.append((current.test, current.lineno))
        nxt = current.orelse[0] if len(current.orelse) == 1 else None
        current = nxt if isinstance(nxt, ast.If) else None
    findings: list[dict] = []
    for i, (test_i, line_i) in enumerate(tests):
        for test_j, _ in tests[:i]:
            if ast.dump(test_i) == ast.dump(test_j):
                check = "duplicate-elif"
            elif _is_negation(test_i, test_j):
                check = "contradictory-elif"
            else:
                continue
            findings.append({"check": check, "line": line_i,
                             "snippet": _snippet(lines, line_i)})
            break
    return findings


def _if_findings(node: ast.If, lines: list[str]) -> list[dict]:
    """Flag else-after-terminator and return-True/False on one If node."""
    is_elif_tail = len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If)
    findings: list[dict] = []
    if node.orelse and not is_elif_tail and isinstance(node.body[-1], TERMINATORS):
        findings.append({"check": "else-after-terminator", "line": node.lineno,
                         "snippet": _snippet(lines, node.lineno)})
    true_val = _bool_returned(node.body)
    false_val = None if is_elif_tail else _bool_returned(node.orelse)
    if true_val is not None and false_val is not None and true_val != false_val:
        hint = "return x" if true_val else "return not x"
        findings.append({"check": "bool-return", "line": node.lineno,
                         "snippet": f"{_snippet(lines, node.lineno)}  # -> {hint}"})
    return findings


def audit_file(rel: str, lines: list[str], tree: ast.Module) -> list[dict]:
    """Run all four branch checks over one parsed file."""
    elif_ids = {id(n.orelse[0]) for n in ast.walk(tree)
                if isinstance(n, ast.If) and len(n.orelse) == 1
                and isinstance(n.orelse[0], ast.If)}
    findings: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While)) and isinstance(node.test, ast.Constant):
            findings.append({"file": rel, "check": "constant-test",
                             "line": node.lineno,
                             "snippet": _snippet(lines, node.lineno)})
        if isinstance(node, ast.If):
            findings.extend({"file": rel, **f} for f in _if_findings(node, lines))
            if id(node) not in elif_ids:  # check each elif chain once, at its head
                findings.extend({"file": rel, **f}
                                for f in _elif_findings(node, lines))
    return [f for f in findings
            if not any(allow in f["snippet"] for allow in ALLOWLIST)]


def audit(root: Path) -> list[dict]:
    """Walk scan subdirs and collect branch-check findings."""
    findings: list[dict] = []
    for sub in SCAN_SUBDIRS:
        base = root / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if _is_skipped(path):
                continue
            try:
                text = path.read_text(encoding="utf-8")
                tree = ast.parse(text)
            except (SyntaxError, UnicodeDecodeError):
                continue
            findings.extend(audit_file(str(path.relative_to(root)),
                                       text.splitlines(), tree))
    return findings


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: print markdown table or JSON, exit 0 always."""
    default_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Audit branch checks via AST scan.")
    parser.add_argument("--root", default=str(default_root))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        print(f"root not found: {root}", file=sys.stderr)
        return 0
    rows = audit(root)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print("file:line | check | snippet")
    print("--- | --- | ---")
    for row in sorted(rows, key=lambda r: (r["file"], r["line"], r["check"])):
        print(f"{row['file']}:{row['line']} | {row['check']} | {row['snippet']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
