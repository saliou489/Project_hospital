"""Schémas Pydantic (entrées et sorties de l'API) avec validateurs stricts.

Convention : les messages des validateurs sont rédigés en français et commencent par une
minuscule ; le gestionnaire d'erreurs (exceptions.py) y ajoute le nom du champ concerné
ou met la première lettre en majuscule.
"""
import re
import unicodedata
from datetime import date, datetime, time, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

HORIZON_MAX_JOURS = 90  # RG-04

from backend.models import (
    DUREES_AUTORISEES_MIN,
    PATRON_ID_MEDECIN,
    PATRON_COULEUR,
    PATRON_ID_PATIENT,
    Canal,
    MotifAnnulation,
    RoleUtilisateur,
    Sexe,
    StatutRdv,
    TypeFermeture,
    aujourdhui,
    maintenant,
)
from backend.securite import LONGUEUR_MIN_CLE, normaliser_cle

# --------------------------------------------------------------------------------------
# Fonctions de validation réutilisées (schémas et import du CSV)
# --------------------------------------------------------------------------------------

# Numéro sénégalais : 9 chiffres commençant par 77, 78, 76, 70, 75 ou 33.
MOTIF_TELEPHONE = re.compile(r"^(77|78|76|70|75|33)\d{7}$")
# Lettres (accents compris), séparées par un espace, une apostrophe ou un tiret.
MOTIF_NOM = re.compile(r"^[^\W\d_]+(?:[ '’\-][^\W\d_]+)*$")


def valider_telephone(valeur: str) -> str:
    """Valide un numéro de téléphone sénégalais et le retourne tel quel (9 chiffres)."""
    texte = str(valeur).strip()
    if not MOTIF_TELEPHONE.fullmatch(texte):
        raise ValueError(
            "numéro invalide : 9 chiffres sans espace, commençant par 77, 78, 76, 70, 75 ou 33 "
            "(exemple : 771234567)"
        )
    return texte


def normaliser_nom(valeur: str) -> str:
    """Nettoie un nom ou un prénom : espaces superflus retirés ; majuscule initiale si la saisie est
    entièrement en majuscules ou en minuscules. Lève ValueError si le texte n'est pas un nom."""
    propre = " ".join(unicodedata.normalize("NFC", str(valeur)).split())
    if not propre:
        raise ValueError("ne peut pas être vide")
    if propre.isupper() or propre.islower():
        propre = propre.title()
    if not MOTIF_NOM.fullmatch(propre):
        raise ValueError("ne peut contenir que des lettres, espaces, tirets et apostrophes")
    return propre


def verifier_date_naissance(valeur: date) -> date:
    """La date de naissance ne peut pas être dans le futur."""
    if valeur > aujourdhui():
        raise ValueError("la date de naissance ne peut pas être dans le futur")
    return valeur


# --------------------------------------------------------------------------------------
# Classes de base
# --------------------------------------------------------------------------------------


class SchemaEntree(BaseModel):
    """Entrée stricte : champs inconnus refusés, espaces superflus retirés."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SchemaSortie(BaseModel):
    """Sortie construite à partir d'objets ORM."""

    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------------------
# Patients
# --------------------------------------------------------------------------------------


class PatientCreation(SchemaEntree):
    """Création d'une fiche patient."""

    nom: str = Field(min_length=1, max_length=80)
    prenom: str = Field(min_length=1, max_length=80)
    sexe: Sexe
    date_naissance: date
    telephone: str
    quartier: str | None = Field(default=None, max_length=60)
    # EF-04 : un patient de même identité est refusé, sauf confirmation explicite (homonyme réel).
    confirmer_doublon: bool = False

    @field_validator("nom", "prenom")
    @classmethod
    def _nom_valide(cls, valeur: str) -> str:
        return normaliser_nom(valeur)

    @field_validator("date_naissance")
    @classmethod
    def _naissance_valide(cls, valeur: date) -> date:
        return verifier_date_naissance(valeur)

    @field_validator("telephone")
    @classmethod
    def _telephone_valide(cls, valeur: str) -> str:
        return valider_telephone(valeur)

    @field_validator("quartier", mode="before")
    @classmethod
    def _quartier_vide(cls, valeur):
        if isinstance(valeur, str) and not valeur.strip():
            return None
        return valeur


