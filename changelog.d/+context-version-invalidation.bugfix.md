Permission-context invalidation is now O(1) via a single INCR, with the fact-write path bumping the version on change.
