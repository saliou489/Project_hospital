"""Logique de planning : créneaux, jours fermés, indisponibilités, taux d'occupation, absences automatiques.

Définitions (cahier des charges, EF-10, EF-11, EF-14, EF-17, RG-03, RG-10, RG-11) :

- une plage de consultation est un intervalle hebdomadaire d'un médecin (ex. lundi 08:00-12:00) ;
- les créneaux d'une plage se calculent à partir de son heure de début, par pas de la durée standard de la
  spécialité ; un créneau doit tenir entièrement dans la plage ;
- un créneau est « ouvert » s'il se situe un jour non férié/fermé ; il est « indisponible » si une indisponibilité
  active du médecin le recoupe, « occupé » si un rendez-vous non annulé le recoupe, sinon « libre ».
"""
import logging
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from backend.exceptions import RegleMetierError
from backend.journal import journaliser
from backend.models import (
    HORIZON_RDV_JOURS,
    JOURS_SEMAINE,
    STATUTS_ACTIFS,
    HistoriqueRdv,
    Indisponibilite,
    JourFerie,
    Medecin,
    PlageConsultation,
    RendezVous,
    Specialite,
    StatutRdv,
    TypeFermeture,
    Utilisateur,
    aujourdhui,
    maintenant,
)

logger = logging.getLogger(__name__)

# Délai après le début d'un rendez-vous au-delà duquel il est passé en « Absent » (EF-17), et ancienneté maximale
# des rendez-vous rattrapés : au-delà, c'est le rapport de qualité qui signale le rendez-vous resté ouvert.
DELAI_ABSENCE_AUTO_MIN = 60
ANCIENNETE_MAX_ABSENCE_AUTO_JOURS = 7
PERIODE_MAX_JOURS = 400  # borne des périodes demandées à l'API (protège le serveur)

PALETTE_COULEURS = ["#1B6B3A", "#2F7FB5", "#B5651D", "#7A4FA3", "#C2495B", "#3E8E8E", "#8A8A1E", "#5B6C7A", "#A0522D", "#4A7A2A"]
STATUTS_OCCUPANTS = (StatutRdv.PLANIFIE, StatutRdv.CONFIRME, StatutRdv.HONORE, StatutRdv.ABSENT)
PLAGES_PAR_DEFAUT = [(jour, time(8, 0), time(12, 0)) for jour in range(6)] + [(jour, time(14, 0), time(18, 0)) for jour in range(6)]


# --------------------------------------------------------------------------------------
# Jours fériés
# --------------------------------------------------------------------------------------


def _paques(annee: int) -> date:
    """Dimanche de Pâques (algorithme grégorien de Meeus/Jones/Butcher)."""
    a, b, c = annee % 19, annee // 100, annee % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    mois = (h + el - 7 * m + 114) // 31
    jour = (h + el - 7 * m + 114) % 31 + 1
    return date(annee, mois, jour)


def jours_feries_calculables(annee: int) -> list[tuple[date, str]]:
    """Jours fériés dont la date se calcule : dates fixes et fêtes chrétiennes mobiles.

    Les fêtes musulmanes (Korité, Tabaski, Tamkharit, Maouloud...) dépendent de l'observation de la lune et d'une
    décision officielle : elles ne sont pas calculées, l'administrateur les ajoute quand la date est connue.
    """
    paques = _paques(annee)
    return [
        (date(annee, 1, 1), "Jour de l'An"),
        (paques + timedelta(days=1), "Lundi de Pâques"),
        (date(annee, 4, 4), "Fête de l'Indépendance"),
        (date(annee, 5, 1), "Fête du Travail"),
        (paques + timedelta(days=39), "Ascension"),
        (paques + timedelta(days=50), "Lundi de Pentecôte"),
        (date(annee, 8, 15), "Assomption"),
        (date(annee, 11, 1), "Toussaint"),
        (date(annee, 12, 25), "Noël"),
    ]


