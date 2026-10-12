"""Endpoints des patients, historique des rendez-vous et règle RG-08 (alerte récidiviste)."""
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.database import ecrire_avec_reessai, get_db
from backend.exceptions import ConflitError, RessourceIntrouvableError
from backend.journal import journaliser
from backend.models import (
    PERIODE_RECIDIVISTE_MOIS,
    PREFIXE_PATIENT,
    SEUIL_ABSENCES_RECIDIVISTE,
    Patient,
    RendezVous,
    RoleUtilisateur,
    StatutRdv,
    Utilisateur,
    maintenant,
    prochain_identifiant,
    reculer_de_mois,
)
from backend.qualite import chercher_doublons, decrire
from backend.routes.stats import calculer_taux_absence
from backend.schemas import (
    AlerteRecidiviste,
    ErreurReponse,
    HistoriquePatient,
    ListePatients,
    PatientCreation,
    PatientDetail,
    PatientLecture,
    PatientMiseAJour,
    RdvLecture,
    VerificationDoublon,
)
from backend.securite import exiger_roles

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/patients", tags=["Patients"])

# Droits (cahier des charges, section 4) : les agents gèrent les fiches ; le responsable des opérations
# les consulte ; l'administrateur a tous les droits. Médecins et Direction n'ont pas accès aux fiches.
exiger_ecriture = exiger_roles(RoleUtilisateur.AGENT, RoleUtilisateur.ADMINISTRATEUR)
exiger_lecture = exiger_roles(RoleUtilisateur.AGENT, RoleUtilisateur.RESPONSABLE, RoleUtilisateur.ADMINISTRATEUR)

REPONSES_ERREURS = {
    401: {"model": ErreurReponse, "description": "Non authentifié ou session expirée"},
    403: {"model": ErreurReponse, "description": "Rôle non autorisé"},
    404: {"model": ErreurReponse, "description": "Patient inexistant"},
    409: {"model": ErreurReponse, "description": "Doublon : un patient de même identité existe déjà"},
    422: {"model": ErreurReponse, "description": "Données invalides (téléphone, date, etc.)"},
    500: {"model": ErreurReponse, "description": "Erreur interne"},
}


def evaluer_alerte_recidiviste(db: Session, patient_id: str) -> AlerteRecidiviste:
    """RG-08 : alerte si le patient totalise au moins 3 absences (statut « Absent ») sur les 6 derniers mois.

    L'alerte est informative : elle est affichée à l'agent mais ne bloque jamais une prise de rendez-vous.
    """
    reference = maintenant()
    debut_fenetre = reculer_de_mois(reference, PERIODE_RECIDIVISTE_MOIS)
    nombre = db.scalar(
        select(func.count())
        .select_from(RendezVous)
        .where(
            RendezVous.patient_id == patient_id,
            RendezVous.statut == StatutRdv.ABSENT,
            RendezVous.debut >= debut_fenetre,
            RendezVous.debut <= reference,
        )
    )
    nombre = int(nombre or 0)
    actif = nombre >= SEUIL_ABSENCES_RECIDIVISTE
    message = (
        f"Attention : ce patient totalise {nombre} absences sur les {PERIODE_RECIDIVISTE_MOIS} derniers mois."
        if actif
        else None
    )
    return AlerteRecidiviste(
        actif=actif,
        nb_absences=nombre,
        seuil=SEUIL_ABSENCES_RECIDIVISTE,
        periode_mois=PERIODE_RECIDIVISTE_MOIS,
        message=message,
    )


def obtenir_patient_ou_404(db: Session, patient_id: str) -> Patient:
    """Retourne le patient ou lève une erreur 404."""
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise RessourceIntrouvableError(f"Patient {patient_id} introuvable.", "PATIENT_INTROUVABLE")
    return patient


def _detail(db: Session, patient: Patient) -> PatientDetail:
    """Construit la fiche patient avec son alerte RG-08."""
    base = PatientLecture.model_validate(patient).model_dump()
    return PatientDetail(**base, alerte_recidiviste=evaluer_alerte_recidiviste(db, patient.id))


