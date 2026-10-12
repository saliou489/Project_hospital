"""Page administrateur : gestion des comptes, journal des actions et correction des pointages.

Toutes les routes sont réservées au rôle Administrateur. « Retirer » un compte signifie le
désactiver : il ne peut plus se connecter, mais l'historique (rendez-vous, journal, pointages)
est conservé, car aucune suppression physique n'est autorisée (RG-11).
"""
import logging
from datetime import date, datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.database import ecrire_avec_reessai, get_db
from backend.exceptions import DonneesInvalidesError, RegleMetierError, RessourceIntrouvableError
from backend.journal import journaliser
from backend.planning import PLAGES_PAR_DEFAUT
from backend.models import (
    PREFIXE_ROLE,
    Agent,
    JournalAction,
    Medecin,
    Pointage,
    PlageConsultation,
    Specialite,
    RendezVous,
    RoleUtilisateur,
    StatutRdv,
    Utilisateur,
    maintenant,
    prochain_identifiant,
)
from backend.schemas import (
    CleGeneree,
    DesactivationDemande,
    DesactivationReponse,
    ErreurReponse,
    JournalLecture,
    ListeJournal,
    ListePointages,
    ListeUtilisateurs,
    PointageCorrection,
    PointageLecture,
    UtilisateurCreation,
    UtilisateurLecture,
)
from backend.securite import exiger_roles, generer_cle, hacher_cle

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Administration"])

exiger_administrateur = exiger_roles(RoleUtilisateur.ADMINISTRATEUR)

REPONSES_ERREURS = {
    400: {"model": ErreurReponse, "description": "Règle métier violée (ex. dernier administrateur)"},
    401: {"model": ErreurReponse, "description": "Non authentifié ou session expirée"},
    403: {"model": ErreurReponse, "description": "Réservé aux administrateurs"},
    404: {"model": ErreurReponse, "description": "Compte ou pointage inexistant"},
    422: {"model": ErreurReponse, "description": "Données invalides"},
    500: {"model": ErreurReponse, "description": "Erreur interne"},
}


def _obtenir_utilisateur(db: Session, identifiant: str) -> Utilisateur:
    utilisateur = db.get(Utilisateur, identifiant.strip().upper())
    if utilisateur is None:
        raise RessourceIntrouvableError(f"Compte {identifiant} introuvable.", "UTILISATEUR_INTROUVABLE")
    return utilisateur


# --------------------------------------------------------------------------------------
# Comptes
# --------------------------------------------------------------------------------------


@router.get(
    "/utilisateurs",
    response_model=ListeUtilisateurs,
    summary="Lister les comptes",
    responses=REPONSES_ERREURS,
)
def lister_utilisateurs(
    role: RoleUtilisateur | None = None,
    actif: bool | None = None,
    q: Annotated[str | None, Query(min_length=1, max_length=60)] = None,
    limite: Annotated[int, Query(ge=1, le=500)] = 200,
    decalage: Annotated[int, Query(ge=0)] = 0,
    db: Session = Depends(get_db),
    _administrateur: Utilisateur = Depends(exiger_administrateur),
) -> ListeUtilisateurs:
    filtres = []
    if role is not None:
        filtres.append(Utilisateur.role == role)
    if actif is not None:
        filtres.append(Utilisateur.actif.is_(actif))
    if q:
        filtres.append(
            or_(
                Utilisateur.id.icontains(q, autoescape=True),
                Utilisateur.nom_affichage.icontains(q, autoescape=True),
            )
        )
    total = int(db.scalar(select(func.count()).select_from(Utilisateur).where(*filtres)) or 0)
    comptes = db.scalars(
        select(Utilisateur).where(*filtres).order_by(Utilisateur.role, Utilisateur.id).limit(limite).offset(decalage)
    ).all()
    return ListeUtilisateurs(
        total=total,
        limite=limite,
        decalage=decalage,
        elements=[UtilisateurLecture.model_validate(c) for c in comptes],
    )


