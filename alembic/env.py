"""Environnement Alembic : branche les migrations sur les modèles et sur DATABASE_URL de l'application."""
import importlib
from logging.config import fileConfig

from alembic import context

from backend.database import URL_BASE_DE_DONNEES, Base, engine

importlib.import_module("backend.models")  # enregistre les tables dans Base.metadata

config = context.config
# En ligne de commande uniquement : appelé depuis l'application, Alembic ne doit pas reconfigurer ses journaux.
if config.config_file_name is not None and config.attributes.get("connection") is None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _options_communes() -> dict:
    return {
        "target_metadata": target_metadata,
        "compare_type": True,
        # SQLite ne sait pas modifier une table en place : Alembic la recrée (mode « batch »).
        "render_as_batch": engine.dialect.name == "sqlite",
    }


def run_migrations_offline() -> None:
    """Génère le SQL sans se connecter à la base (alembic upgrade head --sql)."""
    context.configure(url=URL_BASE_DE_DONNEES, literal_binds=True, dialect_opts={"paramstyle": "named"}, **_options_communes())
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Applique les migrations. Une connexion déjà ouverte (fournie par le code) est réutilisée."""
    connexion = config.attributes.get("connection")
    if connexion is not None:
        context.configure(connection=connexion, **_options_communes())
        with context.begin_transaction():
            context.run_migrations()
        return
    with engine.connect() as connexion:
        context.configure(connection=connexion, **_options_communes())
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
