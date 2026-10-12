"""Endpoints des rendez-vous (CRUD) et règles RG-01, RG-02 et RG-05.

Il n'y a pas de suppression physique : annuler un rendez-vous consiste à passer son statut à
« Annulé » avec un motif (RG-05), ce qui libère immédiatement le créneau.
"""
import logging
from datetime import date, datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.database import ecrire_avec_reessai, get_db
from backend.exceptions import (
    AccesRefuseError,
    ConflitError,
    DonneesInvalidesError,
    RegleMetierError,
    RessourceIntrouvableError,
)
from backend.journal import journaliser
from backend.planning import duree_specialite, historiser, verifier_ouverture
from backend.models import (
    PREFIXE_RDV,
    STATUTS_ACTIFS,
    STATUTS_CLOS,
    TRANSITIONS_STATUT,
    Agent,
    HistoriqueRdv,
    Medecin,
    Patient,
    RendezVous,
    RoleUtilisateur,
    StatutRdv,
    Utilisateur,
    aujourdhui,
    maintenant,
    prochain_identifiant,
)
from backend.routes.patients import evaluer_alerte_recidiviste, obtenir_patient_ou_404
from backend.schemas import (
    AgentLecture,
    Avertissement,
    ErreurReponse,
    HistoriqueLecture,
    ListeRdv,
    MedecinLecture,
    RdvCreation,
    RdvLecture,
    RdvMiseAJour,
    ReponseCreationRdv,
)
from backend.securite import exiger_roles, utilisateur_courant

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rdv", tags=["Rendez-vous"])
router_referentiels = APIRouter(prefix="/referentiels", tags=["Référentiels"])

# Droits (cahier des charges, section 4) : seuls les agents créent des rendez-vous (l'agent saisissant est
# celui du compte) ; la consultation des plannings est ouverte aux agents, aux médecins (leur planning
# uniquement), au responsable des opérations et à l'administrateur.
exiger_creation = exiger_roles(RoleUtilisateur.AGENT)
exiger_planning = exiger_roles(
    RoleUtilisateur.AGENT, RoleUtilisateur.MEDECIN, RoleUtilisateur.RESPONSABLE, RoleUtilisateur.ADMINISTRATEUR
)

REPONSES_ERREURS = {
    400: {"model": ErreurReponse, "description": "Règle métier violée (ex. motif d'annulation manquant)"},
    401: {"model": ErreurReponse, "description": "Non authentifié ou session expirée"},
    403: {"model": ErreurReponse, "description": "Rôle non autorisé pour cette action"},
    404: {"model": ErreurReponse, "description": "Patient, médecin, agent ou rendez-vous inexistant"},
    409: {"model": ErreurReponse, "description": "Conflit d'agenda (médecin ou patient déjà pris)"},
    422: {"model": ErreurReponse, "description": "Données invalides (date, heure, identifiant)"},
    500: {"model": ErreurReponse, "description": "Erreur interne"},
}


# --------------------------------------------------------------------------------------
# Accès aux ressources et règles RG-01 / RG-02
# --------------------------------------------------------------------------------------


def _obtenir_rdv(db: Session, rdv_id: str) -> RendezVous:
    rdv = db.get(RendezVous, rdv_id)
    if rdv is None:
        raise RessourceIntrouvableError(f"Rendez-vous {rdv_id} introuvable.", "RDV_INTROUVABLE")
    return rdv


def _obtenir_medecin(db: Session, medecin_id: str) -> Medecin:
    medecin = db.get(Medecin, medecin_id)
    if medecin is None:
        raise RessourceIntrouvableError(f"Médecin {medecin_id} introuvable.", "MEDECIN_INTROUVABLE")
    return medecin


def _libelle_creneau(rdv: RendezVous) -> str:
    return f"le {rdv.debut:%d/%m/%Y} de {rdv.debut:%H:%M} à {rdv.fin:%H:%M}"


