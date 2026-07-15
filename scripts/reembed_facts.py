#!/usr/bin/env python3
"""Re-embed all facts for a project — standalone, with parallel Ollama calls.

Usage:
    python scripts/reembed_facts.py <project_id> [--concurrency 10]
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

import click
import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

logger = logging.getLogger("reembed_facts")


# nomic-embed-text context window is 2048 tokens; ~1.3 tok/word
_MAX_TOKENS = 1500


def _truncate(text: str, max_words: int = _MAX_TOKENS) -> str:
    """Truncate *text* to *max_words* words so it fits the model's context window."""
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


async def _embed_one(
    sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
    ollama_url: str,
    model: str,
    row_id: uuid.UUID,
    content: str,
) -> tuple[uuid.UUID, list[float] | None]:
    """Embed a single item, rate-limited by *sem*."""
    async with sem:
        prompt = _truncate(content)
        resp = await client.post(
            f"{ollama_url}/api/embeddings",
            json={"model": model, "prompt": prompt},
        )
        resp.raise_for_status()
        data = resp.json()
        emb = data.get("embedding")
        return row_id, emb


async def _reembed_table(
    project_id: uuid.UUID,
    table: str,
    db_url: str,
    ollama_url: str,
    model: str,
    concurrency: int,
    batch_size: int,
    where_extra: str = "",
    update_extra: str = "",
) -> int:
    """Re-embed all rows with NULL embeddings in *table*."""
    engine = create_async_engine(db_url, pool_pre_ping=True)
    async_session = async_sessionmaker(engine, expire_on_commit=False)
    sem = asyncio.Semaphore(concurrency)

    count = 0
    async with async_session() as db:
        offset = 0
        while True:
            rows = await db.execute(
                text(f"""
                    SELECT id, content
                    FROM {table}
                    WHERE project_id = :pid
                      {where_extra}
                      AND embedding IS NULL
                    ORDER BY id
                    LIMIT :limit OFFSET :offset
                """),
                {"pid": project_id, "limit": batch_size, "offset": offset},
            )
            items = rows.all()
            if not items:
                break

            # ── Parallel Ollama calls ──────────────────────────────────────
            async with httpx.AsyncClient(timeout=120) as client:
                tasks = [
                    _embed_one(sem, client, ollama_url, model, row_id, content)
                    for row_id, content in items
                    if content
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)

            # ── Sequential DB writes ───────────────────────────────────────
            for result in results:
                if isinstance(result, Exception):
                    logger.error("Embedding call failed: %s", result)
                    continue
                row_id, embedding = result
                if not embedding:
                    logger.warning("Empty embedding for %s/%s", table, row_id)
                    continue
                await db.execute(
                    text(f"UPDATE {table} SET embedding = :emb, updated_at = now() {update_extra} WHERE id = :rid"),
                    {"emb": embedding, "rid": row_id},
                )
                count += 1

            await db.commit()
            offset += batch_size
            logger.info("[%s] Re-embedded %d so far...", table, count)

    await engine.dispose()
    return count


@click.command()
@click.argument("project_id", type=str)
@click.option("--db-url", default="postgresql+asyncpg://openzep@localhost:5432/openzep", show_default=True)
@click.option("--ollama-url", default="http://localhost:11434", show_default=True)
@click.option("--model", default="nomic-embed-text", show_default=True)
@click.option("--concurrency", default=10, show_default=True, help="Parallel Ollama requests.")
@click.option("--batch-size", default=200, show_default=True)
def main(project_id: str, db_url: str, ollama_url: str, model: str, concurrency: int, batch_size: int) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    pid = uuid.UUID(project_id)

    start = time.monotonic()
    fact_count = asyncio.run(
        _reembed_table(pid, "facts", db_url, ollama_url, model, concurrency, batch_size,
                       where_extra="AND invalid_at IS NULL")
    )
    elapsed = time.monotonic() - start
    logger.info("Done — re-embedded %d facts in %.1fs", fact_count, elapsed)


if __name__ == "__main__":
    main()
