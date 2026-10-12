"""Indicateurs d'analyse du tableau de bord (bloc 3) : jour, carte de chaleur, délai d'attente, annulations, évolution.

Les droits sont ceux du tableau de bord : Responsable des opérations, Direction (vue agrégée, sans détail par médecin)
et Administrateur. Les filtres période / spécialité / médecin sont communs à tous les indicateurs (EF-23).
"""
import logging
import statistics
from collections import Counter, defaultdict
from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import Medecin, RendezVous, StatutRdv, Utilisateur, aujourdhui
from backend.routes.planning import construire_occupation
from backend.routes.stats import (
    REPONSES_ERREURS,
    _construire_filtres,
    _verifier_vue_autorisee,
    calculer_taux_absence,
    exiger_tableau_de_bord,
)
from backend.schemas import (
    AnnulationMois,
    AnnulationMotif,
    AnnulationSpecialite,
    CarteChaleur,
    CelluleChaleur,
    DelaiAttente,
    DelaiSpecialite,
    EvolutionMensuelleSpecialite,
    IndicateursJour,
    RepartitionSpecialiteJour,
    TauxAnnulation,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stats", tags=["Statistiques"])


def _pct(nombre: int, total: int) -> float | None:
    return round(100.0 * nombre / total, 1) if total else None


def _percentile(valeurs: list[int], p: float) -> float:
    """Percentile par interpolation linéaire (valeurs triées, liste non vide)."""
    if len(valeurs) == 1:
        return float(valeurs[0])
    position = (len(valeurs) - 1) * p
    bas = int(position)
    haut = min(bas + 1, len(valeurs) - 1)
    return valeurs[bas] + (valeurs[haut] - valeurs[bas]) * (position - bas)


@router.get("/jour", response_model=IndicateursJour, summary="Indicateurs clés du jour (EF-20)", responses=REPONSES_ERREURS)
def indicateurs_du_jour(
    jour: date | None = None,
    specialite: str | None = None,
    medecin_id: str | None = None,
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(exiger_tableau_de_bord),
) -> IndicateursJour:
    """Rendez-vous prévus, honorés, absents et taux d'occupation d'une journée (aujourd'hui par défaut)."""
    _verifier_vue_autorisee(utilisateur, medecin_id)
    jour = jour or aujourdhui()
    filtres = _construire_filtres(db, jour, jour, specialite, medecin_id)
    statuts = Counter(db.scalars(select(RendezVous.statut).where(*filtres)).all())
    honores, absents = statuts[StatutRdv.HONORE], statuts[StatutRdv.ABSENT]
    annules = statuts[StatutRdv.ANNULE]
    occupation = construire_occupation(db, jour, jour, specialite, medecin_id)
    return IndicateursJour(
        jour=jour,
        prevus=sum(statuts.values()) - annules,
        a_venir=statuts[StatutRdv.PLANIFIE] + statuts[StatutRdv.CONFIRME],
        honores=honores,
        absents=absents,
        annules=annules,
        taux_absence_pct=calculer_taux_absence(honores, absents),
        taux_occupation_pct=occupation.taux_occupation_pct,
    )


@router.get("/carte-chaleur", response_model=CarteChaleur, summary="Carte de chaleur de la demande (EF-21)", responses=REPONSES_ERREURS)
def carte_de_chaleur(
    date_debut: date | None = None,
    date_fin: date | None = None,
    specialite: str | None = None,
    medecin_id: str | None = None,
    inclure_annules: bool = False,
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(exiger_tableau_de_bord),
) -> CarteChaleur:
    """Nombre de rendez-vous par jour de la semaine et par heure, et par spécialité et jour de la semaine."""
    _verifier_vue_autorisee(utilisateur, medecin_id)
    filtres = _construire_filtres(db, date_debut, date_fin, specialite, medecin_id)
    if not inclure_annules:
        filtres.append(RendezVous.statut != StatutRdv.ANNULE)
    lignes = db.execute(
        select(RendezVous.debut, Medecin.specialite).join(Medecin, RendezVous.medecin_id == Medecin.id).where(*filtres)
    ).all()
    cases: Counter[tuple[int, int]] = Counter()
    par_spec: Counter[tuple[str, int]] = Counter()
    for debut, spec in lignes:
        cases[(debut.weekday(), debut.hour)] += 1
        par_spec[(spec, debut.weekday())] += 1
    return CarteChaleur(
        total=len(lignes),
        inclut_annules=inclure_annules,
        cellules=[CelluleChaleur(jour_semaine=j, heure=h, nombre=n) for (j, h), n in sorted(cases.items())],
        par_specialite=[
            RepartitionSpecialiteJour(specialite=s, jour_semaine=j, nombre=n) for (s, j), n in sorted(par_spec.items())
        ],
    )


@router.get("/delai-attente", response_model=DelaiAttente, summary="Délai d'attente médian par spécialité", responses=REPONSES_ERREURS)
def delai_attente(
    date_debut: date | None = None,
    date_fin: date | None = None,
    specialite: str | None = None,
    medecin_id: str | None = None,
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(exiger_tableau_de_bord),
) -> DelaiAttente:
    """Jours entre la date de prise et la date du rendez-vous ; les rendez-vous annulés sont exclus."""
    _verifier_vue_autorisee(utilisateur, medecin_id)
    filtres = _construire_filtres(db, date_debut, date_fin, specialite, medecin_id)
    filtres.append(RendezVous.statut != StatutRdv.ANNULE)
    lignes = db.execute(
        select(RendezVous.date_prise, RendezVous.debut, Medecin.specialite)
        .join(Medecin, RendezVous.medecin_id == Medecin.id)
        .where(*filtres)
    ).all()
    delais: dict[str, list[int]] = defaultdict(list)
    for prise, debut, spec in lignes:
        delais[spec].append(max(0, (debut.date() - prise).days))
    tous = [d for v in delais.values() for d in v]
    par_specialite = []
    for spec, valeurs in sorted(delais.items()):
        valeurs.sort()
        par_specialite.append(
            DelaiSpecialite(
                specialite=spec,
                nombre_rdv=len(valeurs),
                mediane_jours=float(statistics.median(valeurs)),
                moyenne_jours=round(statistics.fmean(valeurs), 1),
                p90_jours=round(_percentile(valeurs, 0.9), 1),
            )
        )
    return DelaiAttente(
        nombre_rdv=len(tous),
        mediane_jours=float(statistics.median(tous)) if tous else None,
        par_specialite=par_specialite,
    )


@router.get("/annulation", response_model=TauxAnnulation, summary="Taux d'annulation par motif, mois et spécialité", responses=REPONSES_ERREURS)
def taux_annulation(
    date_debut: date | None = None,
    date_fin: date | None = None,
    specialite: str | None = None,
    medecin_id: str | None = None,
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(exiger_tableau_de_bord),
) -> TauxAnnulation:
    """Annulés ÷ total créés (tous statuts), avec la répartition par motif (RG-05)."""
    _verifier_vue_autorisee(utilisateur, medecin_id)
    filtres = _construire_filtres(db, date_debut, date_fin, specialite, medecin_id)
    lignes = db.execute(
        select(RendezVous.debut, RendezVous.statut, RendezVous.motif_annulation, Medecin.specialite)
        .join(Medecin, RendezVous.medecin_id == Medecin.id)
        .where(*filtres)
    ).all()
    total = len(lignes)
    motifs: Counter[str] = Counter()
    mois_total: Counter[str] = Counter()
    mois_annules: Counter[str] = Counter()
    mois_motifs: dict[str, Counter[str]] = defaultdict(Counter)
    spec_total: Counter[str] = Counter()
    spec_annules: Counter[str] = Counter()
    for debut, statut, motif, spec in lignes:
        periode = f"{debut.year:04d}-{debut.month:02d}"
        mois_total[periode] += 1
        spec_total[spec] += 1
        if statut == StatutRdv.ANNULE:
            nom = motif.value if motif else "Non renseigné"
            motifs[nom] += 1
            mois_annules[periode] += 1
            mois_motifs[periode][nom] += 1
            spec_annules[spec] += 1
    annules = sum(motifs.values())
    return TauxAnnulation(
        total_cree=total,
        annules=annules,
        taux_annulation_pct=_pct(annules, total),
        par_motif=[
            AnnulationMotif(motif=m, nombre=n, part_pct=_pct(n, total) or 0.0) for m, n in motifs.most_common()
        ],
        par_mois=[
            AnnulationMois(
                periode=p,
                total_cree=mois_total[p],
                annules=mois_annules[p],
                taux_annulation_pct=_pct(mois_annules[p], mois_total[p]),
                par_motif=dict(mois_motifs[p]),
            )
            for p in sorted(mois_total)
        ],
        par_specialite=[
            AnnulationSpecialite(
                specialite=s,
                total_cree=spec_total[s],
                annules=spec_annules[s],
                taux_annulation_pct=_pct(spec_annules[s], spec_total[s]),
            )
            for s in sorted(spec_total)
        ],
    )


@router.get(
    "/evolution-mensuelle",
    response_model=list[EvolutionMensuelleSpecialite],
    summary="Évolution mensuelle du volume et du taux d'absence par spécialité (EF-22)",
    responses=REPONSES_ERREURS,
)
def evolution_mensuelle_par_specialite(
    date_debut: date | None = None,
    date_fin: date | None = None,
    specialite: str | None = None,
    medecin_id: str | None = None,
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(exiger_tableau_de_bord),
) -> list[EvolutionMensuelleSpecialite]:
    _verifier_vue_autorisee(utilisateur, medecin_id)
    filtres = _construire_filtres(db, date_debut, date_fin, specialite, medecin_id)
    lignes = db.execute(
        select(RendezVous.debut, RendezVous.statut, Medecin.specialite)
        .join(Medecin, RendezVous.medecin_id == Medecin.id)
        .where(*filtres)
    ).all()
    cumul: dict[tuple[str, str], Counter[StatutRdv]] = defaultdict(Counter)
    for debut, statut, spec in lignes:
        cumul[(f"{debut.year:04d}-{debut.month:02d}", spec)][statut] += 1
    resultat = []
    for (periode, spec), statuts in sorted(cumul.items()):
        honores, absents = statuts[StatutRdv.HONORE], statuts[StatutRdv.ABSENT]
        resultat.append(
            EvolutionMensuelleSpecialite(
                periode=periode,
                specialite=spec,
                total_rdv=sum(statuts.values()),
                honores=honores,
                absents=absents,
                taux_absence_pct=calculer_taux_absence(honores, absents),
            )
        )
    return resultat
