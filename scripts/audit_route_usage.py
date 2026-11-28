"""Route-usage audit: list defined routes and whether any caller references them.

Usage:
    python openzync-core/scripts/audit_route_usage.py [--root DIR] [--json]

Expects ~83 routes from openzync-core/routers/*.py. No network access;
openapi.json is never fetched. Exit code is always 0.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

# Access-control keep (2026-11-28): PATCH /admin/system/users/{user_id}/role is
# the sole platform role-change path — PATCH /v1/users/{user_id} rejects
# `role` at the schema boundary (extra="forbid"). Deletion would strand admin
# accounts with no demotion path, so it is skipped on doubt per the gate.
ALLOWLIST: set[str] = {
    "/admin/system/users/*/role",
}  # MCP-proxied admin / webhook callbacks + access-control keeps.

_METHODS = ("get", "post", "put", "patch", "delete")
_SKIP_DIRS = {"__tests__", "__pycache__", "node_modules", "dist", ".venv"}
_ANCHORS = ("/v1/", "/health", "/ready", "/metrics", "/admin/", "/openapi.json")
_ROUTER_DIRS = ("openzync-frontend/src", "openzync-python-sdk", "openzync-mcp")
_ROUTE_PARAM = re.compile(r"\{[^}]*\}")
_TMPL_PARAM = re.compile(r"\$\{[^}]*\}")
_COLON_PARAM = re.compile(r"/:[A-Za-z_]\w*")
_QUOTED = re.compile(r"""['"`]([^'"`\n]*\/[^'"`\n]*)['"`]""")
_PATH_START = re.compile(r"(?:^|[\s('\"\[=}])/[\w{}$]")
_BARE_PATH = re.compile(r"(?:/v1/|/health|/ready|/metrics|/admin/|/openapi\.json)[\w\-/{}$:.*]*")


def normalize(path: str) -> str:
    """Normalize a route/caller path, mapping ``{p}``/``:p``/``${p}`` to ``*``."""
    path = _TMPL_PARAM.sub("*", path.strip())
    path = _ROUTE_PARAM.sub("*", path)
    path = _COLON_PARAM.sub("/*", path)
    return re.sub(r"\*+", "*", path).rstrip("/") or "/"


def parse_routers(router_dir: Path) -> list[dict[str, str]]:
    """AST-parse routers/*.py into [{method, path, defined_in, func}]."""
    routes: list[dict[str, str]] = []
    for file in sorted(router_dir.glob("*.py")):
        if file.name == "__init__.py":
            continue
        try:
            tree = ast.parse(file.read_text())
        except (OSError, SyntaxError):
            continue
        prefix = next(
            (str(kw.value.value or "") for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and (getattr(node.func, "attr", "") or getattr(node.func, "id", "")) == "APIRouter"
             for kw in node.keywords
             if kw.arg == "prefix" and isinstance(kw.value, ast.Constant)), "")
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                    continue
                if getattr(dec.func, "attr", "") not in _METHODS:
                    continue
                suffix = str(dec.args[0].value or "") if (
                    dec.args and isinstance(dec.args[0], ast.Constant)) else ""
                routes.append({"method": dec.func.attr.upper(), "path": normalize(prefix + suffix),
                               "defined_in": file.name, "func": node.name})
    return routes


def read_text(file: Path) -> str:
    """Return file text, tolerating undecodable bytes."""
    try:
        return file.read_text(errors="ignore")
    except OSError:
        return ""


def collect_callers(root: Path) -> list[dict[str, str]]:
    """Collect normalized caller path refs (non-test files only)."""
    callers: list[dict[str, str]] = []

    def add(raw: str, origin: str) -> None:
        if len(raw) > 2 and _PATH_START.search(" " + raw) and any(k in raw for k in _ANCHORS):
            callers.append({"path": normalize(raw), "origin": origin})

    for rel, glob in (("openzync-frontend/src", "*.ts*"), ("openzync-python-sdk", "*.py"),
                      ("openzync-mcp", "*.py")):
        base = root / rel
        if base.is_dir():
            for file in base.rglob(glob):
                if any(p in _SKIP_DIRS for p in file.parts) or file.suffix == ".test.ts":
                    continue
                for m in _QUOTED.finditer(read_text(file)):
                    add(m.group(1), f"{rel.split('/')[0]}:{file.name}")
    scripts = root / "openzync-core" / "scripts"
    sh_files = scripts.glob("*.sh") if scripts.is_dir() else []
    yml_files = [f for pat in ("infra/**/*.yml", "infra/**/*.yaml") for f in root.glob(pat)]
    for file in list(sh_files) + yml_files:
        for m in _BARE_PATH.finditer(read_text(file)):
            add(m.group(0), f"infra:{file.name}")
    return callers


def _wildcard_match(pattern: str, value: str) -> bool:
    """Match a ``*``-normalized pattern against a value on segment boundaries."""
    return bool(re.fullmatch(re.escape(pattern).replace(r"\*", r"[^/]+"), value))


def match_route(route: str, callers: list[dict[str, str]]) -> str:
    """Return an example caller path if the route is used, else ''."""
    if any(route == a or route.startswith(a.rstrip("*")) for a in ALLOWLIST if a):
        return "ALLOWLIST"
    if route in ("/health", "/ready", "/metrics", "/openapi.json"):
        return "infra-probe"
    for caller in callers:
        other = caller["path"]
        if len(route) > 1 and route in other:
            return other
        if _wildcard_match(other, route) or _wildcard_match(route, other):
            return other
    return ""


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: print Markdown table (or JSON) and always exit 0."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=None, help="Repo root (auto-detected).")
    parser.add_argument("--json", action="store_true", help="Emit JSON rows.")
    args = parser.parse_args(argv)
    if args.root:
        root = Path(args.root)
    else:  # auto-detect: nearest ancestor containing openzync-core/
        here = Path(__file__).resolve()
        root = next((p for p in [here.parent, *here.parents] if (p / "openzync-core").is_dir()), here.parent)
    callers = collect_callers(root)
    rows = []
    for r in parse_routers(root / "openzync-core" / "routers"):
        hit = match_route(r["path"], callers)
        rows.append({**r, "status": "USED" if hit else "DEAD", "caller_example": hit})
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print("status | method | route | defined_in | caller_example")
    print("--- | --- | --- | --- | ---")
    for r in rows:
        print(f"{r['status']} | {r['method']} | {r['path']} | {r['defined_in']} | {r['caller_example']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
