"""Indicateurs (KPI) et taux d'absence (règle RG-09)."""
import logging
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import extract, func, select
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.exceptions import AccesRefuseError, DonneesInvalidesError, RessourceIntrouvableError
from backend.models import Medecin, RendezVous, RoleUtilisateur, StatutRdv, Utilisateur, aujourdhui
from backend.schemas import (
    AbsenceMensuelle,
    AbsenceSpecialite,
    ErreurReponse,
    KpiGlobaux,
    PeriodeFiltre,
    ReponseOccupation,
)
from backend.routes.planning import construire_occupation
from backend.securite import exiger_roles

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stats", tags=["Statistiques"])

# Responsable des opérations : tableau de bord complet. Direction : vue synthétique uniquement (pas de
# détail par médecin). Administrateur : tous les droits.
exiger_tableau_de_bord = exiger_roles(
    RoleUtilisateur.RESPONSABLE, RoleUtilisateur.DIRECTION, RoleUtilisateur.ADMINISTRATEUR
)

REPONSES_ERREURS = {
    401: {"model": ErreurReponse, "description": "Non authentifié ou session expirée"},
    403: {"model": ErreurReponse, "description": "Rôle non autorisé"},
    404: {"model": ErreurReponse, "description": "Médecin inexistant"},
    422: {"model": ErreurReponse, "description": "Paramètres invalides"},
    500: {"model": ErreurReponse, "description": "Erreur interne"},
}


def calculer_taux_absence(honores: int, absents: int) -> float | None:
    """RG-09 : taux d'absence = absents / (honorés + absents), en pourcentage.

    Les rendez-vous « Planifié », « Confirmé » et « Annulé » sont exclus du dénominateur.
    Retourne None quand le dénominateur est nul (taux non calculable, à ne pas confondre avec 0 %).
    """
    denominateur = honores + absents
    if denominateur == 0:
        return None
    return round(100.0 * absents / denominateur, 1)


def _verifier_vue_autorisee(utilisateur: Utilisateur, medecin_id: str | None) -> None:
    """La Direction ne consulte que des chiffres agrégés : aucun détail par médecin."""
    if utilisateur.role == RoleUtilisateur.DIRECTION and medecin_id:
        raise AccesRefuseError(
            "La Direction consulte la vue synthétique : le détail par médecin n'est pas accessible.",
            "VUE_SYNTHETIQUE_UNIQUEMENT",
        )


def _construire_filtres(
    db: Session,
    date_debut: date | None,
    date_fin: date | None,
    specialite: str | None,
    medecin_id: str | None,
) -> list:
    """Construit les critères SQL communs aux indicateurs (période sur la date du rendez-vous)."""
    if date_debut and date_fin and date_debut > date_fin:
        raise DonneesInvalidesError(
            "La date de début doit être antérieure ou égale à la date de fin.", "PERIODE_INVALIDE"
        )
    filtres: list = []
    if date_debut:
        filtres.append(RendezVous.debut >= datetime.combine(date_debut, time.min))
    if date_fin:
        filtres.append(RendezVous.debut < datetime.combine(date_fin + timedelta(days=1), time.min))
    if specialite:
        filtres.append(RendezVous.medecin_id.in_(select(Medecin.id).where(Medecin.specialite == specialite)))
    if medecin_id:
        if db.get(Medecin, medecin_id) is None:
            raise RessourceIntrouvableError(f"Médecin {medecin_id} introuvable.", "MEDECIN_INTROUVABLE")
        filtres.append(RendezVous.medecin_id == medecin_id)
    return filtres


@router.get(
    "/kpi",
    response_model=KpiGlobaux,
    summary="Indicateurs clés sur une période",
    responses=REPONSES_ERREURS,
)
def indicateurs_cles(
    date_debut: date | None = None,
    date_fin: date | None = None,
    specialite: str | None = None,
    medecin_id: str | None = None,
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(exiger_tableau_de_bord),
) -> KpiGlobaux:
    """Volume de rendez-vous par statut et taux d'absence (RG-09)."""
    _verifier_vue_autorisee(utilisateur, medecin_id)
    filtres = _construire_filtres(db, date_debut, date_fin, specialite, medecin_id)
    lignes = db.execute(
        select(RendezVous.statut, func.count()).where(*filtres).group_by(RendezVous.statut)
    ).all()
    repartition = {statut.value: 0 for statut in StatutRdv}
    for statut, nombre in lignes:
        repartition[statut.value] = nombre
    honores = repartition[StatutRdv.HONORE.value]
    absents = repartition[StatutRdv.ABSENT.value]
    return KpiGlobaux(
        filtres=PeriodeFiltre(
            date_debut=date_debut, date_fin=date_fin, specialite=specialite, medecin_id=medecin_id
        ),
        total_rdv=sum(repartition.values()),
        repartition_statuts=repartition,
        honores=honores,
        absents=absents,
        base_calcul_absence=honores + absents,
        taux_absence_pct=calculer_taux_absence(honores, absents),
    )


