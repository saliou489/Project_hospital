"""Règles métier des rendez-vous : RG-01, RG-02, RG-04, RG-05, RG-07, RG-08, cycle de vie."""
from datetime import date, datetime, time, timedelta

import pytest

from backend.models import StatutRdv
from tests.conftest import corps_rdv, creneau_unique, inserer_rdv


@pytest.fixture
def medecin_id(medecins):
    return next(m for m in medecins if m.get("actif", True))["id"]


@pytest.fixture
def autre_medecin_id(medecins, medecin_id):
    return next(m for m in medecins if m["id"] != medecin_id)["id"]


def creer(agent, patient, medecin, jour=None, heure=None, **extra):
    if jour is None:
        jour, heure = creneau_unique(medecin)
    return agent.post("/rdv", json=corps_rdv(patient, medecin, jour, heure, **extra))


def test_creation_nominale(agent, nouveau_patient, medecin_id):
    r = creer(agent, nouveau_patient(), medecin_id)
    assert r.status_code == 201, r.text
    assert r.json()["rdv"]["statut"] == "Planifié"


def test_seuls_les_agents_creent(admin, responsable, direction, nouveau_patient, medecin_id):
    p = nouveau_patient()
    for client in (admin, responsable, direction):
        assert creer(client, p, medecin_id).status_code == 403


def test_rg01_medecin_double_reservation(agent, nouveau_patient, medecin_id):
    jour, heure = creneau_unique(medecin_id)
    assert creer(agent, nouveau_patient(), medecin_id, jour, heure).status_code == 201
    r = creer(agent, nouveau_patient(), medecin_id, jour, heure)
    assert r.status_code == 409
    assert r.json()["code_erreur"] == "CONFLIT_MEDECIN"


def test_rg03_heure_hors_grille_refusee(agent, nouveau_patient, medecin_id):
    jour, heure = creneau_unique(medecin_id)
    decale = (datetime.combine(jour, heure) + timedelta(minutes=5)).time()
    r = creer(agent, nouveau_patient(), medecin_id, jour, decale)
    assert r.status_code == 400
    assert r.json()["code_erreur"] == "CRENEAU_NON_ALIGNE"


def test_rg02_patient_double_reservation(agent, nouveau_patient, medecin_id, autre_medecin_id):
    jour, heure = creneau_unique(medecin_id, autre_medecin_id)
    p = nouveau_patient()
    assert creer(agent, p, medecin_id, jour, heure).status_code == 201
    r = creer(agent, p, autre_medecin_id, jour, heure)
    assert r.status_code == 409
    assert r.json()["code_erreur"] == "CONFLIT_PATIENT"


def test_annulation_libere_le_creneau(agent, nouveau_patient, medecin_id):
    jour, heure = creneau_unique(medecin_id)
    rdv = creer(agent, nouveau_patient(), medecin_id, jour, heure).json()["rdv"]["id"]
    r = agent.patch(f"/rdv/{rdv}", json={"statut": "Annulé", "motif_annulation": "Patient"})
    assert r.status_code == 200, r.text
    assert creer(agent, nouveau_patient(), medecin_id, jour, heure).status_code == 201


def test_rg04_pas_plus_de_90_jours(agent, nouveau_patient, medecin_id):
    r = creer(agent, nouveau_patient(), medecin_id, date.today() + timedelta(days=91), time(10, 0))
    assert r.status_code == 422
    assert "90 jours" in r.text
    # Le 90e jour est accepté par RG-04 : le refus éventuel vient alors d'une autre règle (dimanche, férié...), pas de RG-04.
    assert creer(agent, nouveau_patient(), medecin_id, date.today() + timedelta(days=90), time(10, 0)).status_code != 422


def test_creation_dans_le_passe_refusee(agent, nouveau_patient, medecin_id):
    r = creer(agent, nouveau_patient(), medecin_id, date.today() - timedelta(days=3), time(10, 0))
    assert r.status_code == 422


def test_patient_ou_medecin_inconnu(agent, nouveau_patient, medecin_id):
    assert creer(agent, "PAT99999", medecin_id).status_code == 404
    assert creer(agent, nouveau_patient(), "MED999").status_code == 404


def test_rg05_annulation_sans_motif(agent, nouveau_patient, medecin_id):
    rdv = creer(agent, nouveau_patient(), medecin_id).json()["rdv"]["id"]
    r = agent.patch(f"/rdv/{rdv}", json={"statut": "Annulé"})
    assert r.status_code == 400
    assert r.json()["code_erreur"] == "MOTIF_ANNULATION_MANQUANT"


def test_motif_sans_annulation(agent, nouveau_patient, medecin_id):
    rdv = creer(agent, nouveau_patient(), medecin_id).json()["rdv"]["id"]
    r = agent.patch(f"/rdv/{rdv}", json={"statut": "Confirmé", "motif_annulation": "Patient"})
    assert r.status_code == 400