def initialiser_referentiels(db: Session, reference: date | None = None) -> dict[str, int]:
    """Complète, sans rien écraser, les référentiels de planning ; valide la transaction.

    - une spécialité par spécialité de médecin connue (durée du premier médecin, couleur de la palette) ;
    - les plages par défaut (lundi-samedi, 08:00-12:00 et 14:00-18:00, celles observées dans le fichier source)
      pour chaque médecin qui n'en a aucune ;
    - les jours fériés calculables de l'année précédente à l'année suivante.
    """
    reference = reference or aujourdhui()
    crees = {"specialites": 0, "plages": 0, "feries": 0}
    medecins = db.scalars(select(Medecin).order_by(Medecin.id)).all()
    connues = {s.nom for s in db.scalars(select(Specialite)).all()}
    for medecin in medecins:
        if medecin.specialite not in connues:
            db.add(
                Specialite(
                    nom=medecin.specialite,
                    duree_standard_min=medecin.duree_consultation_min,
                    couleur=PALETTE_COULEURS[len(connues) % len(PALETTE_COULEURS)],
                    actif=True,
                )
            )
            connues.add(medecin.specialite)
            crees["specialites"] += 1
    avec_plages = set(db.scalars(select(PlageConsultation.medecin_id).distinct()).all())
    for medecin in medecins:
        if medecin.id not in avec_plages:
            db.add_all(PlageConsultation(medecin_id=medecin.id, jour_semaine=j, debut=d, fin=f) for j, d, f in PLAGES_PAR_DEFAUT)
            crees["plages"] += len(PLAGES_PAR_DEFAUT)
    existants = set(db.scalars(select(JourFerie.jour)).all())
    for annee in (reference.year - 1, reference.year, reference.year + 1):
        for jour, libelle in jours_feries_calculables(annee):
            if jour not in existants:
                db.add(JourFerie(jour=jour, libelle=libelle, type=TypeFermeture.FERIE))
                existants.add(jour)
                crees["feries"] += 1
    db.commit()
    if any(crees.values()):
        logger.info("Référentiels de planning complétés : %s", crees)
    return crees


# --------------------------------------------------------------------------------------
# Lecture des référentiels
# --------------------------------------------------------------------------------------


def duree_specialite(db: Session, medecin: Medecin) -> int:
    """Durée standard d'une consultation (EF-01) ; à défaut de fiche spécialité, celle enregistrée sur le médecin."""
    specialite = db.get(Specialite, medecin.specialite)
    return specialite.duree_standard_min if specialite is not None else medecin.duree_consultation_min


def jours_fermes(db: Session, debut: date, fin: date) -> dict[date, JourFerie]:
    lignes = db.scalars(select(JourFerie).where(JourFerie.jour >= debut, JourFerie.jour <= fin)).all()
    return {ligne.jour: ligne for ligne in lignes}


def creneaux_de_plage(jour: date, plage: PlageConsultation, duree: int) -> list[tuple[datetime, datetime]]:
    """Créneaux d'une plage pour un jour donné : pas de ``duree`` minutes depuis le début, le dernier tient dans la plage."""
    resultat = []
    courant = datetime.combine(jour, plage.debut)
    limite = datetime.combine(jour, plage.fin)
    pas = timedelta(minutes=duree)
    while courant + pas <= limite:
        resultat.append((courant, courant + pas))
        courant += pas
    return resultat


@dataclass
class Creneau:
    medecin_id: str
    debut: datetime
    fin: datetime
    etat: str  # « libre », « occupé » ou « indisponible »
    rdv_id: str | None = None


