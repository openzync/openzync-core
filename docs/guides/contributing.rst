Contributing
============

Setup
-----

.. code-block:: bash

   # Clone the repository
   git clone https://github.com/rohnsha0/openzync.git
   cd openzync

   # Create a virtual environment
   python3 -m venv .venv
   source .venv/bin/activate

   # Install in editable mode with dev dependencies
   pip install -e ".[dev]"

   # Set up pre-commit hooks
   pre-commit install

   # Start infrastructure (Postgres, Redis)
   make docker-up

   # Run migrations
   make migrate

   # Start the development server
   make dev

Running tests
-------------

.. code-block:: bash

   # Unit tests (fast, no I/O)
   make test

   # All tests (unit + integration)
   make test-all

   # Integration tests only (requires docker-up)
   make test-integration

   # With coverage
   make test-coverage

Code style
----------

This project follows:

- **PEP 8** with 88-character line length (``black`` / ``ruff``).
- **Google-style docstrings** on all public interfaces.
- **Type hints** on every function signature.
- Strict separation of concerns (routers → services → repositories → models).

Run the linter before pushing:

.. code-block:: bash

   make lint

Documentation
-------------

Documentation is built with **Sphinx** using the **furo** theme.

To build docs locally:

.. code-block:: bash

   # Install doc dependencies
   make docs-install

   # Build HTML
   make docs-build

   # Or serve with live-reload
   make docs-watch

When you add a new Python module, regenerate the API stubs:

.. code-block:: bash

   make docs-apidoc

Pull requests
-------------

1. Create a feature branch from ``main``.
2. Write tests for your changes.
3. Ensure all tests pass and lint is clean.
4. Open a merge request with a clear description.

Commit messages follow ``type(scope): short description``:

- ``feat(agents): add tool retry with exponential backoff``
- ``fix(billing): correct credit deduction on partial failure``
- ``docs(sphinx): initialise Sphinx documentation``

License
-------

OpenZync is licensed under AGPLv3 with a commercial option.
See ``COMMERCIAL-LICENSE.md`` for details.
