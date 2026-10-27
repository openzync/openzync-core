Refresh the leader lock only when this replica still owns it (Lua compare-and-refresh, stepping down otherwise) and await the renew task on shutdown so cancellation completes cleanly.
