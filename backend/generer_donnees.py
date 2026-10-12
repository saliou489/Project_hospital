"""Générateur de données synthétiques (cahier des charges, section 7.2).

Usage (depuis la racine du projet, API arrêtée) :

    python -m backend.generer_donnees --remplacer [--passes 6000] [--futurs 1500] [--patients 1000]
                                      [--graine 2026] [--date-reference AAAA-MM-JJ]

Ce que produit la commande, à partir des médecins et des agents déjà présents en base :
- 12 mois de rendez-vous passés (veille de la date de référence comprise) et 3 mois de rendez-vous à venir
  (90 jours, la limite de RG-04), 1 000 patients fictifs, aucun chevauchement (RG-01 / RG-02) ;
- des statuts cohérents avec la date : passé = honoré, absent ou annulé ; à venir = planifié ou confirmé ;
- des effets réalistes : pics le lundi et le samedi, hausse de juillet à septembre et en décembre, volume par
  spécialité proportionnel à celui du cahier des charges, taux d'absence d'environ 14 % qui varie selon le délai de
  prise, la confirmation, le canal, la spécialité et l'âge, avec des patients plus souvent absents que d'autres ;
- des patients cohérents : pédiatrie pour les moins de 15 ans, gynécologie pour les femmes de 15 ans et plus ;
- un résultat reproductible : la même graine redonne exactement les mêmes données.

Les paramètres statistiques ci-dessous ont été mesurés sur le CSV fourni (rendez_vous_medirdv_propre.csv) ; seule la
répartition par spécialité vient du cahier des charges (section 1.1). Toutes les personnes sont fictives.

Option ``--anomalies`` (désactivée par défaut) : injecte des anomalies volontaires et documentées (doublons de patients,
téléphones mal formés, valeurs manquantes, incohérences de dates et de statuts) pour tester les contrôles et le rapport
de qualité, comme le demande la section 7.2. Chaque anomalie injectée est listée dans un fichier CSV
(``anomalies_injectees.csv``) : type, identifiant concerné et détail.

Sécurité : si la base contient déjà des patients ou des rendez-vous, la commande refuse de s'exécuter sans
``--remplacer``. Avec cette option, les patients et les rendez-vous actuels sont supprimés ET les nouveaux écrits dans
UNE SEULE transaction : en cas d'erreur, rien n'est modifié. Médecins, agents, comptes, pointages et journal sont
conservés.
"""
import argparse
import bisect
import csv
import itertools
import logging
import math
import random
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select

from backend.database import RACINE_PROJET, SessionLocale, initialiser_base, transaction_ecriture
from backend.journal import journaliser
from backend.models import (
    Agent,
    Canal,
    HistoriqueRdv,
    JourFerie,
    Medecin,
    MotifAnnulation,
    Patient,
    RendezVous,
    Sexe,
    StatutRdv,
    aujourdhui,
    reculer_de_mois,
)
from backend.planning import initialiser_referentiels

logger = logging.getLogger("backend.generer_donnees")

# --------------------------------------------------------------------------------------
# Paramètres (mesurés sur le CSV fourni, sauf mention contraire)
# --------------------------------------------------------------------------------------

# Poids d'un couple (jour de la semaine, demi-journée) : lundi = 0 ... samedi = 5 ; pas de dimanche.
POIDS_JOUR_DEMI = {
    (0, "matin"): 246, (0, "apres-midi"): 99, (1, "matin"): 102, (1, "apres-midi"): 95,
    (2, "matin"): 82, (2, "apres-midi"): 86, (3, "matin"): 136, (3, "apres-midi"): 105,
    (4, "matin"): 137, (4, "apres-midi"): 119, (5, "matin"): 229, (5, "apres-midi"): 96,
}  # fmt: skip
# Plages de consultation observées dans le CSV (tous les rendez-vous s'y inscrivent).
FENETRES = {"matin": (time(8, 0), time(12, 0)), "apres-midi": (time(14, 0), time(18, 0))}
# Saisonnalité : volume du mois civil / volume mensuel moyen (juillet-septembre et décembre en hausse).
MULTIPLICATEUR_MOIS = {
    1: 0.95, 2: 0.89, 3: 0.91, 4: 0.82, 5: 0.92, 6: 0.84, 7: 1.18, 8: 1.21, 9: 1.21, 10: 1.02, 11: 0.86, 12: 1.20,
}  # fmt: skip
# Répartition annuelle des consultations par spécialité (cahier des charges, section 1.1).
POIDS_SPECIALITES = {
    "Médecine générale": 12500, "Pédiatrie": 7800, "Gynécologie-obstétrique": 6900, "Cardiologie": 4200,
    "Dermatologie": 3600, "Ophtalmologie": 4100, "ORL": 3000, "Endocrinologie": 3400, "Kinésithérapie": 2500,
}  # fmt: skip
# Délai entre la prise et le rendez-vous : (tranche en jours, probabilité).
TRANCHES_DELAI = [((0, 2), 0.294), ((3, 7), 0.298), ((8, 14), 0.223), ((15, 30), 0.151), ((31, 66), 0.035)]
TAUX_ANNULATION_PASSE = 0.097
POIDS_MOTIFS = {MotifAnnulation.PATIENT: 74, MotifAnnulation.MEDECIN: 33, MotifAnnulation.AUTRE: 24, MotifAnnulation.CLINIQUE: 18}
POIDS_CANAUX = {Canal.TELEPHONE: 606, Canal.GUICHET: 604, Canal.RECOMMANDATION: 307}
P_CONFIRME_PASSE = 0.552
P_CONFIRME_FUTUR = 0.398
HORIZON_MAX_JOURS = 90  # RG-04 : un rendez-vous ne se prend pas plus de 90 jours à l'avance

