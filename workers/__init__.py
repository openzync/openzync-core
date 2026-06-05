"""Background worker tasks for the MemGraph enrichment pipeline.

Workers are dispatched by ARQ (via Redis) after an episode is committed to
PostgreSQL.  Each worker is responsible for a single enrichment step:

* ``extract_entities`` — LLM-based entity extraction → Graphiti nodes.
* ``embed_episode``    — Embedding generation → ``episodes.embedding``.
* ``extract_facts``    — LLM-based fact extraction → ``facts`` table.
* ``sync_to_graph``    — Graphiti episodic node creation → ``graphiti_node_id``.

Workers are idempotent: they check ``episodes.enrichment_status`` bits before
doing work and skip if the step has already been completed.
"""
