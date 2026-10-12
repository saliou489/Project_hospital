"""Modèles ORM, énumérations et constantes métier de MédiRDV.

Entités : Agent, Medecin, Patient, RendezVous.
Les identifiants sont des chaînes lisibles (PAT00480, MED014, AGT07, RDV01965),
identiques à celles du fichier source afin de conserver la traçabilité.
"""
import calendar
import enum
from datetime import date, datetime, time, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from backend.database import Base

# --------------------------------------------------------------------------------------
# Énumérations
# --------------------------------------------------------------------------------------


class StatutRdv(str, enum.Enum):
    """Statuts d'un rendez-vous (cycle de vie : cahier des charges, section 6.1)."""

    PLANIFIE = "Planifié"
    CONFIRME = "Confirmé"
    HONORE = "Honoré"
    ABSENT = "Absent"
    ANNULE = "Annulé"


class MotifAnnulation(str, enum.Enum):
    """Motifs d'annulation autorisés (RG-05)."""

    PATIENT = "Patient"
    MEDECIN = "Médecin"
    CLINIQUE = "Clinique"
    AUTRE = "Autre"


class Sexe(str, enum.Enum):
    """Sexe du patient."""

    FEMININ = "F"
    MASCULIN = "M"


class Canal(str, enum.Enum):
    """Canal de prise de rendez-vous."""

    TELEPHONE = "Téléphone"
    GUICHET = "Guichet"
    RECOMMANDATION = "Recommandation médecin"


class RoleUtilisateur(str, enum.Enum):
    """Rôles de l'application (cahier des charges, section 4) : un seul rôle par utilisateur."""

    AGENT = "Agent d'accueil"
    MEDECIN = "Médecin"
    RESPONSABLE = "Responsable des opérations"
    DIRECTION = "Direction"
    ADMINISTRATEUR = "Administrateur"


# --------------------------------------------------------------------------------------
# Constantes métier
# --------------------------------------------------------------------------------------

# Préfixe et largeur numérique des identifiants de connexion par rôle (ex. OPS01, MED016).
PREFIXE_ROLE = {
    RoleUtilisateur.AGENT: ("AGT", 2),
    RoleUtilisateur.MEDECIN: ("MED", 3),
    RoleUtilisateur.RESPONSABLE: ("OPS", 2),
    RoleUtilisateur.DIRECTION: ("DIR", 2),
    RoleUtilisateur.ADMINISTRATEUR: ("ADM", 2),
}

PREFIXE_PATIENT = "PAT"
PREFIXE_MEDECIN = "MED"
PREFIXE_AGENT = "AGT"
PREFIXE_RDV = "RDV"

PATRON_ID_PATIENT = r"^PAT\d{5}$"
PATRON_ID_MEDECIN = r"^MED\d{3}$"
PATRON_ID_AGENT = r"^AGT\d{2}$"
PATRON_ID_RDV = r"^RDV\d{5}$"

# Durées de consultation autorisées, en minutes (cahier des charges, EF-01).
DUREES_AUTORISEES_MIN = (15, 20, 30)

# RG-08 : alerte à partir de 3 absences sur les 6 derniers mois.
SEUIL_ABSENCES_RECIDIVISTE = 3
PERIODE_RECIDIVISTE_MOIS = 6

# RG-01 / RG-02 : seuls les rendez-vous actifs occupent un créneau.
STATUTS_ACTIFS = (StatutRdv.PLANIFIE, StatutRdv.CONFIRME, StatutRdv.HONORE)
STATUTS_CLOS = (StatutRdv.HONORE, StatutRdv.ABSENT, StatutRdv.ANNULE)

# Cycle de vie (cahier des charges, section 6.1) : un statut clos est définitif et
# l'annulation n'est possible que depuis « Planifié » ou « Confirmé ».
TRANSITIONS_STATUT = {
    StatutRdv.PLANIFIE: {StatutRdv.CONFIRME, StatutRdv.HONORE, StatutRdv.ABSENT, StatutRdv.ANNULE},
    StatutRdv.CONFIRME: {StatutRdv.HONORE, StatutRdv.ABSENT, StatutRdv.ANNULE},
    StatutRdv.HONORE: set(),
    StatutRdv.ABSENT: set(),
    StatutRdv.ANNULE: set(),
}

