"""Test de charge de l'API MédiRDV : ENF-01 (95 % des réponses en moins de 500 ms avec 20 utilisateurs simultanés)
et ENF-02 (chargement du tableau de bord en moins de 3 s sur 12 mois de données).

Ce n'est pas un test pytest : il vise une API en marche (Docker ou uvicorn) avec de vraies données.

    python tests/charge/charge.py --url http://localhost:8000 --cles chemin/cles_a_distribuer_XXXX.csv

Options : --utilisateurs 20  --duree 60 (secondes)  --ecritures (crée aussi des rendez-vous : à réserver à une base jetable).
Par défaut, seules des lectures sont faites. Code de sortie 0 si les deux objectifs sont atteints, 1 sinon.
Le fichier de clés contient des secrets : ne le publiez jamais.
"""
from __future__ import annotations

import argparse
import csv
import random
import statistics
import sys
import threading
import time
from collections import defaultdict
from datetime import date, timedelta

import httpx

OBJECTIF_P95_MS = 500
OBJECTIF_TABLEAU_S = 3.0
ROLES_COURTS = {"AGT": "Agent d'accueil", "MED": "Médecin", "OPS": "Responsable des opérations", "DIR": "Direction"}


class Mesures:
    def __init__(self) -> None:
        self.verrou = threading.Lock()
        self.durees: dict[str, list[float]] = defaultdict(list)
        self.erreurs: dict[str, int] = defaultdict(int)

    def noter(self, nom: str, duree_ms: float, ok: bool) -> None:
        with self.verrou:
            self.durees[nom].append(duree_ms)
            if not ok:
                self.erreurs[nom] += 1


def centile(valeurs: list[float], p: float) -> float:
    ordre = sorted(valeurs)
    return ordre[min(len(ordre) - 1, int(round(p * (len(ordre) - 1))))]


def lire_cles(chemin: str) -> dict[str, tuple[str, str]]:
    with open(chemin, encoding="utf-8-sig", newline="") as f:
        return {l["identifiant"]: (l["role"], l["cle"]) for l in csv.DictReader(f)}


def connecter(url: str, identifiant: str, role: str, cle: str) -> httpx.Client:
    client = httpx.Client(base_url=url, timeout=30)
    r = client.post("/auth/connexion", json={"role": role, "identifiant": identifiant, "cle": cle})
    r.raise_for_status()
    client.headers["Authorization"] = f"Bearer {r.json()['jeton']}"
    return client


def appel(client: httpx.Client, mesures: Mesures, nom: str, chemin: str, **params) -> httpx.Response | None:
    debut = time.perf_counter()
    try:
        r = client.get(chemin, params=params)
        ok = r.status_code == 200
    except httpx.HTTPError:
        r, ok = None, False
    mesures.noter(nom, (time.perf_counter() - debut) * 1000, ok)
    return r


def actions_agent(client, mesures, ctx, ecritures):
    med = random.choice(ctx["medecins"])
    auj = date.today()
    choix = random.random()
    if choix < 0.25:
        appel(client, mesures, "GET /patients?q=", "/patients", q=random.choice(["Di", "Sy", "Fall", "Aw", "Mo", "PAT0"]))
    elif choix < 0.5:
        appel(client, mesures, "GET /planning/premiers-creneaux", "/planning/premiers-creneaux", specialite=med["specialite"])
    elif choix < 0.7:
        appel(client, mesures, "GET /planning/creneaux (semaine)", "/planning/creneaux", medecin_id=med["id"], date_debut=auj.isoformat(), date_fin=(auj + timedelta(days=6)).isoformat())
    elif choix < 0.85:
        appel(client, mesures, "GET /rdv (jour)", "/rdv", date_debut=auj.isoformat(), date_fin=(auj + timedelta(days=1)).isoformat(), limite=100)
    elif choix < 0.95:
        appel(client, mesures, "GET /referentiels/medecins", "/referentiels/medecins")
    else:
        appel(client, mesures, "GET /risque-absence/a-relancer", "/risque-absence/a-relancer", jours=3)
    if ecritures and random.random() < 0.1:
        _creer_rdv(client, mesures, ctx)


def _creer_rdv(client, mesures, ctx):
    med = random.choice(ctx["medecins"])
    creneaux = client.get("/planning/premiers-creneaux", params={"medecin_id": med["id"], "limite": 10}).json()
    patients = client.get("/patients", params={"q": "PAT", "limite": 50}).json()["elements"]
    if not creneaux or not patients:
        return
    c, p = random.choice(creneaux), random.choice(patients)
    debut = time.perf_counter()
    r = client.post("/rdv", json={"patient_id": p["id"], "medecin_id": c["medecin_id"], "date_rdv": c["date_rdv"], "heure_rdv": c["heure_rdv"][:5]})
    # Un conflit (409) ou une règle métier (422) est un refus normal sous concurrence, pas une panne.
    mesures.noter("POST /rdv", (time.perf_counter() - debut) * 1000, r.status_code in (201, 409, 422))


