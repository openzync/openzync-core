"""Sphinx configuration for OpenZep documentation.

This file is exec()'d by Sphinx. It adds the project root to ``sys.path``
so that autodoc can import the flat-layout packages (``core/``, ``routers/``,
``models/``, etc.) without needing an installed package.
"""

from __future__ import annotations

import os
import sys

# ── Path setup ────────────────────────────────────────────────────────────────
# Insert the project root so that flat-layout packages are importable.
# Same approach used by services/api/asgi.py.
_src_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _src_dir)

# ── Project information ───────────────────────────────────────────────────────

project = "openzep"
copyright = "2024, Rohan Shaw"  # noqa: A001
author = "Rohan Shaw"

release = "0.1.0"
version = "0.1.0"

# ── General configuration ─────────────────────────────────────────────────────

# Mock imports that are unavailable at doc-build time (e.g., SDK package
# dependencies that aren't installed in the docs environment).
autodoc_mock_imports = [
    "openzep",  # MCP server depends on the SDK — not guaranteed at doc-build
]

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "sphinx.ext.todo",
    "sphinx.ext.autosummary",
]

templates_path = ["_templates"]

# Patterns to exclude from doc building
exclude_patterns = [
    "_build",
    "Thumbs.db",
    ".DS_Store",
]

# The master toctree document
root_doc = "index"

# ── Napoleon (Google-style docstrings) ────────────────────────────────────────

napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_include_init_with_doc = True
napoleon_include_private_with_doc = False
napoleon_include_special_with_doc = True
napoleon_use_param = True
napoleon_use_rtype = True
napoleon_use_admonition_for_examples = True

# ── autodoc ───────────────────────────────────────────────────────────────────

autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
    "special-members": "__init__",
}
autodoc_typehints = "description"

# ── intersphinx (cross-reference external projects) ───────────────────────────

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "fastapi": ("https://fastapi.tiangolo.com/", None),
    "sqlalchemy": ("https://docs.sqlalchemy.org/en/20/", None),
    "pydantic": ("https://docs.pydantic.dev/latest/", None),
}

# ── Options for HTML output ───────────────────────────────────────────────────

html_theme = "furo"
html_static_path = ["_static"]
html_title = "OpenZep Documentation"
html_logo = None  # set to a path relative to docs/ if you have a logo

# Furo theme options
# https://pradyunsg.me/furo/customisation/
html_theme_options = {
    "source_repository": "https://gitlab.com/rohnsha0/openzep/",
    "source_branch": "main",
    "source_directory": "docs/",
}

# ── autosummary ───────────────────────────────────────────────────────────────

autosummary_generate = True
