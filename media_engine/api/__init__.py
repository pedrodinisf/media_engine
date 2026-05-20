"""REST API — FastAPI app on top of the engine.

Every transport hits the same ``Engine``; the REST surface adds
job-oriented async semantics on top: clients submit work, get a
``job_id`` immediately, and stream events / poll status. Auth is
bearer-token (hashed at rest in ``api_tokens``).

The submodules are kept narrow on purpose:

- ``app.py`` — FastAPI factory + lifespan + dependency wiring.
- ``routes.py`` — every endpoint, one module so the surface is greppable.
- ``auth.py`` — bearer-token verify + token CRUD helpers.
- ``sse.py`` — adapter from ``EventBus`` to ``sse-starlette`` per-job.
- ``jobs.py`` — background-task runner over ``Engine.run`` /
  ``Engine.run_pipeline``.
"""

from media_engine.api.app import build_app

__all__ = ["build_app"]
