"""Score de risque d'absence (EF-25) : variables sans fuite d'information, seuil coût / bénéfice, droits, jamais bloquant."""
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

from backend.analytique import risque
from tests.conftest import corps_rdv


def _brut(lignes):
    return pd.DataFrame(
        lignes, columns=["rdv_id", "patient_id", "specialite", "debut", "date_prise", "statut", "confirme", "date_naissance"]
    )


# --- Fonctions pures ---------------------------------------------------------------------------------

def test_seuil_cout_benefice():
    h = risque.Hypotheses(cout_relance=1000, cout_creneau=15000, efficacite=0.4)
    assert risque.seuil_cout_benefice(h) == pytest.approx(1000 / (0.4 * 15000))
    # Un appel très cher par rapport au gain attendu ne peut pas donner un seuil supérieur à 1.
    assert risque.seuil_cout_benefice(risque.Hypotheses(50000, 1000, 0.1)) == 1.0


def test_historique_n_utilise_que_le_passe():
    """L'historique d'un rendez-vous ignore le rendez-vous lui-même et tous ceux qui le suivent."""
    j = datetime(2026, 1, 5, 9, 0)
    brut = _brut([
        ("R1", "P1", "Cardiologie", j, j.date() - timedelta(days=3), "Absent", False, date(1990, 1, 1)),
        ("R2", "P1", "Cardiologie", j + timedelta(days=7), j.date(), "Honoré", True, date(1990, 1, 1)),
        ("R3", "P1", "Cardiologie", j + timedelta(days=14), j.date(), "Absent", False, date(1990, 1, 1)),
        ("R4", "P1", "Cardiologie", j + timedelta(days=21), j.date(), "Planifié", False, date(1990, 1, 1)),
        ("R5", "P1", "Cardiologie", j + timedelta(days=28), j.date(), "Planifié", False, date(1990, 1, 1)),
    ])
    d = risque.construire_variables(brut).set_index("rdv_id")
    assert list(d.loc[["R1", "R2", "R3", "R4", "R5"], "nb_absences"]) == [0, 1, 1, 2, 2]
    assert list(d.loc[["R1", "R2", "R3", "R4"], "nb_rdv_clos_passes"]) == [0, 1, 2, 3]
    assert d.loc["R5", "nb_rdv_clos_passes"] == 3  # R4 n'est pas encore clos : il ne compte pas
    assert d.loc["R1", "taux_absence_passe"] == pytest.approx(risque.TAUX_ABSENCE_PRIOR)  # aucun historique : taux a priori
    assert d.loc["R1", "delai_jours"] == 3 and d.loc["R1", "heure"] == 9.0 and d.loc["R1", "jour_semaine"] == "0"
    assert d.loc["R1", "age"] == pytest.approx(36, abs=0.1) and d.loc["R1", "confirme"] == 0 and d.loc["R2", "confirme"] == 1


def test_age_inconnu_ou_absurde_est_signale():
    j = datetime(2026, 1, 5, 9, 0)
    brut = _brut([
        ("R1", "P1", "ORL", j, j.date(), "Honoré", False, None),
        ("R2", "P2", "ORL", j, j.date(), "Honoré", False, date(2030, 1, 1)),  # né après le rendez-vous
    ])
    d = risque.construire_variables(brut)
    assert list(d["age_inconnu"]) == [1.0, 1.0] and d["age"].isna().all()


def _jeu_synthetique(n=1500, graine=0):
    rng = np.random.default_rng(graine)
    debut = datetime(2025, 1, 1, 8) + pd.to_timedelta(np.sort(rng.integers(0, 365 * 24 * 60, n)), unit="m")
    delai = rng.integers(0, 40, n)
    p = 1 / (1 + np.exp(-(-2.6 + 0.08 * delai)))  # le délai pilote l'absence : un bon modèle doit le retrouver
    statut = np.where(rng.random(n) < p, "Absent", "Honoré")
    lignes = [
        (f"R{i}", f"P{rng.integers(0, 300)}", "Cardiologie" if i % 2 else "ORL", debut[i],
         (debut[i] - timedelta(days=int(delai[i]))).date(), statut[i], bool(rng.random() < 0.5), date(1985, 6, 1))
        for i in range(n)
    ]
    return risque.construire_variables(_brut(lignes))


def test_apprentissage_retrouve_un_signal_connu_et_evalue_dans_le_futur():
    d = _jeu_synthetique()
    r = risque.apprendre(d, risque.Hypotheses(1000, 15000, 0.35))
    assert set(r.modeles) == {"logistique", "foret"} and r.retenu in r.modeles
    assert r.modeles[r.retenu].auc > 0.68
    assert r.date_coupure > d["debut"].min()
    clos_tries = d.sort_values("debut")
    assert r.nb_apprentissage + r.nb_test == len(clos_tries) and r.nb_test == pytest.approx(len(clos_tries) * 0.25, abs=1)
    m = r.modeles[r.retenu].matrice
    assert sum(map(sum, m)) == r.nb_test  # la matrice couvre exactement la période de test
    e = r.modeles[r.retenu]
    assert e.precision == pytest.approx(m[1][1] / (m[1][1] + m[0][1])) and e.rappel == pytest.approx(m[1][1] / (m[1][1] + m[1][0]))
    # Relancer selon le score fait mieux que de ne relancer personne et que de relancer tout le monde.
    assert r.strategies["relancer_selon_le_score"] > max(0.0, r.strategies["relancer_tout_le_monde"])


