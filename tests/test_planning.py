"""Planning : spécialités, plages, jours fermés, créneaux, indisponibilités, occupation, historique, absence automatique."""
from datetime import date, datetime, time, timedelta

import pytest

from backend.database import SessionLocale
from backend.models import StatutRdv
from backend.planning import (
    ContexteMedecins,
    _paques,
    creneaux_de_plage,
    initialiser_referentiels,
    jours_feries_calculables,
    marquer_absences_automatiques,
)
from tests.conftest import _jours_ouverts, corps_rdv, inserer_rdv, lettres


@pytest.fixture
def medecin_neuf(admin):
    """Médecin créé pour le test (plages par défaut) : ses créneaux et indisponibilités n'affectent aucun autre test."""
    specialites = admin.get("/referentiels/specialites").json()
    spec = next(s for s in specialites if s["actif"])
    r = admin.post("/admin/utilisateurs", json={"role": "Médecin", "nom_medecin": "Dr Test Planning", "specialite": spec["nom"]})
    assert r.status_code == 201, r.text
    return {"id": r.json()["utilisateur"]["id"], "duree": spec["duree_standard_min"], "specialite": spec["nom"]}


def ouvert(rang: int = 0, heure: int = 9) -> tuple[date, time]:
    return _jours_ouverts(depuis=20)[rang], time(heure, 0)


# ---- Unités ---------------------------------------------------------------------------------------


def test_paques_connues():
    assert _paques(2026) == date(2026, 4, 5)
    assert _paques(2027) == date(2027, 3, 28)
    assert _paques(2024) == date(2024, 3, 31)


def test_feries_calculables():
    jours = dict(jours_feries_calculables(2026))
    assert date(2026, 4, 4) in jours and date(2026, 1, 1) in jours and date(2026, 12, 25) in jours
    assert date(2026, 4, 6) in jours  # lundi de Pâques


def test_creneaux_de_plage():
    class Plage:
        debut, fin = time(8, 0), time(9, 0)

    assert [d.strftime("%H:%M") for d, _ in creneaux_de_plage(date(2026, 1, 5), Plage, 20)] == ["08:00", "08:20", "08:40"]
    assert len(creneaux_de_plage(date(2026, 1, 5), Plage, 30)) == 2
    Plage.fin = time(8, 50)  # le dernier créneau doit tenir dans la plage
    assert len(creneaux_de_plage(date(2026, 1, 5), Plage, 30)) == 1


def test_referentiels_idempotents(cles):
    with SessionLocale() as db:
        assert initialiser_referentiels(db) == {"specialites": 0, "plages": 0, "feries": 0}


# ---- EF-01 spécialités ----------------------------------------------------------------------------


def test_specialites_lues_par_tous(agent):
    r = agent.get("/referentiels/specialites")
    assert r.status_code == 200
    assert len(r.json()) == 9
    assert all(s["couleur"].startswith("#") and s["duree_standard_min"] in (15, 20, 30) for s in r.json())


def test_specialite_creation_et_modification(admin, agent):
    nom = "Test" + lettres(900)
    assert agent.post("/referentiels/specialites", json={"nom": nom, "duree_standard_min": 20, "couleur": "#112233"}).status_code == 403
    r = admin.post("/referentiels/specialites", json={"nom": nom, "duree_standard_min": 20, "couleur": "#112233"})
    assert r.status_code == 201, r.text
    assert admin.post("/referentiels/specialites", json={"nom": nom.upper(), "duree_standard_min": 15, "couleur": "#112233"}).status_code == 409
    assert admin.post("/referentiels/specialites", json={"nom": nom + "x", "duree_standard_min": 25, "couleur": "#112233"}).status_code == 422
    assert admin.post("/referentiels/specialites", json={"nom": nom + "y", "duree_standard_min": 15, "couleur": "rouge"}).status_code == 422
    r = admin.patch(f"/referentiels/specialites/{nom}", json={"duree_standard_min": 30, "actif": False})
    assert r.status_code == 200 and r.json()["duree_standard_min"] == 30 and r.json()["actif"] is False
    assert admin.patch(f"/referentiels/specialites/{nom}", json={"duree_standard_min": 30}).status_code == 422
    assert admin.patch("/referentiels/specialites/Inconnue", json={"actif": True}).status_code == 404