@router.post(
    "/utilisateurs",
    response_model=CleGeneree,
    status_code=201,
    summary="Créer un compte (la clé n'est affichée qu'une fois)",
    responses=REPONSES_ERREURS,
)
def creer_utilisateur(
    donnees: UtilisateurCreation,
    db: Session = Depends(get_db),
    administrateur: Utilisateur = Depends(exiger_administrateur),
) -> CleGeneree:
    """Crée un compte. Pour un médecin ou un agent, la fiche correspondante est créée en même temps."""
    cle = generer_cle()
    empreinte = hacher_cle(cle)  # calcul coûteux : fait avant d'ouvrir la transaction

    def operation() -> Utilisateur:
        prefixe, largeur = PREFIXE_ROLE[donnees.role]
        medecin_id = agent_id = None
        if donnees.role == RoleUtilisateur.MEDECIN:
            nom_specialite = " ".join(donnees.specialite.split())
            specialite = db.get(Specialite, nom_specialite)
            if specialite is None or not specialite.actif:
                raise RegleMetierError(
                    f"La spécialité « {nom_specialite} » n'existe pas ou n'est pas active : créez-la d'abord (EF-01).",
                    "SPECIALITE_INCONNUE",
                )
            if donnees.duree_consultation_min not in (None, specialite.duree_standard_min):
                raise RegleMetierError(
                    f"La durée d'une consultation est celle de la spécialité ({specialite.duree_standard_min} minutes).",
                    "DUREE_INCOHERENTE",
                )
            medecin = Medecin(
                id=prochain_identifiant(db, Medecin, prefixe, largeur),
                nom=" ".join(donnees.nom_medecin.split()),
                specialite=nom_specialite,
                duree_consultation_min=specialite.duree_standard_min,
                actif=True,
            )
            db.add(medecin)
            db.flush()
            db.add_all(  # plages habituelles de la clinique ; le responsable ou l'administrateur les ajuste ensuite
                PlageConsultation(medecin_id=medecin.id, jour_semaine=jour, debut=debut, fin=fin)
                for jour, debut, fin in PLAGES_PAR_DEFAUT
            )
            identifiant, nom_affiche, medecin_id = medecin.id, medecin.nom, medecin.id
        elif donnees.role == RoleUtilisateur.AGENT:
            agent = Agent(id=prochain_identifiant(db, Agent, prefixe, largeur), actif=True)
            db.add(agent)
            db.flush()
            identifiant, agent_id = agent.id, agent.id
            nom_affiche = donnees.nom_affichage or f"Agent {agent.id}"
        else:
            identifiant = prochain_identifiant(db, Utilisateur, prefixe, largeur)
            nom_affiche = donnees.nom_affichage or f"{donnees.role.value} {identifiant}"
        utilisateur = Utilisateur(
            id=identifiant,
            role=donnees.role,
            nom_affichage=nom_affiche,
            cle_hash=empreinte,
            actif=True,
            medecin_id=medecin_id,
            agent_id=agent_id,
        )
        db.add(utilisateur)
        journaliser(db, administrateur, "COMPTE_CREE", cible=identifiant, detail=donnees.role.value)
        db.flush()
        return utilisateur

    utilisateur = ecrire_avec_reessai(db, operation)
    logger.info("Compte %s créé.", utilisateur.id)
    return CleGeneree(utilisateur=UtilisateurLecture.model_validate(utilisateur), cle=cle)


@router.post(
    "/utilisateurs/{identifiant}/regenerer-cle",
    response_model=CleGeneree,
    summary="Régénérer la clé d'un compte (l'ancienne cesse de fonctionner)",
    responses=REPONSES_ERREURS,
)
def regenerer_cle(
    identifiant: str,
    db: Session = Depends(get_db),
    administrateur: Utilisateur = Depends(exiger_administrateur),
) -> CleGeneree:
    cle = generer_cle()
    empreinte = hacher_cle(cle)

    def operation() -> Utilisateur:
        utilisateur = _obtenir_utilisateur(db, identifiant)
        utilisateur.cle_hash = empreinte
        utilisateur.essais_echoues = 0
        utilisateur.verrouille_jusqua = None
        journaliser(db, administrateur, "CLE_REGENEREE", cible=utilisateur.id)
        db.flush()
        return utilisateur

    utilisateur = ecrire_avec_reessai(db, operation)
    return CleGeneree(utilisateur=UtilisateurLecture.model_validate(utilisateur), cle=cle)


