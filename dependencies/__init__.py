"""FastAPI dependency injection — auth, database, and service wiring.

Dependencies in this package are designed to be used with FastAPI's
``Depends()`` mechanism.  They read from ``request.app.state`` which is
populated during the application lifespan.
"""