def test_medecin_exige_une_specialite_active(admin):
    r = admin.post("/admin/utilisateurs", json={"role": "Médecin", "nom_medecin": "Dr X", "specialite": "Specialite Inexistante"})
    assert r.status_code == 400 and r.json()["code_erreur"] == "SPECIALITE_INCONNUE"


def test_medecin_duree_incoherente(admin):
    spec = admin.get("/referentiels/specialites").json()[0]
    autre = next(d for d in (15, 20, 30) if d != spec["duree_standard_min"])
    r = admin.post("/admin/utilisateurs", json={"role": "Médecin", "nom_medecin": "Dr Y", "specialite": spec["nom"], "duree_consultation_min": autre})
    assert r.status_code == 400 and r.json()["code_erreur"] == "DUREE_INCOHERENTE"


# ---- EF-02 plages ---------------------------------------------------------------------------------


def test_plages_par_defaut(agent, medecin_neuf):
    r = agent.get(f"/referentiels/medecins/{medecin_neuf['id']}/plages").json()
    assert len(r["plages"]) == 12
    assert {p["jour_semaine"] for p in r["plages"]} == {0, 1, 2, 3, 4, 5}


def test_plages_remplacement_et_droits(agent, responsable, medecin_neuf):
    url = f"/referentiels/medecins/{medecin_neuf['id']}/plages"
    nouvelles = {"plages": [{"jour_semaine": 0, "debut": "09:00", "fin": "12:00"}]}
    assert agent.put(url, json=nouvelles).status_code == 403
    r = responsable.put(url, json=nouvelles)
    assert r.status_code == 200 and len(r.json()["plages"]) == 1
    assert len(responsable.get(url).json()["plages"]) == 1


def test_plages_invalides(responsable, medecin_neuf):
    url = f"/referentiels/medecins/{medecin_neuf['id']}/plages"
    chevauche = {"plages": [{"jour_semaine": 1, "debut": "08:00", "fin": "10:00"}, {"jour_semaine": 1, "debut": "09:00", "fin": "11:00"}]}
    assert responsable.put(url, json=chevauche).json()["code_erreur"] == "PLAGES_CHEVAUCHANTES"
    assert responsable.put(url, json={"plages": [{"jour_semaine": 1, "debut": "12:00", "fin": "08:00"}]}).status_code == 422
    assert responsable.put(url, json={"plages": [{"jour_semaine": 9, "debut": "08:00", "fin": "09:00"}]}).status_code == 422
    assert responsable.put("/referentiels/medecins/MED999/plages", json={"plages": []}).status_code == 404


def test_plages_signalent_les_rdv_sortants(agent, responsable, nouveau_patient, medecin_neuf):
    jour, heure = ouvert(1, 16)
    rdv = agent.post("/rdv", json=corps_rdv(nouveau_patient(), medecin_neuf["id"], jour, heure)).json()["rdv"]["id"]
    r = responsable.put(f"/referentiels/medecins/{medecin_neuf['id']}/plages", json={"plages": [{"jour_semaine": jour.weekday(), "debut": "08:00", "fin": "12:00"}]})
    assert rdv in r.json()["rdv_hors_planning"]


# ---- EF-05 jours fermés + RG-03 -------------------------------------------------------------------


def test_feries_listes(agent):
    annee = date.today().year
    r = agent.get("/referentiels/feries", params={"annee": annee})
    assert r.status_code == 200
    assert any(j["jour"] == f"{annee}-12-25" for j in r.json())


