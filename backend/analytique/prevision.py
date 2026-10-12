"""Prévision du nombre hebdomadaire de rendez-vous par spécialité (EF-24, cahier des charges 8.2).

Série modélisée : nombre de rendez-vous non annulés par semaine (du lundi au dimanche, selon la date du rendez-vous),
semaines complètes uniquement. Trois méthodes sont comparées :

- ``moyenne_mobile`` : référence naïve, moyenne des 4 dernières semaines ;
- ``holt`` : lissage exponentiel avec tendance amortie (paramètres choisis par grille sur l'erreur à un pas) ;
- ``regression`` : régression ridge sur variables calendaires (tendance et jours ouvrés de la semaine).

Évaluation : les 8 dernières semaines complètes sont tenues à l'écart, les modèles sont ajustés sur les semaines
précédentes puis prévoient ces 8 semaines (MAE et MAPE). La méthode retenue est celle dont la MAE totale est la plus
faible sur l'ensemble des spécialités. La prévision finale réajuste la méthode retenue sur toutes les semaines.
L'intervalle d'incertitude est ``prévision ± 1,645 σ`` (environ 90 %), σ étant l'écart quadratique des erreurs de la
période tenue à l'écart, au moins égal à la racine de la prévision (variabilité minimale d'un comptage).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

SEMAINES_TENUES_A_L_ECART = 8
SEMAINES_MIN_ENTRAINEMENT = 12
Z_90 = 1.645

METHODES = {
    "moyenne_mobile": "Moyenne mobile sur 4 semaines (référence naïve)",
    "holt": "Lissage exponentiel de Holt (tendance amortie)",
    "regression": "Régression sur variables calendaires",
}


def lundi_de(jour: date) -> date:
    return jour - timedelta(days=jour.weekday())


@dataclass
class Serie:
    """Série hebdomadaire d'une spécialité : lundis, volumes observés et jours ouvrés (variable calendaire)."""

    nom: str
    semaines: list[date]
    valeurs: np.ndarray
    jours_ouvres: np.ndarray


def jours_ouvres_de_la_semaine(lundi: date, feries: set[date]) -> int:
    """Jours ouvrés d'une semaine : du lundi au samedi, hors jours fériés et fermetures."""
    return sum(1 for i in range(6) if (lundi + timedelta(days=i)) not in feries)


# --------------------------------------------------------------------------------------
# Méthodes de prévision (chacune retourne un vecteur de ``h`` prévisions)
# --------------------------------------------------------------------------------------


def prevoir_moyenne_mobile(y: np.ndarray, h: int, fenetre: int = 4) -> np.ndarray:
    return np.full(h, float(np.mean(y[-fenetre:])))


def _holt_amorti(y: np.ndarray, alpha: float, beta: float, phi: float) -> tuple[float, float, float]:
    """Retourne (niveau, tendance, somme des carrés des erreurs à un pas)."""
    niveau, tendance = float(y[0]), float(y[1] - y[0]) if len(y) > 1 else 0.0
    sse = 0.0
    for valeur in y[1:]:
        prevu = niveau + phi * tendance
        sse += (valeur - prevu) ** 2
        ancien = niveau
        niveau = alpha * valeur + (1 - alpha) * prevu
        tendance = beta * (niveau - ancien) + (1 - beta) * phi * tendance
    return niveau, tendance, sse


def prevoir_holt(y: np.ndarray, h: int) -> np.ndarray:
    meilleur = None
    for alpha in (0.1, 0.2, 0.3, 0.5, 0.7):
        for beta in (0.05, 0.1, 0.2):
            for phi in (0.8, 0.9, 0.98):
                niveau, tendance, sse = _holt_amorti(y, alpha, beta, phi)
                if meilleur is None or sse < meilleur[0]:
                    meilleur = (sse, niveau, tendance, phi)
    _, niveau, tendance, phi = meilleur
    pas = np.arange(1, h + 1)
    return niveau + tendance * np.cumsum(phi**pas)


def _variables_calendaires(indices: np.ndarray, jours_ouvres: np.ndarray) -> np.ndarray:
    """Colonnes : tendance linéaire et nombre de jours ouvrés de la semaine.

    Une saison annuelle (fonctions sinus/cosinus de la semaine de l'année) n'est pas retenue : avec douze mois
    d'historique, un seul cycle est observé et ces variables ajusteraient le bruit (essai fait : erreur nettement
    plus forte sur la période tenue à l'écart quand l'ajustement est peu pénalisé).
    """
    return np.column_stack([indices, jours_ouvres])


def prevoir_regression(
    y: np.ndarray, lundis_train: list[date], ouvres_train: np.ndarray, lundis_futurs: list[date], ouvres_futurs: np.ndarray, lam: float = 1.0
) -> np.ndarray:
    n = len(y)
    X = _variables_calendaires(np.arange(n, dtype=float), ouvres_train)
    Xf = _variables_calendaires(np.arange(n, n + len(lundis_futurs), dtype=float), ouvres_futurs)
    moyenne, ecart = X.mean(axis=0), X.std(axis=0)
    ecart[ecart == 0] = 1.0
    Z, Zf = (X - moyenne) / ecart, (Xf - moyenne) / ecart
    ordonnee = float(np.mean(y))
    # Ridge sans pénalisation de l'ordonnée à l'origine : variables centrées, y centré.
    coefficients = np.linalg.solve(Z.T @ Z + lam * np.eye(Z.shape[1]), Z.T @ (y - ordonnee))
    return np.maximum(0.0, ordonnee + Zf @ coefficients)