class PatientMiseAJour(SchemaEntree):
    """Modification partielle d'une fiche patient."""

    nom: str | None = Field(default=None, min_length=1, max_length=80)
    prenom: str | None = Field(default=None, min_length=1, max_length=80)
    sexe: Sexe | None = None
    date_naissance: date | None = None
    telephone: str | None = None
    quartier: str | None = Field(default=None, max_length=60)
    confirmer_doublon: bool = False

    @field_validator("nom", "prenom")
    @classmethod
    def _nom_valide(cls, valeur: str | None) -> str | None:
        return None if valeur is None else normaliser_nom(valeur)

    @field_validator("date_naissance")
    @classmethod
    def _naissance_valide(cls, valeur: date | None) -> date | None:
        return None if valeur is None else verifier_date_naissance(valeur)

    @field_validator("telephone")
    @classmethod
    def _telephone_valide(cls, valeur: str | None) -> str | None:
        return None if valeur is None else valider_telephone(valeur)

    @model_validator(mode="after")
    def _verifier_champs(self) -> "PatientMiseAJour":
        champs = self.model_fields_set - {"confirmer_doublon"}
        if not champs:
            raise ValueError("aucune modification n'a été fournie.")
        # Seul le quartier est facultatif : les autres champs de la fiche ne peuvent pas être vidés (EF-03).
        vides = sorted(c for c in champs if c != "quartier" and getattr(self, c) is None)
        if vides:
            raise ValueError("ces champs obligatoires ne peuvent pas être vidés : " + ", ".join(vides) + ".")
        return self


class PatientLecture(SchemaSortie):
    """Fiche patient renvoyée par l'API."""

    id: str
    nom: str
    prenom: str
    sexe: Sexe
    date_naissance: date | None
    telephone: str | None
    quartier: str | None


class AlerteRecidiviste(BaseModel):
    """Résultat de la règle RG-08."""

    actif: bool
    nb_absences: int
    seuil: int
    periode_mois: int
    message: str | None = None


class PatientDetail(PatientLecture):
    """Fiche patient accompagnée de l'alerte RG-08."""

    alerte_recidiviste: AlerteRecidiviste


class ListePatients(BaseModel):
    """Page de résultats de recherche de patients."""

    total: int
    limite: int
    decalage: int
    elements: list[PatientLecture]


# --------------------------------------------------------------------------------------
# Rendez-vous
# --------------------------------------------------------------------------------------


class RdvCreation(SchemaEntree):
    """Création d'un rendez-vous (statut initial : Planifié). L'agent est celui du compte connecté."""

    patient_id: str = Field(pattern=PATRON_ID_PATIENT)
    medecin_id: str = Field(pattern=PATRON_ID_MEDECIN)
    # Si elle est absente, la date de prise est la date du jour.
    date_prise: date | None = None
    date_rdv: date
    heure_rdv: time
    canal: Canal | None = None

    @field_validator("heure_rdv")
    @classmethod
    def _heure_sans_secondes(cls, valeur: time) -> time:
        if valeur.second or valeur.microsecond or valeur.tzinfo is not None:
            raise ValueError("heure invalide : format attendu HH:MM")
        return valeur

    @model_validator(mode="after")
    def _verifier_dates(self) -> "RdvCreation":
        # 1. Pas de rendez-vous dans le passé (message testé en premier : c'est le plus parlant
        #    pour l'agent lorsque la date de prise est celle du jour par défaut).
        if datetime.combine(self.date_rdv, self.heure_rdv) < maintenant():
            raise ValueError("impossible de créer un rendez-vous à une date ou une heure passée.")
        # RG-04 : pas plus de 90 jours à l'avance.
        if self.date_rdv > aujourdhui() + timedelta(days=HORIZON_MAX_JOURS):
            raise ValueError(f"impossible de prendre un rendez-vous plus de {HORIZON_MAX_JOURS} jours à l'avance.")
        if self.date_prise is None:
            self.date_prise = aujourdhui()
        # 2. date_prise <= date_rdv.
        if self.date_prise > self.date_rdv:
            raise ValueError(
                "la date de prise du rendez-vous ne peut pas être postérieure à la date du rendez-vous."
            )
        # 3. On ne peut pas avoir pris rendez-vous dans le futur.
        if self.date_prise > aujourdhui():
            raise ValueError("la date de prise du rendez-vous ne peut pas être dans le futur.")
        return self


