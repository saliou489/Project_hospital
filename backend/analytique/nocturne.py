"""Module analytique nocturne (cahier des charges 10.1) : calcule les scores de risque d'absence et les enregistre.

Les indicateurs et la prévision (blocs 3a et 3b) sont calculés à la demande par l'API : ils restent rapides sur un an
d'historique. Seul l'apprentissage du score de risque justifie un calcul différé ; ses résultats sont écrits dans les
tables ``scores_risque`` et ``evaluations_risque``, que l'API se contente de lire.

Utilisation :
    python -m backend.analytique.nocturne --une-fois     # un calcul immédiat puis arrêt
    python -m backend.analytique.nocturne                # boucle : un calcul au démarrage, puis un par nuit à MEDIRDV_HEURE_NOCTURNE (02:00)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from backend.analytique import risque
from backend.database import SessionLocale, initialiser_base
from backend.models import EvaluationRisque, Medecin, Patient, RendezVous, ScoreRisque, StatutRdv, maintenant

logger = logging.getLogger(__name__)

VERROU_ANALYTIQUE = 727275  # verrou PostgreSQL : un seul calcul à la fois (boucle nocturne et recalcul manuel)
STATUTS_A_VENIR = (StatutRdv.PLANIFIE, StatutRdv.CONFIRME)


def charger_donnees(db: Session) -> pd.DataFrame:
    """Rendez-vous non annulés avec les champs bruts utiles au score (aucun nom, aucun téléphone)."""
    lignes = db.execute(
        select(
            RendezVous.id, RendezVous.patient_id, Medecin.specialite, RendezVous.debut, RendezVous.date_prise,
            RendezVous.statut, RendezVous.confirme, Patient.date_naissance,
        )
        .join(Medecin, Medecin.id == RendezVous.medecin_id)
        .join(Patient, Patient.id == RendezVous.patient_id)
        .where(RendezVous.statut != StatutRdv.ANNULE)
    ).all()
    brut = pd.DataFrame(
        [(r[0], r[1], r[2], r[3], r[4], r[5].value, r[6], r[7]) for r in lignes],
        columns=["rdv_id", "patient_id", "specialite", "debut", "date_prise", "statut", "confirme", "date_naissance"],
    )
    return brut


def calculer(db: Session, hypotheses: risque.Hypotheses | None = None) -> dict:
    """Apprend, évalue, note les rendez-vous à venir et remplace les scores enregistrés. Retourne un court bilan."""
    verrou = db.get_bind().dialect.name == "postgresql"
    if verrou:
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": VERROU_ANALYTIQUE})
    brut = charger_donnees(db)
    if brut.empty:
        return {"statut": "aucune_donnee", "scores": 0}
    donnees = risque.construire_variables(brut)
    try:
        resultat = risque.apprendre(donnees, hypotheses)
    except risque.DonneesInsuffisantesError as erreur:
        logger.warning("Score de risque non calculé : %s", erreur)
        return {"statut": "donnees_insuffisantes", "message": str(erreur), "scores": 0}

    instant = maintenant()
    a_venir = donnees[donnees["statut"].isin([s.value for s in STATUTS_A_VENIR]) & (donnees["debut"] >= pd.Timestamp(instant))]
    notes = risque.noter(resultat, a_venir)

    db.execute(delete(ScoreRisque))
    db.add_all(
        ScoreRisque(
            rdv_id=ligne.rdv_id, score=round(float(ligne.score), 4), a_relancer=bool(ligne.score >= resultat.seuil),
            facteurs=(ligne.facteurs or "")[:200], modele=resultat.retenu, calcule_le=instant,
        )
        for ligne in notes.itertuples()
    )
    evaluation = risque.resume_evaluation(resultat)
    db.add(
        EvaluationRisque(
            calcule_le=instant, modele_retenu=resultat.retenu, seuil=resultat.seuil,
            auc=resultat.modeles[resultat.retenu].auc, nb_scores=len(notes), details=json.dumps(evaluation),
        )
    )
    db.commit()
    return {"statut": "ok", "scores": len(notes), "modele": resultat.retenu, "auc": round(resultat.modeles[resultat.retenu].auc, 3)}


def _prochaine_execution(heure: str, maintenant_: datetime) -> datetime:
    h, m = (int(x) for x in heure.split(":"))
    cible = maintenant_.replace(hour=h, minute=m, second=0, microsecond=0)
    return cible if cible > maintenant_ else cible + timedelta(days=1)


def principal() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    analyse = argparse.ArgumentParser(description="Module analytique nocturne de MédiRDV")
    analyse.add_argument("--une-fois", action="store_true", help="calculer maintenant puis s'arrêter")
    arguments = analyse.parse_args()
    initialiser_base()
    heure = os.getenv("MEDIRDV_HEURE_NOCTURNE", "02:00")
    premier = True
    while True:
        if not arguments.une_fois and not premier:
            cible = _prochaine_execution(heure, maintenant())
            logger.info("Prochain calcul analytique : %s", cible.isoformat(sep=" ", timespec="minutes"))
            time.sleep(max(1.0, (cible - maintenant()).total_seconds()))
        premier = False  # un premier calcul au démarrage : les écrans de relance ne restent pas vides jusqu'à la première nuit
        try:
            with SessionLocale() as db:
                logger.info("Calcul analytique : %s", calculer(db))
        except Exception:  # noqa: BLE001 - la boucle ne doit pas mourir sur une erreur ponctuelle
            logger.exception("Échec du calcul analytique")
            if arguments.une_fois:
                raise
        if arguments.une_fois:
            return


if __name__ == "__main__":
    principal()
