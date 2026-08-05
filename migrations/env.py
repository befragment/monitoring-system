import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# Делаем src/ доступным для импорта (env.py запускается вне пакета app)
sys.path.append(str(Path(__file__).parents[1] / "src"))

from app.lib.config import settings          # noqa: E402
from app.lib.postgres import Base            # noqa: E402
from app.repository import _ormmodels        # noqa: E402, F401  импорт нужен, чтобы модели зарегистрировались в Base.metadata

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Подставляем URL из настроек приложения, а не из alembic.ini.
# Миграции гоняем синхронно (psycopg2), даже если рантайм асинхронный (asyncpg).
sync_db_url = settings.database_url.replace("+asyncpg", "")
config.set_main_option("sqlalchemy.url", sync_db_url)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
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
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
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