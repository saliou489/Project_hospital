"""Score de risque d'absence par rendez-vous (EF-25, cahier des charges 8.3).

Cible : le rendez-vous n'est pas honoré (statut « Absent »). Les rendez-vous annulés sont exclus de l'apprentissage :
un patient qui annule prévient la clinique, le créneau peut être réattribué, ce n'est pas le phénomène visé.

Variables, strictement celles que le cahier des charges autorise :

- délai entre la prise et le rendez-vous (jours) ;
- jour de la semaine et heure ;
- spécialité ;
- âge du patient au jour du rendez-vous ;
- historique d'absences : nombre d'absences et taux d'absence lissé sur les rendez-vous clos *antérieurs* (jamais le
  rendez-vous lui-même ni un rendez-vous postérieur) ;
- confirmation ou non.

Le canal de prise, le sexe, le quartier ou le médecin ne sont volontairement pas utilisés.

Deux modèles sont comparés : régression logistique et forêt aléatoire. La validation est temporelle (les 25 % de
rendez-vous clos les plus récents sont tenus à l'écart) : un tirage aléatoire ferait apprendre sur l'avenir.
Le seuil de relance vient d'un raisonnement coût / bénéfice (voir ``seuil_cout_benefice``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, confusion_matrix, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

PART_TEST = 0.25
MIN_RDV_CLOS = 200
MIN_ABSENCES = 20  # sans assez d'absences observées, aucune AUC n'a de sens

NUMERIQUES = ["delai_jours", "delai_log", "heure", "age", "nb_absences", "taux_absence_passe"]
CATEGORIELLES = ["jour_semaine", "specialite"]
BINAIRES = ["confirme", "age_inconnu"]
VARIABLES = NUMERIQUES + CATEGORIELLES + BINAIRES

NOMS_MODELES = {"logistique": "Régression logistique", "foret": "Forêt aléatoire"}
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

# Hypothèses de coût : à faire valider par la clinique, elles ne viennent pas du cahier des charges.
COUT_RELANCE_DEFAUT = 1000.0  # un appel de relance (temps d'agent), en FCFA
COUT_CRENEAU_DEFAUT = 15000.0  # un créneau perdu (recette non réalisée), en FCFA
EFFICACITE_RELANCE_DEFAUT = 0.35  # part des absences évitées lorsque le patient est relancé
TAUX_ABSENCE_PRIOR = 0.14  # taux a priori pour lisser l'historique d'un patient (cahier des charges : environ 14 %)
POIDS_PRIOR = 2.0  # le taux a priori vaut comme deux rendez-vous observés


@dataclass(frozen=True)
class Hypotheses:
    cout_relance: float = COUT_RELANCE_DEFAUT
    cout_creneau: float = COUT_CRENEAU_DEFAUT
    efficacite: float = EFFICACITE_RELANCE_DEFAUT

    @classmethod
    def depuis_environnement(cls) -> "Hypotheses":
        def lire(nom: str, defaut: float) -> float:
            try:
                valeur = float(os.getenv(nom, defaut))
            except ValueError:
                return defaut
            return valeur if valeur > 0 else defaut

        return cls(
            lire("MEDIRDV_COUT_RELANCE", COUT_RELANCE_DEFAUT),
            lire("MEDIRDV_COUT_CRENEAU", COUT_CRENEAU_DEFAUT),
            min(lire("MEDIRDV_EFFICACITE_RELANCE", EFFICACITE_RELANCE_DEFAUT), 1.0),
        )


def seuil_cout_benefice(h: Hypotheses) -> float:
    """Relancer un patient de risque ``p`` rapporte en moyenne ``p × efficacité × coût du créneau − coût de l'appel``.

    L'appel est rentable dès que ce gain est positif, soit ``p ≥ coût de l'appel / (efficacité × coût du créneau)``.
    """
    return min(1.0, h.cout_relance / (h.efficacite * h.cout_creneau))


# --------------------------------------------------------------------------------------------------
# Construction des variables
# --------------------------------------------------------------------------------------------------

def construire_variables(brut: pd.DataFrame) -> pd.DataFrame:
    """Calcule les variables à partir des lignes brutes.

    Colonnes attendues : rdv_id, patient_id, specialite, debut (datetime), date_prise (date), statut,
    confirme (bool ou None), date_naissance (date ou None). Les lignes sont toutes les rendez-vous non annulés ;
    l'historique d'un rendez-vous n'utilise que les rendez-vous clos (Honoré ou Absent) qui le précèdent.
    """
    d = brut.copy()
    d["debut"] = pd.to_datetime(d["debut"])
    d["date_prise"] = pd.to_datetime(d["date_prise"])
    d["date_naissance"] = pd.to_datetime(d["date_naissance"], errors="coerce")

    d["delai_jours"] = (d["debut"].dt.normalize() - d["date_prise"]).dt.days.clip(lower=0).astype(float)
    d["delai_log"] = np.log1p(d["delai_jours"])  # le risque croît vite sur les premiers jours puis plus lentement
    d["jour_semaine"] = d["debut"].dt.dayofweek.astype(str)
    d["heure"] = d["debut"].dt.hour + d["debut"].dt.minute / 60.0
    age = (d["debut"] - d["date_naissance"]).dt.days / 365.25
    age = age.where((age >= 0) & (age <= 110))
    d["age_inconnu"] = age.isna().astype(float)
    d["age"] = age
    d["confirme"] = d["confirme"].fillna(False).astype(float)

    clos = d["statut"].isin(["Honoré", "Absent"])
    d["_absent"] = (d["statut"] == "Absent").astype(float)
    d["_clos"] = clos.astype(float)
    d = d.sort_values(["patient_id", "debut", "rdv_id"]).reset_index(drop=True)
    groupe = d.groupby("patient_id", sort=False)
    # Cumul des rendez-vous clos, moins la ligne elle-même : seuls les rendez-vous antérieurs comptent.
    # (Deux rendez-vous au même instant pour un patient sont exclus par l'index unique de la base.)
    d["nb_absences"] = groupe["_absent"].cumsum() - d["_absent"]
    d["nb_rdv_clos_passes"] = groupe["_clos"].cumsum() - d["_clos"]
    d["taux_absence_passe"] = (d["nb_absences"] + TAUX_ABSENCE_PRIOR * POIDS_PRIOR) / (d["nb_rdv_clos_passes"] + POIDS_PRIOR)
    return d.drop(columns=["_absent", "_clos"])


def _mediane_age(d: pd.DataFrame) -> float:
    valeur = d["age"].median()
    return float(valeur) if pd.notna(valeur) else 35.0


def _preparer(d: pd.DataFrame, age_median: float) -> pd.DataFrame:
    x = d[VARIABLES].copy()
    x["age"] = x["age"].fillna(age_median)
    return x


def _pipeline(modele: str) -> Pipeline:
    transformation = ColumnTransformer(
        [
            ("num", StandardScaler(), NUMERIQUES),
            ("cat", OneHotEncoder(handle_unknown="ignore"), CATEGORIELLES),
            ("bin", "passthrough", BINAIRES),
        ]
    )
    if modele == "logistique":
        estimateur = LogisticRegression(C=1.0, max_iter=2000)
    else:
        estimateur = RandomForestClassifier(
            n_estimators=300, min_samples_leaf=25, max_features="sqrt", random_state=42, n_jobs=1
        )
    return Pipeline([("transformation", transformation), ("modele", estimateur)])


# --------------------------------------------------------------------------------------------------
# Évaluation
# --------------------------------------------------------------------------------------------------

@dataclass
class EvaluationModele:
    modele: str
    auc: float
    precision_moyenne: float
    brier: float
    # Au seuil retenu
    precision: float | None
    rappel: float | None
    matrice: list[list[int]]  # [[vrais négatifs, faux positifs], [faux négatifs, vrais positifs]]
    part_relancee: float
    gain_net: float


@dataclass
class ResultatApprentissage:
    modeles: dict[str, EvaluationModele]
    retenu: str
    seuil: float
    hypotheses: Hypotheses
    date_coupure: datetime
    nb_apprentissage: int
    nb_test: int
    taux_absence_apprentissage: float
    taux_absence_test: float
    courbe_seuils: list[dict] = field(default_factory=list)
    strategies: dict[str, float] = field(default_factory=dict)  # gain net de référence sur la période de test
    pipeline_final: Pipeline | None = None
    pipeline_explicatif: Pipeline | None = None
    age_median: float = 35.0
    nb_total_clos: int = 0


class DonneesInsuffisantesError(Exception):
    """Pas assez de rendez-vous clos ou d'absences pour apprendre."""


