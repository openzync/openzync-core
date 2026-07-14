"""Task definitions for the ARQ / RQ enrichment pipeline.

Each module in this package exports one async function that is registered with
the worker scheduler.
"""

from workers.tasks.classify_dialog import classify_dialog
from workers.tasks.embed_episode import embed_episode
from workers.tasks.embed_fact import embed_fact
from workers.tasks.enrich_episode import enrich_episode
from workers.tasks.extract_entities import extract_entities
from workers.tasks.extract_facts import extract_facts
from workers.tasks.extract_structured import extract_structured
from workers.tasks.ingest_business_data import ingest_business_data
from workers.tasks.merge_duplicate_entities import merge_duplicate_entities
from workers.tasks.summarise_community import summarise_community
from workers.tasks.link_entities_to_episode import link_entities_to_episode
from workers.tasks.compute_observations import compute_observations

__all__ = [
    "classify_dialog",
    "compute_observations",
    "embed_episode",
    "embed_fact",
    "enrich_episode",
    "extract_entities",
    "extract_facts",
    "extract_structured",
    "ingest_business_data",
    "merge_duplicate_entities",
    "summarise_community",
    "link_entities_to_episode",
]
