"""Initialisation automatique d'une base neuve (utilisée par Docker Compose).

    python -m backend.initialiser

Idempotent : si la base contient déjà des médecins, il ne fait rien. Sinon, dans l'ordre :
1. migrations du schéma ;
2. import du CSV des médecins, agents et premiers patients (``backend.seed_data``) ;
3. génération des données de démonstration (``backend.generer_donnees``) si MEDIRDV_DONNEES_DEMO vaut « oui » ;
4. création des comptes de connexion ; le fichier de clés est écrit dans MEDIRDV_DOSSIER_CLES (``/sortie`` en conteneur).

Le fichier de clés contient des secrets : à récupérer puis à supprimer, ne jamais le publier.
"""
import logging
import os
import sys
from pathlib import Path

from sqlalchemy import func, select

from backend import creer_comptes, generer_donnees, seed_data
from backend.database import RACINE_PROJET, SessionLocale, initialiser_base
from backend.models import Medecin

logger = logging.getLogger("backend.initialiser")


def _vrai(valeur: str) -> bool:
    return valeur.strip().lower() in {"1", "oui", "true", "vrai", "yes"}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s : %(message)s")
    dossier = Path(os.getenv("MEDIRDV_DOSSIER_CLES", str(RACINE_PROJET)))
    dossier.mkdir(parents=True, exist_ok=True)
    initialiser_base()
    with SessionLocale() as db:
        deja_initialisee = bool(db.scalar(select(func.count()).select_from(Medecin)))
    if deja_initialisee:
        logger.info("Base déjà initialisée : rien à faire.")
        return 0

    logger.info("Base vide : import des médecins et des agents.")
    if seed_data.main(["--rapport", str(dossier / "rapport_import_rejets.csv")]) != 0:
        return 1
    if _vrai(os.getenv("MEDIRDV_DONNEES_DEMO", "oui")):
        logger.info("Génération des données de démonstration (12 mois d'historique, 3 mois à venir).")
        if generer_donnees.main(["--remplacer", "--fichier-anomalies", str(dossier / "anomalies_injectees.csv")]) != 0:
            return 1
    if creer_comptes.main(["--dossier", str(dossier)]) != 0:
        return 1
    logger.info("Initialisation terminée. Les clés de connexion sont dans %s (fichier cles_a_distribuer_*.csv).", dossier)
    return 0


if __name__ == "__main__":
    sys.exit(main())
