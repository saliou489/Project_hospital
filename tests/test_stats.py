"""Indicateurs : cohérence des chiffres, droits, filtres."""
from datetime import date, timedelta


def test_kpi_coherents(direction):
    r = direction.get("/stats/kpi")
    assert r.status_code == 200
    k = r.json()
    assert k["total_rdv"] == sum(k["repartition_statuts"].values())
    assert k["base_calcul_absence"] == k["honores"] + k["absents"]
    if k["base_calcul_absence"]:
        assert abs(k["taux_absence_pct"] - 100 * k["absents"] / k["base_calcul_absence"]) < 0.1


def test_kpi_acces(agent, direction, responsable, admin):
    assert agent.get("/stats/kpi").status_code == 403
    for c in (direction, responsable, admin):
        assert c.get("/stats/kpi").status_code == 200


def test_kpi_periode_invalide(direction):
    r = direction.get("/stats/kpi", params={"date_debut": "2026-05-02", "date_fin": "2026-05-01"})
    assert r.status_code in (400, 422)


def test_kpi_filtre_specialite_et_medecin(direction, medecins):
    m = medecins[0]
    total = direction.get("/stats/kpi").json()["total_rdv"]
    par_medecin = direction.get("/stats/kpi", params={"medecin_id": m["id"]})
    assert par_medecin.status_code in (200, 403)
    if par_medecin.status_code == 200:
        assert par_medecin.json()["total_rdv"] <= total
    assert direction.get("/stats/kpi", params={"specialite": m["specialite"]}).json()["total_rdv"] <= total


def test_kpi_medecin_inconnu(responsable):
    assert responsable.get("/stats/kpi", params={"medecin_id": "MED999"}).status_code == 404


def test_absence_par_specialite(direction):
    r = direction.get("/stats/absence/par-specialite")
    assert r.status_code == 200 and isinstance(r.json(), list) and r.json()


def test_absence_mensuelle(direction):
    r = direction.get("/stats/absence/mensuelle")
    assert r.status_code == 200 and isinstance(r.json(), list)


def test_futur_hors_taux_absence(direction):
    demain = date.today() + timedelta(days=1)
    k = direction.get("/stats/kpi", params={"date_debut": demain.isoformat()}).json()
    assert k["honores"] == 0 and k["absents"] == 0