def verifier_conflits(
    db: Session,
    *,
    medecin: Medecin,
    patient: Patient,
    debut: datetime,
    fin: datetime,
    exclure_rdv_id: str | None = None,
) -> None:
    """RG-01 puis RG-02 : lève ConflitError (HTTP 409) si l'intervalle chevauche un rendez-vous actif.

    - RG-01 : un médecin ne peut pas avoir deux rendez-vous actifs au même moment ;
    - RG-02 : un patient ne peut pas avoir deux rendez-vous actifs simultanés, tous médecins confondus.

    Sont actifs les rendez-vous « Planifié », « Confirmé » ou « Honoré » : un rendez-vous annulé
    libère son créneau. ``exclure_rdv_id`` permet d'ignorer le rendez-vous en cours de déplacement.
    """
    criteres = [
        RendezVous.statut.in_(STATUTS_ACTIFS),
        RendezVous.debut < fin,
        RendezVous.fin > debut,
    ]
    if exclure_rdv_id is not None:
        criteres.append(RendezVous.id != exclure_rdv_id)

    conflit_medecin = db.scalars(
        select(RendezVous)
        .where(RendezVous.medecin_id == medecin.id, *criteres)
        .order_by(RendezVous.debut)
        .limit(1)
    ).first()
    if conflit_medecin is not None:
        raise ConflitError(
            f"Conflit d'agenda : {medecin.nom} a déjà un rendez-vous {_libelle_creneau(conflit_medecin)}.",
            "CONFLIT_MEDECIN",
        )

    conflit_patient = db.scalars(
        select(RendezVous)
        .where(RendezVous.patient_id == patient.id, *criteres)
        .order_by(RendezVous.debut)
        .limit(1)
    ).first()
    if conflit_patient is not None:
        raise ConflitError(
            f"Conflit patient : {patient.nom} {patient.prenom} a déjà un rendez-vous "
            f"{_libelle_creneau(conflit_patient)} avec {conflit_patient.medecin.nom}.",
            "CONFLIT_PATIENT",
        )


# --------------------------------------------------------------------------------------
# Droits de modification (RG-07)
# --------------------------------------------------------------------------------------


def _verifier_droits_de_modification(utilisateur: Utilisateur, rdv: RendezVous, donnees: RdvMiseAJour) -> None:
    """RG-07 : seul le médecin concerné, un responsable des opérations ou un administrateur marque un
    rendez-vous « Honoré » ou « Absent ». La confirmation, l'annulation et la reprogrammation relèvent
    des agents et de l'administrateur. Lève AccesRefuseError (HTTP 403) sinon."""
    statut_final = donnees.statut in (StatutRdv.HONORE, StatutRdv.ABSENT)
    reprogrammation = donnees.date_rdv is not None or donnees.heure_rdv is not None
    if utilisateur.role == RoleUtilisateur.MEDECIN:
        if rdv.medecin_id != utilisateur.medecin_id:
            raise AccesRefuseError(
                "Un médecin ne peut modifier que ses propres rendez-vous.", "RDV_D_UN_AUTRE_MEDECIN"
            )
        if not statut_final or reprogrammation:
            raise AccesRefuseError(
                "Un médecin peut seulement marquer ses rendez-vous « Honoré » ou « Absent ».", "ACTION_NON_AUTORISEE"
            )
    elif utilisateur.role == RoleUtilisateur.RESPONSABLE:
        if not statut_final or reprogrammation:
            raise AccesRefuseError(
                "Le responsable des opérations peut seulement marquer un rendez-vous « Honoré » ou « Absent ».",
                "ACTION_NON_AUTORISEE",
            )
    elif utilisateur.role == RoleUtilisateur.AGENT and statut_final:
        raise AccesRefuseError(
            "Seuls le médecin concerné, un responsable des opérations ou un administrateur peuvent marquer "
            "un rendez-vous « Honoré » ou « Absent ».",
            "RG07_STATUT_RESERVE",
        )


# --------------------------------------------------------------------------------------
# Rendez-vous
# --------------------------------------------------------------------------------------