def prevoir(methode: str, serie: Serie, n_train: int, lundis_futurs: list[date], ouvres_futurs: np.ndarray) -> np.ndarray:
    """Prévoit ``len(lundis_futurs)`` semaines à partir des ``n_train`` premières semaines de la série."""
    y = serie.valeurs[:n_train]
    h = len(lundis_futurs)
    if methode == "moyenne_mobile":
        resultat = prevoir_moyenne_mobile(y, h)
    elif methode == "holt":
        resultat = prevoir_holt(y, h)
    elif methode == "regression":
        resultat = prevoir_regression(y, serie.semaines[:n_train], serie.jours_ouvres[:n_train], lundis_futurs, ouvres_futurs)
    else:
        raise ValueError(f"Méthode inconnue : {methode}")
    return np.maximum(0.0, resultat)


# --------------------------------------------------------------------------------------
# Évaluation et prévision finale
# --------------------------------------------------------------------------------------


def mae(reel: np.ndarray, prevu: np.ndarray) -> float:
    return float(np.mean(np.abs(reel - prevu)))


def mape(reel: np.ndarray, prevu: np.ndarray) -> float | None:
    """MAPE en pourcentage ; les semaines à zéro rendez-vous sont ignorées (division impossible)."""
    masque = reel > 0
    if not masque.any():
        return None
    return float(100.0 * np.mean(np.abs(reel[masque] - prevu[masque]) / reel[masque]))


@dataclass
class ResultatSpecialite:
    serie: Serie
    evaluation: dict[str, dict]  # méthode -> {"mae", "mape", "rmse", "prevu" (vecteur sur la période tenue à l'écart)}
    futures: dict[str, np.ndarray]  # méthode -> prévisions des semaines futures (modèle réajusté sur toute la série)


@dataclass
class ResultatPrevision:
    lundis_futurs: list[date]
    ouvres_futurs: np.ndarray
    specialites: list[ResultatSpecialite]
    totaux_par_methode: dict[str, dict]  # métrique agrégée sur toutes les spécialités
    methode_retenue: str


def evaluer_serie(serie: Serie, lundis_futurs: list[date], ouvres_futurs: np.ndarray) -> ResultatSpecialite:
    n = len(serie.valeurs)
    n_test = min(SEMAINES_TENUES_A_L_ECART, max(0, n - SEMAINES_MIN_ENTRAINEMENT))
    n_train = n - n_test
    evaluation: dict[str, dict] = {}
    for methode in METHODES:
        if n_test:
            prevu = prevoir(methode, serie, n_train, serie.semaines[n_train:], serie.jours_ouvres[n_train:])
            reel = serie.valeurs[n_train:]
            evaluation[methode] = {
                "mae": mae(reel, prevu),
                "mape": mape(reel, prevu),
                "rmse": float(np.sqrt(np.mean((reel - prevu) ** 2))),
                "prevu": prevu,
                "reel": reel,
            }
        else:
            evaluation[methode] = {"mae": None, "mape": None, "rmse": None, "prevu": np.array([]), "reel": np.array([])}
    futures = {m: prevoir(m, serie, n, lundis_futurs, ouvres_futurs) for m in METHODES}
    return ResultatSpecialite(serie, evaluation, futures)


def choisir_methode(resultats: list[ResultatSpecialite]) -> tuple[str, dict[str, dict]]:
    """Méthode à la MAE la plus faible sur l'ensemble des semaines tenues à l'écart de toutes les spécialités."""
    totaux: dict[str, dict] = {}
    for methode in METHODES:
        reels = [r.evaluation[methode]["reel"] for r in resultats if len(r.evaluation[methode]["reel"])]
        prevus = [r.evaluation[methode]["prevu"] for r in resultats if len(r.evaluation[methode]["reel"])]
        if reels:
            reel, prevu = np.concatenate(reels), np.concatenate(prevus)
            totaux[methode] = {"mae": mae(reel, prevu), "mape": mape(reel, prevu)}
        else:
            totaux[methode] = {"mae": None, "mape": None}
    candidates = {m: t["mae"] for m, t in totaux.items() if t["mae"] is not None}
    retenue = min(candidates, key=candidates.get) if candidates else "moyenne_mobile"
    return retenue, totaux


def intervalle(resultat: ResultatSpecialite, methode: str, prevision: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Intervalle d'environ 90 % : prévision ± 1,645 σ (σ = RMSE de la période tenue à l'écart, plancher de Poisson)."""
    rmse = resultat.evaluation[methode]["rmse"] or 0.0
    sigma = np.maximum(rmse, np.sqrt(np.maximum(prevision, 1.0)))
    return np.maximum(0.0, prevision - Z_90 * sigma), prevision + Z_90 * sigma