class RdvMiseAJour(SchemaEntree):
    """Modification d'un rendez-vous : changement de statut, annulation ou reprogrammation."""

    date_rdv: date | None = None
    heure_rdv: time | None = None
    statut: StatutRdv | None = None
    motif_annulation: MotifAnnulation | None = None

    @field_validator("motif_annulation", mode="before")
    @classmethod
    def _motif_vide(cls, valeur):
        """Un motif vide ou composé d'espaces équivaut à un motif absent."""
        if isinstance(valeur, str) and not valeur.strip():
            return None
        return valeur

    @field_validator("heure_rdv")
    @classmethod
    def _heure_sans_secondes(cls, valeur: time | None) -> time | None:
        if valeur is not None and (valeur.second or valeur.microsecond or valeur.tzinfo is not None):
            raise ValueError("heure invalide : format attendu HH:MM")
        return valeur

    @model_validator(mode="after")
    def _verifier_coherence(self) -> "RdvMiseAJour":
        if not self.model_fields_set:
            raise ValueError("aucune modification n'a été fournie.")
        # RG-05 : un rendez-vous ne peut être annulé qu'avec un motif. Ces deux types d'erreur
        # sont convertis en HTTP 400 (règle métier) par le gestionnaire d'erreurs.
        if self.statut == StatutRdv.ANNULE and self.motif_annulation is None:
            raise PydanticCustomError(
                "motif_annulation_manquant",
                "un motif d'annulation est obligatoire pour annuler un rendez-vous "
                "(patient, médecin, clinique ou autre).",
            )
        if self.motif_annulation is not None and self.statut != StatutRdv.ANNULE:
            raise PydanticCustomError(
                "motif_sans_annulation",
                "un motif d'annulation ne peut être renseigné que pour le statut « Annulé ».",
            )
        if self.statut == StatutRdv.ANNULE and (self.date_rdv is not None or self.heure_rdv is not None):
            raise ValueError("une annulation ne peut pas être combinée à un changement de date ou d'heure.")
        return self


class RdvLecture(SchemaSortie):
    """Rendez-vous renvoyé par l'API."""

    id: str
    patient_id: str
    patient_nom: str
    medecin_id: str
    medecin_nom: str
    specialite: str
    agent_id: str
    date_prise: date
    date_rdv: date
    heure_rdv: time
    duree_min: int
    statut: StatutRdv
    confirme: bool | None
    canal: Canal | None
    motif_annulation: MotifAnnulation | None


class ListeRdv(BaseModel):
    """Page de résultats de rendez-vous."""

    total: int
    limite: int
    decalage: int
    elements: list[RdvLecture]


class Avertissement(BaseModel):
    """Message d'alerte non bloquant destiné à l'agent."""

    code: str
    message: str


class ReponseCreationRdv(BaseModel):
    """Réponse à la création d'un rendez-vous : le rendez-vous et d'éventuels avertissements."""

    rdv: RdvLecture
    avertissements: list[Avertissement]


class HistoriquePatient(BaseModel):
    """Historique des rendez-vous d'un patient et indicateurs associés."""

    patient_id: str
    total_rdv: int
    honores: int
    absents: int
    annules: int
    taux_absence_pct: float | None
    alerte_recidiviste: AlerteRecidiviste
    rendez_vous: list[RdvLecture]