@router.post(
    "",
    response_model=ReponseCreationRdv,
    status_code=201,
    summary="Créer un rendez-vous",
    responses=REPONSES_ERREURS,
)
def creer_rdv(
    donnees: RdvCreation,
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(exiger_creation),
) -> ReponseCreationRdv:
    """Crée un rendez-vous au statut « Planifié ».

    Les vérifications RG-01 et RG-02 sont faites en base, dans la transaction, avant l'insertion.
    La réponse peut contenir un avertissement RG-08 (patient récidiviste) qui ne bloque pas la création.
    """

    def operation() -> RendezVous:
        patient = obtenir_patient_ou_404(db, donnees.patient_id)
        medecin = _obtenir_medecin(db, donnees.medecin_id)
        agent = db.get(Agent, utilisateur.agent_id)  # l'agent saisissant est celui du compte connecté
        if not medecin.actif:
            raise RegleMetierError(f"{medecin.nom} n'est plus actif : aucun rendez-vous possible.", "MEDECIN_INACTIF")
        if not agent.actif:
            raise RegleMetierError(f"L'agent {agent.id} n'est plus actif.", "AGENT_INACTIF")

        debut = datetime.combine(donnees.date_rdv, donnees.heure_rdv)
        fin = debut + timedelta(minutes=duree_specialite(db, medecin))
        verifier_ouverture(db, medecin, debut, fin)  # RG-03 : plages, jours fermés, indisponibilités
        verifier_conflits(db, medecin=medecin, patient=patient, debut=debut, fin=fin)

        rdv = RendezVous(
            id=prochain_identifiant(db, RendezVous, PREFIXE_RDV, 5),
            patient=patient,
            medecin=medecin,
            agent=agent,
            date_prise=donnees.date_prise,
            debut=debut,
            fin=fin,
            statut=StatutRdv.PLANIFIE,
            confirme=False,
            canal=donnees.canal,
        )
        db.add(rdv)
        journaliser(db, utilisateur, "RDV_CREE", cible=rdv.id, detail=f"{medecin.id} le {debut:%Y-%m-%d %H:%M}")
        db.flush()
        historiser(db, rdv.id, utilisateur, "CREATION", "debut", None, f"{debut:%Y-%m-%d %H:%M}")
        return rdv

    rdv = ecrire_avec_reessai(db, operation)
    logger.info("Rendez-vous %s créé.", rdv.id)

    avertissements: list[Avertissement] = []
    alerte = evaluer_alerte_recidiviste(db, rdv.patient_id)
    if alerte.actif and alerte.message:
        avertissements.append(Avertissement(code="PATIENT_RECIDIVISTE", message=alerte.message))
    return ReponseCreationRdv(rdv=RdvLecture.model_validate(rdv), avertissements=avertissements)


@router.get(
    "",
    response_model=ListeRdv,
    summary="Lister les rendez-vous",
    responses=REPONSES_ERREURS,
)
def lister_rdv(
    medecin_id: str | None = None,
    patient_id: str | None = None,
    specialite: str | None = None,
    statuts: Annotated[list[StatutRdv] | None, Query()] = None,
    date_debut: date | None = None,
    date_fin: date | None = None,
    limite: Annotated[int, Query(ge=1, le=1000)] = 100,
    decalage: Annotated[int, Query(ge=0)] = 0,
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(exiger_planning),
) -> ListeRdv:
    """Liste filtrable (médecin, patient, spécialité, statuts, période), triée par date croissante.

    Un médecin ne voit jamais que son propre planning (RG-07) : son identifiant est imposé par le compte.
    """
    if utilisateur.role == RoleUtilisateur.MEDECIN:
        if medecin_id and medecin_id != utilisateur.medecin_id:
            raise AccesRefuseError("Un médecin ne consulte que son propre planning.", "PLANNING_D_UN_AUTRE_MEDECIN")
        medecin_id = utilisateur.medecin_id
    if date_debut and date_fin and date_debut > date_fin:
        raise DonneesInvalidesError(
            "La date de début doit être antérieure ou égale à la date de fin.", "PERIODE_INVALIDE"
        )
    filtres: list = []
    if medecin_id:
        filtres.append(RendezVous.medecin_id == medecin_id)
    if patient_id:
        filtres.append(RendezVous.patient_id == patient_id)
    if specialite:
        filtres.append(RendezVous.medecin_id.in_(select(Medecin.id).where(Medecin.specialite == specialite)))
    if statuts:
        filtres.append(RendezVous.statut.in_(statuts))
    if date_debut:
        filtres.append(RendezVous.debut >= datetime.combine(date_debut, time.min))
    if date_fin:
        filtres.append(RendezVous.debut < datetime.combine(date_fin + timedelta(days=1), time.min))

    total = int(db.scalar(select(func.count()).select_from(RendezVous).where(*filtres)) or 0)
    rendez_vous = db.scalars(
        select(RendezVous).where(*filtres).order_by(RendezVous.debut, RendezVous.id).limit(limite).offset(decalage)
    ).all()
    return ListeRdv(
        total=total,
        limite=limite,
        decalage=decalage,
        elements=[RdvLecture.model_validate(r) for r in rendez_vous],
    )


@router.get(
    "/{rdv_id}",
    response_model=RdvLecture,
    summary="Consulter un rendez-vous",
    responses=REPONSES_ERREURS,
)
def lire_rdv(
    rdv_id: str, db: Session = Depends(get_db), utilisateur: Utilisateur = Depends(exiger_planning)
) -> RdvLecture:
    rdv = _obtenir_rdv(db, rdv_id)
    if utilisateur.role == RoleUtilisateur.MEDECIN and rdv.medecin_id != utilisateur.medecin_id:
        raise AccesRefuseError("Un médecin ne consulte que ses propres rendez-vous.", "RDV_D_UN_AUTRE_MEDECIN")
    return RdvLecture.model_validate(rdv)


