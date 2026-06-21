"""Facts domain client — batch ingestion, listing."""

from __future__ import annotations

from openzep._http import AsyncHTTPTransport
from openzep.models.facts import FactBatchResponse, FactTriple


class AsyncFactsClient:
    """Async client for business fact operations.

    Args:
        http: The shared async HTTP transport instance.
    """

    def __init__(self, http: AsyncHTTPTransport) -> None:
        self._http = http

    async def add(
        self,
        project_id: str,
        facts: list[FactTriple | dict],
        session_id: str | None = None,
    ) -> FactBatchResponse:
        """Ingest a batch of fact triples.

        Args:
            project_id: The internal UUID of the project.
            facts: List of fact triples (max 500).
            session_id: Optional session external ID.

        Returns:
            ``FactBatchResponse`` with job_id and accepted count.
        """
        body: dict = {
            "facts": [
                f.model_dump(exclude_none=True) if isinstance(f, FactTriple) else f
                for f in facts
            ],
        }
        if session_id is not None:
            body["session_id"] = session_id

        data = await self._http.request(
            "POST",
            f"/v1/projects/{project_id}/facts",
            json_body=body,
        )
        return FactBatchResponse(**data)
