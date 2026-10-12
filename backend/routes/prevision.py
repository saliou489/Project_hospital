"""Prévision du volume hebdomadaire de rendez-vous par spécialité (EF-24) ; méthodes dans backend/analytique/prevision.py."""
import logging
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Annotated

import numpy as np
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.analytique.prevision import (
    METHODES,
    ResultatSpecialite,
    Serie,
    choisir_methode,
    evaluer_serie,
    intervalle,
    jours_ouvres_de_la_semaine,
    lundi_de,
)
from backend.database import get_db
from backend.exceptions import RessourceIntrouvableError
from backend.models import JourFerie, Medecin, RendezVous, StatutRdv, Utilisateur, aujourdhui
from backend.routes.stats import REPONSES_ERREURS, exiger_tableau_de_bord
from backend.schemas import (
    EvaluationMethode,
    PointBacktest,
    PointHistorique,
    PointPrevision,
    PrevisionSpecialite,
    ReponsePrevision,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stats", tags=["Statistiques"])

NOM_ENSEMBLE = "Ensemble de la clinique"
SEMAINES_HISTORIQUE_AFFICHEES = 26

LIMITES = [
    "Les données sont synthétiques : les résultats illustrent la méthode, ils ne décrivent pas une activité réelle.",
    "Douze mois d'historique ne permettent pas d'estimer une saisonnalité annuelle (un seul cycle observé).",
    "L'évaluation porte sur 8 semaines seulement : les erreurs mesurées sont indicatives.",
    "La prévision d'une semaine ne descend jamais sous le nombre de rendez-vous déjà enregistrés pour cette semaine.",
    "L'intervalle (environ 90 %) suppose des erreurs de même ampleur que sur la période tenue à l'écart.",
]


def _arrondi(valeur: float | None, chiffres: int = 2) -> float | None:
    return None if valeur is None else round(float(valeur), chiffres)


def _evaluation(resultat: ResultatSpecialite) -> list[EvaluationMethode]:
    return [
        EvaluationMethode(
            methode=m, libelle=METHODES[m], mae=_arrondi(e["mae"]), mape_pct=_arrondi(e["mape"], 1)
        )
        for m, e in resultat.evaluation.items()
    ]


def _construire(
    nom: str,
    resultat: ResultatSpecialite,
    retenue: str,
    lundis_futurs: list[date],
    deja_pris: Counter,
) -> PrevisionSpecialite:
    serie = resultat.serie
    n_test = len(resultat.evaluation[retenue]["prevu"])
    prevision = resultat.futures[retenue]
    bas, haut = intervalle(resultat, retenue, prevision)
    return PrevisionSpecialite(
        specialite=nom,
        historique=[
            PointHistorique(semaine=s, volume=int(v))
            for s, v in zip(serie.semaines[-SEMAINES_HISTORIQUE_AFFICHEES:], serie.valeurs[-SEMAINES_HISTORIQUE_AFFICHEES:])
        ],
        backtest=[
            PointBacktest(
                semaine=serie.semaines[-n_test + i],
                reel=int(serie.valeurs[-n_test + i]),
                par_methode={m: round(float(resultat.evaluation[m]["prevu"][i]), 2) for m in METHODES},
            )
            for i in range(n_test)
        ],
        evaluation=_evaluation(resultat),
        prevision=[
            PointPrevision(
                semaine=lundis_futurs[i],
                # Le volume final d'une semaine ne peut pas être inférieur aux rendez-vous déjà enregistrés.
                prevision=round(max(float(prevision[i]), float(deja_pris[lundis_futurs[i]])), 1),
                bas=round(max(float(bas[i]), float(deja_pris[lundis_futurs[i]])), 1),
                haut=round(max(float(haut[i]), float(deja_pris[lundis_futurs[i]])), 1),
                deja_pris=int(deja_pris[lundis_futurs[i]]),
                par_methode={m: round(float(resultat.futures[m][i]), 1) for m in METHODES},
            )
            for i in range(len(lundis_futurs))
        ],
    )


@router.get(
    "/prevision",
    response_model=ReponsePrevision,
    summary="Prévision du volume hebdomadaire de rendez-vous par spécialité (EF-24)",
    responses=REPONSES_ERREURS,
)
def prevision_hebdomadaire(
    specialite: str | None = None,
    horizon: Annotated[int, Query(ge=1, le=8, description="Nombre de semaines à prévoir")] = 4,
    db: Session = Depends(get_db),
    _utilisateur: Utilisateur = Depends(exiger_tableau_de_bord),
) -> ReponsePrevision:
    """Compare une moyenne mobile, un lissage de Holt et une régression calendaire sur les 8 dernières semaines."""
    aujourd_hui = aujourdhui()
    lundi_courant = lundi_de(aujourd_hui)
    lignes = db.execute(
        select(RendezVous.debut, Medecin.specialite)
        .join(Medecin, RendezVous.medecin_id == Medecin.id)
        .where(RendezVous.statut != StatutRdv.ANNULE)
    ).all()
    feries = set(db.scalars(select(JourFerie.jour)).all())

    passe: dict[str, Counter] = defaultdict(Counter)
    a_venir: Counter = Counter()
    for debut, spec in lignes:
        lundi = lundi_de(debut.date())
        if lundi < lundi_courant:
            passe[spec][lundi] += 1
        else:
            a_venir[lundi] += 1
    if specialite and specialite not in passe and not db.scalar(select(Medecin.id).where(Medecin.specialite == specialite)):
        raise RessourceIntrouvableError(f"Spécialité {specialite} introuvable.", "SPECIALITE_INTROUVABLE")
    if not passe:
        raise RessourceIntrouvableError("Aucun rendez-vous passé : la prévision n'est pas calculable.", "HISTORIQUE_VIDE")

    premier = min(min(c) for c in passe.values())
    semaines = [premier + timedelta(weeks=i) for i in range((lundi_courant - premier).days // 7)]
    ouvres = np.array([jours_ouvres_de_la_semaine(s, feries) for s in semaines])
    futurs = [lundi_courant + timedelta(weeks=i) for i in range(horizon)]
    ouvres_futurs = np.array([jours_ouvres_de_la_semaine(s, feries) for s in futurs])

    # Le détail par spécialité vient des rendez-vous futurs déjà pris, regroupés par spécialité.
    futurs_par_spec: dict[str, Counter] = defaultdict(Counter)
    for debut, spec in lignes:
        lundi = lundi_de(debut.date())
        if lundi >= lundi_courant:
            futurs_par_spec[spec][lundi] += 1

    noms = sorted(passe)
    resultats = {
        nom: evaluer_serie(
            Serie(nom, semaines, np.array([passe[nom][s] for s in semaines], dtype=float), ouvres), futurs, ouvres_futurs
        )
        for nom in noms
    }
    total = Counter()
    for compteur in passe.values():
        total.update(compteur)
    resultat_total = evaluer_serie(
        Serie(NOM_ENSEMBLE, semaines, np.array([total[s] for s in semaines], dtype=float), ouvres), futurs, ouvres_futurs
    )
    retenue, totaux = choisir_methode(list(resultats.values()))
    n_test = len(next(iter(resultats.values())).evaluation[retenue]["prevu"])

    retenues = [nom for nom in noms if not specialite or nom == specialite]
    return ReponsePrevision(
        derniere_semaine_observee=semaines[-1],
        horizon_semaines=horizon,
        semaines_entrainement=len(semaines) - n_test,
        semaines_tenues_a_l_ecart=n_test,
        methode_retenue=retenue,
        methodes=[
            EvaluationMethode(methode=m, libelle=METHODES[m], mae=_arrondi(t["mae"]), mape_pct=_arrondi(t["mape"], 1))
            for m, t in totaux.items()
        ],
        ensemble=_construire(NOM_ENSEMBLE, resultat_total, retenue, futurs, a_venir),
        specialites=[_construire(nom, resultats[nom], retenue, futurs, futurs_par_spec[nom]) for nom in retenues],
        limites=LIMITES,
    )


__all__ = ["router"]
