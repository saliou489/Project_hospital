"""Rapport de qualité et détection de doublons de patients."""
from tests.conftest import corps_patient, inserer_patient


def test_acces_rapport(agent, responsable, direction, admin):
    assert agent.get("/qualite/rapport").status_code == 403
    assert direction.get("/qualite/rapport").status_code == 403
    assert responsable.get("/qualite/rapport").status_code == 200
    assert admin.get("/qualite/rapport").status_code == 200


def test_rapport_structure(responsable):
    r = responsable.get("/qualite/rapport").json()
    assert len(r["controles"]) == 18
    assert {c["famille"] for c in r["controles"]} == {"Valeurs manquantes", "Formats et valeurs invalides", "Doublons", "Incohérences"}
    assert r["controles_sans_anomalie"] <= 18
    assert r["erreurs"] + r["avertissements"] + r["informations"] >= 0


def test_telephone_invalide_detecte(responsable):
    pid = inserer_patient(telephone="12")
    r = responsable.get("/qualite/rapport", params={"exemples": 1000}).json()
    controle = next(c for c in r["controles"] if "TELEPHONE" in c["code"] and "INVALIDE" in c["code"])
    assert controle["nombre"] >= 1
    assert pid in controle["exemples"]


def test_exemples_bornes(responsable):
    r = responsable.get("/qualite/rapport", params={"exemples": 0}).json()
    assert all(c["exemples"] == [] for c in r["controles"])
    assert responsable.get("/qualite/rapport", params={"exemples": 5000}).status_code == 422


def test_doublons_listes(agent):
    corps = corps_patient()
    agent.post("/patients", json=corps)
    agent.post("/patients", json={**corps_patient(), **{k: corps[k] for k in ("nom", "prenom", "date_naissance")}, "confirmer_doublon": True})
    r = agent.get("/qualite/doublons").json()
    assert r["nb_groupes"] >= 1
    assert any(len(g["patients"]) >= 2 for g in r["groupes"])
