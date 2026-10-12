"""Durées de conservation (ENF-08) : anonymisation des patients inactifs, purge du journal, simulation sans effet."""
from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import delete, func, select

from backend import conservation
from backend.database import SessionLocale
from backend.models import (
    EvaluationRisque, HistoriqueRdv, JournalAction, Patient, Pointage, RendezVous, Sexe, StatutRdv, maintenant,
)
from tests.conftest import inserer_rdv


@pytest.fixture()
def jeu(medecins):
    """Quatre patients : inactif depuis 6 ans, inactif mais avec un rendez-vous à venir, actif, et sans rendez-vous (ancien)."""
    maint = maintenant()
    ids = {"vieux": "PATZ0001", "futur": "PATZ0002", "actif": "PATZ0003", "orphelin": "PATZ0004"}
    med = medecins[0]["id"]
    with SessionLocale() as db:
        for cle, pid in ids.items():
            ancien = maint - timedelta(days=365 * 7) if cle in ("vieux", "futur", "orphelin") else maint
            db.add(Patient(id=pid, nom="Diop", prenom="Awa", sexe=Sexe.FEMININ, date_naissance=date(1980, 1, 1), telephone="770000000", quartier="Plateau", cree_le=ancien))
        db.commit()
    six_ans = datetime.combine(date.today() - timedelta(days=365 * 6), time(5, 10))  # heure hors planning : aucun conflit avec les autres données
    inserer_rdv(ids["vieux"], med, six_ans, StatutRdv.HONORE)
    inserer_rdv(ids["futur"], med, six_ans + timedelta(minutes=20), StatutRdv.HONORE)
    inserer_rdv(ids["futur"], med, datetime.combine(date.today() + timedelta(days=30), time(5, 20)))
    inserer_rdv(ids["actif"], med, datetime.combine(date.today() - timedelta(days=60), time(5, 40)), StatutRdv.HONORE)
    with SessionLocale() as db:
        db.add(JournalAction(horodatage=maint - timedelta(days=365 * 3), action="ANCIENNE_ACTION"))
        db.add(Pointage(utilisateur_id="AGT01", jour=date.today() - timedelta(days=365 * 3), arrivee=maint - timedelta(days=365 * 3)))
        db.add(EvaluationRisque(calcule_le=maint - timedelta(days=400), modele_retenu="x", seuil=0.2, auc=0.6, nb_scores=0, details="{}"))
        db.commit()
    yield ids
    with SessionLocale() as db:
        rdv = select(RendezVous.id).where(RendezVous.patient_id.in_(ids.values()))
        db.execute(delete(HistoriqueRdv).where(HistoriqueRdv.rdv_id.in_(rdv)))
        db.execute(delete(RendezVous).where(RendezVous.patient_id.in_(ids.values())))
        db.execute(delete(Patient).where(Patient.id.in_(ids.values())))
        db.execute(delete(JournalAction).where(JournalAction.action.in_(["ANCIENNE_ACTION", "CONSERVATION_APPLIQUEE", "PATIENT_ANONYMISE"])))
        db.execute(delete(Pointage).where(Pointage.jour < date.today() - timedelta(days=365 * 2)))
        db.execute(delete(EvaluationRisque).where(EvaluationRisque.modele_retenu == "x"))
        db.commit()


def test_simulation_ne_modifie_rien(jeu):
    with SessionLocale() as db:
        r = conservation.appliquer(db, conservation.Durees(), executer=False)
        assert r == {"patients_anonymises": 2, "entrees_journal": 1, "pointages": 1, "evaluations_risque": 1}
        assert db.get(Patient, jeu["vieux"]).nom == "Diop"
        assert db.scalar(select(func.count()).select_from(JournalAction).where(JournalAction.action == "ANCIENNE_ACTION")) == 1


def test_application_anonymise_les_seuls_patients_inactifs(jeu):
    with SessionLocale() as db:
        conservation.appliquer(db, conservation.Durees(), executer=True)
    with SessionLocale() as db:
        for cle in ("vieux", "orphelin"):
            p = db.get(Patient, jeu[cle])
            assert (p.nom, p.prenom, p.telephone, p.quartier, p.date_naissance) == ("ANONYME", "ANONYME", None, None, None)
            assert p.sexe == Sexe.FEMININ  # donnée statistique conservée
        for cle in ("futur", "actif"):  # rendez-vous à venir ou récent : jamais touchés
            assert db.get(Patient, jeu[cle]).nom == "Diop" and db.get(Patient, jeu[cle]).telephone == "770000000"
        assert db.scalar(select(func.count()).select_from(RendezVous).where(RendezVous.patient_id == jeu["vieux"])) == 1  # rendez-vous conservés
        assert db.scalar(select(func.count()).select_from(JournalAction).where(JournalAction.action == "ANCIENNE_ACTION")) == 0
        assert db.scalar(select(func.count()).select_from(JournalAction).where(JournalAction.action == "CONSERVATION_APPLIQUEE")) == 1
        # Idempotent : une seconde exécution n'a plus rien à faire.
        assert conservation.appliquer(db, conservation.Durees(), executer=False)["patients_anonymises"] == 0


def test_durees_configurables(jeu, monkeypatch):
    monkeypatch.setenv("MEDIRDV_CONSERVATION_PATIENT_ANS", "10")
    assert conservation.Durees.depuis_environnement().patient_ans == 10
    monkeypatch.setenv("MEDIRDV_CONSERVATION_JOURNAL_MOIS", "pas_un_nombre")
    assert conservation.Durees.depuis_environnement().journal_mois == 24  # valeur invalide : défaut
    with SessionLocale() as db:
        assert conservation.appliquer(db, conservation.Durees.depuis_environnement())["patients_anonymises"] == 0  # 6 ans < 10 ans


def test_droit_a_l_effacement_sur_demande(jeu):
    with SessionLocale() as db:
        assert conservation.anonymiser_patient(db, jeu["actif"], executer=False)
        assert db.get(Patient, jeu["actif"]).nom == "Diop"  # simulation
        assert conservation.anonymiser_patient(db, jeu["actif"], executer=True)
        assert db.get(Patient, jeu["actif"]).nom == "ANONYME"
        assert not conservation.anonymiser_patient(db, "PATINCONNU", executer=True)


def test_ligne_de_commande_simulation(jeu, capsys):
    assert conservation.principal([]) == 0
    sortie = capsys.readouterr().out
    assert "Simulation" in sortie and "patients anonymises : 2" in sortie
    assert conservation.principal(["--patient", "PATINCONNU"]) == 1