@router.post(
    "",
    response_model=PatientDetail,
    status_code=201,
    summary="Créer une fiche patient",
    responses=REPONSES_ERREURS,
)
def creer_patient(
    donnees: PatientCreation,
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(exiger_ecriture),
) -> PatientDetail:
    """Crée une fiche patient après validation stricte (téléphone sénégalais, date de naissance)."""

    def operation() -> Patient:
        # EF-04 : détection des doublons AVANT la création.
        doublons = chercher_doublons(db, donnees.nom, donnees.prenom, donnees.date_naissance)
        if doublons and not donnees.confirmer_doublon:
            raise ConflitError(
                "Un patient de même identité (nom, prénom et date de naissance) existe déjà : "
                f"{decrire(doublons)}. S'il s'agit d'une autre personne, confirmez la création.",
                "PATIENT_DOUBLON",
            )
        patient = Patient(
            id=prochain_identifiant(db, Patient, PREFIXE_PATIENT, 5),
            nom=donnees.nom,
            prenom=donnees.prenom,
            sexe=donnees.sexe,
            date_naissance=donnees.date_naissance,
            telephone=donnees.telephone,
            quartier=donnees.quartier,
        )
        db.add(patient)
        journaliser(db, utilisateur, "PATIENT_CREE", cible=patient.id, detail="doublon confirmé" if doublons else None)
        db.flush()
        return patient

    patient = ecrire_avec_reessai(db, operation)
    logger.info("Patient %s créé.", patient.id)
    return _detail(db, patient)


@router.post(
    "/verification-doublon",
    response_model=list[PatientLecture],
    summary="Rechercher les fiches de même identité avant une création (EF-04)",
    responses=REPONSES_ERREURS,
)
def verifier_doublon(
    donnees: VerificationDoublon,
    db: Session = Depends(get_db),
    _utilisateur: Utilisateur = Depends(exiger_ecriture),
) -> list[PatientLecture]:
    """Retourne les fiches qui ont les mêmes nom, prénom et date de naissance (liste vide si aucun doublon)."""
    return [PatientLecture.model_validate(p) for p in chercher_doublons(db, donnees.nom, donnees.prenom, donnees.date_naissance)]


@router.get(
    "",
    response_model=ListePatients,
    summary="Rechercher des patients",
    responses=REPONSES_ERREURS,
)
def rechercher_patients(
    q: Annotated[str | None, Query(min_length=2, max_length=60)] = None,
    limite: Annotated[int, Query(ge=1, le=200)] = 50,
    decalage: Annotated[int, Query(ge=0)] = 0,
    db: Session = Depends(get_db),
    _utilisateur: Utilisateur = Depends(exiger_lecture),
) -> ListePatients:
    """Recherche par identifiant, nom, prénom ou téléphone (fragment de texte)."""
    requete = select(Patient)
    comptage = select(func.count()).select_from(Patient)
    if q:
        critere = or_(
            Patient.id.icontains(q, autoescape=True),
            Patient.nom.icontains(q, autoescape=True),
            Patient.prenom.icontains(q, autoescape=True),
            Patient.telephone.icontains(q, autoescape=True),
        )
        requete = requete.where(critere)
        comptage = comptage.where(critere)
    total = int(db.scalar(comptage) or 0)
    patients = db.scalars(requete.order_by(Patient.nom, Patient.prenom, Patient.id).limit(limite).offset(decalage)).all()
    return ListePatients(
        total=total,
        limite=limite,
        decalage=decalage,
        elements=[PatientLecture.model_validate(p) for p in patients],
    )


@router.get(
    "/{patient_id}",
    response_model=PatientDetail,
    summary="Consulter une fiche patient",
    responses=REPONSES_ERREURS,
)
def lire_patient(
    patient_id: str, db: Session = Depends(get_db), _utilisateur: Utilisateur = Depends(exiger_lecture)
) -> PatientDetail:
    """Fiche du patient avec l'alerte RG-08 (récidiviste) calculée à l'instant."""
    return _detail(db, obtenir_patient_ou_404(db, patient_id))


