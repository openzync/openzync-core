Fix the context export cache serving a cached `format=json` payload in response to a `format=text` request by scoping the cache key to `format` and `limit`.
