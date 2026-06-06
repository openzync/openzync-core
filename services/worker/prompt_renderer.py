"""Jinja2 prompt template rendering for ARQ worker tasks.

All LLM prompt templates live in ``services/worker/prompts/`` as ``.jinja2``
files and are rendered through this module — never inline f-strings in Python.

Usage:
    from services.worker.prompt_renderer import render_prompt

    prompt = render_prompt("extract_facts_v1", conversation=user_message)
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, TemplateNotFound

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
_ENV = Environment(
    loader=FileSystemLoader(str(_PROMPT_DIR)),
    # LLM prompts are intentionally unescaped — we pass trusted template
    # variables, not user-controlled HTML.
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_prompt(template_name: str, **kwargs) -> str:
    """Render a Jinja2 prompt template with the given context variables.

    Template files are expected in ``services/worker/prompts/`` and may be
    referenced with or without the ``.jinja2`` suffix.

    Args:
        template_name: Name of the template file (e.g. ``"extract_facts_v1"``
            or ``"extract_facts_v1.jinja2"``).
        **kwargs: Template variables passed to the render context.  Keys must
            match the variable names used in the template.

    Returns:
        The fully rendered prompt string, ready to send to the LLM.

    Raises:
        FileNotFoundError: If no template file with the given name exists
            in the prompts directory.

    Example:
        .. code-block:: python

            prompt = render_prompt(
                "extract_facts_v1",
                conversation="My email is alice@acme.com",
            )
    """
    if not template_name.endswith(".jinja2"):
        template_name += ".jinja2"

    try:
        template = _ENV.get_template(template_name)
    except TemplateNotFound:
        raise FileNotFoundError(
            f"Prompt template '{template_name}' not found in {_PROMPT_DIR}"
        ) from None

    return template.render(**kwargs)
