Propagate S3 delete failures from `delete_blobs` instead of logging and continuing, so database rows are never removed while their stored objects still exist.