# Taux d'absence observés (en %) par facteur ; ils servent à calculer des écarts relatifs (log-cotes).
TAUX_ABSENCE_GLOBAL_OBSERVE = 14.4
TAUX_ABSENCE_CIBLE = 0.14  # le cahier des charges part d'environ 14 %
ABSENCE_CONFIRMATION = {True: 9.2, False: 20.8}
ABSENCE_DELAI = [11.1, 10.5, 15.8, 20.7, 39.1]  # une valeur par tranche de TRANCHES_DELAI
ABSENCE_CANAL = {Canal.TELEPHONE: 15.1, Canal.GUICHET: 16.0, Canal.RECOMMANDATION: 10.2}
ABSENCE_SPECIALITE = {
    "Cardiologie": 19.1, "Dermatologie": 16.0, "Endocrinologie": 16.5, "Gynécologie-obstétrique": 9.6,
    "Kinésithérapie": 17.7, "Médecine générale": 13.9, "ORL": 15.0, "Ophtalmologie": 17.0, "Pédiatrie": 12.5,
}  # fmt: skip
TRANCHES_AGE = [((0, 14), 0.229), ((15, 29), 0.314), ((30, 49), 0.277), ((50, 64), 0.089), ((65, 95), 0.089)]
ABSENCE_AGE = [14.3, 14.6, 14.4, 14.9, 12.8]  # une valeur par tranche de TRANCHES_AGE
ECART_TYPE_PATIENT = 0.8  # certains patients sont plus souvent absents que d'autres (historique d'absences)
ECART_TYPE_PROPENSION = 0.8  # certains patients consultent plus souvent que d'autres
PART_FEMININE = 0.60

NOMS = [
    "Ba", "Badji", "Bâ", "Camara", "Cissé", "Coly", "Dia", "Diallo", "Diatta", "Dieng", "Diop", "Diouf", "Fall",
    "Faye", "Gomis", "Gueye", "Kane", "Lo", "Ly", "Mbaye", "Mbengue", "Mendy", "Ndiaye", "Ndour", "Niang", "Sagna",
    "Samb", "Sarr", "Seck", "Sow", "Sy", "Sène", "Tall", "Thiam", "Touré", "Wade",
]  # fmt: skip
PRENOMS = {
    Sexe.FEMININ: [
        "Adama", "Aminata", "Astou", "Awa", "Aïssatou", "Binta", "Coumba", "Dieynaba", "Fatou", "Khady", "Maimouna",
        "Mariama", "Ndeye", "Oumou", "Rokhaya", "Seynabou", "Sokhna", "Yacine",
    ],
    Sexe.MASCULIN: [
        "Abdoulaye", "Aliou", "Alioune", "Babacar", "Cheikh", "Ibrahima", "Lamine", "Malick", "Mamadou", "Modou",
        "Moussa", "Omar", "Ousmane", "Pape", "Saliou", "Serigne", "Souleymane", "Youssou",
    ],
}  # fmt: skip
QUARTIERS = [
    "Fann", "Grand Dakar", "Guédiawaye", "HLM", "Keur Massar", "Liberté", "Mermoz", "Médina", "Ouakam",
    "Parcelles Assainies", "Pikine", "Plateau", "Point E", "Rufisque", "Sacré-Cœur", "Yoff",
]  # fmt: skip
POIDS_INDICATIFS = {"70": 424, "75": 326, "76": 348, "77": 369, "78": 393}