def gain_net(y: np.ndarray, relance: np.ndarray, h: Hypotheses) -> float:
    """Gain net moyen attendu : absences évitées par les relances × coût du créneau − coût des appels."""
    vrais = float(np.sum(relance & (y == 1)))
    return vrais * h.efficacite * h.cout_creneau - float(np.sum(relance)) * h.cout_relance


def _evaluer(y: np.ndarray, p: np.ndarray, seuil: float, h: Hypotheses, modele: str) -> EvaluationModele:
    relance = p >= seuil
    tn, fp, fn, tp = confusion_matrix(y, relance, labels=[0, 1]).ravel()
    return EvaluationModele(
        modele=modele,
        auc=float(roc_auc_score(y, p)),
        precision_moyenne=float(average_precision_score(y, p)),
        brier=float(brier_score_loss(y, p)),
        precision=float(tp / (tp + fp)) if tp + fp else None,
        rappel=float(tp / (tp + fn)) if tp + fn else None,
        matrice=[[int(tn), int(fp)], [int(fn), int(tp)]],
        part_relancee=float(relance.mean()),
        gain_net=gain_net(y, relance, h),
    )


def apprendre(d: pd.DataFrame, hypotheses: Hypotheses | None = None) -> ResultatApprentissage:
    """Évalue les deux modèles sur la période la plus récente, retient le meilleur (AUC) et le réajuste sur tout."""
    h = hypotheses or Hypotheses.depuis_environnement()
    clos = d[d["statut"].isin(["Honoré", "Absent"])].sort_values(["debut", "rdv_id"]).reset_index(drop=True)
    y_tout = (clos["statut"] == "Absent").astype(int).to_numpy()
    if len(clos) < MIN_RDV_CLOS or y_tout.sum() < MIN_ABSENCES or y_tout.sum() == len(y_tout):
        raise DonneesInsuffisantesError(
            f"{len(clos)} rendez-vous clos dont {int(y_tout.sum())} absences : insuffisant pour apprendre "
            f"(minimum {MIN_RDV_CLOS} rendez-vous et {MIN_ABSENCES} absences)."
        )
    coupure = int(len(clos) * (1 - PART_TEST))
    app, test = clos.iloc[:coupure], clos.iloc[coupure:]
    y_app, y_test = y_tout[:coupure], y_tout[coupure:]
    if y_test.sum() == 0 or y_test.sum() == len(y_test) or y_app.sum() == 0:
        raise DonneesInsuffisantesError("La période de test ne contient qu'une seule classe : AUC non calculable.")

    age_median = _mediane_age(app)
    x_app, x_test = _preparer(app, age_median), _preparer(test, age_median)
    seuil = seuil_cout_benefice(h)

    evaluations: dict[str, EvaluationModele] = {}
    scores_test: dict[str, np.ndarray] = {}
    for nom in NOMS_MODELES:
        pipeline = _pipeline(nom).fit(x_app, y_app)
        p = pipeline.predict_proba(x_test)[:, 1]
        scores_test[nom] = p
        evaluations[nom] = _evaluer(y_test, p, seuil, h, nom)
    retenu = max(evaluations, key=lambda m: evaluations[m].auc)

    courbe = []
    for s in np.round(np.arange(0.05, 0.61, 0.05), 2):
        e = _evaluer(y_test, scores_test[retenu], float(s), h, retenu)
        courbe.append(
            {"seuil": float(s), "precision": e.precision, "rappel": e.rappel, "part_relancee": e.part_relancee, "gain_net": e.gain_net}
        )
    strategies = {
        "ne_relancer_personne": 0.0,
        "relancer_tout_le_monde": gain_net(y_test, np.ones(len(y_test), dtype=bool), h),
        "relancer_selon_le_score": evaluations[retenu].gain_net,
    }

    # Modèles définitifs : réajustés sur tous les rendez-vous clos pour noter les rendez-vous à venir.
    age_median_tout = _mediane_age(clos)
    x_tout = _preparer(clos, age_median_tout)
    final = _pipeline(retenu).fit(x_tout, y_tout)
    explicatif = final if retenu == "logistique" else _pipeline("logistique").fit(x_tout, y_tout)

    return ResultatApprentissage(
        modeles=evaluations,
        retenu=retenu,
        seuil=seuil,
        hypotheses=h,
        date_coupure=test["debut"].iloc[0].to_pydatetime(),
        nb_apprentissage=len(app),
        nb_test=len(test),
        taux_absence_apprentissage=float(y_app.mean()),
        taux_absence_test=float(y_test.mean()),
        courbe_seuils=courbe,
        strategies=strategies,
        pipeline_final=final,
        pipeline_explicatif=explicatif,
        age_median=age_median_tout,
        nb_total_clos=len(clos),
    )


