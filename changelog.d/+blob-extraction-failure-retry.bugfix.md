Propagate blob text-extraction failures to the ARQ retry loop instead of recording them as successful extractions — unsupported MIME types remain an explicit no-text outcome without the done bit set.