# Anomalies volontaires (option --anomalies) : nombre injecté par type.
ANOMALIES_PAR_DEFAUT = {
    "DOUBLON_PATIENT": 12,
    "TELEPHONE_INVALIDE": 15,
    "TELEPHONE_MANQUANT": 20,
    "NAISSANCE_MANQUANTE": 10,
    "NOM_MAL_FORME": 8,
    "PRISE_APRES_RDV": 6,
    "PRISE_DANS_LE_FUTUR": 4,
    "STATUT_FINAL_FUTUR": 5,
    "STATUT_OUVERT_PASSE": 5,
}
TELEPHONES_MAL_FORMES = ["771234", "7712345", "12345678", "991234567", "77 12345", "7712-3456", "abcdefghi", "003377"]
VARIANTES_DOUBLON = ["identique", "majuscules", "sans_accents", "espaces", "inverse", "minuscules"]
FICHIER_ANOMALIES_PAR_DEFAUT = RACINE_PROJET / "anomalies_injectees.csv"

SPECIALITE_PEDIATRIE = "Pédiatrie"
SPECIALITE_GYNECOLOGIE = "Gynécologie-obstétrique"


# --------------------------------------------------------------------------------------
# Structures de travail
# --------------------------------------------------------------------------------------


@dataclass
class MedecinInfo:
    identifiant: str
    specialite: str
    duree_min: int


@dataclass
class PatientBrouillon:
    nom: str
    prenom: str
    sexe: Sexe
    naissance: date
    telephone: str
    quartier: str
    propension: float
    effet_absence: float
    occupations: list = field(default_factory=list)  # intervalles (début, fin) des rendez-vous actifs
    nb_rdv: int = 0


@dataclass
class RdvBrouillon:
    medecin: MedecinInfo
    debut: datetime
    fin: datetime
    futur: bool
    annule: bool
    confirme: bool
    canal: Canal
    delai: int
    date_prise: date
    agent_id: str
    patient: int | None = None
    statut: StatutRdv | None = None
    motif: MotifAnnulation | None = None


@dataclass
class Resume:
    nb_patients: int
    nb_passes: int
    nb_futurs: int
    par_statut: dict
    taux_absence: float
    patients_alerte_rg08: int
    anomalies: list = field(default_factory=list)  # (type, identifiant, détail) des anomalies injectées


# --------------------------------------------------------------------------------------
# Outils
# --------------------------------------------------------------------------------------


def _logit(p: float) -> float:
    return math.log(p / (1 - p))


