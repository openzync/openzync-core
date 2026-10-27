Set the ARQ enqueue dedupe key only after a successful enqueue and delete it when the enqueue raises, so a failed fan-out never blocks its retry.