# --------------------------------------------------------------------------------------------------
# Notation des rendez-vous à venir
# --------------------------------------------------------------------------------------------------

def noter(resultat: ResultatApprentissage, a_noter: pd.DataFrame) -> pd.DataFrame:
    """Ajoute ``score`` (probabilité d'absence) et ``facteurs`` (raisons principales, texte) aux rendez-vous donnés."""
    if a_noter.empty:
        return a_noter.assign(score=pd.Series(dtype=float), facteurs=pd.Series(dtype=object))
    x = _preparer(a_noter, resultat.age_median)
    sortie = a_noter.copy()
    sortie["score"] = resultat.pipeline_final.predict_proba(x)[:, 1]
    sortie["facteurs"] = _facteurs(resultat.pipeline_explicatif, x, a_noter)
    return sortie


def _libelle_facteur(groupe: str, ligne: pd.Series) -> str:
    if groupe == "delai":
        return f"rendez-vous pris {int(ligne['delai_jours'])} jours à l'avance"
    if groupe == "confirmation":
        return "rendez-vous non confirmé"
    if groupe == "historique":
        n = int(ligne["nb_absences"])
        return f"{n} absence{'s' if n > 1 else ''} passée{'s' if n > 1 else ''}" if n else "peu d'historique connu"
    if groupe == "jour":
        return f"créneau du {JOURS[int(ligne['jour_semaine'])]}"
    if groupe == "heure":
        h = float(ligne["heure"])
        return f"créneau de {int(h)} h"
    if groupe == "specialite":
        return f"spécialité {ligne['specialite']}"
    return f"patient de {int(ligne['age'])} ans" if not ligne["age_inconnu"] else "âge du patient inconnu"


