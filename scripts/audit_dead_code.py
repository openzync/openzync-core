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

ALLOWLIST: set[str] = {
    # Bucket B false positives — entrypoints, not Python imports (ARQ /
    # __main__ / shell). Never delete these.
    "entrypoint",
    "create_arq_worker",
    "on_shutdown",
    "on_job_end",
    "liveness_check",
    "monitor_queue_depth",
    "try_acquire_leader_lock",
    "renew_leader_lock",
    "rls_scoped_session_factory",
    "get_worker_settings",
    "WorkerSettings.from_env",
    "resolve_prompt_template_by_type",
    # Import-alias false positives (`import X as Y` in routers/health.py).
    "check_db_health",
    "check_redis_health",
    # Same-file-only use flagged by the cross-file rule; both are live.
    "sign_payload",
    "send_invite_email",
    # Live but audit-blind: called from package ``__init__`` facades,
    # which the scanner skips (ObservationService in
    # services/observations/__init__.py). Never delete these.
    "detect_behavioral_patterns",
    "detect_co_occurrences",
    "detect_temporal_gaps",
}

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
    called_per_file = _called_per_file(root)
    names_per_file = _names_per_file(root)
    rows: list[dict] = []
    for d in defs:
        if d["qualname"] in ALLOWLIST or d["simple"] in ALLOWLIST or d["forced_used"]:
            rows.append({**d, "status": "USED", "caller_example": "allowlisted/decorated"})
            continue
        own_called = called_per_file.get(d["defined_in"], set())
        own_names = names_per_file.get(d["defined_in"], set())
        same_file_hit = d["simple"] in own_called or (
            # Bare-name references count for classes/module functions
            # (field annotations, registries). For methods they are
            # usually same-named locals (e.g. `pin = ProjectPin(...)`
            # inside `def pin`) — calls only.
            d["kind"] != "method"
            and d["simple"] in own_names
        )
        if same_file_hit:
            # Actually invoked inside its own file (bare/self. call or
            # bare-name reference such as a processor/registry list) — live.
            # (Attribute access on other objects and docstring mentions do
            # NOT count; `local.method()` same-file calls are a known gap —
            # verify those by grep before deleting.)
            rows.append({**d, "status": "USED", "caller_example": "same-file"})
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
_called_cache: dict[str, dict[str, set[str]]] = {}


def _called_per_file(root: Path) -> dict[str, set[str]]:
    """Return per-file sets of precisely-invoked names (same-file check).

    Counts bare calls (``helper(...)``), ``self.``/``cls.`` access
    (call or property read), and ``local.method()`` attribute access
    where the base is not itself dotted (``ctx.as_params()``,
    ``manifest.get_template_text()``). ``a.b.c()`` chains are excluded —
    the attr most likely belongs to another object's API.
    Attribute access on other objects and docstring mentions do NOT
    count. Known residual gap: ``Type(...).method()`` chained calls.
    """
    key = str(root)
    if key not in _called_cache:
        called: dict[str, set[str]] = {}
        for path in sorted(root.rglob("*.py")):
            if _is_skipped(path):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            names: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Name):
                        names.add(func.id)
                    elif (
                        isinstance(func, ast.Attribute)
                        and isinstance(func.value, ast.Name)
                        and func.value.id in ("self", "cls")
                    ):
                        names.add(func.attr)
                elif (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in ("self", "cls")
                ):
                    # Property-style read (`provider=self.provider_name`).
                    names.add(node.attr)
                elif (
                    isinstance(node, ast.Attribute)
                    and not isinstance(node.value, ast.Attribute)
                ):
                    # `local.method()` on a non-dotted base (`ctx`,
                    # `manifest`, a constructor call) — same-file collaboration.
                    names.add(node.attr)
            called[str(path.relative_to(root))] = names
        _called_cache[key] = called
    return _called_cache[key]


_names_cache: dict[str, dict[str, set[str]]] = {}


def _names_per_file(root: Path) -> dict[str, set[str]]:
    """Return per-file sets of bare-Name occurrences (any context).

    Used only for the same-file check: within one file a bare occurrence
    of the defined name (call, decorator, registry list, alias) almost
    always refers to the definition. Attribute access, string literals,
    and comments are excluded.
    """
    key = str(root)
    if key not in _names_cache:
        names_map: dict[str, set[str]] = {}
        for path in sorted(root.rglob("*.py")):
            if _is_skipped(path):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            found: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    found.add(node.id)
            names_map[str(path.relative_to(root))] = found
        _names_cache[key] = names_map
    return _names_cache[key]


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
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    # `import X as Y` binds Y locally, so the Name walk misses
                    # X — record the original name as used (alias import).
                    for alias in node.names:
                        if alias.asname:
                            names.add(alias.name.split(".")[0])
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