@router.get(
    "/absence/par-specialite",
    response_model=list[AbsenceSpecialite],
    summary="Taux d'absence par spécialité",
    responses=REPONSES_ERREURS,
)
def absence_par_specialite(
    date_debut: date | None = None,
    date_fin: date | None = None,
    medecin_id: str | None = None,
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(exiger_tableau_de_bord),
) -> list[AbsenceSpecialite]:
    """Taux d'absence (RG-09) de chaque spécialité, trié par taux décroissant."""
    _verifier_vue_autorisee(utilisateur, medecin_id)
    filtres = _construire_filtres(db, date_debut, date_fin, None, medecin_id)
    lignes = db.execute(
        select(Medecin.specialite, RendezVous.statut, func.count())
        .select_from(RendezVous)
        .join(Medecin, RendezVous.medecin_id == Medecin.id)
        .where(*filtres)
        .group_by(Medecin.specialite, RendezVous.statut)
    ).all()
    cumul: dict[str, dict[StatutRdv, int]] = {}
    for specialite, statut, nombre in lignes:
        cumul.setdefault(specialite, {})[statut] = nombre
    resultat = []
    for specialite, par_statut in cumul.items():
        honores = par_statut.get(StatutRdv.HONORE, 0)
        absents = par_statut.get(StatutRdv.ABSENT, 0)
        resultat.append(
            AbsenceSpecialite(
                specialite=specialite,
                total_rdv=sum(par_statut.values()),
                honores=honores,
                absents=absents,
                taux_absence_pct=calculer_taux_absence(honores, absents),
            )
        )
    # Les spécialités sans taux calculable sont placées en dernier.
    resultat.sort(key=lambda r: (r.taux_absence_pct is None, -(r.taux_absence_pct or 0.0), r.specialite))
    return resultat


@router.get(
    "/absence/mensuelle",
    response_model=list[AbsenceMensuelle],
    summary="Évolution mensuelle du volume et du taux d'absence",
    responses=REPONSES_ERREURS,
)
def absence_mensuelle(
    date_debut: date | None = None,
    date_fin: date | None = None,
    specialite: str | None = None,
    medecin_id: str | None = None,
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(exiger_tableau_de_bord),
) -> list[AbsenceMensuelle]:
    """Volume de rendez-vous et taux d'absence (RG-09) pour chaque mois, du plus ancien au plus récent."""
    _verifier_vue_autorisee(utilisateur, medecin_id)
    filtres = _construire_filtres(db, date_debut, date_fin, specialite, medecin_id)
    annee = extract("year", RendezVous.debut)
    mois = extract("month", RendezVous.debut)
    lignes = db.execute(
        select(annee, mois, RendezVous.statut, func.count())
        .where(*filtres)
        .group_by(annee, mois, RendezVous.statut)
    ).all()
    cumul: dict[tuple[int, int], dict[StatutRdv, int]] = {}
    for a, m, statut, nombre in lignes:
        cumul.setdefault((int(a), int(m)), {})[statut] = nombre
    resultat = []
    for (a, m), par_statut in sorted(cumul.items()):
        honores = par_statut.get(StatutRdv.HONORE, 0)
        absents = par_statut.get(StatutRdv.ABSENT, 0)
        resultat.append(
            AbsenceMensuelle(
                periode=f"{a:04d}-{m:02d}",
                total_rdv=sum(par_statut.values()),
                honores=honores,
                absents=absents,
                taux_absence_pct=calculer_taux_absence(honores, absents),
            )
        )
    return resultat


@router.get(
    "/occupation",
    response_model=ReponseOccupation,
    summary="Taux d'occupation des créneaux (RG-10)",
    responses=REPONSES_ERREURS,
)
def taux_d_occupation(
    date_debut: date | None = None,
    date_fin: date | None = None,
    specialite: str | None = None,
    medecin_id: str | None = None,
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(exiger_tableau_de_bord),
) -> ReponseOccupation:
    """Créneaux portant un rendez-vous honoré ou absent ÷ créneaux ouverts. Par défaut : le mois en cours jusqu'à aujourd'hui."""
    _verifier_vue_autorisee(utilisateur, medecin_id)
    fin = date_fin or aujourdhui()
    debut = date_debut or fin.replace(day=1)
    reponse = construire_occupation(db, debut, fin, specialite, medecin_id)
    if utilisateur.role == RoleUtilisateur.DIRECTION:
        reponse.par_medecin = []  # la Direction ne voit que le chiffre agrégé
    return reponse