def _sigmoide(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def _effet(taux_pct: float) -> float:
    """Écart de log-cote d'un groupe par rapport au taux d'absence global observé."""
    return _logit(taux_pct / 100) - _logit(TAUX_ABSENCE_GLOBAL_OBSERVE / 100)


def _tirer(rng: random.Random, poids: dict):
    return rng.choices(list(poids), weights=list(poids.values()))[0]


def _age(naissance: date, jour: date) -> int:
    return jour.year - naissance.year - ((jour.month, jour.day) < (naissance.month, naissance.day))


def _indice_tranche(valeur: int, tranches: list) -> int:
    for i, ((a, b), _) in enumerate(tranches):
        if a <= valeur <= b:
            return i
    return len(tranches) - 1


_cache_creneaux: dict = {}


def _creneaux(duree: int, demi: str) -> list[time]:
    """Heures de début possibles d'un médecin dans une demi-journée (grille de la durée de consultation)."""
    cle = (duree, demi)
    if cle not in _cache_creneaux:
        debut, fin = FENETRES[demi]
        courant = datetime.combine(date.min, debut)
        limite = datetime.combine(date.min, fin)
        heures = []
        while courant + timedelta(minutes=duree) <= limite:
            heures.append(courant.time())
            courant += timedelta(minutes=duree)
        _cache_creneaux[cle] = heures
    return _cache_creneaux[cle]


def _calendrier(debut: date, fin: date, fermes: frozenset = frozenset()) -> tuple[list, list]:
    """Couples (jour, demi-journée) ouvrés entre deux dates (hors jours fermés), avec leurs poids cumulés."""
    jours, poids = [], []
    jour = debut
    while jour <= fin:
        if jour.weekday() < 6 and jour not in fermes:
            for demi in ("matin", "apres-midi"):
                jours.append((jour, demi))
                poids.append(POIDS_JOUR_DEMI[(jour.weekday(), demi)] * MULTIPLICATEUR_MOIS[jour.month])
        jour += timedelta(days=1)
    return jours, list(itertools.accumulate(poids))


def _tirer_jour(rng: random.Random, calendrier: tuple[list, list]) -> tuple[date, str]:
    jours, cumul = calendrier
    return jours[min(bisect.bisect_left(cumul, rng.random() * cumul[-1]), len(jours) - 1)]


# --------------------------------------------------------------------------------------
# Génération
# --------------------------------------------------------------------------------------


def generer_patients(rng: random.Random, nombre: int, reference: date) -> list[PatientBrouillon]:
    """Patients fictifs : identité unique (nom, prénom, naissance), téléphone sénégalais valide et unique."""
    patients: list[PatientBrouillon] = []
    identites, telephones = set(), set()
    poids_age = {i: p for i, (_, p) in enumerate(TRANCHES_AGE)}
    for _ in range(nombre):
        sexe = Sexe.FEMININ if rng.random() < PART_FEMININE else Sexe.MASCULIN
        (age_min, age_max), _ = TRANCHES_AGE[_tirer(rng, poids_age)]
        age = rng.randint(age_min, age_max)
        naissance = reference - timedelta(days=int(age * 365.25) + rng.randint(0, 364))
        while True:
            nom, prenom = rng.choice(NOMS), rng.choice(PRENOMS[sexe])
            if (nom, prenom, naissance) not in identites:
                identites.add((nom, prenom, naissance))
                break
        while True:
            telephone = _tirer(rng, POIDS_INDICATIFS) + f"{rng.randrange(10**7):07d}"
            if telephone not in telephones:
                telephones.add(telephone)
                break
        patients.append(
            PatientBrouillon(
                nom=nom,
                prenom=prenom,
                sexe=sexe,
                naissance=naissance,
                telephone=telephone,
                quartier=rng.choice(QUARTIERS),
                propension=rng.lognormvariate(0, ECART_TYPE_PROPENSION),
                effet_absence=rng.gauss(0, ECART_TYPE_PATIENT),
            )
        )
    return patients


def generer_rendez_vous(
    rng: random.Random, medecins: list[MedecinInfo], agents: list[str], reference: date, passes: int, futurs: int,
    fermes: frozenset = frozenset(),
) -> list[RdvBrouillon]:  # fmt: skip
    """Rendez-vous sans patient : médecin, créneau, délai de prise, canal et agent. Aucun créneau n'est utilisé deux fois."""
    par_specialite: dict[str, list[MedecinInfo]] = defaultdict(list)
    for medecin in medecins:
        par_specialite[medecin.specialite].append(medecin)
    moyenne = sum(POIDS_SPECIALITES.values()) / len(POIDS_SPECIALITES)
    poids_specialites = {s: POIDS_SPECIALITES.get(s, moyenne) for s in par_specialite}
    poids_tranches = {i: p for i, (_, p) in enumerate(TRANCHES_DELAI)}

    debut_passe = reculer_de_mois(datetime.combine(reference, time.min), 12).date()
    calendriers = {
        False: _calendrier(debut_passe, reference - timedelta(days=1), fermes),
        True: _calendrier(reference, reference + timedelta(days=HORIZON_MAX_JOURS), fermes),
    }
    occupes: set = set()  # (médecin, début) des rendez-vous actifs
    brouillons: list[RdvBrouillon] = []
    for futur, total in ((False, passes), (True, futurs)):
        for _ in range(total):
            annule = (not futur) and rng.random() < TAUX_ANNULATION_PASSE
            for _essai in range(500):
                medecin = rng.choice(par_specialite[_tirer(rng, poids_specialites)])
                jour, demi = _tirer_jour(rng, calendriers[futur])
                debut = datetime.combine(jour, rng.choice(_creneaux(medecin.duree_min, demi)))
                if annule or (medecin.identifiant, debut) not in occupes:
                    break
            else:
                raise RuntimeError("Impossible de trouver un créneau libre : volume demandé trop élevé.")
            if not annule:
                occupes.add((medecin.identifiant, debut))
            (jour_min, jour_max), _ = TRANCHES_DELAI[_tirer(rng, poids_tranches)]
            delai = rng.randint(jour_min, jour_max)
            if futur:  # la prise a eu lieu au plus tard aujourd'hui, et au plus 90 jours avant le rendez-vous
                delai = min((jour - reference).days + delai, HORIZON_MAX_JOURS)
            confirme = (not annule) and rng.random() < (P_CONFIRME_FUTUR if futur else P_CONFIRME_PASSE)
            brouillons.append(
                RdvBrouillon(
                    medecin=medecin,
                    debut=debut,
                    fin=debut + timedelta(minutes=medecin.duree_min),
                    futur=futur,
                    annule=annule,
                    confirme=confirme,
                    canal=_tirer(rng, POIDS_CANAUX),
                    delai=delai,
                    date_prise=jour - timedelta(days=delai),
                    agent_id=rng.choice(agents),
                    motif=_tirer(rng, POIDS_MOTIFS) if annule else None,
                )
            )
    return brouillons


def _eligible(patient: PatientBrouillon, rdv: RdvBrouillon) -> bool:
    """Le patient est né, a l'âge de la spécialité, et n'a pas déjà un rendez-vous actif au même moment."""
    jour = rdv.debut.date()
    if patient.naissance > jour:
        return False
    age = _age(patient.naissance, jour)
    if rdv.medecin.specialite == SPECIALITE_PEDIATRIE and age > 14:
        return False
    if rdv.medecin.specialite == SPECIALITE_GYNECOLOGIE and (patient.sexe != Sexe.FEMININ or age < 15):
        return False
    if not rdv.annule and any(rdv.debut < fin and debut < rdv.fin for debut, fin in patient.occupations):
        return False
    return True


def affecter_patients(rng: random.Random, rdvs: list[RdvBrouillon], patients: list[PatientBrouillon]) -> None:
    """Chaque patient reçoit au moins un rendez-vous, puis les autres vont aux patients qui consultent le plus."""

    def attribuer(indice_rdv: int, indice_patient: int) -> None:
        rdv, patient = rdvs[indice_rdv], patients[indice_patient]
        rdv.patient = indice_patient
        patient.nb_rdv += 1
        if not rdv.annule:
            patient.occupations.append((rdv.debut, rdv.fin))

    restants = list(range(len(rdvs)))
    ordre = list(range(len(patients)))
    rng.shuffle(ordre)
    for indice_patient in ordre:  # passe 1 : au moins un rendez-vous par patient
        for _ in range(400):
            position = rng.randrange(len(restants))
            if _eligible(patients[indice_patient], rdvs[restants[position]]):
                attribuer(restants[position], indice_patient)
                restants[position] = restants[-1]
                restants.pop()
                break
    cumul = list(itertools.accumulate(p.propension for p in patients))
    for indice_rdv in restants:  # passe 2 : le reste, selon la propension à consulter
        for _ in range(300):
            indice_patient = min(bisect.bisect_left(cumul, rng.random() * cumul[-1]), len(patients) - 1)
            if _eligible(patients[indice_patient], rdvs[indice_rdv]):
                attribuer(indice_rdv, indice_patient)
                break
        else:  # repli : premier patient éligible dans un ordre aléatoire
            candidats = list(range(len(patients)))
            rng.shuffle(candidats)
            indice_patient = next((i for i in candidats if _eligible(patients[i], rdvs[indice_rdv])), None)
            if indice_patient is None:
                raise RuntimeError("Aucun patient éligible pour un rendez-vous : réduisez le volume ou augmentez le nombre de patients.")
            attribuer(indice_rdv, indice_patient)


def determiner_statuts(rng: random.Random, rdvs: list[RdvBrouillon], patients: list[PatientBrouillon]) -> float:
    """Statuts selon la date ; probabilité d'absence = modèle logistique calibré sur le taux cible. Retourne le taux obtenu."""
    clos: list[tuple[RdvBrouillon, float]] = []
    for rdv in rdvs:
        if rdv.annule:
            rdv.statut = StatutRdv.ANNULE
        elif rdv.futur:
            rdv.statut = StatutRdv.CONFIRME if rdv.confirme else StatutRdv.PLANIFIE
        else:
            patient = patients[rdv.patient]
            tranche_delai = _indice_tranche(rdv.delai, TRANCHES_DELAI)
            tranche_age = _indice_tranche(_age(patient.naissance, rdv.debut.date()), TRANCHES_AGE)
            lineaire = (
                _effet(ABSENCE_DELAI[tranche_delai])
                + _effet(ABSENCE_CONFIRMATION[rdv.confirme])
                + _effet(ABSENCE_CANAL[rdv.canal])
                + _effet(ABSENCE_SPECIALITE.get(rdv.medecin.specialite, TAUX_ABSENCE_GLOBAL_OBSERVE))
                + _effet(ABSENCE_AGE[tranche_age])
                + patient.effet_absence
            )
            clos.append((rdv, lineaire))
    bas, haut = -12.0, 6.0  # recherche de l'ordonnée à l'origine qui donne le taux cible
    for _ in range(80):
        milieu = (bas + haut) / 2
        moyenne = sum(_sigmoide(milieu + l) for _, l in clos) / len(clos)
        bas, haut = (milieu, haut) if moyenne < TAUX_ABSENCE_CIBLE else (bas, milieu)
    origine = (bas + haut) / 2
    absents = 0
    for rdv, lineaire in clos:
        absent = rng.random() < _sigmoide(origine + lineaire)
        rdv.statut = StatutRdv.ABSENT if absent else StatutRdv.HONORE
        absents += absent
    return absents / len(clos)


# --------------------------------------------------------------------------------------
# Anomalies volontaires (option --anomalies)
# --------------------------------------------------------------------------------------


def _sans_accents(texte: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texte) if not unicodedata.combining(c))