# --------------------------------------------------------------------------------------
# Utilitaires de date et d'identifiant
# --------------------------------------------------------------------------------------


def maintenant() -> datetime:
    """Heure courante de Dakar (UTC+0 toute l'année), sans fuseau : cohérente avec les colonnes DateTime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def aujourdhui() -> date:
    """Date du jour à Dakar."""
    return maintenant().date()


def reculer_de_mois(reference: datetime, mois: int) -> datetime:
    """Retourne ``reference`` reculée de ``mois`` mois calendaires (jour ramené au dernier jour du mois si besoin)."""
    total = reference.year * 12 + (reference.month - 1) - mois
    annee, indice = divmod(total, 12)
    mois_cible = indice + 1
    dernier_jour = calendar.monthrange(annee, mois_cible)[1]
    return reference.replace(year=annee, month=mois_cible, day=min(reference.day, dernier_jour))


def prochain_identifiant(db: Session, modele, prefixe: str, largeur: int) -> str:
    """Calcule le prochain identifiant séquentiel d'un modèle (ex. RDV02016).

    Les identifiants ont une largeur fixe : l'ordre alphabétique est donc l'ordre numérique.
    """
    dernier = db.scalar(select(func.max(modele.id)).where(modele.id.like(f"{prefixe}%")))
    numero = int(dernier[len(prefixe):]) + 1 if dernier else 1
    return f"{prefixe}{numero:0{largeur}d}"


def _type_enum(classe: type[enum.Enum], nom: str) -> Enum:
    """Colonne texte contrainte (CHECK) qui stocke les valeurs françaises de l'énumération."""
    return Enum(
        classe,
        name=nom,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        length=30,
        values_callable=lambda membres: [membre.value for membre in membres],
    )


# --------------------------------------------------------------------------------------
# Modèles
# --------------------------------------------------------------------------------------


class Agent(Base):
    """Agent d'accueil qui saisit les rendez-vous."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(10), primary_key=True)
    actif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    rendez_vous: Mapped[list["RendezVous"]] = relationship(back_populates="agent")


class Medecin(Base):
    """Médecin et sa spécialité."""

    __tablename__ = "medecins"
    __table_args__ = (
        CheckConstraint("duree_consultation_min IN (15, 20, 30)", name="ck_medecin_duree"),
    )

    id: Mapped[str] = mapped_column(String(10), primary_key=True)
    nom: Mapped[str] = mapped_column(String(120), nullable=False)
    specialite: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    # Durée d'une consultation, utilisée pour calculer l'heure de fin d'un nouveau rendez-vous.
    duree_consultation_min: Mapped[int] = mapped_column(Integer, nullable=False)
    actif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    rendez_vous: Mapped[list["RendezVous"]] = relationship(back_populates="medecin")


class Patient(Base):
    """Fiche patient (aucune donnée médicale n'est stockée)."""

    __tablename__ = "patients"

    id: Mapped[str] = mapped_column(String(12), primary_key=True)
    nom: Mapped[str] = mapped_column(String(80), nullable=False)
    prenom: Mapped[str] = mapped_column(String(80), nullable=False)
    sexe: Mapped[Sexe] = mapped_column(_type_enum(Sexe, "sexe_patient"), nullable=False)
    # Facultatifs en base : l'historique importé peut être incomplet. La création par l'API
    # (schémas Pydantic) les exige.
    date_naissance: Mapped[date | None] = mapped_column(Date, nullable=True)
    telephone: Mapped[str | None] = mapped_column(String(9), nullable=True)
    quartier: Mapped[str | None] = mapped_column(String(60), nullable=True)
    cree_le: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=maintenant)

    rendez_vous: Mapped[list["RendezVous"]] = relationship(back_populates="patient")


_CLAUSE_ACTIF = "statut IN (" + ", ".join(f"'{s.value}'" for s in STATUTS_ACTIFS) + ")"


