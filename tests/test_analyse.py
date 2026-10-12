"""Indicateurs d'analyse (bloc 3a) : valeurs exactes sur un jeu de données maîtrisé, droits et filtres."""
from datetime import date, datetime, time, timedelta

import pytest

from sqlalchemy import delete, select

from backend.database import SessionLocale
from backend.models import (
    HistoriqueRdv, Indisponibilite, Medecin, PlageConsultation, Pointage, RendezVous, StatutRdv, Utilisateur,
)
from tests.conftest import inserer_rdv

PATIENT = "PAT00001"


@pytest.fixture(scope="module")
def jeu(admin):
    """Un médecin neuf (les filtres l'isolent des autres données), avec 4 rendez-vous dont on connaît tous les chiffres."""
    nom = next(x["nom"] for x in admin.get("/referentiels/specialites").json() if x["actif"])
    r = admin.post("/admin/utilisateurs", json={"role": "Médecin", "nom_medecin": "Dr Analyse Test", "specialite": nom})
    assert r.status_code == 201, r.text
    mid = r.json()["utilisateur"]["id"]
    lundi = date.today() - timedelta(days=28)
    lundi -= timedelta(days=lundi.weekday())
    mardi = lundi + timedelta(days=1)
    inserer_rdv(PATIENT, mid, datetime.combine(lundi, time(9, 0)), StatutRdv.HONORE, date_prise=lundi - timedelta(days=5))
    inserer_rdv(PATIENT, mid, datetime.combine(lundi, time(9, 20)), StatutRdv.ABSENT, date_prise=lundi - timedelta(days=10))
    inserer_rdv(PATIENT, mid, datetime.combine(mardi, time(10, 0)), StatutRdv.HONORE, date_prise=mardi - timedelta(days=3))
    inserer_rdv(PATIENT, mid, datetime.combine(mardi, time(10, 15)), StatutRdv.ANNULE, date_prise=mardi - timedelta(days=1))
    yield {"specialite": nom, "medecin": mid, "lundi": lundi, "mardi": mardi}
    # Nettoyage : la base est partagée entre tous les tests, le médecin de test ne doit pas leur survivre.
    with SessionLocale() as db:
        ids_rdv = select(RendezVous.id).where(RendezVous.medecin_id == mid)
        db.execute(delete(HistoriqueRdv).where(HistoriqueRdv.rdv_id.in_(ids_rdv)))
        db.execute(delete(RendezVous).where(RendezVous.medecin_id == mid))
        db.execute(delete(PlageConsultation).where(PlageConsultation.medecin_id == mid))
        db.execute(delete(Indisponibilite).where(Indisponibilite.medecin_id == mid))
        ids_users = select(Utilisateur.id).where(Utilisateur.medecin_id == mid)
        db.execute(delete(Pointage).where(Pointage.utilisateur_id.in_(ids_users)))
        db.execute(delete(Utilisateur).where(Utilisateur.medecin_id == mid))
        db.execute(delete(Medecin).where(Medecin.id == mid))
        db.commit()


def test_jour(responsable, jeu):
    r = responsable.get("/stats/jour", params={"jour": jeu["lundi"].isoformat(), "medecin_id": jeu["medecin"]})
    assert r.status_code == 200, r.text
    j = r.json()
    assert (j["prevus"], j["honores"], j["absents"], j["annules"], j["a_venir"]) == (2, 1, 1, 0, 0)
    assert j["taux_absence_pct"] == 50.0
    assert j["taux_occupation_pct"] is None or 0 <= j["taux_occupation_pct"] <= 100


def test_jour_par_defaut_est_aujourdhui(responsable):
    r = responsable.get("/stats/jour")
    assert r.status_code == 200 and r.json()["jour"] == date.today().isoformat()