@router.patch(
    "/{rdv_id}",
    response_model=RdvLecture,
    summary="Modifier, reprogrammer ou annuler un rendez-vous",
    responses=REPONSES_ERREURS,
)
def modifier_rdv(
    rdv_id: str,
    donnees: RdvMiseAJour,
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(exiger_planning),
) -> RdvLecture:
    """Change le statut (Confirmé, Honoré, Absent, Annulé) ou reprogramme un rendez-vous.

    - RG-05 : le statut « Annulé » exige un motif (vérifié par le schéma, puis ici par défense en profondeur) ;
    - cycle de vie : un rendez-vous clos (Honoré, Absent, Annulé) ne peut plus être modifié, et
      « Honoré » / « Absent » ne se fixent qu'à partir du jour du rendez-vous ;
    - reprogrammation : le nouveau créneau repasse par les contrôles RG-01 et RG-02 ;
    - droits (RG-07) : « Honoré » / « Absent » par le médecin concerné, le responsable des opérations ou un
      administrateur ; confirmation, annulation et reprogrammation par un agent ou un administrateur.
    """

    def operation() -> RendezVous:
        rdv = _obtenir_rdv(db, rdv_id)
        _verifier_droits_de_modification(utilisateur, rdv, donnees)
        if rdv.statut in STATUTS_CLOS:
            raise RegleMetierError(
                f"Le rendez-vous {rdv.id} est clos (statut « {rdv.statut.value} ») et ne peut plus être modifié.",
                "RDV_CLOS",
            )

        nouveau_statut = donnees.statut if donnees.statut is not None else rdv.statut
        changement_statut = nouveau_statut != rdv.statut
        reprogrammation = donnees.date_rdv is not None or donnees.heure_rdv is not None
        if not changement_statut and not reprogrammation:
            raise DonneesInvalidesError(
                "Aucune modification effective : les valeurs fournies sont identiques à l'existant.",
                "AUCUNE_MODIFICATION",
            )

        # --- Contrôles de statut (aucune écriture tant que tout n'est pas validé) ----------
        if changement_statut:
            if nouveau_statut not in TRANSITIONS_STATUT[rdv.statut]:
                raise RegleMetierError(
                    f"Transition de statut non autorisée : « {rdv.statut.value} » vers « {nouveau_statut.value} ».",
                    "TRANSITION_STATUT_INVALIDE",
                )
            if nouveau_statut in (StatutRdv.HONORE, StatutRdv.ABSENT) and rdv.debut.date() > aujourdhui():
                raise RegleMetierError(
                    "Le statut « Honoré » ou « Absent » ne peut être fixé qu'à partir du jour du rendez-vous.",
                    "STATUT_FINAL_PREMATURE",
                )
            if nouveau_statut == StatutRdv.ANNULE and donnees.motif_annulation is None:
                raise RegleMetierError(
                    "Un motif d'annulation est obligatoire pour annuler un rendez-vous.",
                    "MOTIF_ANNULATION_MANQUANT",
                )
        if reprogrammation and nouveau_statut not in (StatutRdv.PLANIFIE, StatutRdv.CONFIRME):
            raise RegleMetierError(
                "Un rendez-vous ne peut être reprogrammé que s'il reste « Planifié » ou « Confirmé ».",
                "MODIFICATION_INCOHERENTE",
            )

        # --- Reprogrammation : nouveau créneau, contrôles puis RG-01 / RG-02 ----------------
        nouveau_debut = nouvelle_fin = None
        if reprogrammation:
            nouveau_debut = datetime.combine(
                donnees.date_rdv or rdv.debut.date(), donnees.heure_rdv or rdv.debut.time()
            )
            if nouveau_debut == rdv.debut and not changement_statut:
                raise DonneesInvalidesError(
                    "Aucune modification effective : le créneau demandé est celui déjà enregistré.",
                    "AUCUNE_MODIFICATION",
                )
            if nouveau_debut < maintenant():
                raise DonneesInvalidesError(
                    "Impossible de reprogrammer un rendez-vous à une date ou une heure passée.", "DATE_PASSEE"
                )
            if rdv.date_prise > nouveau_debut.date():
                raise DonneesInvalidesError(
                    "La nouvelle date ne peut pas précéder la date de prise du rendez-vous.", "DATE_INCOHERENTE"
                )
            nouvelle_fin = nouveau_debut + (rdv.fin - rdv.debut)
            verifier_ouverture(db, rdv.medecin, nouveau_debut, nouvelle_fin)  # RG-03
            verifier_conflits(
                db,
                medecin=rdv.medecin,
                patient=rdv.patient,
                debut=nouveau_debut,
                fin=nouvelle_fin,
                exclure_rdv_id=rdv.id,
            )

        # --- Application des changements, avec trace dans le journal --------------------------
        ancien_statut, ancien_debut = rdv.statut, rdv.debut
        if changement_statut:
            rdv.statut = nouveau_statut
            historiser(db, rdv.id, utilisateur, "STATUT", "statut", ancien_statut.value, nouveau_statut.value)
            if nouveau_statut == StatutRdv.ANNULE:
                rdv.motif_annulation = donnees.motif_annulation
                historiser(db, rdv.id, utilisateur, "ANNULATION", "motif_annulation", None, donnees.motif_annulation.value)
                journaliser(
                    db, utilisateur, "RDV_ANNULE", cible=rdv.id, detail=f"motif : {donnees.motif_annulation.value}"
                )
            else:
                if nouveau_statut == StatutRdv.CONFIRME:
                    rdv.confirme = True
                journaliser(
                    db, utilisateur, "RDV_STATUT", cible=rdv.id,
                    detail=f"{ancien_statut.value} -> {nouveau_statut.value}",
                )  # fmt: skip
        if nouveau_debut is not None:
            rdv.debut = nouveau_debut
            rdv.fin = nouvelle_fin
            historiser(db, rdv.id, utilisateur, "REPROGRAMMATION", "debut", f"{ancien_debut:%Y-%m-%d %H:%M}", f"{nouveau_debut:%Y-%m-%d %H:%M}")
            journaliser(
                db, utilisateur, "RDV_DEPLACE", cible=rdv.id,
                detail=f"{ancien_debut:%Y-%m-%d %H:%M} -> {nouveau_debut:%Y-%m-%d %H:%M}",
            )  # fmt: skip
        db.flush()
        return rdv

    rdv = ecrire_avec_reessai(db, operation)
    logger.info("Rendez-vous %s modifié (statut : %s).", rdv.id, rdv.statut.value)
    return RdvLecture.model_validate(rdv)


