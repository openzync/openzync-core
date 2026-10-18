Read upload byte streams per episode rather than re-reading a one-shot `UploadFile`, which returned empty content and silently ingested nothing for episodes sharing a file.
