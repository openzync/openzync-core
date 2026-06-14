"""Service layer for custom instructions.

Custom instructions are named domain-specific text snippets that guide
LLM extraction behavior.  The service is deliberately thin:

* CRUD is delegated to ``CustomInstructionRepository``.
* The only domain logic is ``format_custom_instructions()`` — a pure
  formatting utility that turns a list of ``{name, text}`` dicts into a
  prompt-ready text block.

No async methods exist beyond what the repository provides.
"""

from __future__ import annotations


def format_custom_instructions(instructions: list[dict]) -> str:
    """Format a list of ``{name, text}`` instruction dicts into a prompt-ready text block.

    Each instruction produces a Markdown section with the name as a level-3
    heading followed by the text body.  Sections are separated by a blank
    line, creating clear visual separation in the prompt.

    Args:
        instructions: List of dicts with ``name`` and ``text`` keys.

    Returns:
        A formatted string suitable for injection into an LLM prompt.
        Returns an empty string if ``instructions`` is empty.

    Examples:
        >>> format_custom_instructions([
        ...     {"name": "legal", "text": "Use legal terminology."},
        ... ])
        '### legal\\nUse legal terminology.'

        >>> format_custom_instructions([])
        ''
    """
    if not instructions:
        return ""

    blocks: list[str] = [
        f"### {instr['name']}\n{instr['text']}"
        for instr in instructions
    ]

    return "\n\n".join(blocks)