def _alterer_nom(nom: str, prenom: str, variante: str) -> tuple[str, str]:
    """Variante de saisie d'une même identité."""
    if variante == "majuscules":
        return nom.upper(), prenom.upper()
    if variante == "minuscules":
        return nom.lower(), prenom.lower()
    if variante == "sans_accents":
        return _sans_accents(nom), _sans_accents(prenom)
    if variante == "espaces":
        return f"  {nom} ", f" {prenom}  "
    if variante == "inverse":
        return prenom, nom
    return nom, prenom


def injecter_anomalies(
    rng: random.Random, patients: list[Patient], rdvs: list[RendezVous], reference: date, quantites: dict | None = None
) -> list[tuple[str, str, str]]:
    """Altère des objets pas encore écrits en base et retourne la liste documentée des anomalies injectées.

    Chaque patient et chaque rendez-vous reçoit au plus une anomalie, pour que le rapport de qualité puisse être
    comparé exactement à cette liste.
    """
    nombre = {**ANOMALIES_PAR_DEFAUT, **(quantites or {})}
    journal: list[tuple[str, str, str]] = []
    par_patient: dict[str, list[RendezVous]] = defaultdict(list)
    for rdv in rdvs:
        par_patient[rdv.patient_id].append(rdv)
    originaux = list(patients)
    rng.shuffle(originaux)
    utilises: set[str] = set()
    telephones = {p.telephone for p in patients if p.telephone}

    # 1. Doublons : une copie de l'identité (variante de saisie) qui reprend un ou deux rendez-vous de la fiche d'origine.
    candidats = [p for p in originaux if sum(1 for r in par_patient[p.id] if r.statut != StatutRdv.ANNULE) >= 2]
    for numero, original in enumerate(candidats[: nombre["DOUBLON_PATIENT"]]):
        utilises.add(original.id)
        variante = VARIANTES_DOUBLON[numero % len(VARIANTES_DOUBLON)]
        nom, prenom = _alterer_nom(original.nom, original.prenom, variante)
        while True:
            telephone = _tirer(rng, POIDS_INDICATIFS) + f"{rng.randrange(10**7):07d}"
            if telephone not in telephones:
                telephones.add(telephone)
                break
        copie = Patient(
            id=f"PAT{len(patients) + 1:05d}", nom=nom, prenom=prenom, sexe=original.sexe,
            date_naissance=original.date_naissance, telephone=telephone, quartier=original.quartier,
            cree_le=original.cree_le,
        )  # fmt: skip
        patients.append(copie)
        utilises.add(copie.id)
        deplaçables = sorted((r for r in par_patient[original.id] if r.statut != StatutRdv.ANNULE), key=lambda r: r.debut)
        a_deplacer = deplaçables[-min(2, len(deplaçables) - 1):]
        for rdv in a_deplacer:
            rdv.patient_id = copie.id
        journal.append(("DOUBLON_PATIENT", copie.id, f"doublon de {original.id} ; variante={variante} ; rendez-vous déplacés={len(a_deplacer)}"))

    # 2. Anomalies sur les fiches (chaque fiche d'origine non utilisée reçoit au plus une anomalie).
    libres = [p for p in originaux if p.id not in utilises]
    for type_anomalie in ("TELEPHONE_INVALIDE", "TELEPHONE_MANQUANT", "NAISSANCE_MANQUANTE", "NOM_MAL_FORME"):
        for _ in range(nombre[type_anomalie]):
            patient = libres.pop()
            if type_anomalie == "TELEPHONE_INVALIDE":
                patient.telephone = rng.choice(TELEPHONES_MAL_FORMES)
                detail = f"téléphone « {patient.telephone} »"
            elif type_anomalie == "TELEPHONE_MANQUANT":
                patient.telephone, detail = None, "téléphone supprimé"
            elif type_anomalie == "NAISSANCE_MANQUANTE":
                patient.date_naissance, detail = None, "date de naissance supprimée"
            else:
                variante = rng.choice(["majuscules", "minuscules", "espaces"])
                patient.nom, patient.prenom = _alterer_nom(patient.nom, patient.prenom, variante)
                detail = f"variante={variante}"
            journal.append((type_anomalie, patient.id, detail))

    # 3. Anomalies sur les rendez-vous.
    passes = [r for r in rdvs if r.debut.date() < reference]
    futurs = [r for r in rdvs if r.debut.date() >= reference]
    rng.shuffle(passes)
    rng.shuffle(futurs)
    anciens = [r for r in passes if r.debut.date() <= reference - timedelta(days=15)]
    for _ in range(nombre["PRISE_APRES_RDV"]):
        rdv = anciens.pop()
        passes.remove(rdv)
        rdv.date_prise = rdv.debut.date() + timedelta(days=3)
        journal.append(("PRISE_APRES_RDV", rdv.id, f"date de prise {rdv.date_prise} postérieure au rendez-vous"))
    lointains = [r for r in futurs if r.debut.date() >= reference + timedelta(days=10)]
    for _ in range(nombre["PRISE_DANS_LE_FUTUR"]):
        rdv = lointains.pop()
        futurs.remove(rdv)
        rdv.date_prise = reference + timedelta(days=2)
        journal.append(("PRISE_DANS_LE_FUTUR", rdv.id, f"date de prise {rdv.date_prise} dans le futur"))
    a_venir = [r for r in futurs if r.debut.date() > reference]
    for _ in range(nombre["STATUT_FINAL_FUTUR"]):
        rdv = a_venir.pop()
        futurs.remove(rdv)
        rdv.statut = StatutRdv.HONORE
        journal.append(("STATUT_FINAL_FUTUR", rdv.id, f"« Honoré » pour un rendez-vous du {rdv.debut.date()}"))
    clos = [r for r in passes if r.statut in (StatutRdv.HONORE, StatutRdv.ABSENT) and r.debut.date() <= reference - timedelta(days=2)]
    for _ in range(nombre["STATUT_OUVERT_PASSE"]):
        rdv = clos.pop()
        rdv.statut = StatutRdv.PLANIFIE
        journal.append(("STATUT_OUVERT_PASSE", rdv.id, f"« Planifié » pour un rendez-vous du {rdv.debut.date()}"))
    return journal