@router.get(
    "/{rdv_id}/historique",
    response_model=list[HistoriqueLecture],
    summary="Historique des modifications d'un rendez-vous (RG-11)",
    responses=REPONSES_ERREURS,
)
def historique_rdv(
    rdv_id: str, db: Session = Depends(get_db), utilisateur: Utilisateur = Depends(exiger_planning)
) -> list[HistoriqueRdv]:
    """Toutes les modifications, de la plus ancienne à la plus récente. Aucune ligne n'est jamais modifiée ni supprimée."""
    rdv = _obtenir_rdv(db, rdv_id)
    if utilisateur.role == RoleUtilisateur.MEDECIN and rdv.medecin_id != utilisateur.medecin_id:
        raise AccesRefuseError("Un médecin ne consulte que ses propres rendez-vous.", "RDV_D_UN_AUTRE_MEDECIN")
    return list(db.scalars(select(HistoriqueRdv).where(HistoriqueRdv.rdv_id == rdv_id).order_by(HistoriqueRdv.horodatage, HistoriqueRdv.id)).all())


# --------------------------------------------------------------------------------------
# Référentiels (listes nécessaires à la saisie)
# --------------------------------------------------------------------------------------


@router_referentiels.get(
    "/medecins",
    response_model=list[MedecinLecture],
    summary="Lister les médecins actifs",
    responses=REPONSES_ERREURS,
)
def lister_medecins(
    specialite: str | None = None,
    db: Session = Depends(get_db),
    _utilisateur: Utilisateur = Depends(utilisateur_courant),
) -> list[Medecin]:
    requete = select(Medecin).where(Medecin.actif.is_(True))
    if specialite:
        requete = requete.where(Medecin.specialite == specialite)
    return list(db.scalars(requete.order_by(Medecin.specialite, Medecin.nom)).all())


@router_referentiels.get(
    "/agents",
    response_model=list[AgentLecture],
    summary="Lister les agents d'accueil actifs",
    responses=REPONSES_ERREURS,
)
def lister_agents(
    db: Session = Depends(get_db),
    _utilisateur: Utilisateur = Depends(exiger_roles(RoleUtilisateur.ADMINISTRATEUR)),
) -> list[Agent]:
    return list(db.scalars(select(Agent).where(Agent.actif.is_(True)).order_by(Agent.id)).all())