class RendezVous(Base):
    """Rendez-vous entre un patient et un médecin, saisi par un agent."""

    __tablename__ = "rendez_vous"
    __table_args__ = (
        CheckConstraint("fin > debut", name="ck_rdv_fin_apres_debut"),
        # RG-05 en base : un motif existe si et seulement si le rendez-vous est annulé.
        CheckConstraint(
            "(statut = 'Annulé' AND motif_annulation IS NOT NULL) "
            "OR (statut <> 'Annulé' AND motif_annulation IS NULL)",
            name="ck_rdv_motif_annulation",
        ),
        # RG-01 / RG-02 en base : filet de sécurité contre deux écritures simultanées
        # sur un même créneau (la vérification de chevauchement se fait avant l'insertion).
        Index(
            "uq_rdv_medecin_debut_actif",
            "medecin_id",
            "debut",
            unique=True,
            sqlite_where=text(_CLAUSE_ACTIF),
            postgresql_where=text(_CLAUSE_ACTIF),
        ),
        Index(
            "uq_rdv_patient_debut_actif",
            "patient_id",
            "debut",
            unique=True,
            sqlite_where=text(_CLAUSE_ACTIF),
            postgresql_where=text(_CLAUSE_ACTIF),
        ),
        Index("ix_rdv_debut_statut", "debut", "statut"),
    )

    id: Mapped[str] = mapped_column(String(12), primary_key=True)
    patient_id: Mapped[str] = mapped_column(
        ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    medecin_id: Mapped[str] = mapped_column(
        ForeignKey("medecins.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    date_prise: Mapped[date] = mapped_column(Date, nullable=False)
    debut: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    fin: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    statut: Mapped[StatutRdv] = mapped_column(
        _type_enum(StatutRdv, "statut_rdv"), nullable=False, default=StatutRdv.PLANIFIE
    )
    confirme: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    canal: Mapped[Canal | None] = mapped_column(_type_enum(Canal, "canal_rdv"), nullable=True)
    motif_annulation: Mapped[MotifAnnulation | None] = mapped_column(
        _type_enum(MotifAnnulation, "motif_annulation"), nullable=True
    )
    cree_le: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=maintenant)
    modifie_le: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=maintenant, onupdate=maintenant
    )

    patient: Mapped[Patient] = relationship(back_populates="rendez_vous", lazy="joined", innerjoin=True)
    medecin: Mapped[Medecin] = relationship(back_populates="rendez_vous", lazy="joined", innerjoin=True)
    agent: Mapped[Agent] = relationship(back_populates="rendez_vous")

    # --- Propriétés calculées, utilisées pour la sérialisation de l'API ---------------

    @property
    def date_rdv(self) -> date:
        """Date du rendez-vous."""
        return self.debut.date()

    @property
    def heure_rdv(self) -> time:
        """Heure de début du rendez-vous."""
        return self.debut.time()

    @property
    def duree_min(self) -> int:
        """Durée du rendez-vous en minutes."""
        return int((self.fin - self.debut).total_seconds() // 60)

    @property
    def specialite(self) -> str:
        """Spécialité du médecin."""
        return self.medecin.specialite

    @property
    def medecin_nom(self) -> str:
        """Nom du médecin."""
        return self.medecin.nom

    @property
    def patient_nom(self) -> str:
        """Nom complet du patient (nom puis prénom)."""
        return f"{self.patient.nom} {self.patient.prenom}"


# --------------------------------------------------------------------------------------
# Comptes, pointage et journal des actions
# --------------------------------------------------------------------------------------


def _lien_role(role: RoleUtilisateur, colonne: str) -> str:
    """Contrainte : la colonne de lien n'est renseignée que pour le rôle concerné."""
    valeur = role.value.replace("'", "''")
    return f"(role = '{valeur}') = ({colonne} IS NOT NULL)"


class Utilisateur(Base):
    """Compte de connexion. L'identifiant est celui de l'employé (MED016, AGT01, OPS01, DIR01, ADM01)."""

    __tablename__ = "utilisateurs"
    __table_args__ = (
        CheckConstraint(_lien_role(RoleUtilisateur.MEDECIN, "medecin_id"), name="ck_utilisateur_lien_medecin"),
        CheckConstraint(_lien_role(RoleUtilisateur.AGENT, "agent_id"), name="ck_utilisateur_lien_agent"),
    )

    id: Mapped[str] = mapped_column(String(12), primary_key=True)
    role: Mapped[RoleUtilisateur] = mapped_column(_type_enum(RoleUtilisateur, "role_utilisateur"), nullable=False)
    nom_affichage: Mapped[str] = mapped_column(String(120), nullable=False)
    # Empreinte Argon2 de la clé personnelle : la clé elle-même n'est jamais stockée.
    cle_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    actif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    medecin_id: Mapped[str | None] = mapped_column(
        ForeignKey("medecins.id", ondelete="RESTRICT"), nullable=True, unique=True
    )
    agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("agents.id", ondelete="RESTRICT"), nullable=True, unique=True
    )
    essais_echoues: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verrouille_jusqua: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    derniere_connexion: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cree_le: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=maintenant)

    medecin: Mapped[Medecin | None] = relationship()
    agent: Mapped[Agent | None] = relationship()