def ecrire_fichier_anomalies(anomalies: list[tuple[str, str, str]], chemin: Path) -> None:
    with open(chemin, "w", newline="", encoding="utf-8-sig") as fichier:
        ecrivain = csv.writer(fichier)
        ecrivain.writerow(["type", "identifiant", "detail"])
        ecrivain.writerows(anomalies)


# --------------------------------------------------------------------------------------
# Écriture en base
# --------------------------------------------------------------------------------------


def _lire_referentiels(db) -> tuple[list[MedecinInfo], list[str]]:
    medecins = [
        MedecinInfo(m.id, m.specialite, m.duree_consultation_min)
        for m in db.scalars(select(Medecin).where(Medecin.actif.is_(True)).order_by(Medecin.id)).all()
    ]
    agents = list(db.scalars(select(Agent.id).where(Agent.actif.is_(True)).order_by(Agent.id)).all())
    return medecins, agents


def generer(
    db, *, passes: int, futurs: int, nb_patients: int, graine: int, reference: date, remplacer: bool, anomalies: bool = False
) -> Resume:
    """Génère les données puis les écrit en une seule transaction."""
    medecins, agents = _lire_referentiels(db)
    if not medecins or not agents:
        raise ValueError(
            "Aucun médecin ou agent actif en base : importez d'abord les données avec « python -m backend.seed_data »."
        )
    initialiser_referentiels(db, reference)  # plages par défaut et jours fériés : aucun rendez-vous un jour fermé
    fermes = frozenset(db.scalars(select(JourFerie.jour)).all())
    existants = (db.scalar(select(func.count()).select_from(Patient)) or 0) + (
        db.scalar(select(func.count()).select_from(RendezVous)) or 0
    )
    if existants and not remplacer:
        raise ValueError(
            "La base contient déjà des patients ou des rendez-vous. Relancez avec --remplacer pour les remplacer par "
            "les données générées (médecins, agents et comptes sont conservés)."
        )

    if anomalies and (nb_patients < 200 or passes < 1000):
        raise ValueError(
            "L'injection d'anomalies exige au moins 200 patients et 1 000 rendez-vous passés "
            "(chaque type d'anomalie touche des fiches distinctes)."
        )
    rng = random.Random(graine)
    patients = generer_patients(rng, nb_patients, reference)
    rdvs = generer_rendez_vous(rng, medecins, agents, reference, passes, futurs, fermes)
    affecter_patients(rng, rdvs, patients)
    taux = determiner_statuts(rng, rdvs, patients)
    rdvs.sort(key=lambda r: (r.debut, r.medecin.identifiant))

    # Patients ayant au moins 3 absences dans les 6 derniers mois (alerte RG-08).
    limite = reculer_de_mois(datetime.combine(reference, time.min), 6)
    absences = Counter(r.patient for r in rdvs if r.statut == StatutRdv.ABSENT and r.debut >= limite)

    premiere_prise: dict[int, date] = {}
    for r in rdvs:
        premiere_prise[r.patient] = min(premiere_prise.get(r.patient, r.date_prise), r.date_prise)
    objets_patients = [
        Patient(
            id=f"PAT{i + 1:05d}", nom=p.nom, prenom=p.prenom, sexe=p.sexe, date_naissance=p.naissance,
            telephone=p.telephone, quartier=p.quartier, cree_le=datetime.combine(premiere_prise[i], time.min),
        )
        for i, p in enumerate(patients)
    ]  # fmt: skip
    objets_rdv = [
        RendezVous(
            id=f"RDV{n + 1:05d}", patient_id=f"PAT{r.patient + 1:05d}", medecin_id=r.medecin.identifiant,
            agent_id=r.agent_id, date_prise=r.date_prise, debut=r.debut, fin=r.fin, statut=r.statut,
            confirme=r.confirme, canal=r.canal, motif_annulation=r.motif,
            cree_le=datetime.combine(r.date_prise, time.min), modifie_le=datetime.combine(r.date_prise, time.min),
        )
        for n, r in enumerate(rdvs)
    ]  # fmt: skip
    anomalies_injectees = injecter_anomalies(rng, objets_patients, objets_rdv, reference) if anomalies else []

    with transaction_ecriture(db):
        if remplacer:
            db.execute(delete(HistoriqueRdv))
            db.execute(delete(RendezVous))
            db.execute(delete(Patient))
        db.add_all(objets_patients)
        db.flush()
        db.add_all(objets_rdv)
        detail = f"{passes} passés, {futurs} à venir, {nb_patients} patients, graine {graine}, référence {reference}"
        if anomalies:
            detail += f", {len(anomalies_injectees)} anomalies injectées"
        journaliser(db, None, "DONNEES_GENEREES", detail=detail)
    return Resume(
        nb_patients=len(objets_patients),
        nb_passes=sum(1 for r in rdvs if not r.futur),
        nb_futurs=sum(1 for r in rdvs if r.futur),
        par_statut=dict(Counter(r.statut.value for r in rdvs)),
        taux_absence=taux,
        patients_alerte_rg08=sum(1 for n in absences.values() if n >= 3),
        anomalies=anomalies_injectees,
    )


