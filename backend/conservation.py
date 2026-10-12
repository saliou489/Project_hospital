"""Durées de conservation des données (ENF-08, loi n° 2008-12 sur la protection des données à caractère personnel).

Principes : minimisation (aucune donnée médicale n'est stockée), finalité (organiser les rendez-vous), durée limitée.
Les durées par défaut ci-dessous sont des PROPOSITIONS à faire valider par la clinique et à reporter dans sa déclaration
auprès de la Commission de protection des données personnelles (CDP) ; elles se règlent par variables d'environnement.

- Patients : anonymisés (nom, prénom, téléphone, quartier et date de naissance effacés) lorsque leur dernier rendez-vous
  date de plus de ``MEDIRDV_CONSERVATION_PATIENT_ANS`` ans (5 par défaut) et qu'aucun rendez-vous n'est à venir. Les
  rendez-vous restent, sans lien avec une personne identifiable : les statistiques historiques sont conservées.
- Journal des actions : ``MEDIRDV_CONSERVATION_JOURNAL_MOIS`` mois (24).
- Pointages des employés : ``MEDIRDV_CONSERVATION_POINTAGE_MOIS`` mois (24).
- Évaluations du score de risque : ``MEDIRDV_CONSERVATION_EVALUATIONS_MOIS`` mois (12).
- Sauvegardes : ``MEDIRDV_NB_SAUVEGARDES`` sauvegardes quotidiennes (14).

Utilisation : ``python -m backend.conservation`` simule (rien n'est modifié) ; ``--appliquer`` exécute ;
``--patient PAT00001`` anonymise un patient à sa demande (droit à l'effacement), avec ``--appliquer``.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from backend.journal import journaliser
from backend.models import EvaluationRisque, JournalAction, Patient, Pointage, RendezVous, maintenant

logger = logging.getLogger(__name__)

ANONYME = "ANONYME"


def _entier(nom: str, defaut: int) -> int:
    try:
        valeur = int(os.getenv(nom, defaut))
    except ValueError:
        return defaut
    return valeur if valeur > 0 else defaut


@dataclass(frozen=True)
class Durees:
    patient_ans: int = 5
    journal_mois: int = 24
    pointage_mois: int = 24
    evaluations_mois: int = 12

    @classmethod
    def depuis_environnement(cls) -> "Durees":
        return cls(
            _entier("MEDIRDV_CONSERVATION_PATIENT_ANS", 5), _entier("MEDIRDV_CONSERVATION_JOURNAL_MOIS", 24),
            _entier("MEDIRDV_CONSERVATION_POINTAGE_MOIS", 24), _entier("MEDIRDV_CONSERVATION_EVALUATIONS_MOIS", 12),
        )


def _il_y_a(instant: datetime, mois: int) -> datetime:
    return instant - timedelta(days=round(mois * 30.4375))


def patients_a_anonymiser(db: Session, limite: datetime) -> list[str]:
    """Patients non anonymisés dont le dernier rendez-vous (ou, sans rendez-vous, la création) précède ``limite``
    et qui n'ont aucun rendez-vous futur."""
    dernier = select(RendezVous.patient_id, func.max(RendezVous.debut).label("dernier")).group_by(RendezVous.patient_id).subquery()
    lignes = db.execute(
        select(Patient.id, Patient.cree_le, dernier.c.dernier)
        .outerjoin(dernier, dernier.c.patient_id == Patient.id)
        .where(Patient.nom != ANONYME)
    ).all()
    return sorted(i for i, cree, dern in lignes if (dern or cree) < limite)


def _anonymiser(db: Session, identifiants: list[str]) -> None:
    for debut in range(0, len(identifiants), 500):
        db.execute(
            update(Patient).where(Patient.id.in_(identifiants[debut:debut + 500]))
            .values(nom=ANONYME, prenom=ANONYME, telephone=None, quartier=None, date_naissance=None)
        )


def appliquer(db: Session, durees: Durees | None = None, executer: bool = False, instant: datetime | None = None) -> dict[str, int]:
    """Calcule (et, si ``executer``, applique) la purge ; retourne le nombre d'éléments concernés par catégorie."""
    d = durees or Durees.depuis_environnement()
    maintenant_ = instant or maintenant()
    limite_patient = maintenant_ - timedelta(days=round(d.patient_ans * 365.25))
    limite_journal, limite_pointage = _il_y_a(maintenant_, d.journal_mois), _il_y_a(maintenant_, d.pointage_mois)
    limite_evaluations = _il_y_a(maintenant_, d.evaluations_mois)

    patients = patients_a_anonymiser(db, limite_patient)
    # Un patient ayant un rendez-vous à venir n'est jamais anonymisé (son dernier rendez-vous est dans le futur).
    resultat = {
        "patients_anonymises": len(patients),
        "entrees_journal": db.scalar(select(func.count()).select_from(JournalAction).where(JournalAction.horodatage < limite_journal)) or 0,
        "pointages": db.scalar(select(func.count()).select_from(Pointage).where(Pointage.jour < limite_pointage.date())) or 0,
        "evaluations_risque": db.scalar(select(func.count()).select_from(EvaluationRisque).where(EvaluationRisque.calcule_le < limite_evaluations)) or 0,
    }
    if executer:
        _anonymiser(db, patients)
        db.execute(delete(JournalAction).where(JournalAction.horodatage < limite_journal))
        db.execute(delete(Pointage).where(Pointage.jour < limite_pointage.date()))
        db.execute(delete(EvaluationRisque).where(EvaluationRisque.calcule_le < limite_evaluations))
        journaliser(db, None, "CONSERVATION_APPLIQUEE", detail=", ".join(f"{k} {v}" for k, v in resultat.items()))
        db.commit()
    return resultat


def anonymiser_patient(db: Session, patient_id: str, executer: bool = False) -> bool:
    """Droit à l'effacement : anonymise un patient à sa demande ; ses rendez-vous restent, sans lien avec lui."""
    patient = db.get(Patient, patient_id)
    if patient is None:
        return False
    if executer:
        _anonymiser(db, [patient_id])
        journaliser(db, None, "PATIENT_ANONYMISE", cible=patient_id, detail="à la demande de la personne")
        db.commit()
    return True


def principal(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    analyse = argparse.ArgumentParser(description="Conservation et anonymisation des données MédiRDV")
    analyse.add_argument("--appliquer", action="store_true", help="exécuter (sans cette option : simulation)")
    analyse.add_argument("--patient", help="anonymiser ce patient à sa demande (ex. PAT00001)")
    args = analyse.parse_args(argv)

    from backend.database import SessionLocale, initialiser_base

    initialiser_base()
    with SessionLocale() as db:
        if args.patient:
            if not anonymiser_patient(db, args.patient, args.appliquer):
                print(f"Erreur : patient {args.patient} introuvable.", file=sys.stderr)
                return 1
            print(f"Patient {args.patient} {'anonymisé' if args.appliquer else 'serait anonymisé (simulation)'}.")
            return 0
        resultat = appliquer(db, executer=args.appliquer)
    print(("Purge appliquée" if args.appliquer else "Simulation, rien n'a été modifié") + " :")
    for cle, valeur in resultat.items():
        print(f"  {cle.replace('_', ' ')} : {valeur}")
    return 0


if __name__ == "__main__":
    sys.exit(principal())