def test_confirmation_puis_pas_de_retour(agent, nouveau_patient, medecin_id):
    rdv = creer(agent, nouveau_patient(), medecin_id).json()["rdv"]["id"]
    assert agent.patch(f"/rdv/{rdv}", json={"statut": "Confirmé"}).status_code == 200
    assert agent.patch(f"/rdv/{rdv}", json={"statut": "Planifié"}).status_code == 400


def test_rdv_clos_non_modifiable(agent, nouveau_patient, medecin_id):
    rdv = creer(agent, nouveau_patient(), medecin_id).json()["rdv"]["id"]
    agent.patch(f"/rdv/{rdv}", json={"statut": "Annulé", "motif_annulation": "Patient"})
    r = agent.patch(f"/rdv/{rdv}", json={"statut": "Confirmé"})
    assert r.status_code == 400
    assert r.json()["code_erreur"] == "RDV_CLOS"


def test_honore_avant_le_jour_refuse(agent, admin, nouveau_patient, medecin_id):
    rdv = creer(agent, nouveau_patient(), medecin_id).json()["rdv"]["id"]
    r = admin.patch(f"/rdv/{rdv}", json={"statut": "Honoré"})
    assert r.status_code == 400
    assert r.json()["code_erreur"] == "STATUT_FINAL_PREMATURE"


def test_agent_ne_marque_pas_honore(agent, nouveau_patient, medecin_id):
    patient = nouveau_patient()
    rdv = inserer_rdv(patient, medecin_id, datetime.combine(date.today(), time(5, 0)))
    assert agent.patch(f"/rdv/{rdv}", json={"statut": "Honoré"}).status_code == 403


def test_responsable_marque_honore_et_absent(responsable, nouveau_patient, medecin_id):
    p = nouveau_patient()
    r1 = inserer_rdv(p, medecin_id, datetime.combine(date.today(), time(4, 0)))
    r2 = inserer_rdv(p, medecin_id, datetime.combine(date.today(), time(4, 30)))
    assert responsable.patch(f"/rdv/{r1}", json={"statut": "Honoré"}).status_code == 200
    assert responsable.patch(f"/rdv/{r2}", json={"statut": "Absent"}).status_code == 200


def test_reprogrammation_controle_les_conflits(agent, nouveau_patient, medecin_id):
    jour, heure = creneau_unique(medecin_id)
    creer(agent, nouveau_patient(), medecin_id, jour, heure)
    rdv = creer(agent, nouveau_patient(), medecin_id).json()["rdv"]["id"]
    r = agent.patch(f"/rdv/{rdv}", json={"date_rdv": jour.isoformat(), "heure_rdv": heure.strftime("%H:%M")})
    assert r.status_code == 409
    nj, nh = creneau_unique(medecin_id)
    assert agent.patch(f"/rdv/{rdv}", json={"date_rdv": nj.isoformat(), "heure_rdv": nh.strftime("%H:%M")}).status_code == 200


def test_patch_vide_refuse(agent, nouveau_patient, medecin_id):
    rdv = creer(agent, nouveau_patient(), medecin_id).json()["rdv"]["id"]
    assert agent.patch(f"/rdv/{rdv}", json={}).status_code in (400, 422)


def test_rdv_introuvable(agent):
    assert agent.patch("/rdv/RDV00000000", json={"statut": "Confirmé"}).status_code == 404


def test_rg08_alerte_recidiviste(agent, nouveau_patient, medecin_id):
    p = nouveau_patient()
    for i in range(3):
        inserer_rdv(p, medecin_id, datetime.combine(date.today() - timedelta(days=20 + i), time(8 + i, 0)), statut=StatutRdv.ABSENT)
    fiche = agent.get(f"/patients/{p}").json()
    assert fiche["alerte_recidiviste"]["actif"] is True
    r = creer(agent, p, medecin_id)
    assert r.status_code == 201
    assert any(a["code"] == "PATIENT_RECIDIVISTE" for a in r.json()["avertissements"])


def test_medecin_ne_voit_que_ses_rdv(connexion, medecins):
    client = connexion("MED001") if any(m["id"] == "MED001" for m in medecins) else None
    if client is None:
        pytest.skip("compte MED001 absent")
    ids = {r["medecin_id"] for r in client.get("/rdv?limite=200").json()["elements"]}
    assert ids <= {"MED001"}


def test_liste_filtres(agent, medecin_id):
    r = agent.get(f"/rdv?medecin_id={medecin_id}&statuts=Planifié&limite=5")
    assert r.status_code == 200
    assert all(e["statut"] == "Planifié" for e in r.json()["elements"])


def test_referentiels(agent):
    assert agent.get("/referentiels/medecins").status_code == 200
