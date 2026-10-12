"""Importation sécurisée du fichier ``rendez_vous_medirdv_propre.csv``.

Usage (depuis la racine du projet) :

    python -m backend.seed_data [--csv CHEMIN] [--rapport CHEMIN] [--reinitialiser]

Principes :
- aucune valeur n'est inventée : une ligne qui ne peut pas être importée telle quelle (date de
  rendez-vous absente, durée hors des valeurs autorisées, motif d'annulation manquant, etc.) est
  REJETÉE et listée dans un rapport CSV, pour être corrigée à la source ;
- les normalisations sans perte de sens (espaces, casse des noms, graphie du sexe, format de
  l'heure) sont appliquées et comptées dans le compte rendu ;
- les règles RG-01 et RG-02 (aucun chevauchement pour un médecin ou un patient) et RG-05 (motif
  obligatoire pour une annulation) s'appliquent à l'import comme à la saisie : en cas de conflit,
  le rendez-vous pris le plus tôt est conservé (à égalité, le plus petit identifiant) ;
- la règle « pas de rendez-vous dans le passé » ne s'applique pas ici : il s'agit d'un historique ;
- l'écriture en base se fait en une seule transaction (tout ou rien) ;
- l'import refuse de s'exécuter sur une base déjà remplie, sauf avec --reinitialiser.
"""
import argparse
import csv
import logging
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import func, select, text

from backend.database import (
    RACINE_PROJET,
    Base,
    SessionLocale,
    engine,
    initialiser_base,
    transaction_ecriture,
)
from backend.planning import initialiser_referentiels
from backend.models import (
    DUREES_AUTORISEES_MIN,
    PATRON_ID_AGENT,
    PATRON_ID_MEDECIN,
    PATRON_ID_PATIENT,
    PATRON_ID_RDV,
    STATUTS_ACTIFS,
    Agent,
    Canal,
    Medecin,
    MotifAnnulation,
    Patient,
    RendezVous,
    Sexe,
    StatutRdv,
    aujourdhui,
)
from backend.schemas import normaliser_nom, valider_telephone

logger = logging.getLogger("backend.seed_data")

CSV_PAR_DEFAUT = RACINE_PROJET / "data" / "rendez_vous_medirdv_propre.csv"
RAPPORT_PAR_DEFAUT = RACINE_PROJET / "rapport_import_rejets.csv"

COLONNES_ATTENDUES = (
    "id_rdv", "date_prise", "date_rdv", "heure_rdv", "duree_min", "specialite", "medecin_id",
    "medecin_nom", "patient_id", "patient_nom", "patient_prenom", "sexe", "date_naissance",
    "telephone", "quartier", "canal", "confirme", "statut", "motif_annulation", "agent_saisie",
)  # fmt: skip

# Graphies du sexe rencontrées dans le fichier source (comparaison sans tenir compte de la casse).
CORRESPONDANCE_SEXE = {
    "f": Sexe.FEMININ,
    "femme": Sexe.FEMININ,
    "m": Sexe.MASCULIN,
    "h": Sexe.MASCULIN,
    "homme": Sexe.MASCULIN,
}

MOTIF_DATE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MOTIF_HEURE = re.compile(r"^(\d{1,2})\s*[:hH]\s*(\d{2})$")
MOTIF_HEURE_STANDARD = re.compile(r"^\d{2}:\d{2}$")


class ErreurImport(Exception):
    """Erreur bloquante : l'import s'arrête sans rien écrire en base."""


class RejetLigne(Exception):
    """Ligne du CSV qui ne peut pas être importée (code stable + explication)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass
class LigneRdv:
    """Ligne du CSV nettoyée et validée."""

    id_rdv: str
    date_prise: date
    debut: datetime
    duree_min: int
    specialite: str
    medecin_id: str
    medecin_nom: str
    patient_id: str
    nom: str
    prenom: str
    sexe: Sexe
    date_naissance: date | None
    telephone: str | None
    quartier: str | None
    canal: Canal | None
    confirme: bool | None
    statut: StatutRdv
    motif: MotifAnnulation | None
    agent_id: str
    corrections: Counter = field(default_factory=Counter)

    @property
    def fin(self) -> datetime:
        return self.debut + timedelta(minutes=self.duree_min)


@dataclass
class Rejet:
    """Ligne rejetée, telle qu'écrite dans le rapport."""

    ligne_csv: int
    id_rdv: str
    code: str
    detail: str


