"""Alembic environment — runs migrations against the engine's cache URL.

The cache URL comes from ``EngineConfig.resolve_cache_db_url`` (which in
turn honors ``MEDIA_ENGINE_DB_URL`` / ``cache_db_url`` from
``config.toml``); we override ``sqlalchemy.url`` here so the same
``alembic upgrade head`` works against the user's actual store rather
than the placeholder URL in ``alembic.ini``.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from media_engine.config import EngineConfig
from media_engine.runtime.cache import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Read the URL the engine would use, so `alembic upgrade head` targets
# whichever store the user has configured. When ``med db migrate``
# already pinned the URL via ``cli/db.py`` (and stamped
# ``url_source='cli'`` on the config attributes), respect that —
# otherwise ``med db migrate --db-url X`` would be silently shadowed
# by ``MEDIA_ENGINE_DB_URL``.
if config.attributes.get("url_source") != "cli":
    try:
        _cfg = EngineConfig.load()
        config.set_main_option("sqlalchemy.url", _cfg.resolve_cache_db_url())
    except Exception:  # pragma: no cover -- alembic must boot even on bad config
        pass

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