# --------------------------------------------------------------------------------------
# Référentiels
# --------------------------------------------------------------------------------------


class MedecinLecture(SchemaSortie):
    """Médecin renvoyé par l'API."""

    id: str
    nom: str
    specialite: str
    duree_consultation_min: int


class AgentLecture(SchemaSortie):
    """Agent renvoyé par l'API."""

    id: str


# --------------------------------------------------------------------------------------
# Statistiques
# --------------------------------------------------------------------------------------


class PeriodeFiltre(BaseModel):
    """Filtres appliqués à un calcul statistique."""

    date_debut: date | None = None
    date_fin: date | None = None
    specialite: str | None = None
    medecin_id: str | None = None


class KpiGlobaux(BaseModel):
    """Indicateurs clés sur la période filtrée."""

    filtres: PeriodeFiltre
    total_rdv: int
    repartition_statuts: dict[str, int]
    honores: int
    absents: int
    base_calcul_absence: int
    taux_absence_pct: float | None


class AbsenceSpecialite(BaseModel):
    """Taux d'absence (RG-09) d'une spécialité."""

    specialite: str
    total_rdv: int
    honores: int
    absents: int
    taux_absence_pct: float | None


class AbsenceMensuelle(BaseModel):
    """Volume et taux d'absence (RG-09) d'un mois."""

    periode: str
    total_rdv: int
    honores: int
    absents: int
    taux_absence_pct: float | None


class ErreurReponse(BaseModel):
    """Format uniforme des erreurs de l'API."""

    detail: str
    code_erreur: str


# --------------------------------------------------------------------------------------
# Authentification, comptes, pointage et journal des actions
# --------------------------------------------------------------------------------------


class ConnexionDemande(SchemaEntree):
    """Demande de connexion : rôle choisi (vérifié contre le compte), identifiant et clé personnelle."""

    role: RoleUtilisateur
    identifiant: str = Field(min_length=1, max_length=12)
    cle: str = Field(min_length=1, max_length=64)


class UtilisateurLecture(SchemaSortie):
    """Compte renvoyé par l'API (jamais la clé ni son empreinte)."""

    id: str
    role: RoleUtilisateur
    nom_affichage: str
    actif: bool
    medecin_id: str | None
    agent_id: str | None
    derniere_connexion: datetime | None


class ConnexionReponse(BaseModel):
    """Jeton de session et profil de l'utilisateur connecté."""

    jeton: str
    type_jeton: str = "bearer"
    expire_le: datetime
    utilisateur: UtilisateurLecture


class UtilisateurCreation(SchemaEntree):
    """Création d'un compte par l'administrateur. Un compte Médecin crée aussi la fiche du médecin."""

    role: RoleUtilisateur
    nom_affichage: str | None = Field(default=None, min_length=1, max_length=120)
    nom_medecin: str | None = Field(default=None, min_length=1, max_length=120)
    specialite: str | None = Field(default=None, min_length=1, max_length=60)
    duree_consultation_min: int | None = None

    @model_validator(mode="after")
    def _verifier_coherence(self) -> "UtilisateurCreation":
        champs_medecin = (self.nom_medecin, self.specialite, self.duree_consultation_min)
        if self.role == RoleUtilisateur.MEDECIN:
            if self.nom_medecin is None or self.specialite is None:
                raise ValueError("pour un médecin, le nom et la spécialité sont obligatoires.")
            # La durée est celle de la spécialité (EF-01) ; si elle est fournie, elle doit lui correspondre.
            if self.duree_consultation_min is not None and self.duree_consultation_min not in DUREES_AUTORISEES_MIN:
                raise ValueError("la durée de consultation doit être de 15, 20 ou 30 minutes.")
        elif any(champ is not None for champ in champs_medecin):
            raise ValueError("le nom du médecin, la spécialité et la durée ne concernent que le rôle Médecin.")
        return self