class Pointage(Base):
    """Pointage d'un employé : une heure d'arrivée et une heure de départ par jour."""

    __tablename__ = "pointages"
    __table_args__ = (
        UniqueConstraint("utilisateur_id", "jour", name="uq_pointage_utilisateur_jour"),
        CheckConstraint("depart IS NULL OR depart >= arrivee", name="ck_pointage_depart_apres_arrivee"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    utilisateur_id: Mapped[str] = mapped_column(
        ForeignKey("utilisateurs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    jour: Mapped[date] = mapped_column(Date, nullable=False)
    arrivee: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    depart: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Renseignés lorsqu'un administrateur corrige l'heure saisie par l'employé.
    corrige_par: Mapped[str | None] = mapped_column(String(12), nullable=True)
    corrige_le: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    utilisateur: Mapped[Utilisateur] = relationship(lazy="joined", innerjoin=True)

    @property
    def nom_affichage(self) -> str:
        return self.utilisateur.nom_affichage

    @property
    def role(self) -> RoleUtilisateur:
        return self.utilisateur.role

    @property
    def duree_minutes(self) -> int | None:
        """Durée de présence en minutes ; None tant que le départ n'est pas pointé."""
        if self.depart is None:
            return None
        return int((self.depart - self.arrivee).total_seconds() // 60)

    @property
    def corrige(self) -> bool:
        return self.corrige_par is not None


class JournalAction(Base):
    """Journal des actions (EF-32) : qui a fait quoi, sur quoi, et quand. Sans donnée personnelle en clair."""

    __tablename__ = "journal_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    horodatage: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=maintenant, index=True)
    # Identifiant et rôle conservés sous forme de texte : l'historique doit survivre à tout changement de compte.
    utilisateur_id: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    role: Mapped[str | None] = mapped_column(String(30), nullable=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    cible: Mapped[str | None] = mapped_column(String(30), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(300), nullable=True)


# --------------------------------------------------------------------------------------
# Planning : spécialités, plages de consultation, jours fériés, indisponibilités, historique
# --------------------------------------------------------------------------------------

PATRON_COULEUR = r"^#[0-9A-Fa-f]{6}$"
JOURS_SEMAINE = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
HORIZON_RDV_JOURS = 90  # RG-04


class TypeFermeture(str, enum.Enum):
    """EF-05 : jour férié (calendrier national) ou fermeture exceptionnelle de la clinique."""

    FERIE = "Jour férié"
    FERMETURE = "Fermeture exceptionnelle"


class Specialite(Base):
    """EF-01 : spécialité, durée standard d'une consultation et couleur d'affichage.

    Le nom est l'identifiant : il ne change jamais (les médecins y font référence) et une spécialité
    n'est jamais supprimée, seulement désactivée.
    """

    __tablename__ = "specialites"
    __table_args__ = (
        CheckConstraint("duree_standard_min IN (15, 20, 30)", name="ck_specialite_duree"),
    )

    nom: Mapped[str] = mapped_column(String(60), primary_key=True)
    duree_standard_min: Mapped[int] = mapped_column(Integer, nullable=False)
    couleur: Mapped[str] = mapped_column(String(7), nullable=False)
    actif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class PlageConsultation(Base):
    """EF-02 : plage hebdomadaire de consultation d'un médecin (un jour de la semaine, de début à fin)."""

    __tablename__ = "plages_consultation"
    __table_args__ = (
        CheckConstraint("jour_semaine BETWEEN 0 AND 6", name="ck_plage_jour"),
        CheckConstraint("fin > debut", name="ck_plage_fin_apres_debut"),
        Index("ix_plage_medecin_jour", "medecin_id", "jour_semaine"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    medecin_id: Mapped[str] = mapped_column(ForeignKey("medecins.id"), nullable=False)
    jour_semaine: Mapped[int] = mapped_column(Integer, nullable=False)  # 0 = lundi ... 6 = dimanche
    debut: Mapped[time] = mapped_column(Time, nullable=False)
    fin: Mapped[time] = mapped_column(Time, nullable=False)


class JourFerie(Base):
    """EF-05 : jour férié ou fermeture exceptionnelle (aucun rendez-vous possible ce jour-là)."""

    __tablename__ = "jours_feries"

    jour: Mapped[date] = mapped_column(Date, primary_key=True)
    libelle: Mapped[str] = mapped_column(String(120), nullable=False)
    type: Mapped[TypeFermeture] = mapped_column(_type_enum(TypeFermeture, "type_fermeture"), nullable=False)


class Indisponibilite(Base):
    """EF-14 : période pendant laquelle un médecin ne consulte pas. Levée plutôt que supprimée."""

    __tablename__ = "indisponibilites"
    __table_args__ = (
        CheckConstraint("fin > debut", name="ck_indispo_fin_apres_debut"),
        Index("ix_indispo_medecin_debut", "medecin_id", "debut"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    medecin_id: Mapped[str] = mapped_column(ForeignKey("medecins.id"), nullable=False)
    debut: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    fin: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    motif: Mapped[str] = mapped_column(String(200), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    declaree_par: Mapped[str | None] = mapped_column(String(12), nullable=True)
    declaree_le: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=maintenant)

    medecin: Mapped[Medecin] = relationship(lazy="joined", innerjoin=True)


class HistoriqueRdv(Base):
    """RG-11 : une ligne par changement d'un rendez-vous (création, statut, reprogrammation). Jamais modifiée."""

    __tablename__ = "historique_rdv"
    __table_args__ = (Index("ix_historique_rdv", "rdv_id", "horodatage"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rdv_id: Mapped[str] = mapped_column(ForeignKey("rendez_vous.id"), nullable=False)
    horodatage: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=maintenant)
    utilisateur_id: Mapped[str | None] = mapped_column(String(12), nullable=True)  # NULL = action automatique du système
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    champ: Mapped[str | None] = mapped_column(String(30), nullable=True)
    avant: Mapped[str | None] = mapped_column(String(120), nullable=True)
    apres: Mapped[str | None] = mapped_column(String(120), nullable=True)


class ScoreRisque(Base):
    """EF-25 : score de risque d'absence d'un rendez-vous à venir, recalculé chaque nuit par le module analytique.

    Cette table n'est lue que par les écrans de relance : elle n'est jamais jointe aux réponses de prise de rendez-vous.
    """

    __tablename__ = "scores_risque"

    rdv_id: Mapped[str] = mapped_column(ForeignKey("rendez_vous.id", ondelete="CASCADE"), primary_key=True)
    score: Mapped[float] = mapped_column(Float, nullable=False)  # probabilité estimée de non-présentation, entre 0 et 1
    a_relancer: Mapped[bool] = mapped_column(Boolean, nullable=False)  # score au-dessus du seuil de la dernière évaluation
    facteurs: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    modele: Mapped[str] = mapped_column(String(30), nullable=False)
    calcule_le: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=maintenant)

    __table_args__ = (Index("ix_score_a_relancer", "a_relancer", "score"),)


class EvaluationRisque(Base):
    """Une ligne par exécution du module analytique : métriques, seuil et hypothèses (JSON), pour suivre la dérive."""

    __tablename__ = "evaluations_risque"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    calcule_le: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=maintenant, index=True)
    modele_retenu: Mapped[str] = mapped_column(String(30), nullable=False)
    seuil: Mapped[float] = mapped_column(Float, nullable=False)
    auc: Mapped[float] = mapped_column(Float, nullable=False)
    nb_scores: Mapped[int] = mapped_column(Integer, nullable=False)
    details: Mapped[str] = mapped_column(Text, nullable=False)  # JSON de risque.resume_evaluation
