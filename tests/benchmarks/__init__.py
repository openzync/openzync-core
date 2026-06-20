"""OpenZep benchmarks — DMR (MemGPT/MSC) and LongMemEval.

Usage:
    1. Start OpenZep + worker::
        make dev
        python -m services.worker.worker

    2. Configure environment::
        export LLM_API_KEY="<optional-for-openrouter-free>"
        export LLM_MODEL="openai/gpt-oss-120b:free"  # default

    3. Run benchmarks::
        python -m tests.benchmarks.run_dmr
        python -m tests.benchmarks.run_longmemeval
"""

from __future__ import annotations
