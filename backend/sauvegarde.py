"""Sauvegarde et restauration de la base (ENF-10).

Sauvegarde *logique* : un fichier ZIP contenant une table par fichier (JSON, une ligne par enregistrement) et un manifeste
(date, révision du schéma, nombre de lignes et empreinte SHA-256 de chaque table). Elle fonctionne à l'identique sous
PostgreSQL et SQLite, et ne demande aucun outil externe (pas de pg_dump) : la procédure de restauration est donc testée
automatiquement (tests/test_sauvegarde.py), y compris d'un moteur vers l'autre.

Utilisation (dans le conteneur : ``docker compose exec api python -m backend.sauvegarde ...``) :
    sauvegarder                  créer une sauvegarde maintenant
    lister                       afficher les sauvegardes disponibles
    verifier FICHIER             contrôler l'intégrité d'une sauvegarde (empreintes) sans rien restaurer
    restaurer FICHIER            restaurer dans une base VIDE (schéma à jour, aucune donnée)
    restaurer FICHIER --remplacer   remplacer le contenu d'une base non vide ; une sauvegarde de sécurité de l'état
                                 actuel est faite d'abord, car cette opération EFFACE les données actuelles
    boucle                       service : une sauvegarde au démarrage puis une par jour (MEDIRDV_HEURE_SAUVEGARDE, 03:00)

Le fichier contient des données personnelles et les empreintes des clés : il est créé en lecture seule pour son
propriétaire et doit être conservé hors de la machine, dans un endroit protégé.
"""
from __future__ import annotations

import argparse
import enum
import hashlib
import json
import logging
import os
import sys
import time
import zipfile
from datetime import date, datetime, time as heure, timedelta
from pathlib import Path

from sqlalchemy import Boolean, Date, DateTime, Enum, Table, Time, delete, func, insert, select, text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

VERSION_FORMAT = 1
TAILLE_LOT = 500
PREFIXE = "medirdv_"


class SauvegardeError(Exception):
    """Sauvegarde illisible, altérée, incompatible avec le schéma, ou restauration refusée."""


def _tables() -> list[Table]:
    import importlib

    from backend.database import Base

    importlib.import_module("backend.models")  # enregistre les tables dans Base.metadata
    return list(Base.metadata.sorted_tables)  # ordre des clés étrangères : parents avant enfants


def _vers_json(valeur):
    if isinstance(valeur, enum.Enum):
        return valeur.value
    if isinstance(valeur, (datetime, date, heure)):
        return valeur.isoformat()
    return valeur


def _depuis_json(colonne, valeur):
    if valeur is None:
        return None
    type_ = colonne.type
    if isinstance(type_, Enum):
        return type_.enum_class(valeur)
    if isinstance(type_, DateTime):
        return datetime.fromisoformat(valeur)
    if isinstance(type_, Date):
        return date.fromisoformat(valeur)
    if isinstance(type_, Time):
        return heure.fromisoformat(valeur)
    if isinstance(type_, Boolean):
        return bool(valeur)
    return valeur


def _revision(connexion: Connection) -> str | None:
    try:
        return connexion.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:  # noqa: BLE001 - table absente : base sans schéma
        connexion.rollback()
        return None


def _serialiser(connexion: Connection, table: Table) -> tuple[bytes, int]:
    cles = [c.name for c in table.primary_key.columns]
    requete = select(table).order_by(*[table.c[k] for k in cles])
    lignes = [json.dumps({c.name: _vers_json(ligne._mapping[c.name]) for c in table.columns}, ensure_ascii=False) for ligne in connexion.execute(requete)]
    return ("\n".join(lignes) + ("\n" if lignes else "")).encode("utf-8"), len(lignes)