def main(argv: list[str] | None = None) -> int:
    analyseur = argparse.ArgumentParser(description="Génère des rendez-vous et des patients fictifs (cahier des charges 7.2).")
    analyseur.add_argument("--passes", type=int, default=6000, help="rendez-vous des 12 derniers mois (défaut : 6000)")
    analyseur.add_argument("--futurs", type=int, default=1500, help="rendez-vous des 3 prochains mois (défaut : 1500)")
    analyseur.add_argument("--patients", type=int, default=1000, help="nombre de patients fictifs (défaut : 1000)")
    analyseur.add_argument("--graine", type=int, default=2026, help="graine aléatoire : même graine, mêmes données")
    analyseur.add_argument("--date-reference", type=date.fromisoformat, default=None, help="date du jour simulée (défaut : aujourd'hui)")
    analyseur.add_argument("--remplacer", action="store_true", help="supprime les patients et rendez-vous actuels avant d'écrire")
    analyseur.add_argument("--anomalies", action="store_true", help="injecte des anomalies volontaires et documentées (tests du rapport de qualité)")
    analyseur.add_argument("--fichier-anomalies", type=Path, default=FICHIER_ANOMALIES_PAR_DEFAUT, help="fichier CSV listant les anomalies injectées")
    arguments = analyseur.parse_args(argv)
    if min(arguments.passes, arguments.futurs, arguments.patients) < 1 or arguments.futurs > arguments.passes:
        print("Les volumes doivent être positifs, avec plus de rendez-vous passés que de rendez-vous à venir.", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s : %(message)s")
    reference = arguments.date_reference or aujourdhui()
    try:
        initialiser_base()
        with SessionLocale() as db:
            resume = generer(
                db, passes=arguments.passes, futurs=arguments.futurs, nb_patients=arguments.patients,
                graine=arguments.graine, reference=reference, remplacer=arguments.remplacer, anomalies=arguments.anomalies,
            )  # fmt: skip
    except ValueError as erreur:
        logger.error("Génération interrompue : %s", erreur)
        return 2
    except Exception:  # noqa: BLE001 - dernier filet : trace complète dans le journal, rien n'est modifié
        logger.exception("Erreur inattendue ; la transaction a été annulée, aucune donnée n'a été modifiée.")
        return 1
    logger.info("Date de référence : %s (graine %d)", reference, arguments.graine)
    logger.info("%d patients, %d rendez-vous passés, %d rendez-vous à venir", resume.nb_patients, resume.nb_passes, resume.nb_futurs)
    for statut, nombre in sorted(resume.par_statut.items()):
        logger.info("  %-10s %d", statut, nombre)
    logger.info("Taux d'absence (RG-09) : %.1f %% | patients avec alerte RG-08 : %d", 100 * resume.taux_absence, resume.patients_alerte_rg08)
    if resume.anomalies:
        ecrire_fichier_anomalies(resume.anomalies, arguments.fichier_anomalies)
        logger.warning("%d anomalies volontaires injectées, listées dans %s", len(resume.anomalies), arguments.fichier_anomalies)
    return 0


if __name__ == "__main__":
    sys.exit(main())
