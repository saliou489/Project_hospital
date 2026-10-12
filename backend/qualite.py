"""Qualité des données (cahier des charges, section 7.3) : doublons de patients et rapport d'anomalies.

- Doublon de patient (EF-04) : même nom, même prénom et même date de naissance. La comparaison ignore les accents,
  la casse, les espaces, les tirets et les apostrophes, et tolère un nom et un prénom inversés (erreur de saisie
  fréquente). Deux personnes différentes peuvent réellement partager ces trois valeurs : la création d'un doublon
  est donc refusée par défaut, mais l'agent peut la confirmer.
- Rapport de qualité : liste les valeurs manquantes, les doublons et les incohérences détectés dans la base.
  Il ne modifie rien : les corrections se font par l'écran de modification des fiches. Aucune suppression physique
  n'est autorisée (RG-11).
"""
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models import (
    STATUTS_ACTIFS,
    Medecin,
    Patient,
    Pointage,
    RendezVous,
    StatutRdv,
    aujourdhui,
    maintenant,
)
from backend.schemas import MOTIF_TELEPHONE, ControleQualite, RapportQualite, normaliser_nom

AGE_MAX_VRAISEMBLABLE = 120

# --------------------------------------------------------------------------------------
# Doublons de patients
# --------------------------------------------------------------------------------------


