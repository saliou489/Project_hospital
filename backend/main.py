"""Application FastAPI MédiRDV.

Lancement (depuis la racine du projet) :

    uvicorn backend.main:app --reload

La documentation interactive (Swagger) est disponible sur /docs.
"""
import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from backend.database import SessionLocale, engine, get_db, initialiser_base
from backend.entetes_http import ajouter_entetes
from backend.exceptions import enregistrer_gestionnaires
from backend.journal_acces import configurer_journal_acces
from backend.models import Medecin, Utilisateur
from backend.planning import initialiser_referentiels, marquer_absences_automatiques
from backend.routes import admin, analyse, auth, export, patients, planning, pointage, prevision, qualite, rdv, risque, stats

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s : %(message)s",
)
configurer_journal_acces()  # ENF-16 : journal d'accès horodaté et sans donnée personnelle
logger = logging.getLogger(__name__)


def _passer_absents() -> int:
    with SessionLocale() as db:
        return marquer_absences_automatiques(db)


async def _boucle_absences_automatiques(intervalle_s: int) -> None:
    """EF-17 : vérifie périodiquement les rendez-vous non marqués une heure après leur début."""
    while True:
        try:
            await asyncio.to_thread(_passer_absents)
        except Exception:  # noqa: BLE001 - la boucle ne doit jamais s'arrêter sur une erreur ponctuelle
            logger.exception("Échec du passage automatique en « Absent » ; nouvelle tentative au prochain cycle.")
        await asyncio.sleep(intervalle_s)


@asynccontextmanager
async def cycle_de_vie(_application: FastAPI):
    """Au démarrage : migrations, référentiels de planning manquants, puis tâche d'absence automatique (EF-17)."""
    initialiser_base()
    logger.info("Base de données prête (moteur : %s).", engine.dialect.name)
    with SessionLocale() as db:
        initialiser_referentiels(db)
    with SessionLocale() as db:
        if not db.scalar(select(func.count()).select_from(Utilisateur)):
            logger.warning(
                "Aucun compte de connexion en base : lancez « python -m backend.creer_comptes » pour les créer."
            )
        if not db.scalar(select(func.count()).select_from(Medecin)):
            logger.warning(
                "Aucun médecin en base : importez les données avec « python -m backend.seed_data », puis lancez "
                "« python -m backend.creer_comptes » pour créer les comptes des médecins et des agents."
            )
    intervalle = int(os.getenv("MEDIRDV_ABSENCE_AUTO_SECONDES", "60"))
    tache = asyncio.create_task(_boucle_absences_automatiques(intervalle)) if intervalle > 0 else None
    try:
        yield
    finally:
        if tache is not None:
            tache.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tache


app = FastAPI(
    title="BAOBAB - API de gestion des rendez-vous",
    version="1.0.0",
    description=(
        "API de prise et de suivi des rendez-vous médicaux. "
        "Toutes les erreurs sont renvoyées au format {\"detail\": ..., \"code_erreur\": ...}."
    ),
    lifespan=cycle_de_vie,
)

enregistrer_gestionnaires(app)
app.middleware("http")(ajouter_entetes)

app.include_router(auth.router)
app.include_router(rdv.router)
app.include_router(rdv.router_referentiels)
app.include_router(planning.router_referentiels)
app.include_router(planning.router)
app.include_router(patients.router)
app.include_router(stats.router)
app.include_router(analyse.router)
app.include_router(prevision.router)
app.include_router(risque.router)
app.include_router(export.router)
app.include_router(pointage.router)
app.include_router(qualite.router)
app.include_router(admin.router)


@app.get("/sante", tags=["Système"], summary="Contrôle de disponibilité de l'API et de la base")
def sante(db: Session = Depends(get_db)) -> dict[str, str]:
    """Répond « ok » si l'API et la base de données sont joignables (sinon erreur 500 au format uniforme)."""
    db.execute(text("SELECT 1"))
    return {"statut": "ok", "base_de_donnees": "disponible"}