def test_carte_de_chaleur(responsable, jeu):
    p = {"medecin_id": jeu["medecin"]}
    r = responsable.get("/stats/carte-chaleur", params=p).json()
    cases = {(c["jour_semaine"], c["heure"]): c["nombre"] for c in r["cellules"]}
    assert cases == {(0, 9): 2, (1, 10): 1} and r["total"] == 3 and not r["inclut_annules"]
    assert sorted((x["jour_semaine"], x["nombre"]) for x in r["par_specialite"]) == [(0, 2), (1, 1)]
    avec = responsable.get("/stats/carte-chaleur", params={**p, "inclure_annules": True}).json()
    assert avec["total"] == 4 and {(c["jour_semaine"], c["heure"]): c["nombre"] for c in avec["cellules"]}[(1, 10)] == 2


def test_delai_attente_mediane(responsable, jeu):
    r = responsable.get("/stats/delai-attente", params={"medecin_id": jeu["medecin"]}).json()
    assert r["nombre_rdv"] == 3 and r["mediane_jours"] == 5.0  # délais 3, 5 et 10 ; l'annulé est exclu
    s = r["par_specialite"][0]
    assert (s["specialite"], s["mediane_jours"], s["moyenne_jours"]) == (jeu["specialite"], 5.0, 6.0)
    assert 5.0 <= s["p90_jours"] <= 10.0


def test_taux_annulation(responsable, jeu):
    r = responsable.get("/stats/annulation", params={"medecin_id": jeu["medecin"]}).json()
    assert (r["total_cree"], r["annules"], r["taux_annulation_pct"]) == (4, 1, 25.0)
    assert r["par_motif"] == [{"motif": "Patient", "nombre": 1, "part_pct": 25.0}]
    assert sum(m["annules"] for m in r["par_mois"]) == 1 and sum(m["total_cree"] for m in r["par_mois"]) == 4
    assert r["par_specialite"][0]["taux_annulation_pct"] == 25.0


def test_evolution_mensuelle_par_specialite(responsable, jeu):
    r = responsable.get("/stats/evolution-mensuelle", params={"medecin_id": jeu["medecin"]}).json()
    assert sum(x["total_rdv"] for x in r) == 4 and {x["specialite"] for x in r} == {jeu["specialite"]}
    assert sum(x["honores"] for x in r) == 2 and sum(x["absents"] for x in r) == 1
    toutes = responsable.get("/stats/evolution-mensuelle").json()
    assert len({x["specialite"] for x in toutes}) > 1


def test_filtre_periode_exclut(responsable, jeu):
    p = {"medecin_id": jeu["medecin"], "date_debut": (jeu["mardi"] + timedelta(days=1)).isoformat()}
    assert responsable.get("/stats/carte-chaleur", params=p).json()["total"] == 0
    assert responsable.get("/stats/delai-attente", params=p).json()["mediane_jours"] is None
    assert responsable.get("/stats/annulation", params=p).json()["taux_annulation_pct"] is None


@pytest.mark.parametrize("chemin", ["jour", "carte-chaleur", "delai-attente", "annulation", "evolution-mensuelle"])
def test_droits_et_vue_synthetique(chemin, agent, direction, admin, anonyme, jeu):
    assert anonyme.get(f"/stats/{chemin}").status_code == 401
    assert agent.get(f"/stats/{chemin}").status_code == 403
    assert admin.get(f"/stats/{chemin}", params={"medecin_id": jeu["medecin"]}).status_code == 200
    assert direction.get(f"/stats/{chemin}").status_code == 200
    r = direction.get(f"/stats/{chemin}", params={"medecin_id": jeu["medecin"]})
    assert r.status_code == 403 and r.json()["code_erreur"] == "VUE_SYNTHETIQUE_UNIQUEMENT"


def test_medecin_inexistant_et_periode_invalide(responsable):
    assert responsable.get("/stats/carte-chaleur", params={"medecin_id": "MED999"}).status_code == 404
    r = responsable.get("/stats/annulation", params={"date_debut": "2026-05-02", "date_fin": "2026-05-01"})
    assert r.status_code == 422