class CleGeneree(BaseModel):
    """Compte créé ou clé régénérée : la clé n'est affichée qu'une seule fois."""

    utilisateur: UtilisateurLecture
    cle: str
    avertissement: str = "Notez cette clé maintenant : elle ne sera plus jamais affichée."


class DesactivationDemande(SchemaEntree):
    """Désactivation d'un compte ; pour un médecin, on peut aussi le retirer des prises de rendez-vous."""

    retirer_du_planning: bool = False


class DesactivationReponse(BaseModel):
    utilisateur: UtilisateurLecture
    rdv_a_venir: int


class ListeUtilisateurs(BaseModel):
    total: int
    limite: int
    decalage: int
    elements: list[UtilisateurLecture]


class JournalLecture(SchemaSortie):
    id: int
    horodatage: datetime
    utilisateur_id: str | None
    role: str | None
    action: str
    cible: str | None
    detail: str | None


class ListeJournal(BaseModel):
    total: int
    limite: int
    decalage: int
    elements: list[JournalLecture]


class PointageLecture(SchemaSortie):
    id: int
    utilisateur_id: str
    nom_affichage: str
    role: RoleUtilisateur
    jour: date
    arrivee: datetime
    depart: datetime | None
    duree_minutes: int | None
    corrige: bool


class ListePointages(BaseModel):
    total: int
    limite: int
    decalage: int
    elements: list[PointageLecture]


class EtatPointage(BaseModel):
    """Situation de pointage de l'employé connecté pour le jour courant."""

    jour: date
    arrivee: datetime | None
    depart: datetime | None


class PointageCorrection(SchemaEntree):
    """Correction d'un pointage par l'administrateur : heures (HH:MM) du jour pointé."""

    arrivee: time | None = None
    depart: time | None = None

    @field_validator("arrivee", "depart")
    @classmethod
    def _heure_sans_secondes(cls, valeur: time | None) -> time | None:
        if valeur is not None and (valeur.second or valeur.microsecond or valeur.tzinfo is not None):
            raise ValueError("heure invalide : format attendu HH:MM")
        return valeur

    @model_validator(mode="after")
    def _au_moins_une_heure(self) -> "PointageCorrection":
        if self.arrivee is None and self.depart is None:
            raise ValueError("aucune modification n'a été fournie.")
        return self


# --------------------------------------------------------------------------------------
# Doublons de patients, rapport de qualité et changement de clé
# --------------------------------------------------------------------------------------


class VerificationDoublon(SchemaEntree):
    """Identité à comparer aux fiches existantes avant une création (EF-04)."""

    nom: str = Field(min_length=1, max_length=80)
    prenom: str = Field(min_length=1, max_length=80)
    date_naissance: date

    @field_validator("nom", "prenom")
    @classmethod
    def _nom_valide(cls, valeur: str) -> str:
        return normaliser_nom(valeur)

    @field_validator("date_naissance")
    @classmethod
    def _naissance_valide(cls, valeur: date) -> date:
        return verifier_date_naissance(valeur)


class PatientDoublon(PatientLecture):
    """Fiche d'un groupe de doublons, avec son nombre de rendez-vous."""

    nb_rdv: int


class GroupeDoublons(BaseModel):
    numero: int
    patients: list[PatientDoublon]


class ListeDoublons(BaseModel):
    nb_groupes: int
    nb_patients: int
    groupes: list[GroupeDoublons]


class ControleQualite(BaseModel):
    """Résultat d'un contrôle du rapport de qualité (section 7.3)."""

    code: str
    famille: str
    libelle: str
    gravite: Literal["erreur", "avertissement", "information"]
    nombre: int
    reference: int
    pourcentage: float | None
    exemples: list[str]


class RapportQualite(BaseModel):
    genere_le: datetime
    patients: int
    rendez_vous: int
    pointages: int
    erreurs: int
    avertissements: int
    informations: int
    controles_sans_anomalie: int
    controles: list[ControleQualite]


