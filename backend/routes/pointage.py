"""Pointage des employés : une heure d'arrivée et une heure de départ par jour.

Chaque employé pointe lui-même depuis son compte. L'heure est celle du serveur (heure de Dakar) :
elle ne dépend pas de l'horloge de l'ordinateur de l'employé. Seul un administrateur peut corriger
un pointage (voir routes/admin.py), avec une trace dans le journal des actions.
"""
import logging
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.database import ecrire_avec_reessai, get_db
from backend.exceptions import ConflitError, DonneesInvalidesError, RegleMetierError
from backend.models import Pointage, Utilisateur, aujourdhui, maintenant
from backend.schemas import ErreurReponse, EtatPointage, ListePointages, PointageLecture
from backend.securite import utilisateur_courant

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pointage", tags=["Pointage"])

REPONSES_ERREURS = {
    401: {"model": ErreurReponse, "description": "Non authentifié ou session expirée"},
    409: {"model": ErreurReponse, "description": "Arrivée ou départ déjà pointé"},
    422: {"model": ErreurReponse, "description": "Paramètres invalides"},
    500: {"model": ErreurReponse, "description": "Erreur interne"},
}


def _pointage_du_jour(db: Session, utilisateur: Utilisateur) -> Pointage | None:
    return db.scalar(
        select(Pointage).where(Pointage.utilisateur_id == utilisateur.id, Pointage.jour == aujourdhui())
    )


@router.get(
    "/aujourdhui",
    response_model=EtatPointage,
    summary="Situation de pointage du jour pour l'employé connecté",
    responses=REPONSES_ERREURS,
)
def etat_du_jour(
    db: Session = Depends(get_db), utilisateur: Utilisateur = Depends(utilisateur_courant)
) -> EtatPointage:
    pointage = _pointage_du_jour(db, utilisateur)
    return EtatPointage(
        jour=aujourdhui(),
        arrivee=pointage.arrivee if pointage else None,
        depart=pointage.depart if pointage else None,
    )


@router.post(
    "/arrivee",
    response_model=PointageLecture,
    status_code=201,
    summary="Pointer son arrivée",
    responses=REPONSES_ERREURS,
)
def pointer_arrivee(
    db: Session = Depends(get_db), utilisateur: Utilisateur = Depends(utilisateur_courant)
) -> PointageLecture:
    def operation() -> Pointage:
        existant = _pointage_du_jour(db, utilisateur)
        if existant is not None:
            raise ConflitError(
                f"Votre arrivée est déjà pointée aujourd'hui, à {existant.arrivee:%H:%M}.", "DEJA_POINTE"
            )
        instant = maintenant()
        pointage = Pointage(utilisateur=utilisateur, jour=instant.date(), arrivee=instant)
        db.add(pointage)
        db.flush()
        return pointage

    return PointageLecture.model_validate(ecrire_avec_reessai(db, operation))


@router.post(
    "/depart",
    response_model=PointageLecture,
    summary="Pointer son départ",
    responses=REPONSES_ERREURS,
)
def pointer_depart(
    db: Session = Depends(get_db), utilisateur: Utilisateur = Depends(utilisateur_courant)
) -> PointageLecture:
    def operation() -> Pointage:
        pointage = _pointage_du_jour(db, utilisateur)
        if pointage is None:
            raise RegleMetierError(
                "Aucune arrivée pointée aujourd'hui : pointez d'abord votre arrivée.", "ARRIVEE_MANQUANTE"
            )
        if pointage.depart is not None:
            raise ConflitError(
                f"Votre départ est déjà pointé aujourd'hui, à {pointage.depart:%H:%M}.", "DEPART_DEJA_POINTE"
            )
        pointage.depart = maintenant()
        db.flush()
        return pointage

    return PointageLecture.model_validate(ecrire_avec_reessai(db, operation))


@router.get(
    "/mes-pointages",
    response_model=ListePointages,
    summary="Historique de mes pointages",
    responses=REPONSES_ERREURS,
)
def mes_pointages(
    date_debut: date | None = None,
    date_fin: date | None = None,
    limite: Annotated[int, Query(ge=1, le=366)] = 62,
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(utilisateur_courant),
) -> ListePointages:
    if date_debut and date_fin and date_debut > date_fin:
        raise DonneesInvalidesError(
            "La date de début doit être antérieure ou égale à la date de fin.", "PERIODE_INVALIDE"
        )
    filtres = [Pointage.utilisateur_id == utilisateur.id]
    if date_debut:
        filtres.append(Pointage.jour >= date_debut)
    if date_fin:
        filtres.append(Pointage.jour <= date_fin)
    total = int(db.scalar(select(func.count()).select_from(Pointage).where(*filtres)) or 0)
    pointages = db.scalars(select(Pointage).where(*filtres).order_by(Pointage.jour.desc()).limit(limite)).all()
    return ListePointages(
        total=total, limite=limite, decalage=0, elements=[PointageLecture.model_validate(p) for p in pointages]
    )
