"""Dead-code audit: find definitions never referenced outside their own file.

Usage:
    python openzync-core/scripts/audit_dead_code.py [--root DIR] [--json]
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import sys
from pathlib import Path

ALLOWLIST: set[str] = set()

SCAN_SUBDIRS = ("services", "schemas", "core", "repositories", "workers")
SKIP_PARTS = (".venv", "__pycache__", "tests", "migrations")
ROUTER_DECORATORS = {"get", "post", "put", "delete", "patch", "options", "head", "websocket"}

log = logging.getLogger(__name__)


def _is_skipped(path: Path) -> bool:
    """Return True if path should be excluded from scanning."""
    if path.name == "__init__.py":
        return True
    return any(part in SKIP_PARTS for part in path.parts)


def _is_public(name: str) -> bool:
    """Return True for public (non-underscore) symbol names."""
    return not name.startswith("_")


def collect_definitions(root: Path) -> list[dict]:
    """Collect audited definitions from scan subdirs.

    Args:
        root: Repo subdir to audit (openzync-core).

    Returns:
        List of dicts with keys: qualname, simple, kind, defined_in.
    """
    defs: list[dict] = []
    for sub in SCAN_SUBDIRS:
        base = root / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if _is_skipped(path):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError) as exc:
                log.warning("skip %s: %s", path, exc)
                continue
            rel = str(path.relative_to(root))
            is_schema = sub == "schemas"
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    if not _is_public(node.name):
                        continue
                    if is_schema:
                        defs.append({"qualname": node.name, "simple": node.name,
                                     "kind": "class", "defined_in": rel,
                                     "forced_used": False})
                    else:
                        for item in node.body:
                            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                if not _is_public(item.name):
                                    continue
                                defs.append({"qualname": f"{node.name}.{item.name}",
                                             "simple": item.name, "kind": "method",
                                             "defined_in": rel,
                                             "forced_used": _is_router_handler(item)})
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not _is_public(node.name):
                        continue
                    defs.append({"qualname": node.name, "simple": node.name,
                                 "kind": "function", "defined_in": rel,
                                 "forced_used": _is_router_handler(node)})
    return defs


def _is_router_handler(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True if function carries a router/lifespan/entry decorator."""
    for dec in node.decorator_list:
        name = ""
        if isinstance(dec, ast.Call):
            dec = dec.func
        if isinstance(dec, ast.Attribute):
            name = dec.attr
        elif isinstance(dec, ast.Name):
            name = dec.id
        if name in ROUTER_DECORATORS or name in {"lifespan", "on_event"}:
            return True
    return False


def audit(root: Path) -> list[dict]:
    """Audit definitions against cross-file usages.

    Args:
        root: Repo subdir to audit.

    Returns:
        Rows with status, kind, qualname, defined_in, caller_example.
    """
    defs = collect_definitions(root)
    per_file = _usages_per_file(root)
    rows: list[dict] = []
    for d in defs:
        if d["qualname"] in ALLOWLIST or d["simple"] in ALLOWLIST or d["forced_used"]:
            rows.append({**d, "status": "USED", "caller_example": "allowlisted/decorated"})
            continue
        caller = ""
        for fname, names in per_file.items():
            if fname == d["defined_in"]:
                continue
            if d["simple"] in names or d["qualname"] in names:
                caller = fname
                break
        rows.append({**d, "status": "USED" if caller else "DEAD",
                     "caller_example": caller or "-"})
    return rows


_usages_cache: dict[str, dict[str, set[str]]] = {}


def _usages_per_file(root: Path) -> dict[str, set[str]]:
    """Return cached per-file used-name sets (avoids double parse)."""
    key = str(root)
    if key not in _usages_cache:
        per_file: dict[str, set[str]] = {}
        for path in sorted(root.rglob("*.py")):
            if _is_skipped(path):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            names: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    names.add(node.id)
                elif isinstance(node, ast.Attribute):
                    names.add(node.attr)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    names.add(node.value)
            per_file[str(path.relative_to(root))] = names
        _usages_cache[key] = per_file
    return _usages_cache[key]


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: print markdown table or JSON, exit 0 always."""
    default_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Audit dead code via AST usage scan.")
    parser.add_argument("--root", default=str(default_root))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        log.error("root not found: %s", root)
        return 0
    rows = audit(root)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print("status | kind | qualified-name | defined_in | caller_example")
    print("--- | --- | --- | --- | ---")
    for r in sorted(rows, key=lambda x: (x["status"], x["defined_in"], x["qualname"])):
        print(f"{r['status']} | {r['kind']} | {r['qualname']} | "
              f"{r['defined_in']} | {r['caller_example']}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())