class ChangementCle(SchemaEntree):
    """Changement de sa propre clé par l'employé connecté."""

    cle_actuelle: str = Field(min_length=1, max_length=64)
    nouvelle_cle: str = Field(min_length=1, max_length=64)

    @field_validator("nouvelle_cle")
    @classmethod
    def _politique(cls, valeur: str) -> str:
        brute = normaliser_cle(valeur)
        if len(brute) < LONGUEUR_MIN_CLE:
            raise ValueError(f"la nouvelle clé doit contenir au moins {LONGUEUR_MIN_CLE} caractères (espaces et tirets non comptés).")
        if len(set(brute)) < 4:
            raise ValueError("la nouvelle clé est trop simple : utilisez des caractères variés.")
        return valeur


# --------------------------------------------------------------------------------------
# Planning : spécialités, plages, jours fériés, indisponibilités, créneaux
# --------------------------------------------------------------------------------------


def _verifier_heure(valeur: time) -> time:
    if valeur.second or valeur.microsecond or valeur.tzinfo is not None:
        raise ValueError("heure invalide : format attendu HH:MM")
    return valeur


class SpecialiteCreation(SchemaEntree):
    nom: str = Field(min_length=2, max_length=60)
    duree_standard_min: int
    couleur: str

    @field_validator("duree_standard_min")
    @classmethod
    def _duree(cls, valeur: int) -> int:
        if valeur not in DUREES_AUTORISEES_MIN:
            raise ValueError("la durée standard doit être de 15, 20 ou 30 minutes.")
        return valeur

    @field_validator("couleur")
    @classmethod
    def _couleur(cls, valeur: str) -> str:
        if not re.fullmatch(PATRON_COULEUR, valeur):
            raise ValueError("couleur invalide : format attendu #RRGGBB.")
        return valeur.upper()


class SpecialiteMiseAJour(SchemaEntree):
    duree_standard_min: int | None = None
    couleur: str | None = None
    actif: bool | None = None

    @model_validator(mode="after")
    def _verifier(self) -> "SpecialiteMiseAJour":
        if not self.model_fields_set:
            raise ValueError("aucune modification n'a été fournie.")
        if self.duree_standard_min is not None and self.duree_standard_min not in DUREES_AUTORISEES_MIN:
            raise ValueError("la durée standard doit être de 15, 20 ou 30 minutes.")
        if self.couleur is not None:
            if not re.fullmatch(PATRON_COULEUR, self.couleur):
                raise ValueError("couleur invalide : format attendu #RRGGBB.")
            self.couleur = self.couleur.upper()
        return self


class SpecialiteLecture(SchemaSortie):
    nom: str
    duree_standard_min: int
    couleur: str
    actif: bool
    nb_medecins: int = 0


class PlageSaisie(SchemaEntree):
    jour_semaine: int = Field(ge=0, le=6, description="0 = lundi ... 6 = dimanche")
    debut: time
    fin: time

    @field_validator("debut", "fin")
    @classmethod
    def _heures(cls, valeur: time) -> time:
        return _verifier_heure(valeur)

    @model_validator(mode="after")
    def _ordre(self) -> "PlageSaisie":
        if self.fin <= self.debut:
            raise ValueError("l'heure de fin doit suivre l'heure de début.")
        return self


class PlagesRemplacement(SchemaEntree):
    plages: list[PlageSaisie] = Field(max_length=42)


class PlageLecture(SchemaSortie):
    jour_semaine: int
    debut: time
    fin: time


class ReponsePlages(BaseModel):
    medecin_id: str
    plages: list[PlageLecture]
    rdv_hors_planning: list[str] = []


class JourFermeCreation(SchemaEntree):
    jour: date
    libelle: str = Field(min_length=2, max_length=120)
    type: TypeFermeture = TypeFermeture.FERIE


class JourFermeLecture(SchemaSortie):
    jour: date
    libelle: str
    type: TypeFermeture


class ReponseJourFerme(BaseModel):
    jour_ferme: JourFermeLecture
    rdv_a_reprogrammer: list[RdvLecture]