def actions_medecin(client, mesures, ctx, ecritures):
    auj = date.today()
    if random.random() < 0.7:
        appel(client, mesures, "GET /rdv (planning médecin)", "/rdv", date_debut=auj.isoformat(), date_fin=(auj + timedelta(days=7)).isoformat(), limite=200)
    else:
        appel(client, mesures, "GET /referentiels/medecins", "/referentiels/medecins")


def actions_responsable(client, mesures, ctx, ecritures):
    d1, d2 = (date.today() - timedelta(days=365)).isoformat(), date.today().isoformat()
    cible = random.choice([
        ("GET /stats/kpi", "/stats/kpi"), ("GET /stats/jour", "/stats/jour"), ("GET /stats/absence/par-specialite", "/stats/absence/par-specialite"),
        ("GET /stats/absence/mensuelle", "/stats/absence/mensuelle"), ("GET /stats/carte-chaleur", "/stats/carte-chaleur"),
        ("GET /stats/delai-attente", "/stats/delai-attente"), ("GET /stats/annulation", "/stats/annulation"),
        ("GET /stats/occupation", "/stats/occupation"), ("GET /stats/prevision", "/stats/prevision"),
        ("GET /risque-absence/evaluation", "/risque-absence/evaluation"),
    ])
    appel(client, mesures, cible[0], cible[1], **({} if cible[1] in ("/stats/jour", "/risque-absence/evaluation", "/stats/prevision") else {"date_debut": d1, "date_fin": d2}))


ACTIONS = {"AGT": actions_agent, "MED": actions_medecin, "OPS": actions_responsable}


def utilisateur_virtuel(client, role_court, mesures, ctx, fin, ecritures, pause):
    while time.time() < fin:
        ACTIONS[role_court](client, mesures, ctx, ecritures)
        time.sleep(random.uniform(*pause))


def parcours_tableau_de_bord(client) -> dict[str, float]:
    """Les appels que fait l'écran « Tableau de bord » (12 mois), dans l'ordre ; retourne le temps cumulé par étape."""
    d1, d2 = (date.today() - timedelta(days=365)).isoformat(), date.today().isoformat()
    p = {"date_debut": d1, "date_fin": d2}
    etapes = {
        "Chargement initial (jour, indicateurs, occupation, vue Absences)": [
            ("/stats/jour", {}), ("/stats/kpi", p), ("/stats/occupation", p), ("/stats/absence/par-specialite", p), ("/stats/absence/mensuelle", p)],
        "Vue Demande": [("/stats/carte-chaleur", p), ("/stats/delai-attente", p)],
        "Vue Annulations": [("/stats/annulation", p)],
        "Vue Prévision": [("/stats/prevision", {})],
        "Vue Risque d'absence": [("/risque-absence/evaluation", {})],
    }
    resultat = {}
    for nom, appels in etapes.items():
        debut = time.perf_counter()
        for chemin, params in appels:
            r = client.get(chemin, params=params)
            if r.status_code != 200:
                raise RuntimeError(f"{chemin} : HTTP {r.status_code} {r.text[:120]}")
        resultat[nom] = time.perf_counter() - debut
    return resultat