def _simplifier(texte: str) -> str:
    """Forme de comparaison : sans accents ni casse, en ne gardant que les lettres et les chiffres."""
    sans_accents = "".join(c for c in unicodedata.normalize("NFKD", texte) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", sans_accents.casefold())


def cle_identite(nom: str, prenom: str, naissance: date) -> tuple:
    """Clé d'identité : l'ordre nom / prénom est ignoré."""
    return (frozenset((_simplifier(nom), _simplifier(prenom))), naissance)


def chercher_doublons(
    db: Session, nom: str, prenom: str, naissance: date, exclure_id: str | None = None
) -> list[Patient]:
    """Patients déjà enregistrés avec la même identité (``exclure_id`` écarte la fiche en cours de modification)."""
    cle = cle_identite(nom, prenom, naissance)
    candidats = db.scalars(select(Patient).where(Patient.date_naissance == naissance).order_by(Patient.id)).all()
    return [p for p in candidats if p.id != exclure_id and cle_identite(p.nom, p.prenom, naissance) == cle]


def decrire(patients: list[Patient]) -> str:
    return ", ".join(f"{p.id} ({p.nom} {p.prenom})" for p in patients)


def groupes_de_doublons(db: Session) -> list[list[Patient]]:
    """Tous les groupes de fiches ayant la même identité (les fiches sans date de naissance ne sont pas comparables)."""
    groupes: dict[tuple, list[Patient]] = defaultdict(list)
    for patient in db.scalars(
        select(Patient).where(Patient.date_naissance.is_not(None)).order_by(Patient.id)
    ).all():
        groupes[cle_identite(patient.nom, patient.prenom, patient.date_naissance)].append(patient)
    return sorted((g for g in groupes.values() if len(g) > 1), key=lambda g: g[0].id)


# --------------------------------------------------------------------------------------
# Rapport de qualité
# --------------------------------------------------------------------------------------


def _controle(code, famille, libelle, gravite, nombre, reference, exemples) -> ControleQualite:
    pourcentage = round(100.0 * nombre / reference, 1) if reference else None
    return ControleQualite(
        code=code, famille=famille, libelle=libelle, gravite=gravite, nombre=nombre,
        reference=reference, pourcentage=pourcentage, exemples=exemples,
    )  # fmt: skip


def _compter(db: Session, modele, condition, nb_exemples: int, jointure=None) -> tuple[int, list[str]]:
    """Nombre d'enregistrements vérifiant la condition, et les premiers identifiants."""
    requete_nombre = select(func.count()).select_from(modele)
    requete_ids = select(modele.id)
    if jointure is not None:
        requete_nombre = requete_nombre.join(*jointure)
        requete_ids = requete_ids.join(*jointure)
    nombre = int(db.scalar(requete_nombre.where(condition)) or 0)
    exemples = [str(i) for i in db.scalars(requete_ids.where(condition).order_by(modele.id).limit(nb_exemples)).all()]
    return nombre, exemples


def _chevauchements(db: Session, colonne, nb_exemples: int) -> tuple[int, list[str]]:
    """Paires de rendez-vous actifs qui se chevauchent pour un même médecin ou un même patient (RG-01 / RG-02)."""
    par_cle: dict[str, list] = defaultdict(list)
    for cle, debut, fin, identifiant in db.execute(
        select(colonne, RendezVous.debut, RendezVous.fin, RendezVous.id)
        .where(RendezVous.statut.in_(STATUTS_ACTIFS))
        .order_by(colonne, RendezVous.debut)
    ).all():
        par_cle[cle].append((debut, fin, identifiant))
    nombre, exemples = 0, []
    for plages in par_cle.values():
        for (_, fin1, id1), (debut2, _, id2) in zip(plages, plages[1:]):
            if debut2 < fin1:
                nombre += 1
                if len(exemples) < nb_exemples:
                    exemples.append(f"{id1} / {id2}")
    return nombre, exemples


def construire_rapport(db: Session, nb_exemples: int = 20) -> RapportQualite:
    """Exécute tous les contrôles et assemble le rapport."""
    aujourd_hui = aujourdhui()
    demain = datetime.combine(aujourd_hui + timedelta(days=1), time.min)
    aujourd_hui_minuit = datetime.combine(aujourd_hui, time.min)
    nb_patients = int(db.scalar(select(func.count()).select_from(Patient)) or 0)
    nb_rdv = int(db.scalar(select(func.count()).select_from(RendezVous)) or 0)
    nb_pointages = int(db.scalar(select(func.count()).select_from(Pointage)) or 0)
    controles: list[ControleQualite] = []

    def ajouter(code, famille, libelle, gravite, resultat, reference):
        controles.append(_controle(code, famille, libelle, gravite, resultat[0], reference, resultat[1]))

    # --- Valeurs manquantes -------------------------------------------------------------
    famille = "Valeurs manquantes"
    ajouter("PATIENT_NAISSANCE_MANQUANTE", famille, "Patients sans date de naissance", "avertissement",
            _compter(db, Patient, Patient.date_naissance.is_(None), nb_exemples), nb_patients)  # fmt: skip
    ajouter("PATIENT_TELEPHONE_MANQUANT", famille, "Patients sans numéro de téléphone", "avertissement",
            _compter(db, Patient, Patient.telephone.is_(None), nb_exemples), nb_patients)  # fmt: skip
    ajouter("PATIENT_QUARTIER_MANQUANT", famille, "Patients sans quartier (champ facultatif)", "information",
            _compter(db, Patient, Patient.quartier.is_(None), nb_exemples), nb_patients)  # fmt: skip
    ajouter("RDV_CANAL_MANQUANT", famille, "Rendez-vous sans canal de prise (champ facultatif)", "information",
            _compter(db, RendezVous, RendezVous.canal.is_(None), nb_exemples), nb_rdv)  # fmt: skip
    ajouter("POINTAGE_DEPART_MANQUANT", famille, "Pointages d'un jour passé sans heure de départ", "information",
            _compter(db, Pointage, (Pointage.depart.is_(None)) & (Pointage.jour < aujourd_hui), nb_exemples), nb_pointages)  # fmt: skip

    # --- Formats et valeurs invalides ----------------------------------------------------
    famille = "Formats et valeurs invalides"
    tels, noms = [], []
    for identifiant, nom, prenom, telephone in db.execute(select(Patient.id, Patient.nom, Patient.prenom, Patient.telephone)).all():
        if telephone is not None and not MOTIF_TELEPHONE.fullmatch(telephone):
            tels.append(identifiant)
        try:
            mal_forme = normaliser_nom(nom) != nom or normaliser_nom(prenom) != prenom
        except ValueError:
            mal_forme = True
        if mal_forme:
            noms.append(identifiant)
    ajouter("PATIENT_TELEPHONE_INVALIDE", famille, "Téléphones qui ne respectent pas le format sénégalais (9 chiffres, 77/78/76/70/75/33)", "erreur",
            (len(tels), tels[:nb_exemples]), nb_patients)  # fmt: skip
    ajouter("PATIENT_NOM_MAL_FORME", famille, "Noms ou prénoms mal saisis (espaces superflus, casse incohérente, caractères interdits)", "avertissement",
            (len(noms), noms[:nb_exemples]), nb_patients)  # fmt: skip
    ajouter("PATIENT_NAISSANCE_FUTURE", famille, "Dates de naissance dans le futur", "erreur",
            _compter(db, Patient, Patient.date_naissance > aujourd_hui, nb_exemples), nb_patients)  # fmt: skip
    limite_age = date(aujourd_hui.year - AGE_MAX_VRAISEMBLABLE, aujourd_hui.month, min(aujourd_hui.day, 28))
    ajouter("PATIENT_AGE_INVRAISEMBLABLE", famille, f"Patients de plus de {AGE_MAX_VRAISEMBLABLE} ans", "avertissement",
            _compter(db, Patient, Patient.date_naissance < limite_age, nb_exemples), nb_patients)  # fmt: skip

    # --- Doublons --------------------------------------------------------------------------
    groupes = groupes_de_doublons(db)
    nb_comparables = int(db.scalar(select(func.count()).select_from(Patient).where(Patient.date_naissance.is_not(None))) or 0)
    ajouter("PATIENT_DOUBLON", "Doublons", "Groupes de fiches patients de même identité (nom, prénom, date de naissance)", "avertissement",
            (len(groupes), [" / ".join(p.id for p in g) for g in groupes[:nb_exemples]]), nb_comparables)  # fmt: skip

    # --- Incohérences ----------------------------------------------------------------------
    famille = "Incohérences"
    date_rdv = func.date(RendezVous.debut)
    ajouter("RDV_PRISE_APRES_RDV", famille, "Rendez-vous dont la date de prise est postérieure à la date du rendez-vous", "erreur",
            _compter(db, RendezVous, RendezVous.date_prise > date_rdv, nb_exemples), nb_rdv)  # fmt: skip
    ajouter("RDV_PRISE_DANS_LE_FUTUR", famille, "Rendez-vous dont la date de prise est postérieure à aujourd'hui", "erreur",
            _compter(db, RendezVous, RendezVous.date_prise > aujourd_hui, nb_exemples), nb_rdv)  # fmt: skip
    ajouter("RDV_AVANT_NAISSANCE", famille, "Rendez-vous antérieurs à la naissance du patient", "erreur",
            _compter(db, RendezVous, date_rdv < Patient.date_naissance, nb_exemples, jointure=(Patient, RendezVous.patient_id == Patient.id)), nb_rdv)  # fmt: skip
    ajouter("RDV_STATUT_FINAL_FUTUR", famille, "Rendez-vous « Honoré » ou « Absent » datés d'un jour à venir", "erreur",
            _compter(db, RendezVous, RendezVous.statut.in_([StatutRdv.HONORE, StatutRdv.ABSENT]) & (RendezVous.debut >= demain), nb_exemples), nb_rdv)  # fmt: skip
    ajouter("RDV_STATUT_OUVERT_PASSE", famille, "Rendez-vous « Planifié » ou « Confirmé » d'un jour passé, jamais clôturés", "avertissement",
            _compter(db, RendezVous, RendezVous.statut.in_([StatutRdv.PLANIFIE, StatutRdv.CONFIRME]) & (RendezVous.debut < aujourd_hui_minuit), nb_exemples), nb_rdv)  # fmt: skip
    ajouter("RDV_MEDECIN_RETIRE", famille, "Rendez-vous à venir d'un médecin retiré du planning (à reprogrammer)", "avertissement",
            _compter(db, RendezVous, RendezVous.statut.in_([StatutRdv.PLANIFIE, StatutRdv.CONFIRME]) & (RendezVous.debut >= maintenant()) & Medecin.actif.is_(False), nb_exemples, jointure=(Medecin, RendezVous.medecin_id == Medecin.id)), nb_rdv)  # fmt: skip
    ajouter("RDV_CHEVAUCHEMENT_MEDECIN", famille, "Rendez-vous actifs qui se chevauchent pour un même médecin (RG-01)", "erreur",
            _chevauchements(db, RendezVous.medecin_id, nb_exemples), nb_rdv)  # fmt: skip
    ajouter("RDV_CHEVAUCHEMENT_PATIENT", famille, "Rendez-vous actifs qui se chevauchent pour un même patient (RG-02)", "erreur",
            _chevauchements(db, RendezVous.patient_id, nb_exemples), nb_rdv)  # fmt: skip

    avec_anomalie = [c for c in controles if c.nombre > 0]
    return RapportQualite(
        genere_le=maintenant(),
        patients=nb_patients,
        rendez_vous=nb_rdv,
        pointages=nb_pointages,
        erreurs=sum(1 for c in avec_anomalie if c.gravite == "erreur"),
        avertissements=sum(1 for c in avec_anomalie if c.gravite == "avertissement"),
        informations=sum(1 for c in avec_anomalie if c.gravite == "information"),
        controles_sans_anomalie=len(controles) - len(avec_anomalie),
        controles=controles,
    )
