"""Score de risque d'absence (EF-25) : liste des rendez-vous à relancer et évaluation du modèle.

Le score est une aide à la relance. Il n'est utilisé nulle part dans la création ou la modification d'un rendez-vous
(il ne peut donc jamais bloquer une prise) et il n'apparaît dans aucune réponse destinée à la prise de rendez-vous :
seuls ces endpoints le lisent, réservés au personnel qui relance ou pilote.
"""
import json
import logging
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.analytique import nocturne
from backend.database import get_db
from backend.exceptions import RessourceIntrouvableError
from backend.models import EvaluationRisque, RendezVous, RoleUtilisateur, ScoreRisque, StatutRdv, Utilisateur, maintenant
from backend.routes.stats import REPONSES_ERREURS
from backend.schemas import BilanCalculRisque, EvaluationRisqueReponse, ListeARelancer, RdvARelancer
from backend.securite import exiger_roles

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/risque-absence", tags=["Risque d'absence"])

# Les agents relancent ; le responsable pilote ; l'administrateur a tous les droits. La direction ne voit que l'évaluation.
exiger_relance = exiger_roles(RoleUtilisateur.AGENT, RoleUtilisateur.RESPONSABLE, RoleUtilisateur.ADMINISTRATEUR)
exiger_evaluation = exiger_roles(RoleUtilisateur.RESPONSABLE, RoleUtilisateur.DIRECTION, RoleUtilisateur.ADMINISTRATEUR)
exiger_recalcul = exiger_roles(RoleUtilisateur.RESPONSABLE, RoleUtilisateur.ADMINISTRATEUR)

LIMITES = [
    "Les données sont synthétiques : les performances mesurées illustrent la méthode, elles ne prédisent pas l'activité réelle.",
    "Le score n'est qu'une aide à la relance : il ne bloque aucune prise de rendez-vous et n'est jamais montré au patient.",
    "Biais possibles : un patient récemment inscrit n'a pas d'historique, donc un score moins informatif ; le modèle ne sait "
    "rien des raisons personnelles d'une absence (transport, maladie, travail).",
    "Dérive : le comportement des patients change avec le temps ; le module nocturne réapprend et enregistre l'AUC à chaque "
    "exécution, une baisse durable doit alerter.",
    "Les coûts du seuil (appel, créneau perdu, efficacité d'une relance) sont des hypothèses à faire valider par la clinique.",
    "L'effet d'une relance n'est pas mesuré dans les données : le gain net est une estimation sous ces hypothèses.",
]


@router.get(
    "/a-relancer",
    response_model=ListeARelancer,
    summary="Rendez-vous à relancer (risque d'absence élevé)",
    responses=REPONSES_ERREURS,
)
def lister_a_relancer(
    db: Annotated[Session, Depends(get_db)],
    _utilisateur: Annotated[Utilisateur, Depends(exiger_relance)],
    jours: Annotated[int, Query(ge=1, le=90, description="Horizon, en jours à partir de maintenant")] = 7,
    tous: Annotated[bool, Query(description="Inclure aussi les rendez-vous sous le seuil de relance")] = False,
    specialite: str | None = None,
    medecin_id: str | None = None,
) -> ListeARelancer:
    instant = maintenant()
    requete = (
        select(ScoreRisque, RendezVous)
        .join(RendezVous, RendezVous.id == ScoreRisque.rdv_id)
        .where(
            # Un rendez-vous annulé, honoré ou déplacé depuis le dernier calcul n'a plus à être relancé.
            RendezVous.statut.in_([StatutRdv.PLANIFIE, StatutRdv.CONFIRME]),
            RendezVous.debut >= instant,
            RendezVous.debut < instant + timedelta(days=jours),
        )
        .order_by(ScoreRisque.score.desc(), RendezVous.debut)
    )
    if not tous:
        requete = requete.where(ScoreRisque.a_relancer.is_(True))
    if medecin_id:
        requete = requete.where(RendezVous.medecin_id == medecin_id)
    lignes = db.execute(requete).all()
    if specialite:
        lignes = [(s, r) for s, r in lignes if r.medecin.specialite == specialite]
    derniere = db.scalar(select(EvaluationRisque).order_by(EvaluationRisque.calcule_le.desc()).limit(1))
    return ListeARelancer(
        calcule_le=derniere.calcule_le if derniere else None,
        modele=derniere.modele_retenu if derniere else None,
        seuil_pct=round(derniere.seuil * 100, 1) if derniere else None,
        nombre=len(lignes),
        rendez_vous=[
            RdvARelancer(
                rdv_id=r.id, patient_id=r.patient_id, patient=f"{r.patient.prenom} {r.patient.nom}",
                telephone=r.patient.telephone, medecin=r.medecin.nom, specialite=r.medecin.specialite,
                date_rdv=r.date_rdv, heure_rdv=r.heure_rdv, statut=r.statut, score_pct=round(s.score * 100, 1),
                a_relancer=s.a_relancer, facteurs=s.facteurs,
            )
            for s, r in lignes
        ],
    )


@router.get(
    "/evaluation",
    response_model=EvaluationRisqueReponse,
    summary="Évaluation du modèle : AUC, précision, rappel, matrice de confusion, seuil",
    responses={**REPONSES_ERREURS, 404: REPONSES_ERREURS[403]},
)
def lire_evaluation(
    db: Annotated[Session, Depends(get_db)],
    _utilisateur: Annotated[Utilisateur, Depends(exiger_evaluation)],
) -> EvaluationRisqueReponse:
    derniere = db.scalar(select(EvaluationRisque).order_by(EvaluationRisque.calcule_le.desc()).limit(1))
    if derniere is None:
        raise RessourceIntrouvableError(
            "Aucune évaluation n'a encore été calculée (le module analytique s'exécute chaque nuit).", "EVALUATION_INTROUVABLE"
        )
    return EvaluationRisqueReponse(calcule_le=derniere.calcule_le, limites=LIMITES, **json.loads(derniere.details))


@router.post(
    "/recalculer",
    response_model=BilanCalculRisque,
    summary="Relancer maintenant le calcul que le module nocturne fait chaque nuit",
    responses=REPONSES_ERREURS,
)
def recalculer(
    db: Annotated[Session, Depends(get_db)],
    _utilisateur: Annotated[Utilisateur, Depends(exiger_recalcul)],
) -> BilanCalculRisque:
    bilan = nocturne.calculer(db)
    return BilanCalculRisque(**{k: bilan.get(k) for k in ("statut", "scores", "modele", "auc", "message")})