def principal() -> int:
    analyse = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    analyse.add_argument("--url", default="http://localhost:8000")
    analyse.add_argument("--cles", required=True, help="fichier cles_a_distribuer_*.csv")
    analyse.add_argument("--utilisateurs", type=int, default=20)
    analyse.add_argument("--duree", type=int, default=60)
    analyse.add_argument("--pause-min", type=float, default=0.2)
    analyse.add_argument("--pause-max", type=float, default=1.0)
    analyse.add_argument("--ecritures", action="store_true", help="crée aussi des rendez-vous (base jetable uniquement)")
    args = analyse.parse_args()

    cles = lire_cles(args.cles)
    comptes = sorted(cles)
    agents = [c for c in comptes if c.startswith("AGT")]
    medecins_comptes = [c for c in comptes if c.startswith("MED")]
    responsables = [c for c in comptes if c.startswith("OPS")]
    # Répartition réaliste : surtout des agents, quelques médecins, un ou deux responsables.
    n = args.utilisateurs
    nb_resp = max(1, round(n * 0.1))
    nb_med = max(1, round(n * 0.3))
    choisis = (agents * 5)[: n - nb_resp - nb_med] + medecins_comptes[:nb_med] + responsables[:nb_resp]
    print(f"Connexion de {len(choisis)} utilisateurs ({len(choisis) - nb_med - nb_resp} agents, {nb_med} médecins, {nb_resp} responsables)...")

    mesures_connexion = Mesures()
    clients = []
    for ident in choisis:
        role, cle = cles[ident]
        debut = time.perf_counter()
        clients.append((ident[:3], connecter(args.url, ident, role, cle)))
        mesures_connexion.noter("POST /auth/connexion", (time.perf_counter() - debut) * 1000, True)

    ref = clients[0][1]
    ctx = {"medecins": ref.get("/referentiels/medecins").json()}
    responsable = next(c for r, c in clients if r == "OPS")

    print("Tableau de bord seul, sans autre utilisateur (3 passages)...")
    seul = [parcours_tableau_de_bord(responsable) for _ in range(3)]

    print(f"Charge : {len(clients)} utilisateurs simultanés pendant {args.duree} s...")
    mesures = Mesures()
    fin = time.time() + args.duree
    fils = [threading.Thread(target=utilisateur_virtuel, args=(c, r, mesures, ctx, fin, args.ecritures, (args.pause_min, args.pause_max)), daemon=True) for r, c in clients]
    debut_charge = time.time()
    for f in fils:
        f.start()
    sous_charge = []
    observateur = connecter(args.url, "OPS01", *cles["OPS01"]) if "OPS01" in cles else responsable
    while time.time() < fin - 2:  # le tableau de bord est aussi chargé pendant la charge
        time.sleep(min(8, max(1, fin - time.time() - 2)))
        sous_charge.append(parcours_tableau_de_bord(observateur))
    for f in fils:
        f.join()
    duree_reelle = time.time() - debut_charge

    toutes = [d for v in mesures.durees.values() for d in v]
    erreurs = sum(mesures.erreurs.values())
    print(f"\n{len(toutes)} requêtes en {duree_reelle:.0f} s ({len(toutes) / duree_reelle:.1f} requêtes/s), {erreurs} erreur(s).\n")
    print(f"{'Requête':<42}{'nb':>6}{'moy':>8}{'p50':>8}{'p95':>8}{'p99':>8}{'max':>8}  (ms)")
    for nom in sorted(mesures.durees, key=lambda k: -centile(mesures.durees[k], 0.95)):
        v = mesures.durees[nom]
        print(f"{nom:<42}{len(v):>6}{statistics.fmean(v):>8.0f}{centile(v, .5):>8.0f}{centile(v, .95):>8.0f}{centile(v, .99):>8.0f}{max(v):>8.0f}")
    p95 = centile(toutes, 0.95)
    connexions = mesures_connexion.durees["POST /auth/connexion"]
    print(f"\nConnexions (hachage Argon2, volontairement lent) : moyenne {statistics.fmean(connexions):.0f} ms, max {max(connexions):.0f} ms (hors objectif).")

    print("\nTableau de bord, temps de réponse de l'API (secondes) :")
    print(f"{'Étape':<68}{'seul':>7}{'sous charge':>13}")
    pire_seul = pire_charge = 0.0
    for nom in seul[0]:
        s = max(x[nom] for x in seul)
        c = max((x[nom] for x in sous_charge), default=float("nan"))
        print(f"{nom:<68}{s:>7.2f}{c:>13.2f}")
    initial = "Chargement initial (jour, indicateurs, occupation, vue Absences)"
    pire_charge = max((x[initial] for x in sous_charge), default=0.0)
    pire_seul = max(x[initial] for x in seul)

    ok_api = p95 < OBJECTIF_P95_MS and erreurs == 0
    ok_tab = max(pire_seul, pire_charge) < OBJECTIF_TABLEAU_S
    print(f"\nENF-01 : 95e centile global = {p95:.0f} ms (objectif < {OBJECTIF_P95_MS} ms) : {'ATTEINT' if ok_api else 'NON ATTEINT'}")
    print(f"ENF-02 : chargement initial du tableau de bord = {max(pire_seul, pire_charge):.2f} s (objectif < {OBJECTIF_TABLEAU_S:.0f} s) : {'ATTEINT' if ok_tab else 'NON ATTEINT'}")
    print("(Le temps d'affichage de Streamlit s'ajoute au temps de l'API : mesurez-le séparément dans le navigateur.)")
    return 0 if ok_api and ok_tab else 1


if __name__ == "__main__":
    sys.exit(principal())
