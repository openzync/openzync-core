Harden the RLS bypass reset with a nested rollback so a failed reset releases the transaction (never leaves bypass on), and log loudly when the post-commit context re-bind fails.