class ContexteMedecins:
    """Charge en une fois les données nécessaires au calcul des créneaux de plusieurs médecins sur une période."""

    def __init__(self, db: Session, medecins: list[Medecin], debut: date, fin: date, statuts_rdv=STATUTS_OCCUPANTS):
        self.debut, self.fin = debut, fin
        self.medecins = {m.id: m for m in medecins}
        ids = list(self.medecins)
        self.durees = {m.id: duree_specialite(db, m) for m in medecins}
        self.fermes = jours_fermes(db, debut, fin)
        self.plages: dict[tuple[str, int], list[PlageConsultation]] = defaultdict(list)
        self.indispos: dict[str, list[Indisponibilite]] = defaultdict(list)
        self.rdv: dict[str, list[RendezVous]] = defaultdict(list)
        self._rdv_tries: dict[str, list[tuple[datetime, datetime, str]]] = {}
        self._rdv_debuts: dict[str, list[datetime]] = {}
        self._rdv_duree_max: dict[str, timedelta] = {}
        self._indispos_tuples: dict[str, list[tuple[datetime, datetime]]] = {}
        if not ids:
            return
        for plage in db.scalars(select(PlageConsultation).where(PlageConsultation.medecin_id.in_(ids))):
            self.plages[(plage.medecin_id, plage.jour_semaine)].append(plage)
        bornes = (datetime.combine(debut, time.min), datetime.combine(fin + timedelta(days=1), time.min))
        for indispo in db.scalars(
            select(Indisponibilite).where(
                Indisponibilite.medecin_id.in_(ids), Indisponibilite.active.is_(True),
                Indisponibilite.debut < bornes[1], Indisponibilite.fin > bornes[0],
            )
        ):  # fmt: skip
            self.indispos[indispo.medecin_id].append(indispo)
        for rdv in db.scalars(
            select(RendezVous).where(
                RendezVous.medecin_id.in_(ids), RendezVous.statut.in_(statuts_rdv),
                RendezVous.debut < bornes[1], RendezVous.fin > bornes[0],
            )
        ):  # fmt: skip
            self.rdv[rdv.medecin_id].append(rdv)
        # Index de recherche rapide : tuples (début, fin, id) triés, sans accès répété aux objets ORM.
        for identifiant, lignes in self.rdv.items():
            tries = sorted((r.debut, r.fin, r.id) for r in lignes)
            self._rdv_tries[identifiant] = tries
            self._rdv_debuts[identifiant] = [t[0] for t in tries]
            self._rdv_duree_max[identifiant] = max(t[1] - t[0] for t in tries)
        self._indispos_tuples = {k: [(i.debut, i.fin) for i in v] for k, v in self.indispos.items()}

    def _occupant(self, medecin_id: str, debut: datetime, fin: datetime) -> str | None:
        """Identifiant d'un rendez-vous chevauchant [debut, fin[, ou None (recherche par dichotomie)."""
        tries = self._rdv_tries.get(medecin_id)
        if not tries:
            return None
        debuts = self._rdv_debuts[medecin_id]
        borne = bisect_left(debuts, fin)  # seuls les rendez-vous commençant avant la fin du créneau peuvent chevaucher
        plancher = debut - self._rdv_duree_max[medecin_id]
        for i in range(borne - 1, -1, -1):
            r_debut, r_fin, r_id = tries[i]
            if r_debut <= plancher:
                break
            if r_fin > debut:
                return r_id
        return None

    def creneaux(self, medecin_id: str, jour: date) -> list[Creneau]:
        """Créneaux ouverts ou indisponibles du jour (vide un jour férié/fermé ou sans plage)."""
        if jour in self.fermes:
            return []
        resultat = []
        for plage in sorted(self.plages.get((medecin_id, jour.weekday()), []), key=lambda p: p.debut):
            for debut, fin in creneaux_de_plage(jour, plage, self.durees[medecin_id]):
                if any(i_debut < fin and i_fin > debut for i_debut, i_fin in self._indispos_tuples.get(medecin_id, ())):
                    resultat.append(Creneau(medecin_id, debut, fin, "indisponible"))
                    continue
                occupant = self._occupant(medecin_id, debut, fin)
                resultat.append(Creneau(medecin_id, debut, fin, "occupé" if occupant else "libre", occupant))
        return resultat


def _verifier_periode(debut: date, fin: date) -> None:
    if fin < debut:
        raise RegleMetierError("La date de fin précède la date de début.", "PERIODE_INVALIDE")
    if (fin - debut).days > PERIODE_MAX_JOURS:
        raise RegleMetierError(f"La période ne peut pas dépasser {PERIODE_MAX_JOURS} jours.", "PERIODE_TROP_LONGUE")


def lister_creneaux(db: Session, medecin: Medecin, debut: date, fin: date) -> list[Creneau]:
    """EF-10 : créneaux d'un médecin sur une période, avec leur état."""
    _verifier_periode(debut, fin)
    contexte = ContexteMedecins(db, [medecin], debut, fin, statuts_rdv=STATUTS_OCCUPANTS)
    resultat = []
    jour = debut
    while jour <= fin:
        resultat.extend(contexte.creneaux(medecin.id, jour))
        jour += timedelta(days=1)
    return resultat


