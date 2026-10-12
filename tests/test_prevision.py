"""Prévision hebdomadaire (EF-24) : méthodes sur séries connues, cohérence de l'API, droits."""
from datetime import date, timedelta

import numpy as np
import pytest

from backend.analytique.prevision import (
    METHODES,
    Serie,
    choisir_methode,
    evaluer_serie,
    intervalle,
    jours_ouvres_de_la_semaine,
    lundi_de,
    mae,
    mape,
    prevoir_moyenne_mobile,
)


def _serie(valeurs, ouvres=None, debut=date(2025, 1, 6)) -> Serie:
    n = len(valeurs)
    semaines = [debut + timedelta(weeks=i) for i in range(n)]
    return Serie("Test", semaines, np.array(valeurs, dtype=float), np.array(ouvres if ouvres is not None else [6] * n))


def _futurs(serie: Serie, h=4):
    dernier = serie.semaines[-1]
    return [dernier + timedelta(weeks=i + 1) for i in range(h)], np.array([6] * h)


# ---- Unités -----------------------------------------------------------------------------------------


def test_lundi_et_jours_ouvres():
    assert lundi_de(date(2026, 10, 14)) == date(2026, 10, 12)
    assert lundi_de(date(2026, 10, 12)) == date(2026, 10, 12)
    assert jours_ouvres_de_la_semaine(date(2026, 10, 12), set()) == 6
    assert jours_ouvres_de_la_semaine(date(2026, 10, 12), {date(2026, 10, 14), date(2026, 10, 17)}) == 4
    assert jours_ouvres_de_la_semaine(date(2026, 10, 12), {date(2026, 10, 18)}) == 6  # le dimanche est déjà fermé


def test_mae_et_mape_exactes():
    reel, prevu = np.array([10.0, 20.0, 0.0]), np.array([12.0, 15.0, 3.0])
    assert mae(reel, prevu) == pytest.approx((2 + 5 + 3) / 3)
    assert mape(reel, prevu) == pytest.approx(100 * (2 / 10 + 5 / 20) / 2)  # la semaine à zéro est ignorée
    assert mape(np.zeros(3), prevu) is None


def test_moyenne_mobile_est_la_moyenne_des_4_dernieres_semaines():
    assert prevoir_moyenne_mobile(np.array([1.0, 1.0, 10.0, 20.0, 30.0, 40.0]), 3).tolist() == [25.0, 25.0, 25.0]


def test_serie_constante_prevue_constante():
    s = _serie([20] * 40)
    for methode in METHODES:
        r = evaluer_serie(s, *_futurs(s))
        assert r.evaluation[methode]["mae"] == pytest.approx(0.0, abs=1e-6)
        assert r.futures[methode] == pytest.approx([20.0] * 4, abs=1e-6)


def test_tendance_lineaire_suivie_par_la_regression_pas_par_la_reference():
    s = _serie([10 + i for i in range(48)])
    r = evaluer_serie(s, *_futurs(s))
    assert r.futures["regression"] == pytest.approx([58, 59, 60, 61], abs=1.0)
    assert r.evaluation["regression"]["mae"] < r.evaluation["moyenne_mobile"]["mae"] / 3
    assert choisir_methode([r])[0] in ("regression", "holt")


def test_semaines_a_cinq_jours_ouvres_tirent_la_regression_vers_le_bas():
    ouvres = [6] * 40
    valeurs = [60.0] * 40
    for i in (5, 11, 17, 23, 29, 35):
        ouvres[i] = 5
        valeurs[i] = 50.0
    s = _serie(valeurs, ouvres)
    futurs, _ = _futurs(s)
    r6 = evaluer_serie(s, futurs, np.array([6, 6, 6, 6]))
    r5 = evaluer_serie(s, futurs, np.array([5, 5, 5, 5]))
    assert r5.futures["regression"].mean() < r6.futures["regression"].mean() - 5


def test_holt_ne_prevoit_jamais_de_negatif():
    s = _serie([max(0, 40 - 3 * i) for i in range(30)])
    r = evaluer_serie(s, *_futurs(s))
    assert all((v >= 0).all() for v in r.futures.values())


def test_periode_d_evaluation_de_8_semaines_et_intervalle_encadre():
    s = _serie([20 + (i % 5) for i in range(52)])
    r = evaluer_serie(s, *_futurs(s))
    assert all(len(e["prevu"]) == 8 for e in r.evaluation.values())
    prevision = r.futures["moyenne_mobile"]
    bas, haut = intervalle(r, "moyenne_mobile", prevision)
    assert (bas >= 0).all() and (bas < prevision).all() and (prevision < haut).all()


def test_serie_trop_courte_pas_d_evaluation_mais_une_prevision():
    s = _serie([5, 6, 7, 8, 9, 10])
    r = evaluer_serie(s, *_futurs(s))
    assert all(e["mae"] is None for e in r.evaluation.values())
    assert all(len(v) == 4 for v in r.futures.values())


# ---- API --------------------------------------------------------------------------------------------


def test_prevision_api_structure_et_coherence(responsable):
    r = responsable.get("/stats/prevision")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["horizon_semaines"] == 4 and d["semaines_tenues_a_l_ecart"] == 8
    assert set(m["methode"] for m in d["methodes"]) == set(METHODES)
    maes = {m["methode"]: m["mae"] for m in d["methodes"]}
    assert d["methode_retenue"] == min(maes, key=maes.get)
    assert d["ensemble"]["specialite"] == "Ensemble de la clinique"
    assert len(d["specialites"]) >= 2 and d["limites"]
    for sp in d["specialites"] + [d["ensemble"]]:
        assert len(sp["prevision"]) == 4 and len(sp["backtest"]) == 8
        assert len(sp["evaluation"]) == 3 and len(sp["historique"]) > 0
        for p in sp["prevision"]:
            assert 0 <= p["bas"] <= p["prevision"] <= p["haut"]
            assert p["prevision"] >= p["deja_pris"] and p["bas"] >= p["deja_pris"]
            assert date.fromisoformat(p["semaine"]).weekday() == 0
        semaines = [p["semaine"] for p in sp["prevision"]]
        assert semaines == sorted(semaines) and semaines[0] == lundi_de(date.today()).isoformat()


def test_prevision_ensemble_proche_de_la_somme_des_specialites(responsable):
    d = responsable.get("/stats/prevision").json()
    somme = sum(sp["historique"][-1]["volume"] for sp in d["specialites"])
    assert somme == d["ensemble"]["historique"][-1]["volume"]


def test_prevision_filtre_horizon_et_erreurs(responsable, direction, admin, agent, anonyme):
    toutes = responsable.get("/stats/prevision").json()
    nom = toutes["specialites"][0]["specialite"]
    un = responsable.get("/stats/prevision", params={"specialite": nom, "horizon": 6}).json()
    assert [s["specialite"] for s in un["specialites"]] == [nom] and len(un["specialites"][0]["prevision"]) == 6
    assert responsable.get("/stats/prevision", params={"specialite": "Inconnue"}).status_code == 404
    assert responsable.get("/stats/prevision", params={"horizon": 0}).status_code == 422
    assert responsable.get("/stats/prevision", params={"horizon": 9}).status_code == 422
    assert direction.get("/stats/prevision").status_code == 200
    assert admin.get("/stats/prevision").status_code == 200
    assert agent.get("/stats/prevision").status_code == 403
    assert anonyme.get("/stats/prevision").status_code == 401
