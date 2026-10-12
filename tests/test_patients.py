"""Fiches patients : création, validation, doublons (EF-04), modification, recherche."""
from tests.conftest import corps_patient


def test_creation_et_lecture(agent, nouveau_patient):
    pid = nouveau_patient()
    r = agent.get(f"/patients/{pid}")
    assert r.status_code == 200
    assert r.json()["id"] == pid


def test_creation_reservee_aux_agents_et_admin(responsable, direction):
    assert responsable.post("/patients", json=corps_patient()).status_code == 403
    assert direction.get("/patients").status_code == 403


def test_telephone_invalide(agent):
    r = agent.post("/patients", json=corps_patient(telephone="12345"))
    assert r.status_code == 422
    assert r.json()["code_erreur"] == "DONNEES_INVALIDES"


def test_nom_avec_chiffres(agent):
    assert agent.post("/patients", json=corps_patient(nom="Dupont2")).status_code == 422


def test_naissance_future(agent):
    assert agent.post("/patients", json=corps_patient(date_naissance="2999-01-01")).status_code == 422


def test_champ_inconnu_refuse(agent):
    assert agent.post("/patients", json={**corps_patient(), "role": "x"}).status_code == 422


def test_doublon_refuse_puis_confirme(agent):
    corps = corps_patient()
    assert agent.post("/patients", json=corps).status_code == 201
    inverse = {**corps_patient(), "nom": corps["prenom"].upper(), "prenom": corps["nom"], "date_naissance": corps["date_naissance"]}
    r = agent.post("/patients", json=inverse)
    assert r.status_code == 409
    assert r.json()["code_erreur"] == "PATIENT_DOUBLON"
    assert agent.post("/patients", json={**inverse, "confirmer_doublon": True}).status_code == 201


def test_verification_doublon(agent):
    corps = corps_patient()
    agent.post("/patients", json=corps)
    r = agent.post("/patients/verification-doublon", json={k: corps[k] for k in ("nom", "prenom", "date_naissance")})
    assert r.status_code == 200 and len(r.json()) == 1
    r = agent.post("/patients/verification-doublon", json={"nom": "Zzzinconnu", "prenom": "Yyy", "date_naissance": "1970-01-01"})
    assert r.json() == []


def test_modification(agent, nouveau_patient):
    pid = nouveau_patient()
    r = agent.patch(f"/patients/{pid}", json={"quartier": "Ouakam"})
    assert r.status_code == 200 and r.json()["quartier"] == "Ouakam"


def test_modification_vide_ou_identique(agent, nouveau_patient):
    pid = nouveau_patient()
    assert agent.patch(f"/patients/{pid}", json={}).status_code in (400, 422)


def test_modification_cree_un_doublon(agent, nouveau_patient):
    a = corps_patient()
    agent.post("/patients", json=a)
    pid = nouveau_patient()
    r = agent.patch(f"/patients/{pid}", json={"nom": a["nom"], "prenom": a["prenom"], "date_naissance": a["date_naissance"]})
    assert r.status_code == 409


def test_patient_introuvable(agent):
    r = agent.get("/patients/PAT99999")
    assert r.status_code == 404
    assert r.json()["code_erreur"] == "PATIENT_INTROUVABLE"


def test_recherche(agent):
    corps = corps_patient()
    agent.post("/patients", json=corps)
    r = agent.get("/patients", params={"q": corps["nom"]})
    assert r.status_code == 200 and r.json()["total"] >= 1
    assert agent.get("/patients", params={"q": "a"}).status_code == 422
    assert agent.get("/patients", params={"q": "%%"}).json()["total"] == 0  # les jokers sont échappés


def test_pagination(agent):
    r = agent.get("/patients", params={"limite": 5, "decalage": 5})
    assert r.status_code == 200 and len(r.json()["elements"]) <= 5
    assert agent.get("/patients", params={"limite": 500}).status_code == 422