def test_rdv_refuse_un_jour_ferme(agent, responsable, nouveau_patient, medecin_neuf):
    jour = _jours_ouverts(depuis=40)[3]
    r = responsable.post("/referentiels/feries", json={"jour": jour.isoformat(), "libelle": "Fermeture de test", "type": "Fermeture exceptionnelle"})
    assert r.status_code == 201, r.text
    assert responsable.post("/referentiels/feries", json={"jour": jour.isoformat(), "libelle": "Doublon"}).status_code == 409
    refus = agent.post("/rdv", json=corps_rdv(nouveau_patient(), medecin_neuf["id"], jour, time(9, 0)))
    assert refus.status_code == 400 and refus.json()["code_erreur"] == "JOUR_FERME"
    assert responsable.delete(f"/referentiels/feries/{jour.isoformat()}").status_code == 204
    assert responsable.delete(f"/referentiels/feries/{jour.isoformat()}").status_code == 404
    assert agent.post("/rdv", json=corps_rdv(nouveau_patient(), medecin_neuf["id"], jour, time(9, 0))).status_code == 201


def test_ferie_liste_les_rdv_a_reprogrammer(agent, responsable, nouveau_patient, medecin_neuf):
    jour = _jours_ouverts(depuis=40)[8]
    rdv = agent.post("/rdv", json=corps_rdv(nouveau_patient(), medecin_neuf["id"], jour, time(10, 0))).json()["rdv"]["id"]
    r = responsable.post("/referentiels/feries", json={"jour": jour.isoformat(), "libelle": "Fermeture imprévue", "type": "Fermeture exceptionnelle"})
    assert rdv in [x["id"] for x in r.json()["rdv_a_reprogrammer"]]
    responsable.delete(f"/referentiels/feries/{jour.isoformat()}")


def test_feries_droits(agent):
    assert agent.post("/referentiels/feries", json={"jour": "2031-01-01", "libelle": "Test"}).status_code == 403


# ---- RG-03 plages ---------------------------------------------------------------------------------


def test_rdv_refuse_hors_plage(agent, nouveau_patient, medecin_neuf):
    jour, _ = ouvert(2)
    p = nouveau_patient()
    r = agent.post("/rdv", json=corps_rdv(p, medecin_neuf["id"], jour, time(13, 0)))  # pause de midi
    assert r.status_code == 400 and r.json()["code_erreur"] == "HORS_PLAGE"
    r = agent.post("/rdv", json=corps_rdv(p, medecin_neuf["id"], jour, time(7, 0)))
    assert r.json()["code_erreur"] == "HORS_PLAGE"


def test_rdv_refuse_le_dimanche(agent, nouveau_patient, medecin_neuf):
    jour = date.today() + timedelta(days=30)
    while jour.weekday() != 6:
        jour += timedelta(days=1)
    r = agent.post("/rdv", json=corps_rdv(nouveau_patient(), medecin_neuf["id"], jour, time(9, 0)))
    assert r.status_code == 400 and r.json()["code_erreur"] in ("HORS_PLAGE", "JOUR_FERME")


def test_reprogrammation_controle_les_plages(agent, nouveau_patient, medecin_neuf):
    jour, heure = ouvert(4)
    rdv = agent.post("/rdv", json=corps_rdv(nouveau_patient(), medecin_neuf["id"], jour, heure)).json()["rdv"]["id"]
    r = agent.patch(f"/rdv/{rdv}", json={"date_rdv": jour.isoformat(), "heure_rdv": "13:00"})
    assert r.status_code == 400 and r.json()["code_erreur"] == "HORS_PLAGE"


# ---- EF-10 et EF-11 créneaux ----------------------------------------------------------------------