def sauvegarder(moteur: Engine, dossier: Path, etiquette: str = "") -> Path:
    """Écrit une sauvegarde complète et cohérente (une seule transaction de lecture) dans ``dossier``."""
    dossier.mkdir(parents=True, exist_ok=True)
    options = {"isolation_level": "REPEATABLE READ"} if moteur.dialect.name == "postgresql" else {}
    contenus: dict[str, bytes] = {}
    manifeste = {"format": VERSION_FORMAT, "cree_le": datetime.now().isoformat(timespec="seconds"), "dialecte": moteur.dialect.name, "tables": {}}
    with moteur.connect().execution_options(**options) as connexion:
        manifeste["revision_schema"] = _revision(connexion)
        for table in _tables():
            donnees, nombre = _serialiser(connexion, table)
            contenus[f"{table.name}.jsonl"] = donnees
            manifeste["tables"][table.name] = {"lignes": nombre, "sha256": hashlib.sha256(donnees).hexdigest()}
    manifeste["total_lignes"] = sum(t["lignes"] for t in manifeste["tables"].values())
    nom = f"{PREFIXE}{datetime.now():%Y%m%d_%H%M%S}{('_' + etiquette) if etiquette else ''}.zip"
    chemin = dossier / nom
    temporaire = chemin.with_suffix(".zip.partiel")
    with zipfile.ZipFile(temporaire, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifeste.json", json.dumps(manifeste, ensure_ascii=False, indent=2))
        for fichier, donnees in contenus.items():
            archive.writestr(fichier, donnees)
    os.chmod(temporaire, 0o600)
    temporaire.replace(chemin)  # le fichier n'apparaît sous son nom définitif qu'une fois complet
    logger.info("Sauvegarde créée : %s (%s lignes)", chemin.name, manifeste["total_lignes"])
    return chemin


def lire_et_verifier(chemin: Path) -> tuple[dict, dict[str, bytes]]:
    """Ouvre la sauvegarde et contrôle les empreintes de chaque table ; lève ``SauvegardeError`` au moindre doute."""
    try:
        with zipfile.ZipFile(chemin) as archive:
            if archive.testzip() is not None:
                raise SauvegardeError("Archive endommagée.")
            manifeste = json.loads(archive.read("manifeste.json"))
            contenus = {}
            for nom, info in manifeste["tables"].items():
                donnees = archive.read(f"{nom}.jsonl")
                if hashlib.sha256(donnees).hexdigest() != info["sha256"]:
                    raise SauvegardeError(f"Table {nom} altérée : l'empreinte ne correspond pas.")
                if len([ligne for ligne in donnees.decode("utf-8").split("\n") if ligne]) != info["lignes"]:
                    raise SauvegardeError(f"Table {nom} : nombre de lignes inattendu.")
                contenus[nom] = donnees
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, FileNotFoundError) as erreur:
        raise SauvegardeError(f"Fichier de sauvegarde illisible ({type(erreur).__name__}).") from erreur
    if manifeste.get("format") != VERSION_FORMAT:
        raise SauvegardeError(f"Format de sauvegarde inconnu : {manifeste.get('format')}.")
    return manifeste, contenus


def _reinitialiser_sequences(connexion: Connection, tables: list[Table]) -> None:
    if connexion.dialect.name != "postgresql":
        return
    for table in tables:
        cles = list(table.primary_key.columns)
        if len(cles) == 1 and cles[0].name == "id" and type(cles[0].type).__name__ == "Integer":
            # Les prochains identifiants générés repartent après le plus grand identifiant restauré.
            connexion.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {table.name}"
            ))


def restaurer(moteur: Engine, chemin: Path, remplacer: bool = False) -> dict[str, int]:
    """Recharge la sauvegarde dans ``moteur`` (schéma déjà créé, à la même révision), en une seule transaction.

    Sans ``remplacer``, la base doit être vide. En cas d'erreur, la transaction est annulée : la base reste intacte.
    """
    manifeste, contenus = lire_et_verifier(chemin)
    tables = _tables()
    with moteur.begin() as connexion:
        revision = _revision(connexion)
        if revision != manifeste["revision_schema"]:
            raise SauvegardeError(
                f"Schéma incompatible : la base est à la révision {revision}, la sauvegarde à la {manifeste['revision_schema']}. "
                "Mettez la base à jour (migrations) avant de restaurer."
            )
        existantes = sum(connexion.execute(select(func.count()).select_from(t)).scalar() or 0 for t in tables)
        if existantes and not remplacer:
            raise SauvegardeError(
                f"La base contient déjà {existantes} enregistrements : la restauration est refusée pour ne rien écraser. "
                "Utilisez --remplacer pour remplacer le contenu (une sauvegarde de sécurité est alors faite d'abord)."
            )
        for table in reversed(tables):
            connexion.execute(delete(table))
        restaures: dict[str, int] = {}
        for table in tables:
            lignes = [json.loads(ligne) for ligne in contenus[table.name].decode("utf-8").split("\n") if ligne]
            for debut in range(0, len(lignes), TAILLE_LOT):
                lot = [{c.name: _depuis_json(c, ligne[c.name]) for c in table.columns if c.name in ligne} for ligne in lignes[debut:debut + TAILLE_LOT]]
                connexion.execute(insert(table), lot)
            restaures[table.name] = len(lignes)
        _reinitialiser_sequences(connexion, tables)
    logger.info("Restauration terminée : %s lignes", sum(restaures.values()))
    return restaures