def premiers_creneaux_libres(
    db: Session,
    *,
    specialite: str | None,
    medecin_id: str | None,
    date_debut: date | None,
    date_fin: date | None,
    patient_id: str | None = None,
    limite: int = 1,
) -> list[Creneau]:
    """EF-11 : les ``limite`` premiers créneaux libres, par ordre chronologique.

    Seuls les créneaux à venir sont proposés, dans la limite de 90 jours (RG-04). Avec ``patient_id``, les créneaux
    qui chevaucheraient un rendez-vous actif du patient sont écartés (RG-02).
    """
    maintenant_ = maintenant()
    premier = max(date_debut or aujourdhui(), aujourdhui())
    dernier = min(date_fin or (aujourdhui() + timedelta(days=HORIZON_RDV_JOURS)), aujourdhui() + timedelta(days=HORIZON_RDV_JOURS))
    if dernier < premier:
        return []
    _verifier_periode(premier, dernier)
    requete = select(Medecin).where(Medecin.actif.is_(True))
    if medecin_id:
        requete = requete.where(Medecin.id == medecin_id)
    if specialite:
        requete = requete.where(Medecin.specialite == specialite)
    medecins = list(db.scalars(requete.order_by(Medecin.id)).all())
    contexte = ContexteMedecins(db, medecins, premier, dernier, statuts_rdv=STATUTS_ACTIFS)
    occupe_patient: list[tuple[datetime, datetime]] = []
    if patient_id:
        bornes = (datetime.combine(premier, time.min), datetime.combine(dernier + timedelta(days=1), time.min))
        occupe_patient = [
            (r.debut, r.fin)
            for r in db.scalars(
                select(RendezVous).where(
                    RendezVous.patient_id == patient_id, RendezVous.statut.in_(STATUTS_ACTIFS),
                    RendezVous.debut < bornes[1], RendezVous.fin > bornes[0],
                )
            )
        ]  # fmt: skip
    trouves: list[Creneau] = []
    jour = premier
    while jour <= dernier and len(trouves) < limite:
        du_jour = []
        for medecin in medecins:
            for creneau in contexte.creneaux(medecin.id, jour):
                if creneau.etat != "libre" or creneau.debut <= maintenant_:
                    continue
                if any(d < creneau.fin and creneau.debut < f for d, f in occupe_patient):
                    continue
                du_jour.append(creneau)
        du_jour.sort(key=lambda c: (c.debut, c.medecin_id))
        trouves.extend(du_jour)
        jour += timedelta(days=1)
    return trouves[:limite]


# --------------------------------------------------------------------------------------
# RG-03 : un rendez-vous ne se prend que dans les plages ouvertes
# --------------------------------------------------------------------------------------