def test_creneaux_du_jour(agent, medecin_neuf):
    jour, _ = ouvert(5)
    r = agent.get("/planning/creneaux", params={"medecin_id": medecin_neuf["id"], "date_debut": jour.isoformat(), "date_fin": jour.isoformat()})
    assert r.status_code == 200
    duree = medecin_neuf["duree"]
    attendu = 2 * (240 // duree)
    assert len(r.json()) == attendu
    assert all(c["etat"] == "libre" for c in r.json())
    assert r.json()[0]["heure_rdv"].startswith("08:00")


def test_creneau_pris_devient_occupe_puis_libre(agent, nouveau_patient, medecin_neuf):
    jour, heure = ouvert(6, 10)
    params = {"medecin_id": medecin_neuf["id"], "date_debut": jour.isoformat(), "date_fin": jour.isoformat()}
    rdv = agent.post("/rdv", json=corps_rdv(nouveau_patient(), medecin_neuf["id"], jour, heure)).json()["rdv"]["id"]
    pris = [c for c in agent.get("/planning/creneaux", params=params).json() if c["etat"] == "occupé"]
    assert len(pris) == 1 and pris[0]["rdv_id"] == rdv
    agent.patch(f"/rdv/{rdv}", json={"statut": "Annulé", "motif_annulation": "Patient"})  # RG-06
    assert not [c for c in agent.get("/planning/creneaux", params=params).json() if c["etat"] == "occupé"]


def test_creneaux_periode_invalide(agent, medecin_neuf):
    r = agent.get("/planning/creneaux", params={"medecin_id": medecin_neuf["id"], "date_debut": "2027-01-10", "date_fin": "2027-01-01"})
    assert r.status_code == 400
    r = agent.get("/planning/creneaux", params={"medecin_id": medecin_neuf["id"], "date_debut": "2027-01-01", "date_fin": "2029-01-01"})
    assert r.json()["code_erreur"] == "PERIODE_TROP_LONGUE"
    assert agent.get("/planning/creneaux", params={"medecin_id": "MED999", "date_debut": "2027-01-01", "date_fin": "2027-01-02"}).status_code == 404


def test_premier_creneau_libre_par_medecin(agent, medecin_neuf):
    r = agent.get("/planning/premiers-creneaux", params={"medecin_id": medecin_neuf["id"], "limite": 3})
    assert r.status_code == 200 and len(r.json()) == 3
    debuts = [(c["date_rdv"], c["heure_rdv"]) for c in r.json()]
    assert debuts == sorted(debuts)
    premier = datetime.fromisoformat(f"{debuts[0][0]}T{debuts[0][1]}")
    assert premier > datetime.utcnow()


def test_premier_creneau_libre_par_specialite(agent, medecin_neuf):
    r = agent.get("/planning/premiers-creneaux", params={"specialite": medecin_neuf["specialite"], "limite": 5})
    assert r.status_code == 200 and r.json()
    assert all(c["specialite"] == medecin_neuf["specialite"] and c["etat"] == "libre" for c in r.json())


def test_premier_creneau_libre_saute_le_pris(agent, nouveau_patient, medecin_neuf):
    premier = agent.get("/planning/premiers-creneaux", params={"medecin_id": medecin_neuf["id"]}).json()[0]
    agent.post("/rdv", json=corps_rdv(nouveau_patient(), medecin_neuf["id"], date.fromisoformat(premier["date_rdv"]), time.fromisoformat(premier["heure_rdv"])))
    suivant = agent.get("/planning/premiers-creneaux", params={"medecin_id": medecin_neuf["id"]}).json()[0]
    assert (suivant["date_rdv"], suivant["heure_rdv"]) != (premier["date_rdv"], premier["heure_rdv"])


def test_premier_creneau_respecte_rg02(agent, nouveau_patient, medecin_neuf):
    patient = nouveau_patient()
    premier = agent.get("/planning/premiers-creneaux", params={"medecin_id": medecin_neuf["id"]}).json()[0]
    pris = False
    for autre in (m for m in agent.get("/referentiels/medecins").json() if m["id"] != medecin_neuf["id"]):
        r = agent.post("/rdv", json=corps_rdv(patient, autre["id"], date.fromisoformat(premier["date_rdv"]), time.fromisoformat(premier["heure_rdv"])))
        if r.status_code == 201:  # un autre médecin déjà occupé à cette heure (base partagée) ne convient pas : on essaie le suivant
            pris = True
            break
    assert pris, "aucun autre médecin libre sur ce créneau"
    avec = agent.get("/planning/premiers-creneaux", params={"medecin_id": medecin_neuf["id"], "patient_id": patient}).json()[0]
    assert (avec["date_rdv"], avec["heure_rdv"]) != (premier["date_rdv"], premier["heure_rdv"])


def test_premier_creneau_horizon_90_jours(agent, medecin_neuf):
    loin = (date.today() + timedelta(days=200)).isoformat()
    assert agent.get("/planning/premiers-creneaux", params={"medecin_id": medecin_neuf["id"], "date_debut": loin}).json() == []


def test_premier_creneau_critere_obligatoire_et_droits(agent, direction):
    assert agent.get("/planning/premiers-creneaux").json()["code_erreur"] == "CRITERE_MANQUANT"
    assert direction.get("/planning/premiers-creneaux", params={"specialite": "ORL"}).status_code == 403


# ---- EF-14 indisponibilités -----------------------------------------------------------------------


def test_indisponibilite_bloque_et_liste_les_rdv(agent, responsable, nouveau_patient, medecin_neuf):
    jour, heure = ouvert(10, 14)
    mid = medecin_neuf["id"]
    rdv = agent.post("/rdv", json=corps_rdv(nouveau_patient(), mid, jour, heure)).json()["rdv"]["id"]
    corps = {"medecin_id": mid, "debut": f"{jour}T08:00:00", "fin": f"{jour}T18:00:00", "motif": "Congrès médical"}
    r = responsable.post("/planning/indisponibilites", json=corps)
    assert r.status_code == 201, r.text
    ident = r.json()["indisponibilite"]["id"]
    assert rdv in [x["id"] for x in r.json()["rdv_a_reprogrammer"]]
    refus = agent.post("/rdv", json=corps_rdv(nouveau_patient(), mid, jour, time(9, 0)))
    assert refus.status_code == 400 and refus.json()["code_erreur"] == "MEDECIN_INDISPONIBLE"
    etats = {c["etat"] for c in agent.get("/planning/creneaux", params={"medecin_id": mid, "date_debut": jour.isoformat(), "date_fin": jour.isoformat()}).json()}
    assert "indisponible" in etats and "libre" not in etats
    # Reprogrammer le rendez-vous le fait disparaître de la liste des rendez-vous impactés.
    nj, nh = ouvert(11, 15)
    assert agent.patch(f"/rdv/{rdv}", json={"date_rdv": nj.isoformat(), "heure_rdv": nh.strftime("%H:%M")}).status_code == 200
    assert responsable.get(f"/planning/indisponibilites/{ident}/rdv-impactes").json() == []
    # Levée : les créneaux se rouvrent, la ligne est conservée.
    assert responsable.post(f"/planning/indisponibilites/{ident}/lever").status_code == 200
    assert responsable.post(f"/planning/indisponibilites/{ident}/lever").status_code == 409
    assert agent.post("/rdv", json=corps_rdv(nouveau_patient(), mid, jour, time(9, 0))).status_code == 201
    inactives = responsable.get("/planning/indisponibilites", params={"actives_seulement": "false", "medecin_id": mid}).json()
    assert any(i["id"] == ident and i["active"] is False for i in inactives)


def test_indisponibilite_validations(responsable):
    base = {"medecin_id": "MED001", "motif": "Congé"}
    assert responsable.post("/planning/indisponibilites", json={**base, "debut": "2031-01-02T08:00:00", "fin": "2031-01-01T08:00:00"}).status_code == 422
    assert responsable.post("/planning/indisponibilites", json={**base, "debut": "2020-01-01T08:00:00", "fin": "2020-01-02T08:00:00"}).status_code == 422
    assert responsable.post("/planning/indisponibilites", json={**base, "medecin_id": "MED999", "debut": "2031-01-01T08:00:00", "fin": "2031-01-02T08:00:00"}).status_code == 404
    assert responsable.post("/planning/indisponibilites/99999/lever").status_code == 404


def test_medecin_declare_seulement_ses_indisponibilites(connexion, medecins):
    a, b = medecins[0]["id"], medecins[1]["id"]
    client = connexion(a)
    corps = {"debut": "2031-02-01T08:00:00", "fin": "2031-02-01T12:00:00", "motif": "Formation"}
    assert client.post("/planning/indisponibilites", json={**corps, "medecin_id": b}).status_code == 403
    r = client.post("/planning/indisponibilites", json={**corps, "medecin_id": a})
    assert r.status_code == 201
    client.post(f"/planning/indisponibilites/{r.json()['indisponibilite']['id']}/lever")


def test_agent_ne_declare_pas_d_indisponibilite(agent):
    corps = {"medecin_id": "MED001", "motif": "Congé", "debut": "2031-01-01T08:00:00", "fin": "2031-01-02T08:00:00"}
    assert agent.post("/planning/indisponibilites", json=corps).status_code == 403


# ---- RG-10 occupation -----------------------------------------------------------------------------


def test_occupation(direction, responsable):
    debut = (date.today() - timedelta(days=60)).isoformat()
    fin = (date.today() - timedelta(days=1)).isoformat()
    r = responsable.get("/stats/occupation", params={"date_debut": debut, "date_fin": fin})
    assert r.status_code == 200
    d = r.json()
    assert d["creneaux_ouverts"] > 0 and 0 <= d["taux_occupation_pct"] <= 100
    assert d["creneaux_ouverts"] == sum(m["creneaux_ouverts"] for m in d["par_medecin"])
    assert d["creneaux_occupes"] == sum(m["creneaux_occupes"] for m in d["par_medecin"])
    agrege = direction.get("/stats/occupation", params={"date_debut": debut, "date_fin": fin}).json()
    assert agrege["par_medecin"] == [] and agrege["taux_occupation_pct"] == d["taux_occupation_pct"]
    assert direction.get("/stats/occupation", params={"medecin_id": "MED001"}).status_code == 403


def test_occupation_exclut_fermetures_et_indisponibilites(responsable, agent, medecin_neuf):
    mid = medecin_neuf["id"]
    jour = date.today() - timedelta(days=14)
    while jour.weekday() == 6:
        jour -= timedelta(days=1)
    params = {"date_debut": jour.isoformat(), "date_fin": jour.isoformat(), "medecin_id": mid}
    r = responsable.get("/stats/occupation", params=params).json()
    if r["creneaux_ouverts"] == 0:  # le jour est férié : rien à tester
        pytest.skip("jour férié")
    assert r["creneaux_ouverts"] == 2 * (240 // medecin_neuf["duree"]) and r["creneaux_occupes"] == 0
    inserer_rdv("PAT00001", mid, datetime.combine(jour, time(9, 0)), statut=StatutRdv.HONORE, duree=medecin_neuf["duree"])
    r2 = responsable.get("/stats/occupation", params=params).json()
    assert r2["creneaux_occupes"] == 1


def test_occupation_periode_invalide(responsable):
    assert responsable.get("/stats/occupation", params={"date_debut": "2026-05-02", "date_fin": "2026-05-01"}).status_code == 400


# ---- RG-11 historique -----------------------------------------------------------------------------


def test_historique_complet(agent, admin, nouveau_patient, medecin_neuf):
    mid = medecin_neuf["id"]
    jour, heure = ouvert(12, 9)
    rdv = agent.post("/rdv", json=corps_rdv(nouveau_patient(), mid, jour, heure)).json()["rdv"]["id"]
    agent.patch(f"/rdv/{rdv}", json={"statut": "Confirmé"})
    nj, nh = ouvert(13, 10)
    agent.patch(f"/rdv/{rdv}", json={"date_rdv": nj.isoformat(), "heure_rdv": nh.strftime("%H:%M")})
    agent.patch(f"/rdv/{rdv}", json={"statut": "Annulé", "motif_annulation": "Clinique"})
    h = agent.get(f"/rdv/{rdv}/historique").json()
    assert [x["action"] for x in h] == ["CREATION", "STATUT", "REPROGRAMMATION", "STATUT", "ANNULATION"]
    assert h[0]["utilisateur_id"] == "AGT01" and h[3]["avant"] == "Confirmé" and h[3]["apres"] == "Annulé"
    assert agent.get("/rdv/RDV00000000/historique").status_code == 404


def test_historique_d_un_autre_medecin_refuse(connexion, medecins, agent, nouveau_patient):
    a, b = medecins[0]["id"], medecins[1]["id"]
    rdv = inserer_rdv(nouveau_patient(), b, datetime.combine(date.today() - timedelta(days=40), time(9, 0)))
    assert connexion(a).get(f"/rdv/{rdv}/historique").status_code == 403


# ---- EF-17 absence automatique --------------------------------------------------------------------


def test_absence_automatique(nouveau_patient, medecin_neuf, agent):
    mid = medecin_neuf["id"]
    p = nouveau_patient()
    maintenant = datetime.utcnow().replace(microsecond=0)
    recent = inserer_rdv(p, mid, maintenant - timedelta(minutes=90), duree=medecin_neuf["duree"])
    trop_recent = inserer_rdv(nouveau_patient(), mid, maintenant - timedelta(minutes=30), duree=medecin_neuf["duree"])
    ancien = inserer_rdv(nouveau_patient(), mid, maintenant - timedelta(days=20), duree=medecin_neuf["duree"])
    confirme = inserer_rdv(nouveau_patient(), mid, maintenant - timedelta(hours=3), statut=StatutRdv.CONFIRME, duree=medecin_neuf["duree"])
    honore = inserer_rdv(nouveau_patient(), mid, maintenant - timedelta(hours=5), statut=StatutRdv.HONORE, duree=medecin_neuf["duree"])
    with SessionLocale() as db:
        assert marquer_absences_automatiques(db) >= 2
        assert marquer_absences_automatiques(db) == 0  # idempotent
    statut = lambda rid: agent.get(f"/rdv/{rid}").json()["statut"]  # noqa: E731
    assert statut(recent) == "Absent" and statut(confirme) == "Absent"
    assert statut(trop_recent) == "Planifié" and statut(ancien) == "Planifié" and statut(honore) == "Honoré"
    historique = agent.get(f"/rdv/{recent}/historique").json()
    assert historique[-1]["action"] == "ABSENCE_AUTO" and historique[-1]["utilisateur_id"] is None


# ---- Recherche rapide du créneau occupé -------------------------------------------------------------


def test_recherche_occupant_identique_au_parcours_exhaustif(medecin_neuf):
    """La recherche par dichotomie doit donner le même résultat que le parcours de tous les rendez-vous,
    y compris pour des rendez-vous de durée irrégulière chevauchant plusieurs créneaux."""
    from backend.models import Medecin

    mid = medecin_neuf["id"]
    jour = date.today() - timedelta(days=21)
    while jour.weekday() == 6:
        jour -= timedelta(days=1)
    for heure, minute, duree in [(8, 0, 15), (9, 10, 40), (10, 0, 90), (14, 5, 20), (16, 30, 45)]:
        inserer_rdv("PAT00001", mid, datetime.combine(jour, time(heure, minute)), statut=StatutRdv.HONORE, duree=duree)
    with SessionLocale() as db:
        medecin = db.get(Medecin, mid)
        contexte = ContexteMedecins(db, [medecin], jour, jour)
        creneaux = contexte.creneaux(mid, jour)
        if not creneaux:
            pytest.skip("jour férié")
        occupes = 0
        for c in creneaux:
            attendu = next((r.id for r in contexte.rdv[mid] if r.debut < c.fin and r.fin > c.debut), None)
            assert c.rdv_id == attendu
            occupes += attendu is not None
        assert occupes >= 5


def test_taux_occupation_une_annee_reste_rapide():
    import time as chrono

    from backend.planning import taux_occupation

    with SessionLocale() as db:
        debut = chrono.perf_counter()
        taux_occupation(db, date.today() - timedelta(days=365), date.today())
        assert chrono.perf_counter() - debut < 5.0