def appliquer_retention(dossier: Path, garder: int) -> list[Path]:
    """Ne garde que les ``garder`` sauvegardes automatiques les plus récentes ; retourne celles qui sont supprimées."""
    fichiers = sorted(dossier.glob(f"{PREFIXE}*.zip"), reverse=True)
    # Les sauvegardes « avant restauration » (étiquette) ne sont jamais supprimées automatiquement.
    automatiques = [f for f in fichiers if f.stem.count("_") == 2]
    supprimees = automatiques[garder:]
    for fichier in supprimees:
        fichier.unlink()
    return supprimees


def _dossier(argument: str | None) -> Path:
    return Path(argument or os.getenv("MEDIRDV_DOSSIER_SAUVEGARDES", "sauvegardes"))


def _prochaine(heure_texte: str, maintenant: datetime) -> datetime:
    h, m = (int(x) for x in heure_texte.split(":"))
    cible = maintenant.replace(hour=h, minute=m, second=0, microsecond=0)
    return cible if cible > maintenant else cible + timedelta(days=1)


def _boucle(moteur: Engine, dossier: Path) -> None:
    garder = int(os.getenv("MEDIRDV_NB_SAUVEGARDES", "14"))
    heure_texte = os.getenv("MEDIRDV_HEURE_SAUVEGARDE", "03:00")
    premiere = True
    while True:
        if not premiere:
            cible = _prochaine(heure_texte, datetime.now())
            logger.info("Prochaine sauvegarde : %s", cible.isoformat(sep=" ", timespec="minutes"))
            time.sleep(max(1.0, (cible - datetime.now()).total_seconds()))
        premiere = False
        try:
            sauvegarder(moteur, dossier)
            for fichier in appliquer_retention(dossier, garder):
                logger.info("Ancienne sauvegarde supprimée : %s", fichier.name)
        except Exception:  # noqa: BLE001 - le service ne doit pas mourir sur une erreur ponctuelle
            logger.exception("Échec de la sauvegarde ; nouvelle tentative à la prochaine échéance.")


def principal(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    analyse = argparse.ArgumentParser(description="Sauvegarde et restauration de la base MédiRDV")
    sous = analyse.add_subparsers(dest="commande", required=True)
    for nom in ("sauvegarder", "lister", "boucle"):
        p = sous.add_parser(nom)
        p.add_argument("--dossier")
    for nom in ("verifier", "restaurer"):
        p = sous.add_parser(nom)
        p.add_argument("fichier")
        p.add_argument("--dossier")
        if nom == "restaurer":
            p.add_argument("--remplacer", action="store_true", help="remplacer une base non vide (efface les données actuelles)")
    args = analyse.parse_args(argv)

    from backend.database import engine, initialiser_base

    dossier = _dossier(args.dossier)
    try:
        if args.commande == "lister":
            for f in sorted(dossier.glob(f"{PREFIXE}*.zip"), reverse=True):
                print(f"{f.name}  {f.stat().st_size / 1024:.0f} Ko")
            return 0
        if args.commande in ("verifier", "restaurer"):
            chemin = Path(args.fichier)
            if not chemin.exists() and (dossier / args.fichier).exists():
                chemin = dossier / args.fichier
            manifeste, _ = lire_et_verifier(chemin)
            print(f"Sauvegarde valide : {manifeste['cree_le']}, {manifeste['total_lignes']} lignes, schéma {manifeste['revision_schema']}.")
            if args.commande == "verifier":
                return 0
            initialiser_base()
            if args.remplacer:
                protection = sauvegarder(engine, dossier, "avant_restauration")
                print(f"Sauvegarde de sécurité de l'état actuel : {protection.name}")
            restaures = restaurer(engine, chemin, remplacer=args.remplacer)
            print(f"Restauration terminée : {sum(restaures.values())} lignes dans {len(restaures)} tables.")
            return 0
        initialiser_base()
        if args.commande == "boucle":
            _boucle(engine, dossier)
            return 0
        print(f"Sauvegarde créée : {sauvegarder(engine, dossier)}")
        return 0
    except SauvegardeError as erreur:
        print(f"Erreur : {erreur}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(principal())