def verifier_ouverture(db: Session, medecin: Medecin, debut: datetime, fin: datetime) -> None:
    """RG-03 : lève RegleMetierError si le créneau demandé n'est pas ouvert à la réservation.

    Contrôles, dans cet ordre : jour férié ou fermé, plage du médecin et alignement sur la grille des créneaux,
    indisponibilité du médecin.
    """
    jour = debut.date()
    ferme = db.get(JourFerie, jour)
    if ferme is not None:
        raise RegleMetierError(f"La clinique est fermée le {jour:%d/%m/%Y} ({ferme.libelle}).", "JOUR_FERME")
    duree = int((fin - debut).total_seconds() // 60)
    plages = db.scalars(
        select(PlageConsultation).where(PlageConsultation.medecin_id == medecin.id, PlageConsultation.jour_semaine == jour.weekday())
    ).all()
    dans_une_plage = [p for p in plages if datetime.combine(jour, p.debut) <= debut and fin <= datetime.combine(jour, p.fin)]
    if not dans_une_plage:
        raise RegleMetierError(
            f"{medecin.nom} ne consulte pas le {JOURS_SEMAINE[jour.weekday()]} {jour:%d/%m/%Y} à {debut:%H:%M} "
            "(hors de ses plages de consultation).",
            "HORS_PLAGE",
        )
    if not any((debut, fin) in creneaux_de_plage(jour, p, duree) for p in dans_une_plage):
        raise RegleMetierError(
            f"L'heure {debut:%H:%M} ne correspond à aucun créneau de {medecin.nom} (créneaux de {duree} minutes "
            "à partir du début de la plage).",
            "CRENEAU_NON_ALIGNE",
        )
    indispo = db.scalars(
        select(Indisponibilite).where(
            Indisponibilite.medecin_id == medecin.id, Indisponibilite.active.is_(True),
            Indisponibilite.debut < fin, Indisponibilite.fin > debut,
        ).limit(1)
    ).first()  # fmt: skip
    if indispo is not None:
        raise RegleMetierError(
            f"{medecin.nom} est indisponible du {indispo.debut:%d/%m/%Y %H:%M} au {indispo.fin:%d/%m/%Y %H:%M} ({indispo.motif}).",
            "MEDECIN_INDISPONIBLE",
        )


# --------------------------------------------------------------------------------------
# EF-14 : rendez-vous impactés
# --------------------------------------------------------------------------------------


def rdv_a_reprogrammer(db: Session, medecin_id: str | None, debut: datetime, fin: datetime) -> list[RendezVous]:
    """Rendez-vous à venir (Planifié ou Confirmé) recoupant la période ; ``medecin_id`` None = tous les médecins."""
    criteres = [
        RendezVous.statut.in_((StatutRdv.PLANIFIE, StatutRdv.CONFIRME)),
        RendezVous.debut < fin,
        RendezVous.fin > debut,
        RendezVous.debut > maintenant(),
    ]
    if medecin_id:
        criteres.append(RendezVous.medecin_id == medecin_id)
    return list(db.scalars(select(RendezVous).where(and_(*criteres)).order_by(RendezVous.debut)).all())


# --------------------------------------------------------------------------------------
# RG-10 : taux d'occupation
# --------------------------------------------------------------------------------------


@dataclass
class OccupationMedecin:
    medecin_id: str
    medecin_nom: str
    specialite: str
    creneaux_ouverts: int
    creneaux_occupes: int

    @property
    def taux_pct(self) -> float | None:
        return round(100 * self.creneaux_occupes / self.creneaux_ouverts, 1) if self.creneaux_ouverts else None


def taux_occupation(
    db: Session, debut: date, fin: date, specialite: str | None = None, medecin_id: str | None = None
) -> list[OccupationMedecin]:
    """RG-10 : créneaux portant un rendez-vous honoré ou absent ÷ créneaux ouverts, par médecin.

    Les créneaux ouverts excluent les jours fériés/fermés, les hors-plages et les créneaux d'indisponibilité.
    """
    _verifier_periode(debut, fin)
    requete = select(Medecin)
    if medecin_id:
        requete = requete.where(Medecin.id == medecin_id)
    if specialite:
        requete = requete.where(Medecin.specialite == specialite)
    medecins = list(db.scalars(requete.order_by(Medecin.specialite, Medecin.nom)).all())
    contexte = ContexteMedecins(db, medecins, debut, fin, statuts_rdv=(StatutRdv.HONORE, StatutRdv.ABSENT))
    resultat = []
    for medecin in medecins:
        ouverts = occupes = 0
        jour = debut
        while jour <= fin:
            for creneau in contexte.creneaux(medecin.id, jour):
                if creneau.etat == "indisponible":
                    continue
                ouverts += 1
                occupes += creneau.etat == "occupé"
            jour += timedelta(days=1)
        resultat.append(OccupationMedecin(medecin.id, medecin.nom, medecin.specialite, ouverts, occupes))
    return resultat


# --------------------------------------------------------------------------------------
# RG-11 : historique, et EF-17 : absence automatique
# --------------------------------------------------------------------------------------


def historiser(
    db: Session, rdv_id: str, utilisateur: Utilisateur | None, action: str,
    champ: str | None = None, avant: str | None = None, apres: str | None = None,
) -> None:  # fmt: skip
    """Ajoute une ligne d'historique à la transaction en cours (``utilisateur`` None = système)."""
    db.add(
        HistoriqueRdv(
            rdv_id=rdv_id, utilisateur_id=utilisateur.id if utilisateur is not None else None,
            action=action, champ=champ, avant=avant, apres=apres,
        )
    )  # fmt: skip


def marquer_absences_automatiques(db: Session, instant: datetime | None = None) -> int:
    """EF-17 : passe en « Absent » les rendez-vous Planifié/Confirmé non marqués une heure après leur début.

    Seuls les rendez-vous des 7 derniers jours sont rattrapés ; les plus anciens restent visibles dans le rapport de
    qualité. L'opération est idempotente et peut tourner dans plusieurs processus sans conséquence.
    """
    instant = instant or maintenant()
    limite_haute = instant - timedelta(minutes=DELAI_ABSENCE_AUTO_MIN)
    limite_basse = instant - timedelta(days=ANCIENNETE_MAX_ABSENCE_AUTO_JOURS)
    en_retard = db.scalars(
        select(RendezVous).where(
            RendezVous.statut.in_((StatutRdv.PLANIFIE, StatutRdv.CONFIRME)),
            RendezVous.debut <= limite_haute, RendezVous.debut >= limite_basse,
        )
    ).all()  # fmt: skip
    for rdv in en_retard:
        ancien = rdv.statut
        rdv.statut = StatutRdv.ABSENT
        historiser(db, rdv.id, None, "ABSENCE_AUTO", "statut", ancien.value, StatutRdv.ABSENT.value)
        journaliser(db, None, "RDV_ABSENCE_AUTO", cible=rdv.id, detail=f"{ancien.value} -> Absent (1 h après le début)")
    db.commit()
    if en_retard:
        logger.info("%d rendez-vous passés automatiquement en « Absent ».", len(en_retard))
    return len(en_retard)