@dataclass
class ResultatAnalyse:
    """Résultat de l'analyse du fichier, avant toute écriture en base."""

    lignes_lues: int
    acceptees: list[LigneRdv]
    rejets: list[Rejet]
    corrections: Counter
    avertissements: Counter
    medecins: dict[str, dict]
    patients: dict[str, dict]
    agents: list[str]


# --------------------------------------------------------------------------------------
# Lecture et nettoyage ligne par ligne
# --------------------------------------------------------------------------------------


def _texte(brut: dict, cle: str) -> str:
    """Valeur d'une colonne : forme Unicode normalisée (NFC), espaces de début et de fin retirés."""
    return unicodedata.normalize("NFC", brut.get(cle) or "").strip()


def _lire_date_iso(valeur: str) -> date | None:
    if not MOTIF_DATE_ISO.fullmatch(valeur):
        return None
    try:
        return date.fromisoformat(valeur)
    except ValueError:
        return None


def _lire_heure(valeur: str) -> time | None:
    correspondance = MOTIF_HEURE.fullmatch(valeur)
    if not correspondance:
        return None
    heures, minutes = int(correspondance.group(1)), int(correspondance.group(2))
    if heures > 23 or minutes > 59:
        return None
    return time(heures, minutes)


def nettoyer_ligne(brut: dict) -> LigneRdv:
    """Nettoie et valide une ligne ; lève RejetLigne si elle ne peut pas être importée telle quelle."""
    corrections: Counter = Counter()

    id_rdv = _texte(brut, "id_rdv")
    if not re.fullmatch(PATRON_ID_RDV, id_rdv):
        raise RejetLigne("ID_RDV_INVALIDE", f"Identifiant de rendez-vous invalide : « {id_rdv} ».")
    for cle, patron in (
        ("patient_id", PATRON_ID_PATIENT),
        ("medecin_id", PATRON_ID_MEDECIN),
        ("agent_saisie", PATRON_ID_AGENT),
    ):
        if not re.fullmatch(patron, _texte(brut, cle)):
            raise RejetLigne("ID_REFERENCE_INVALIDE", f"Valeur invalide pour « {cle} » : « {_texte(brut, cle)} ».")

    date_rdv = _lire_date_iso(_texte(brut, "date_rdv"))
    if date_rdv is None:
        raise RejetLigne("DATE_RDV_ABSENTE_OU_INVALIDE", "La date du rendez-vous est absente ou illisible.")
    date_prise = _lire_date_iso(_texte(brut, "date_prise"))
    if date_prise is None:
        raise RejetLigne("DATE_PRISE_ABSENTE_OU_INVALIDE", "La date de prise est absente ou illisible.")
    if date_prise > date_rdv:
        raise RejetLigne(
            "DATE_PRISE_POSTERIEURE",
            f"La date de prise ({date_prise}) est postérieure à la date du rendez-vous ({date_rdv}).",
        )

    heure_brute = _texte(brut, "heure_rdv")
    heure = _lire_heure(heure_brute)
    if heure is None:
        raise RejetLigne("HEURE_INVALIDE", f"Heure illisible : « {heure_brute} ».")
    if not MOTIF_HEURE_STANDARD.fullmatch(heure_brute):
        corrections["heure_reformatee"] += 1

    duree_texte = _texte(brut, "duree_min")
    try:
        duree = int(duree_texte)
    except ValueError:
        duree = None
    if duree not in DUREES_AUTORISEES_MIN:
        raise RejetLigne(
            "DUREE_INVALIDE",
            f"Durée « {duree_texte} » minutes hors des valeurs autorisées {DUREES_AUTORISEES_MIN}.",
        )

    specialite = _texte(brut, "specialite")
    medecin_nom = _texte(brut, "medecin_nom")
    if not specialite or not medecin_nom:
        raise RejetLigne("MEDECIN_INCOMPLET", "Le nom du médecin ou sa spécialité est absent.")

    statut_texte = _texte(brut, "statut")
    try:
        statut = StatutRdv(statut_texte)
    except ValueError:
        raise RejetLigne("STATUT_INVALIDE", f"Statut inconnu : « {statut_texte} ».") from None

    motif_texte = _texte(brut, "motif_annulation")
    motif = None
    if motif_texte:
        try:
            motif = MotifAnnulation(motif_texte)
        except ValueError:
            raise RejetLigne("MOTIF_INVALIDE", f"Motif d'annulation inconnu : « {motif_texte} ».") from None
    # RG-05 : le motif existe si et seulement si le rendez-vous est annulé.
    if statut == StatutRdv.ANNULE and motif is None:
        raise RejetLigne("MOTIF_ANNULATION_MANQUANT", "Rendez-vous annulé sans motif d'annulation (RG-05).")
    if statut != StatutRdv.ANNULE and motif is not None:
        raise RejetLigne("MOTIF_SANS_ANNULATION", "Motif d'annulation renseigné pour un rendez-vous non annulé.")

    try:
        nom = normaliser_nom(brut.get("patient_nom") or "")
        prenom = normaliser_nom(brut.get("patient_prenom") or "")
    except ValueError as exc:
        raise RejetLigne("NOM_INVALIDE", f"Nom ou prénom du patient invalide : {exc}.") from None
    if nom != _texte(brut, "patient_nom") or prenom != _texte(brut, "patient_prenom"):
        corrections["nom_normalise"] += 1

    sexe = CORRESPONDANCE_SEXE.get(_texte(brut, "sexe").casefold())
    if sexe is None:
        raise RejetLigne("SEXE_INVALIDE", f"Sexe non reconnu : « {_texte(brut, 'sexe')} ».")
    if sexe.value != (brut.get("sexe") or ""):
        corrections["sexe_normalise"] += 1

    # Champs facultatifs : une valeur absente ou invalide devient « non renseigné » (jamais inventée).
    date_naissance = None
    naissance_brute = _texte(brut, "date_naissance")
    if naissance_brute:
        date_naissance = _lire_date_iso(naissance_brute)
        if date_naissance is None or date_naissance > date_rdv:
            corrections["naissance_invalide_ignoree"] += 1
            date_naissance = None

    telephone = None
    telephone_brut = _texte(brut, "telephone")
    if telephone_brut:
        try:
            telephone = valider_telephone(telephone_brut)
        except ValueError:
            corrections["telephone_invalide_ignore"] += 1

    canal = None
    canal_brut = _texte(brut, "canal")
    if canal_brut:
        try:
            canal = Canal(canal_brut)
        except ValueError:
            corrections["canal_inconnu_ignore"] += 1

    confirme_brut = _texte(brut, "confirme").casefold()
    confirme = {"oui": True, "non": False}.get(confirme_brut)
    if confirme_brut and confirme is None:
        corrections["confirme_inconnu_ignore"] += 1

    return LigneRdv(
        id_rdv=id_rdv,
        date_prise=date_prise,
        debut=datetime.combine(date_rdv, heure),
        duree_min=duree,
        specialite=specialite,
        medecin_id=_texte(brut, "medecin_id"),
        medecin_nom=medecin_nom,
        patient_id=_texte(brut, "patient_id"),
        nom=nom,
        prenom=prenom,
        sexe=sexe,
        date_naissance=date_naissance,
        telephone=telephone,
        quartier=_texte(brut, "quartier") or None,
        canal=canal,
        confirme=confirme,
        statut=statut,
        motif=motif,
        agent_id=_texte(brut, "agent_saisie"),
        corrections=corrections,
    )