def test_notation_et_facteurs():
    d = _jeu_synthetique()
    r = risque.apprendre(d, risque.Hypotheses(1000, 15000, 0.35))
    notes = risque.noter(r, d.iloc[:50])
    assert notes["score"].between(0, 1).all() and len(notes["facteurs"]) == 50
    long, court = d[d["delai_jours"] >= 35].iloc[:30], d[d["delai_jours"] <= 2].iloc[:30]
    assert risque.noter(r, long)["score"].mean() > risque.noter(r, court)["score"].mean()
    assert any("à l'avance" in t for t in risque.noter(r, long)["facteurs"])


def test_donnees_insuffisantes():
    with pytest.raises(risque.DonneesInsuffisantesError):
        risque.apprendre(_jeu_synthetique(n=100))


# --- API ---------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def calcule(admin):
    r = admin.post("/risque-absence/recalculer")
    assert r.status_code == 200, r.text
    return r.json()


def test_recalcul_et_evaluation(calcule, responsable):
    assert calcule["statut"] == "ok" and calcule["scores"] > 0 and 0.4 < calcule["auc"] < 1
    e = responsable.get("/risque-absence/evaluation").json()
    assert e["retenu"] in ("logistique", "foret") and set(e["modeles"]) == {"logistique", "foret"}
    assert e["seuil"] == pytest.approx(e["hypotheses"]["cout_relance"] / (e["hypotheses"]["efficacite"] * e["hypotheses"]["cout_creneau"]))
    m = e["modeles"][e["retenu"]]
    assert sum(map(sum, m["matrice"])) == e["nb_test"] and 0 <= m["auc"] <= 1
    assert any("synthétiques" in x for x in e["limites"]) and e["courbe_seuils"]


def test_liste_a_relancer(calcule, agent):
    r = agent.get("/risque-absence/a-relancer", params={"jours": 90, "tous": True})
    assert r.status_code == 200, r.text
    corps = r.json()
    assert corps["nombre"] == len(corps["rendez_vous"]) > 0
    scores = [x["score_pct"] for x in corps["rendez_vous"]]
    assert scores == sorted(scores, reverse=True) and all(0 <= s <= 100 for s in scores)
    seuil = corps["seuil_pct"]
    seuls = agent.get("/risque-absence/a-relancer", params={"jours": 90}).json()["rendez_vous"]
    assert all(x["score_pct"] >= seuil - 0.05 and x["a_relancer"] for x in seuls) and len(seuls) <= corps["nombre"]
    assert all(x["statut"] in ("Planifié", "Confirmé") for x in corps["rendez_vous"])
    premier = corps["rendez_vous"][0]
    filtre = agent.get("/risque-absence/a-relancer", params={"jours": 90, "tous": True, "specialite": premier["specialite"]}).json()
    assert filtre["rendez_vous"] and {x["specialite"] for x in filtre["rendez_vous"]} == {premier["specialite"]}
    court = agent.get("/risque-absence/a-relancer", params={"jours": 1, "tous": True}).json()
    assert court["nombre"] <= corps["nombre"]


def test_droits(calcule, connexion, medecins, responsable, agent, admin):
    assert connexion(medecins[0]["id"]).get("/risque-absence/a-relancer").status_code == 403
    assert connexion(medecins[0]["id"]).get("/risque-absence/evaluation").status_code == 403
    direction = connexion("DIR01")
    assert direction.get("/risque-absence/a-relancer").status_code == 403  # la direction ne voit que l'évaluation
    assert direction.get("/risque-absence/evaluation").status_code == 200
    assert agent.get("/risque-absence/evaluation").status_code == 403
    assert agent.post("/risque-absence/recalculer").status_code == 403
    assert responsable.post("/risque-absence/recalculer").status_code == 200


def test_anonyme_refuse(anonyme):
    assert anonyme.get("/risque-absence/a-relancer").status_code == 401
    assert anonyme.get("/risque-absence/evaluation").status_code == 401


def test_le_score_ne_bloque_jamais_et_n_apparait_pas_dans_les_rdv(calcule, agent, nouveau_patient, medecins):
    medecin_neuf = medecins[-1]
    """Même pour un patient à très haut risque, la prise reste possible ; la réponse ne contient aucun score."""
    premier = agent.get("/planning/premiers-creneaux", params={"medecin_id": medecin_neuf["id"]}).json()[0]
    r = agent.post("/rdv", json=corps_rdv(nouveau_patient(), medecin_neuf["id"], date.fromisoformat(premier["date_rdv"]), time.fromisoformat(premier["heure_rdv"])))
    assert r.status_code == 201, r.text
    texte = r.text.lower()
    assert "score" not in texte and "risque" not in texte and "relanc" not in texte
    rdv_id = r.json()["rdv"]["id"]
    lecture = agent.get(f"/rdv/{rdv_id}")
    assert lecture.status_code == 200 and "score" not in lecture.text.lower() and "risque" not in lecture.text.lower()


def test_prochaine_execution_nocturne():
    from backend.analytique.nocturne import _prochaine_execution

    assert _prochaine_execution("02:00", datetime(2026, 10, 12, 9, 30)) == datetime(2026, 10, 13, 2, 0)
    assert _prochaine_execution("02:00", datetime(2026, 10, 12, 1, 0)) == datetime(2026, 10, 12, 2, 0)


def test_recalcul_remplace_les_scores_sans_doublon(calcule, admin):
    from sqlalchemy import func, select

    from backend.database import SessionLocale
    from backend.models import ScoreRisque

    avant = admin.post("/risque-absence/recalculer").json()["scores"]
    with SessionLocale() as db:
        assert db.scalar(select(func.count()).select_from(ScoreRisque)) == avant
