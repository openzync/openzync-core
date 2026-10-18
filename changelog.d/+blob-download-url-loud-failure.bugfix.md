Let presigned download-URL generation raise `S3StorageError` instead of returning `None`, so a storage outage surfaces as an error rather than a successful response with a null URL.