def lire_csv(chemin: Path) -> list[dict]:
    """Lit le CSV (UTF-8, avec ou sans BOM) et vérifie la présence de toutes les colonnes attendues."""
    if not chemin.is_file():
        raise ErreurImport(f"Fichier introuvable : {chemin}")
    with open(chemin, newline="", encoding="utf-8-sig") as fichier:
        lecteur = csv.DictReader(fichier)
        manquantes = [c for c in COLONNES_ATTENDUES if c not in (lecteur.fieldnames or [])]
        if manquantes:
            raise ErreurImport(f"Colonnes manquantes dans le fichier : {', '.join(manquantes)}.")
        return list(lecteur)


# --------------------------------------------------------------------------------------
# Analyse de l'ensemble du fichier
# --------------------------------------------------------------------------------------


def _chercher_chevauchement(occupations: list, debut: datetime, fin: datetime) -> str | None:
    """Retourne l'identifiant du rendez-vous déjà retenu qui chevauche [debut, fin[, sinon None."""
    for debut_existant, fin_existante, identifiant in occupations:
        if debut < fin_existante and debut_existant < fin:
            return identifiant
    return None


def analyser(lignes_brutes: list[dict]) -> ResultatAnalyse:
    """Nettoie, contrôle et consolide toutes les lignes ; n'écrit rien en base."""
    rejets: list[Rejet] = []

    # 1. Nettoyage ligne par ligne (la ligne 1 du fichier est l'en-tête).
    nettoyees: list[tuple[int, LigneRdv]] = []
    for numero, brut in enumerate(lignes_brutes, start=2):
        try:
            nettoyees.append((numero, nettoyer_ligne(brut)))
        except RejetLigne as rejet:
            rejets.append(Rejet(numero, _texte(brut, "id_rdv"), rejet.code, rejet.detail))

    # 2. Identifiants de rendez-vous en double : seule la première occurrence est conservée.
    premiere_ligne: dict[str, int] = {}
    sans_doublon: list[tuple[int, LigneRdv]] = []
    for numero, ligne in nettoyees:
        if ligne.id_rdv in premiere_ligne:
            rejets.append(
                Rejet(numero, ligne.id_rdv, "ID_RDV_DOUBLON",
                      f"Identifiant déjà présent à la ligne {premiere_ligne[ligne.id_rdv]}.")
            )  # fmt: skip
        else:
            premiere_ligne[ligne.id_rdv] = numero
            sans_doublon.append((numero, ligne))

    # 3. Cohérence des médecins : un identifiant = un nom et une spécialité (valeur majoritaire).
    variantes: dict[str, Counter] = defaultdict(Counter)
    for _, ligne in sans_doublon:
        variantes[ligne.medecin_id][(ligne.medecin_nom, ligne.specialite)] += 1
    reference_medecin = {mid: c.most_common(1)[0][0] for mid, c in variantes.items()}
    coherentes: list[tuple[int, LigneRdv]] = []
    for numero, ligne in sans_doublon:
        if (ligne.medecin_nom, ligne.specialite) != reference_medecin[ligne.medecin_id]:
            rejets.append(
                Rejet(numero, ligne.id_rdv, "MEDECIN_INCOHERENT",
                      f"Nom ou spécialité différent de la valeur majoritaire pour {ligne.medecin_id}.")
            )  # fmt: skip
        else:
            coherentes.append((numero, ligne))

    # 4. RG-01 / RG-02 : aucun chevauchement entre rendez-vous actifs d'un médecin ou d'un patient.
    actives = sorted(
        (item for item in coherentes if item[1].statut in STATUTS_ACTIFS),
        key=lambda item: (item[1].date_prise, item[1].id_rdv),
    )
    occupe_medecin: dict[str, list] = defaultdict(list)
    occupe_patient: dict[str, list] = defaultdict(list)
    ids_en_conflit: set[str] = set()
    for numero, ligne in actives:
        conflit = _chercher_chevauchement(occupe_medecin[ligne.medecin_id], ligne.debut, ligne.fin)
        code = "CONFLIT_MEDECIN"
        if conflit is None:
            conflit = _chercher_chevauchement(occupe_patient[ligne.patient_id], ligne.debut, ligne.fin)
            code = "CONFLIT_PATIENT"
        if conflit is not None:
            ids_en_conflit.add(ligne.id_rdv)
            rejets.append(
                Rejet(numero, ligne.id_rdv, code,
                      f"Chevauche le rendez-vous {conflit}, conservé car pris plus tôt (RG-01 / RG-02).")
            )  # fmt: skip
        else:
            occupation = (ligne.debut, ligne.fin, ligne.id_rdv)
            occupe_medecin[ligne.medecin_id].append(occupation)
            occupe_patient[ligne.patient_id].append(occupation)
    acceptees = [ligne for _, ligne in coherentes if ligne.id_rdv not in ids_en_conflit]
    acceptees.sort(key=lambda ligne: ligne.id_rdv)

    # 5. Corrections comptées uniquement pour les lignes réellement importées.
    corrections: Counter = Counter()
    for ligne in acceptees:
        corrections.update(ligne.corrections)

    # 6. Médecins : durée de consultation = durée la plus fréquente de ses rendez-vous importés.
    medecins: dict[str, dict] = {}
    for ligne in acceptees:
        medecins.setdefault(
            ligne.medecin_id,
            {"nom": ligne.medecin_nom, "specialite": ligne.specialite, "durees": Counter()},
        )["durees"][ligne.duree_min] += 1
    for donnees in medecins.values():
        donnees["duree_consultation_min"] = donnees.pop("durees").most_common(1)[0][0]

    # 7. Patients : valeur majoritaire de chaque champ parmi les rendez-vous importés.
    avertissements: Counter = Counter()
    par_patient: dict[str, list[LigneRdv]] = defaultdict(list)
    for ligne in acceptees:
        par_patient[ligne.patient_id].append(ligne)
    patients: dict[str, dict] = {}
    for patient_id, lignes in par_patient.items():
        fiche: dict = {}
        for champ in ("nom", "prenom", "sexe", "date_naissance", "telephone", "quartier"):
            valeurs = Counter(getattr(l, champ) for l in lignes if getattr(l, champ) is not None)
            fiche[champ] = valeurs.most_common(1)[0][0] if valeurs else None
            if len(valeurs) > 1:
                avertissements[f"patient_{champ}_valeurs_divergentes"] += 1
        patients[patient_id] = fiche

    identites: dict[tuple, set] = defaultdict(set)
    for patient_id, fiche in patients.items():
        if fiche["date_naissance"] is not None:
            identites[(fiche["nom"].casefold(), fiche["prenom"].casefold(), fiche["date_naissance"])].add(patient_id)
    avertissements["patients_doublons_potentiels"] = sum(1 for ids in identites.values() if len(ids) > 1)

    # 8. Constats temporels (informatifs, calculés à la date de l'import ; rien n'est rejeté).
    aujourd_hui = aujourdhui()
    for ligne in acceptees:
        if ligne.statut in (StatutRdv.HONORE, StatutRdv.ABSENT) and ligne.debut.date() > aujourd_hui:
            avertissements["statut_final_pour_un_rdv_futur"] += 1
        if ligne.statut in (StatutRdv.PLANIFIE, StatutRdv.CONFIRME) and ligne.debut.date() < aujourd_hui:
            avertissements["statut_ouvert_pour_un_rdv_passe"] += 1
        if ligne.date_prise > aujourd_hui:
            avertissements["date_prise_dans_le_futur"] += 1

    return ResultatAnalyse(
        lignes_lues=len(lignes_brutes),
        acceptees=acceptees,
        rejets=sorted(rejets, key=lambda r: r.ligne_csv),
        corrections=corrections,
        avertissements=avertissements,
        medecins=medecins,
        patients=patients,
        agents=sorted({ligne.agent_id for ligne in acceptees}),
    )


