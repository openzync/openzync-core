"""Context composer — formats OpenZep context for LLM question answering.

Concatenates facts, entities, episodes, and communities into a prompt
block matching the paper's format as closely as OpenZep's context API
allows.  The primary entry point is ``compose_prompt`` which takes an
**already-retrieved** ``ContextResponse`` and a question, and produces
a few-shot prompt for the answer LLM.

The paper's context format (section 5.1)::

    <FACTS>
      - {fact} ({valid_at} - {invalid_at or 'present'})
    </FACTS>
    <ENTITIES>
      - {entity_name}: {entity_summary}
    </ENTITIES>

OpenZep's ``get_context()`` returns a pre-formatted string that includes
fact timelines, entity summaries, and recent episode snippets.  This
module optionally wraps it for the benchmark's exact prompting needs.
"""

from __future__ import annotations

from openzep.models.memory import ContextResponse

# ── Default system prompt template (paper-inspired) ────────────────────────────

SYSTEM_PROMPT_TEMPLATE: str = """You are a helpful assistant answering questions about a conversation. Use the context below to answer. If the context does not contain the information needed, say "I cannot answer this from the given context."

## Context

{context}

## Question

{question}

## Answer
"""


def compose_prompt(
    question: str,
    context: str,
    *,
    system_prompt: str = SYSTEM_PROMPT_TEMPLATE,
) -> str:
    """Compose a full prompt from a question and an OpenZep context string.

    Args:
        question: The question to answer.
        context: The pre-formatted context string from ``get_context()``.
        system_prompt: Template with ``{context}`` and ``{question}`` placeholders.

    Returns:
        A complete prompt string ready to send to the LLM.
    """
    return system_prompt.format(context=context, question=question)


def compose_prompt_from_response(
    question: str,
    context_response: ContextResponse,
) -> str:
    """Convenience wrapper that extracts the context string from a response.

    Args:
        question: The question to answer.
        context_response: A ``ContextResponse`` from the SDK.

    Returns:
        A complete prompt string.
    """
    return compose_prompt(question, context_response.context)