class IndisponibiliteCreation(SchemaEntree):
    medecin_id: str = Field(pattern=PATRON_ID_MEDECIN)
    debut: datetime
    fin: datetime
    motif: str = Field(min_length=3, max_length=200)

    @model_validator(mode="after")
    def _verifier(self) -> "IndisponibiliteCreation":
        if self.debut.tzinfo is not None or self.fin.tzinfo is not None:
            raise ValueError("les dates ne doivent pas contenir de fuseau horaire.")
        if self.fin <= self.debut:
            raise ValueError("la fin doit suivre le début.")
        if self.fin <= maintenant():
            raise ValueError("l'indisponibilité doit se terminer dans le futur.")
        return self


class IndisponibiliteLecture(SchemaSortie):
    id: int
    medecin_id: str
    medecin_nom: str | None = None
    debut: datetime
    fin: datetime
    motif: str
    active: bool
    declaree_par: str | None
    declaree_le: datetime


class ReponseIndisponibilite(BaseModel):
    indisponibilite: IndisponibiliteLecture
    rdv_a_reprogrammer: list[RdvLecture]


class CreneauLecture(BaseModel):
    medecin_id: str
    medecin_nom: str
    specialite: str
    date_rdv: date
    heure_rdv: time
    heure_fin: time
    etat: Literal["libre", "occupé", "indisponible"]
    rdv_id: str | None = None


class OccupationLecture(BaseModel):
    medecin_id: str
    medecin_nom: str
    specialite: str
    creneaux_ouverts: int
    creneaux_occupes: int
    taux_occupation_pct: float | None


class ReponseOccupation(BaseModel):
    date_debut: date
    date_fin: date
    creneaux_ouverts: int
    creneaux_occupes: int
    taux_occupation_pct: float | None
    par_medecin: list[OccupationLecture]


class HistoriqueLecture(SchemaSortie):
    horodatage: datetime
    utilisateur_id: str | None
    action: str
    champ: str | None
    avant: str | None
    apres: str | None


# --------------------------------------------------------------------------------------
# Bloc 3 : indicateurs d'analyse (EF-20 à EF-23, tableau 8.1 du cahier des charges)
# --------------------------------------------------------------------------------------


class IndicateursJour(BaseModel):
    """EF-20 : indicateurs clés d'une journée."""

    jour: date
    prevus: int  # rendez-vous du jour non annulés
    a_venir: int  # planifiés ou confirmés, pas encore traités
    honores: int
    absents: int
    annules: int
    taux_absence_pct: float | None
    taux_occupation_pct: float | None


class CelluleChaleur(BaseModel):
    jour_semaine: int  # 0 = lundi ... 6 = dimanche
    heure: int
    nombre: int


class RepartitionSpecialiteJour(BaseModel):
    specialite: str
    jour_semaine: int
    nombre: int


class CarteChaleur(BaseModel):
    """EF-21 : répartition des rendez-vous par jour de la semaine et heure, et par spécialité."""

    total: int
    inclut_annules: bool
    cellules: list[CelluleChaleur]
    par_specialite: list[RepartitionSpecialiteJour]


class DelaiSpecialite(BaseModel):
    specialite: str
    nombre_rdv: int
    mediane_jours: float
    moyenne_jours: float
    p90_jours: float


class DelaiAttente(BaseModel):
    """Délai d'attente : jours entre la prise et la date du rendez-vous (médiane), rendez-vous non annulés."""

    nombre_rdv: int
    mediane_jours: float | None
    par_specialite: list[DelaiSpecialite]


class AnnulationMotif(BaseModel):
    motif: str
    nombre: int
    part_pct: float  # part des rendez-vous créés


class AnnulationMois(BaseModel):
    periode: str
    total_cree: int
    annules: int
    taux_annulation_pct: float | None
    par_motif: dict[str, int]


class AnnulationSpecialite(BaseModel):
    specialite: str
    total_cree: int
    annules: int
    taux_annulation_pct: float | None