# --------------------------------------------------------------------------------------
# Écriture en base (une seule transaction)
# --------------------------------------------------------------------------------------


def ecrire_en_base(resultat: ResultatAnalyse) -> None:
    """Écrit agents, médecins, patients puis rendez-vous en une transaction : tout ou rien."""
    db = SessionLocale()
    try:
        with transaction_ecriture(db):
            db.add_all(Agent(id=identifiant, actif=True) for identifiant in resultat.agents)
            db.add_all(
                Medecin(
                    id=identifiant,
                    nom=donnees["nom"],
                    specialite=donnees["specialite"],
                    duree_consultation_min=donnees["duree_consultation_min"],
                    actif=True,
                )
                for identifiant, donnees in sorted(resultat.medecins.items())
            )
            db.add_all(
                Patient(
                    id=identifiant,
                    nom=fiche["nom"],
                    prenom=fiche["prenom"],
                    sexe=fiche["sexe"],
                    date_naissance=fiche["date_naissance"],
                    telephone=fiche["telephone"],
                    quartier=fiche["quartier"],
                )
                for identifiant, fiche in sorted(resultat.patients.items())
            )
            db.flush()  # les clés étrangères des rendez-vous exigent que ces lignes existent déjà
            db.add_all(
                RendezVous(
                    id=ligne.id_rdv,
                    patient_id=ligne.patient_id,
                    medecin_id=ligne.medecin_id,
                    agent_id=ligne.agent_id,
                    date_prise=ligne.date_prise,
                    debut=ligne.debut,
                    fin=ligne.fin,
                    statut=ligne.statut,
                    confirme=ligne.confirme,
                    canal=ligne.canal,
                    motif_annulation=ligne.motif,
                    # Seule la date de prise est connue : elle sert de date de création.
                    cree_le=datetime.combine(ligne.date_prise, time.min),
                    modifie_le=datetime.combine(ligne.date_prise, time.min),
                )
                for ligne in resultat.acceptees
            )
    finally:
        db.close()