@router.patch(
    "/{patient_id}",
    response_model=PatientDetail,
    summary="Modifier une fiche patient",
    responses=REPONSES_ERREURS,
)
def modifier_patient(
    patient_id: str,
    donnees: PatientMiseAJour,
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(exiger_ecriture),
) -> PatientDetail:
    """Modifie les champs fournis (nom, prénom, date de naissance, téléphone, quartier)."""

    def operation() -> Patient:
        patient = obtenir_patient_ou_404(db, patient_id)
        champs = donnees.model_fields_set - {"confirmer_doublon"}
        if champs & {"nom", "prenom", "date_naissance"}:
            # La modification ne doit pas donner à la fiche l'identité d'une autre fiche (EF-04).
            nom = donnees.nom if "nom" in champs else patient.nom
            prenom = donnees.prenom if "prenom" in champs else patient.prenom
            naissance = donnees.date_naissance if "date_naissance" in champs else patient.date_naissance
            doublons = chercher_doublons(db, nom, prenom, naissance, exclure_id=patient.id) if naissance else []
            if doublons and not donnees.confirmer_doublon:
                raise ConflitError(
                    f"Cette modification donnerait à la fiche {patient.id} la même identité (nom, prénom et date de "
                    f"naissance) que : {decrire(doublons)}. S'il s'agit d'une autre personne, confirmez la modification.",
                    "PATIENT_DOUBLON",
                )
        modifies = []
        for champ in champs:
            valeur = getattr(donnees, champ)
            # nom et prénom sont obligatoires : une valeur nulle est ignorée plutôt qu'écrite.
            if valeur is None and champ in ("nom", "prenom"):
                continue
            setattr(patient, champ, valeur)
            modifies.append(champ)
        # Le journal ne conserve que les noms des champs modifiés, jamais leurs valeurs.
        journaliser(db, utilisateur, "PATIENT_MODIFIE", cible=patient.id, detail=", ".join(sorted(modifies)))
        db.flush()
        return patient

    patient = ecrire_avec_reessai(db, operation)
    return _detail(db, patient)


@router.get(
    "/{patient_id}/historique",
    response_model=HistoriquePatient,
    summary="Historique des rendez-vous d'un patient",
    responses=REPONSES_ERREURS,
)
def historique_patient(
    patient_id: str,
    limite: Annotated[int, Query(ge=1, le=500)] = 200,
    db: Session = Depends(get_db),
    _utilisateur: Utilisateur = Depends(exiger_lecture),
) -> HistoriquePatient:
    """Rendez-vous du patient (du plus récent au plus ancien), compteurs, taux d'absence (RG-09) et alerte RG-08."""
    patient = obtenir_patient_ou_404(db, patient_id)
    rendez_vous = db.scalars(
        select(RendezVous)
        .where(RendezVous.patient_id == patient.id)
        .order_by(RendezVous.debut.desc(), RendezVous.id.desc())
        .limit(limite)
    ).all()
    compteurs = dict(
        db.execute(
            select(RendezVous.statut, func.count())
            .where(RendezVous.patient_id == patient.id)
            .group_by(RendezVous.statut)
        ).all()
    )
    honores = compteurs.get(StatutRdv.HONORE, 0)
    absents = compteurs.get(StatutRdv.ABSENT, 0)
    return HistoriquePatient(
        patient_id=patient.id,
        total_rdv=sum(compteurs.values()),
        honores=honores,
        absents=absents,
        annules=compteurs.get(StatutRdv.ANNULE, 0),
        taux_absence_pct=calculer_taux_absence(honores, absents),
        alerte_recidiviste=evaluer_alerte_recidiviste(db, patient.id),
        rendez_vous=[RdvLecture.model_validate(r) for r in rendez_vous],
    )
