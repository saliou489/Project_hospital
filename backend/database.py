"""Connexion SQLAlchemy et gestion des sessions de MédiRDV.

Ce module centralise :
- la création du moteur : SQLite par défaut, PostgreSQL (ou autre) en renseignant
  la variable d'environnement ``DATABASE_URL`` ;
- la fabrique de sessions et la dépendance FastAPI ``get_db`` ;
- l'encapsulation transactionnelle de toutes les écritures : validation (commit) si
  tout réussit, annulation (rollback) automatique au moindre échec.
"""
import importlib
import logging
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

logger = logging.getLogger(__name__)

T = TypeVar("T")

RACINE_PROJET = Path(__file__).resolve().parent.parent
# Par défaut : un fichier SQLite à la racine du projet.
URL_PAR_DEFAUT = f"sqlite:///{(RACINE_PROJET / 'medirdv.db').as_posix()}"


def _lire_url() -> str:
    """Lit l'URL de la base ; corrige le préfixe ``postgres://`` fourni par certains hébergeurs."""
    url = os.getenv("DATABASE_URL", URL_PAR_DEFAUT).strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        # Pilote fixé explicitement (psycopg2) : le pilote par défaut dépend de la version de SQLAlchemy.
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


URL_BASE_DE_DONNEES = _lire_url()
EST_SQLITE = URL_BASE_DE_DONNEES.startswith("sqlite")


class Base(DeclarativeBase):
    """Classe de base déclarative de tous les modèles ORM."""


def _creer_moteur():
    """Crée le moteur SQLAlchemy.

    ``hide_parameters`` empêche les valeurs des requêtes (donc les données
    personnelles) d'apparaître dans les messages d'erreur et les journaux.
    """
    options = {"pool_pre_ping": True, "hide_parameters": True}
    if EST_SQLITE:
        options["connect_args"] = {"check_same_thread": False, "timeout": 15}
    return create_engine(URL_BASE_DE_DONNEES, **options)


engine = _creer_moteur()

if EST_SQLITE:

    @event.listens_for(engine, "connect")
    def _activer_cles_etrangeres(connexion_dbapi, _enregistrement):
        """SQLite n'applique les clés étrangères que si on le demande, connexion par connexion."""
        if isinstance(connexion_dbapi, sqlite3.Connection):
            curseur = connexion_dbapi.cursor()
            curseur.execute("PRAGMA foreign_keys=ON")
            curseur.close()


# autoflush désactivé : chaque écriture est envoyée explicitement (flush) pour garder
# la maîtrise de l'ordre des vérifications ; expire_on_commit désactivé pour pouvoir
# sérialiser les objets après la validation de la transaction.
SessionLocale = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """Dépendance FastAPI : fournit une session et garantit sa fermeture."""
    db = SessionLocale()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def transaction_ecriture(db: Session) -> Iterator[Session]:
    """Encapsule une opération d'écriture dans une transaction.

    - succès : commit ;
    - exception : rollback, puis l'exception est relancée pour être traitée par les
      gestionnaires d'erreurs globaux ;
    - dans tous les cas (bloc ``finally``) : aucune transaction ne reste ouverte.
    """
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        logger.debug("Transaction annulée (rollback).")
        raise
    finally:
        # Filet de sécurité pour les interruptions qui ne dérivent pas de Exception.
        if db.in_transaction():
            db.rollback()


def ecrire_avec_reessai(db: Session, operation: Callable[[], T], tentatives: int = 3) -> T:
    """Exécute ``operation`` dans une transaction ; la rejoue en cas d'IntegrityError.

    Une IntegrityError peut venir d'un accès concurrent (deux agents qui enregistrent
    au même instant). À la nouvelle tentative, les vérifications métier sont rejouées :
    elles produisent alors l'erreur précise (par exemple un conflit d'agenda, HTTP 409)
    ou l'écriture aboutit avec un nouvel identifiant.
    """
    for essai in range(1, tentatives + 1):
        try:
            with transaction_ecriture(db):
                return operation()
        except IntegrityError:
            if essai >= tentatives:
                raise
            logger.warning(
                "Conflit d'intégrité (accès concurrent probable) : nouvelle tentative %d/%d.",
                essai + 1,
                tentatives,
            )
    raise RuntimeError("Nombre de tentatives épuisé")  # inatteignable, satisfait l'analyse statique


REVISION_INITIALE = "0001"  # première migration : schéma créé avant l'arrivée d'Alembic
VERROU_MIGRATION = 727274  # verrou PostgreSQL : un seul processus migre à la fois (API, import, génération)


def initialiser_base() -> None:
    """Amène la base au dernier schéma via les migrations Alembic (opération idempotente).

    - base vide : toutes les migrations sont appliquées ;
    - base déjà à jour : rien n'est fait ;
    - base créée avant Alembic (tables présentes mais sans table ``alembic_version``) : elle est d'abord marquée
      comme étant à la révision initiale, sans toucher à ses données, puis les migrations suivantes s'appliquent ;
    - sous PostgreSQL, un verrou empêche deux processus de migrer en même temps (démarrage simultané de conteneurs).
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    importlib.import_module("backend.models")  # enregistre les tables dans Base.metadata
    configuration = Config(str(RACINE_PROJET / "alembic.ini"))
    configuration.set_main_option("script_location", str(RACINE_PROJET / "alembic"))
    logging.getLogger("alembic").setLevel(logging.WARNING)
    with engine.connect() as connexion:
        if connexion.dialect.name == "postgresql":
            connexion.execute(text("SELECT pg_advisory_lock(:cle)"), {"cle": VERROU_MIGRATION})
        try:
            tables = set(inspect(connexion).get_table_names())
            configuration.attributes["connection"] = connexion
            if "alembic_version" not in tables and "rendez_vous" in tables:
                logger.warning("Base créée avant les migrations : elle est rattachée à la révision %s.", REVISION_INITIALE)
                command.stamp(configuration, REVISION_INITIALE)
            command.upgrade(configuration, "head")
            connexion.commit()
        finally:
            if connexion.dialect.name == "postgresql":
                connexion.rollback()
                connexion.execute(text("SELECT pg_advisory_unlock(:cle)"), {"cle": VERROU_MIGRATION})
                connexion.commit()


def verifier_connexion() -> None:
    """Lève une exception SQLAlchemy si la base n'est pas joignable."""
    with engine.connect() as connexion:
        connexion.execute(text("SELECT 1"))
