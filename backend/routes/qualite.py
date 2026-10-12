"""Qualité des données : doublons de patients et rapport d'anomalies (cahier des charges, section 7.3).

Ces routes ne modifient rien. Les corrections se font par la modification de la fiche patient
(PATCH /patients/{id}), tracée dans le journal ; aucune fiche n'est supprimée (RG-11).
"""
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import RendezVous, RoleUtilisateur, Utilisateur
from backend.qualite import construire_rapport, groupes_de_doublons
from backend.schemas import (
    ErreurReponse,
    GroupeDoublons,
    ListeDoublons,
    PatientDoublon,
    PatientLecture,
    RapportQualite,
)
from backend.securite import exiger_roles

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/qualite", tags=["Qualité des données"])

# Les agents voient les doublons pour pouvoir les corriger ; le rapport complet est réservé à la gestion.
exiger_doublons = exiger_roles(RoleUtilisateur.AGENT, RoleUtilisateur.RESPONSABLE, RoleUtilisateur.ADMINISTRATEUR)
exiger_rapport = exiger_roles(RoleUtilisateur.RESPONSABLE, RoleUtilisateur.ADMINISTRATEUR)

REPONSES_ERREURS = {
    401: {"model": ErreurReponse, "description": "Non authentifié ou session expirée"},
    403: {"model": ErreurReponse, "description": "Rôle non autorisé"},
    422: {"model": ErreurReponse, "description": "Paramètres invalides"},
    500: {"model": ErreurReponse, "description": "Erreur interne"},
}


@router.get(
    "/doublons",
    response_model=ListeDoublons,
    summary="Lister les groupes de fiches patients en doublon",
    responses=REPONSES_ERREURS,
)
def lister_doublons(db: Session = Depends(get_db), _utilisateur: Utilisateur = Depends(exiger_doublons)) -> ListeDoublons:
    """Fiches de même identité (nom, prénom, date de naissance), avec leur nombre de rendez-vous."""
    groupes = groupes_de_doublons(db)
    identifiants = [p.id for groupe in groupes for p in groupe]
    nb_rdv = {}
    if identifiants:
        nb_rdv = dict(
            db.execute(
                select(RendezVous.patient_id, func.count())
                .where(RendezVous.patient_id.in_(identifiants))
                .group_by(RendezVous.patient_id)
            ).all()
        )
    return ListeDoublons(
        nb_groupes=len(groupes),
        nb_patients=len(identifiants),
        groupes=[
            GroupeDoublons(
                numero=numero,
                patients=[
                    PatientDoublon(**PatientLecture.model_validate(p).model_dump(), nb_rdv=nb_rdv.get(p.id, 0))
                    for p in groupe
                ],
            )
            for numero, groupe in enumerate(groupes, start=1)
        ],
    )


@router.get(
    "/rapport",
    response_model=RapportQualite,
    summary="Rapport de qualité : valeurs manquantes, doublons et incohérences",
    responses=REPONSES_ERREURS,
)
def rapport_qualite(
    exemples: Annotated[int, Query(ge=0, le=1000)] = 20,
    db: Session = Depends(get_db),
    _utilisateur: Utilisateur = Depends(exiger_rapport),
) -> RapportQualite:
    """Exécute tous les contrôles ; ``exemples`` borne le nombre d'identifiants cités par contrôle."""
    return construire_rapport(db, exemples)
