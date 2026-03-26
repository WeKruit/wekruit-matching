"""Alembic environment configuration.

Reads DATABASE_URL from pydantic-settings (which reads from .env).
Uses SQLAlchemy metadata from db.tables for autogenerate support.

IMPORTANT: Do not hardcode DATABASE_URL here or in alembic.ini.
"""
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure the src/ directory is on the path so wekruit_matching is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wekruit_matching.config import get_settings
from wekruit_matching.db.tables import metadata

# alembic Config object
config = context.config

# Set DATABASE_URL from pydantic-settings (reads .env)
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

# Setup logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for autogenerate — points to our SQLAlchemy table definitions
target_metadata = metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (generates SQL without DB connection)."""
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
    """Run migrations in 'online' mode (live DB connection)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
