Serialize Pydantic validation error details to plain strings before returning 422 from memory ingest, fixing `ValueError`-from-validator failures surfacing as plain-text 500s.