@router.post(
    "/utilisateurs/{identifiant}/desactiver",
    response_model=DesactivationReponse,
    summary="Désactiver (retirer) un compte : l'historique est conservé",
    responses=REPONSES_ERREURS,
)
def desactiver_utilisateur(
    identifiant: str,
    donnees: DesactivationDemande,
    db: Session = Depends(get_db),
    administrateur: Utilisateur = Depends(exiger_administrateur),
) -> DesactivationReponse:
    """Garde-fous : pas d'auto-désactivation, et jamais le dernier administrateur actif."""

    def operation() -> tuple[Utilisateur, int]:
        utilisateur = _obtenir_utilisateur(db, identifiant)
        if utilisateur.id == administrateur.id:
            raise RegleMetierError("Vous ne pouvez pas désactiver votre propre compte.", "AUTO_DESACTIVATION_INTERDITE")
        if not utilisateur.actif:
            raise RegleMetierError("Ce compte est déjà désactivé.", "COMPTE_DEJA_DESACTIVE")
        if donnees.retirer_du_planning and utilisateur.role != RoleUtilisateur.MEDECIN:
            raise DonneesInvalidesError("Le retrait du planning ne concerne que les médecins.", "DONNEES_INVALIDES")
        if utilisateur.role == RoleUtilisateur.ADMINISTRATEUR:
            autres_actifs = db.scalar(
                select(func.count())
                .select_from(Utilisateur)
                .where(
                    Utilisateur.role == RoleUtilisateur.ADMINISTRATEUR,
                    Utilisateur.actif.is_(True),
                    Utilisateur.id != utilisateur.id,
                )
            )
            if not autres_actifs:
                raise RegleMetierError(
                    "Impossible de désactiver le dernier administrateur actif.", "DERNIER_ADMINISTRATEUR"
                )

        utilisateur.actif = False
        rdv_a_venir = 0
        if utilisateur.role == RoleUtilisateur.MEDECIN and utilisateur.medecin_id:
            rdv_a_venir = int(
                db.scalar(
                    select(func.count())
                    .select_from(RendezVous)
                    .where(
                        RendezVous.medecin_id == utilisateur.medecin_id,
                        RendezVous.statut.in_([StatutRdv.PLANIFIE, StatutRdv.CONFIRME]),
                        RendezVous.debut >= maintenant(),
                    )
                )
                or 0
            )
            if donnees.retirer_du_planning:
                db.get(Medecin, utilisateur.medecin_id).actif = False
        detail = "retiré du planning" if donnees.retirer_du_planning else None
        journaliser(db, administrateur, "COMPTE_DESACTIVE", cible=utilisateur.id, detail=detail)
        db.flush()
        return utilisateur, rdv_a_venir

    utilisateur, rdv_a_venir = ecrire_avec_reessai(db, operation)
    return DesactivationReponse(utilisateur=UtilisateurLecture.model_validate(utilisateur), rdv_a_venir=rdv_a_venir)


@router.post(
    "/utilisateurs/{identifiant}/reactiver",
    response_model=UtilisateurLecture,
    summary="Réactiver un compte désactivé",
    responses=REPONSES_ERREURS,
)
def reactiver_utilisateur(
    identifiant: str,
    db: Session = Depends(get_db),
    administrateur: Utilisateur = Depends(exiger_administrateur),
) -> UtilisateurLecture:
    def operation() -> Utilisateur:
        utilisateur = _obtenir_utilisateur(db, identifiant)
        if utilisateur.actif:
            raise RegleMetierError("Ce compte est déjà actif.", "COMPTE_DEJA_ACTIF")
        utilisateur.actif = True
        utilisateur.essais_echoues = 0
        utilisateur.verrouille_jusqua = None
        if utilisateur.medecin_id:
            db.get(Medecin, utilisateur.medecin_id).actif = True  # le médecin redevient réservable
        journaliser(db, administrateur, "COMPTE_REACTIVE", cible=utilisateur.id)
        db.flush()
        return utilisateur

    return UtilisateurLecture.model_validate(ecrire_avec_reessai(db, operation))


# --------------------------------------------------------------------------------------
# Journal des actions
# --------------------------------------------------------------------------------------


@router.get(
    "/journal",
    response_model=ListeJournal,
    summary="Consulter le journal des actions (du plus récent au plus ancien)",
    responses=REPONSES_ERREURS,
)
def consulter_journal(
    utilisateur_id: str | None = None,
    action: str | None = None,
    date_debut: date | None = None,
    date_fin: date | None = None,
    limite: Annotated[int, Query(ge=1, le=500)] = 100,
    decalage: Annotated[int, Query(ge=0)] = 0,
    db: Session = Depends(get_db),
    _administrateur: Utilisateur = Depends(exiger_administrateur),
) -> ListeJournal:
    if date_debut and date_fin and date_debut > date_fin:
        raise DonneesInvalidesError(
            "La date de début doit être antérieure ou égale à la date de fin.", "PERIODE_INVALIDE"
        )
    filtres = []
    if utilisateur_id:
        filtres.append(JournalAction.utilisateur_id == utilisateur_id.strip().upper())
    if action:
        filtres.append(JournalAction.action == action)
    if date_debut:
        filtres.append(JournalAction.horodatage >= datetime.combine(date_debut, time.min))
    if date_fin:
        filtres.append(JournalAction.horodatage < datetime.combine(date_fin + timedelta(days=1), time.min))
    total = int(db.scalar(select(func.count()).select_from(JournalAction).where(*filtres)) or 0)
    entrees = db.scalars(
        select(JournalAction)
        .where(*filtres)
        .order_by(JournalAction.horodatage.desc(), JournalAction.id.desc())
        .limit(limite)
        .offset(decalage)
    ).all()
    return ListeJournal(
        total=total, limite=limite, decalage=decalage, elements=[JournalLecture.model_validate(e) for e in entrees]
    )