_GROUPES_COLONNES = {
    "delai": ["delai_jours", "delai_log"],
    "heure": ["heure"],
    "age": ["age", "age_inconnu"],
    "historique": ["nb_absences", "taux_absence_passe"],
    "confirmation": ["confirme"],
    "jour": ["jour_semaine"],
    "specialite": ["specialite"],
}


def _facteurs(pipeline: Pipeline, x: pd.DataFrame, brut: pd.DataFrame, combien: int = 2) -> list[str]:
    """Contribution de chaque groupe de variables au logit du modèle linéaire ; on garde celles qui augmentent le risque."""
    transformation, modele = pipeline.named_steps["transformation"], pipeline.named_steps["modele"]
    matrice = transformation.transform(x)
    matrice = matrice.toarray() if hasattr(matrice, "toarray") else np.asarray(matrice)
    contributions = matrice * modele.coef_[0]
    noms = list(transformation.get_feature_names_out())

    def groupe_de(nom: str) -> str:
        for groupe, colonnes in _GROUPES_COLONNES.items():
            if any(nom.split("__", 1)[1].startswith(c) for c in colonnes):
                return groupe
        return "age"

    indices: dict[str, list[int]] = {}
    for i, nom in enumerate(noms):
        indices.setdefault(groupe_de(nom), []).append(i)
    totaux = {g: contributions[:, idx].sum(axis=1) for g, idx in indices.items()}
    # Le « zéro » d'un groupe est son niveau moyen : seule la partie qui dépasse la moyenne est une raison d'alerter.
    moyennes = {g: v.mean() for g, v in totaux.items()}
    brut = brut.reset_index(drop=True)
    textes = []
    for i in range(len(brut)):
        ecarts = sorted(((totaux[g][i] - moyennes[g], g) for g in totaux), reverse=True)
        retenus = [g for ecart, g in ecarts[:combien] if ecart > 0.05]
        textes.append(" ; ".join(_libelle_facteur(g, brut.iloc[i]) for g in retenus))
    return textes


def resume_evaluation(r: ResultatApprentissage) -> dict:
    """Représentation sérialisable (JSON) de l'évaluation, enregistrée par le module nocturne."""
    return {
        "retenu": r.retenu,
        "seuil": r.seuil,
        "hypotheses": {"cout_relance": r.hypotheses.cout_relance, "cout_creneau": r.hypotheses.cout_creneau, "efficacite": r.hypotheses.efficacite},
        "date_coupure": r.date_coupure.isoformat(),
        "nb_apprentissage": r.nb_apprentissage,
        "nb_test": r.nb_test,
        "nb_total_clos": r.nb_total_clos,
        "taux_absence_apprentissage": r.taux_absence_apprentissage,
        "taux_absence_test": r.taux_absence_test,
        "modeles": {
            nom: {
                "libelle": NOMS_MODELES[nom], "auc": e.auc, "precision_moyenne": e.precision_moyenne, "brier": e.brier,
                "precision": e.precision, "rappel": e.rappel, "matrice": e.matrice, "part_relancee": e.part_relancee,
                "gain_net": e.gain_net,
            }
            for nom, e in r.modeles.items()
        },
        "courbe_seuils": r.courbe_seuils,
        "strategies": r.strategies,
    }