class TauxAnnulation(BaseModel):
    """Taux d'annulation = annulés / total créés, par motif, par mois et par spécialité."""

    total_cree: int
    annules: int
    taux_annulation_pct: float | None
    par_motif: list[AnnulationMotif]
    par_mois: list[AnnulationMois]
    par_specialite: list[AnnulationSpecialite]


class EvolutionMensuelleSpecialite(BaseModel):
    """EF-22 : volume et taux d'absence d'un mois pour une spécialité."""

    periode: str
    specialite: str
    total_rdv: int
    honores: int
    absents: int
    taux_absence_pct: float | None


# --------------------------------------------------------------------------------------
# Bloc 3b : prévision hebdomadaire (EF-24)
# --------------------------------------------------------------------------------------


class PointHistorique(BaseModel):
    semaine: date  # lundi de la semaine
    volume: int


class PointBacktest(BaseModel):
    """Une semaine tenue à l'écart : valeur réelle et valeur prévue par chaque méthode."""

    semaine: date
    reel: int
    par_methode: dict[str, float]


class PointPrevision(BaseModel):
    semaine: date
    prevision: float
    bas: float
    haut: float
    deja_pris: int  # rendez-vous déjà enregistrés pour cette semaine
    par_methode: dict[str, float]


class EvaluationMethode(BaseModel):
    methode: str
    libelle: str
    mae: float | None
    mape_pct: float | None


class PrevisionSpecialite(BaseModel):
    specialite: str
    historique: list[PointHistorique]
    backtest: list[PointBacktest]
    evaluation: list[EvaluationMethode]
    prevision: list[PointPrevision]


class ReponsePrevision(BaseModel):
    """EF-24 : prévision du nombre hebdomadaire de rendez-vous par spécialité."""

    derniere_semaine_observee: date
    horizon_semaines: int
    semaines_entrainement: int
    semaines_tenues_a_l_ecart: int
    methode_retenue: str
    methodes: list[EvaluationMethode]  # évaluation agrégée sur toutes les spécialités
    ensemble: PrevisionSpecialite
    specialites: list[PrevisionSpecialite]
    limites: list[str]


# --------------------------------------------------------------------------------------------------
# Score de risque d'absence (EF-25) : lu par les agents et le responsable, jamais par le patient
# --------------------------------------------------------------------------------------------------

class RdvARelancer(SchemaSortie):
    rdv_id: str
    patient_id: str
    patient: str
    telephone: str | None
    medecin: str
    specialite: str
    date_rdv: date
    heure_rdv: time
    statut: StatutRdv
    score_pct: float
    a_relancer: bool
    facteurs: str


class ListeARelancer(SchemaSortie):
    calcule_le: datetime | None
    modele: str | None
    seuil_pct: float | None
    nombre: int
    rendez_vous: list[RdvARelancer]


class EvaluationUnModele(SchemaSortie):
    libelle: str
    auc: float
    precision_moyenne: float
    brier: float
    precision: float | None
    rappel: float | None
    matrice: list[list[int]]
    part_relancee: float
    gain_net: float


class PointSeuil(SchemaSortie):
    seuil: float
    precision: float | None
    rappel: float | None
    part_relancee: float
    gain_net: float


class HypothesesCout(SchemaSortie):
    cout_relance: float
    cout_creneau: float
    efficacite: float


class EvaluationRisqueReponse(SchemaSortie):
    calcule_le: datetime
    retenu: str
    seuil: float
    hypotheses: HypothesesCout
    date_coupure: datetime
    nb_apprentissage: int
    nb_test: int
    nb_total_clos: int
    taux_absence_apprentissage: float
    taux_absence_test: float
    modeles: dict[str, EvaluationUnModele]
    courbe_seuils: list[PointSeuil]
    strategies: dict[str, float]
    limites: list[str]


class BilanCalculRisque(SchemaSortie):
    statut: str
    scores: int
    modele: str | None = None
    auc: float | None = None
    message: str | None = None