# --------------------------------------------------------------------------------------
# Pointages
# --------------------------------------------------------------------------------------


@router.get(
    "/pointages",
    response_model=ListePointages,
    summary="Consulter les pointages de tous les employés",
    responses=REPONSES_ERREURS,
)
def lister_pointages(
    date_debut: date | None = None,
    date_fin: date | None = None,
    utilisateur_id: str | None = None,
    limite: Annotated[int, Query(ge=1, le=500)] = 200,
    decalage: Annotated[int, Query(ge=0)] = 0,
    db: Session = Depends(get_db),
    _administrateur: Utilisateur = Depends(exiger_administrateur),
) -> ListePointages:
    if date_debut and date_fin and date_debut > date_fin:
        raise DonneesInvalidesError(
            "La date de début doit être antérieure ou égale à la date de fin.", "PERIODE_INVALIDE"
        )
    filtres = []
    if date_debut:
        filtres.append(Pointage.jour >= date_debut)
    if date_fin:
        filtres.append(Pointage.jour <= date_fin)
    if utilisateur_id:
        filtres.append(Pointage.utilisateur_id == utilisateur_id.strip().upper())
    total = int(db.scalar(select(func.count()).select_from(Pointage).where(*filtres)) or 0)
    pointages = db.scalars(
        select(Pointage)
        .where(*filtres)
        .order_by(Pointage.jour.desc(), Pointage.utilisateur_id)
        .limit(limite)
        .offset(decalage)
    ).all()
    return ListePointages(
        total=total, limite=limite, decalage=decalage, elements=[PointageLecture.model_validate(p) for p in pointages]
    )


@router.patch(
    "/pointages/{pointage_id}",
    response_model=PointageLecture,
    summary="Corriger un pointage (la correction est tracée dans le journal)",
    responses=REPONSES_ERREURS,
)
def corriger_pointage(
    pointage_id: int,
    donnees: PointageCorrection,
    db: Session = Depends(get_db),
    administrateur: Utilisateur = Depends(exiger_administrateur),
) -> PointageLecture:
    def operation() -> Pointage:
        pointage = db.get(Pointage, pointage_id)
        if pointage is None:
            raise RessourceIntrouvableError(f"Pointage {pointage_id} introuvable.", "POINTAGE_INTROUVABLE")
        avant = f"{pointage.arrivee:%H:%M}-{pointage.depart:%H:%M}" if pointage.depart else f"{pointage.arrivee:%H:%M}-"
        if donnees.arrivee is not None:
            pointage.arrivee = datetime.combine(pointage.jour, donnees.arrivee)
        if donnees.depart is not None:
            pointage.depart = datetime.combine(pointage.jour, donnees.depart)
        if pointage.depart is not None and pointage.depart < pointage.arrivee:
            raise DonneesInvalidesError(
                "L'heure de départ ne peut pas précéder l'heure d'arrivée.", "POINTAGE_INCOHERENT"
            )
        instant = maintenant()
        if pointage.arrivee > instant or (pointage.depart is not None and pointage.depart > instant):
            raise DonneesInvalidesError("Une heure de pointage ne peut pas être dans le futur.", "POINTAGE_INCOHERENT")
        pointage.corrige_par = administrateur.id
        pointage.corrige_le = instant
        apres = f"{pointage.arrivee:%H:%M}-{pointage.depart:%H:%M}" if pointage.depart else f"{pointage.arrivee:%H:%M}-"
        journaliser(
            db,
            administrateur,
            "POINTAGE_CORRIGE",
            cible=str(pointage.id),
            detail=f"{pointage.utilisateur_id} le {pointage.jour} : {avant} -> {apres}",
        )
        db.flush()
        return pointage

    return PointageLecture.model_validate(ecrire_avec_reessai(db, operation))