def ecrire_rapport_rejets(rejets: list[Rejet], chemin: Path) -> None:
    """Écrit la liste des lignes rejetées (CSV lisible dans Excel)."""
    with open(chemin, "w", newline="", encoding="utf-8-sig") as fichier:
        ecrivain = csv.writer(fichier)
        ecrivain.writerow(["ligne_csv", "id_rdv", "code_rejet", "detail"])
        for rejet in rejets:
            ecrivain.writerow([rejet.ligne_csv, rejet.id_rdv, rejet.code, rejet.detail])


def afficher_compte_rendu(resultat: ResultatAnalyse, chemin_rapport: Path) -> None:
    """Journalise le compte rendu de l'import."""
    logger.info("Lignes lues : %d", resultat.lignes_lues)
    logger.info("Rendez-vous importés : %d", len(resultat.acceptees))
    logger.info("Lignes rejetées : %d (détail dans %s)", len(resultat.rejets), chemin_rapport)
    for code, nombre in sorted(Counter(r.code for r in resultat.rejets).items()):
        logger.info("  rejet %-32s %d", code, nombre)
    logger.info(
        "Créés : %d patients, %d médecins, %d agents",
        len(resultat.patients), len(resultat.medecins), len(resultat.agents),
    )  # fmt: skip
    for nom, nombre in sorted(resultat.corrections.items()):
        logger.info("  normalisation %-32s %d", nom, nombre)
    for nom, nombre in sorted(resultat.avertissements.items()):
        logger.info("  constat %-38s %d", nom, nombre)


# --------------------------------------------------------------------------------------
# Point d'entrée
# --------------------------------------------------------------------------------------


def executer_import(chemin_csv: Path, chemin_rapport: Path, reinitialiser: bool) -> ResultatAnalyse:
    """Analyse le fichier puis écrit en base. L'analyse est faite AVANT toute modification de la base."""
    resultat = analyser(lire_csv(chemin_csv))
    if not resultat.acceptees:
        raise ErreurImport("Aucune ligne importable : la base n'a pas été modifiée.")

    if reinitialiser:
        logger.warning("Réinitialisation demandée : toutes les tables vont être supprimées puis recréées.")
        Base.metadata.drop_all(bind=engine)
        with engine.begin() as connexion:  # sans cela, Alembic croirait le schéma déjà à jour
            connexion.execute(text("DROP TABLE IF EXISTS alembic_version"))
    initialiser_base()

    with SessionLocale() as db:
        deja_remplie = db.scalar(select(func.count()).select_from(RendezVous)) or db.scalar(
            select(func.count()).select_from(Patient)
        )
    if deja_remplie:
        raise ErreurImport(
            "La base contient déjà des données. Relancez avec --reinitialiser pour la recréer "
            "(toutes les données actuelles seront supprimées)."
        )

    ecrire_en_base(resultat)
    with SessionLocale() as db:
        initialiser_referentiels(db)  # spécialités, plages et jours fériés déduits des médecins importés
    ecrire_rapport_rejets(resultat.rejets, chemin_rapport)
    afficher_compte_rendu(resultat, chemin_rapport)
    return resultat


def main(argv: list[str] | None = None) -> int:
    """Retourne 0 en cas de succès, 2 pour une erreur attendue, 1 pour une erreur inattendue."""
    analyseur = argparse.ArgumentParser(description="Importe le CSV des rendez-vous dans la base MédiRDV.")
    analyseur.add_argument("--csv", type=Path, default=CSV_PAR_DEFAUT, help="chemin du fichier CSV")
    analyseur.add_argument("--rapport", type=Path, default=RAPPORT_PAR_DEFAUT, help="chemin du rapport des rejets")
    analyseur.add_argument("--reinitialiser", action="store_true", help="supprime puis recrée les tables avant l'import")
    arguments = analyseur.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s : %(message)s")
    try:
        executer_import(arguments.csv, arguments.rapport, arguments.reinitialiser)
    except ErreurImport as erreur:
        logger.error("Import interrompu : %s", erreur)
        return 2
    except Exception:  # noqa: BLE001 - dernier filet : trace complète dans le journal, code de sortie non nul
        logger.exception("Erreur inattendue pendant l'import ; la transaction a été annulée.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
