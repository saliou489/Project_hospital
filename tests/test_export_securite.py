"""Export CSV et PDF (EF-26) et en-têtes de sécurité HTTP / injections (ENF-06)."""
import csv
import io
from datetime import date, timedelta

import pytest

from backend.routes.export import _cellule_csv


def _lire_csv(reponse) -> list[list[str]]:
    texte = reponse.content.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(texte), delimiter=";"))


# --- Export ---------------------------------------------------------------------------------------

def test_export_csv_coherent_avec_le_tableau_de_bord(responsable):
    p = {"date_debut": (date.today() - timedelta(days=200)).isoformat(), "date_fin": date.today().isoformat()}
    kpi = responsable.get("/stats/kpi", params=p).json()
    r = responsable.get("/export/indicateurs", params={**p, "format": "csv"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="indicateurs_medirdv_' in r.headers["content-disposition"]
    assert r.content.startswith(b"\xef\xbb\xbf")  # BOM : Excel lit correctement les accents
    lignes = _lire_csv(r)
    valeurs = {l[0]: l[1] for l in lignes if len(l) == 2}
    assert valeurs["Rendez-vous"] == str(kpi["total_rdv"]) and valeurs["Absents"] == str(kpi["absents"])
    assert valeurs["Honorés"] == str(kpi["honores"])
    assert valeurs["Taux d'absence (%)"] == f"{kpi['taux_absence_pct']:.1f}".replace(".", ",")
    titres = {l[0] for l in lignes if len(l) == 1}
    assert {"Indicateurs clés", "Évolution mensuelle", "Annulations par motif", "Taux d'absence par spécialité"} <= titres
    assert any(l[0] == "Exporté par" and l[1] == "OPS01" for l in lignes)


def test_export_pdf_valide(responsable):
    r = responsable.get("/export/indicateurs", params={"format": "pdf"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF-") and r.content.rstrip().endswith(b"%%EOF") and len(r.content) > 2000


def test_export_filtre_par_specialite_et_medecin(responsable, medecins):
    m = medecins[0]
    r = responsable.get("/export/indicateurs", params={"format": "csv", "medecin_id": m["id"]})
    lignes = _lire_csv(r)
    assert any(l[:2] == ["Médecin", m["id"]] for l in lignes)
    assert "Taux d'absence par spécialité" not in {l[0] for l in lignes if len(l) == 1}  # sans objet pour un seul médecin
    r = responsable.get("/export/indicateurs", params={"format": "csv", "specialite": m["specialite"]})
    assert any(l[:2] == ["Spécialité", m["specialite"]] for l in _lire_csv(r))


def test_export_droits_et_erreurs(connexion, medecins, agent, admin, anonyme, responsable):
    assert anonyme.get("/export/indicateurs").status_code == 401
    assert agent.get("/export/indicateurs").status_code == 403
    assert connexion("DIR01").get("/export/indicateurs").status_code == 403  # la direction consulte, elle n'exporte pas
    assert connexion(medecins[0]["id"]).get("/export/indicateurs").status_code == 403
    assert admin.get("/export/indicateurs").status_code == 200
    assert responsable.get("/export/indicateurs", params={"format": "xls"}).status_code == 422
    assert responsable.get("/export/indicateurs", params={"date_debut": "2026-05-02", "date_fin": "2026-05-01"}).status_code == 422
    assert responsable.get("/export/indicateurs", params={"medecin_id": "MED999"}).status_code == 404


def test_export_journalise(responsable, admin):
    responsable.get("/export/indicateurs", params={"format": "pdf"})
    actions = admin.get("/admin/journal", params={"action": "EXPORT_INDICATEURS"}).json()
    elements = actions["elements"] if isinstance(actions, dict) else actions
    assert elements and elements[0]["utilisateur_id"] == "OPS01" and elements[0]["cible"] == "PDF"


def test_csv_neutralise_les_formules():
    assert _cellule_csv("=SOMME(A1:A9)") == "'=SOMME(A1:A9)"
    assert _cellule_csv("@cmd") == "'@cmd" and _cellule_csv("+33") == "'+33"
    assert _cellule_csv("12,5") == "12,5" and _cellule_csv("Cardiologie") == "Cardiologie" and _cellule_csv(7) == 7


# --- En-têtes de sécurité -------------------------------------------------------------------------

@pytest.mark.parametrize("chemin,statut", [("/sante", 200), ("/chemin-inconnu", 404)])
def test_entetes_de_securite(anonyme, chemin, statut):
    r = anonyme.get(chemin)
    assert r.status_code == statut
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert "strict-transport-security" not in r.headers  # requête en HTTP simple : pas de HSTS


def test_entetes_sur_reponse_authentifiee_et_erreur(agent, anonyme):
    assert agent.get("/referentiels/medecins").headers["x-content-type-options"] == "nosniff"
    r = anonyme.get("/export/indicateurs")
    assert r.status_code == 401 and r.headers["x-frame-options"] == "DENY"


def test_hsts_derriere_un_proxy_https(anonyme):
    r = anonyme.get("/sante", headers={"X-Forwarded-Proto": "https"})
    assert "max-age=" in r.headers["strict-transport-security"]


def test_documentation_non_bloquee_par_la_politique(anonyme):
    r = anonyme.get("/docs")
    assert r.status_code == 200 and "content-security-policy" not in r.headers


# --- Injections ----------------------------------------------------------------------------------

CHAINES_HOSTILES = ["' OR '1'='1", "1; DROP TABLE patients; --", "\" OR 1=1 --", "<script>alert(1)</script>", "%' UNION SELECT cle_hash FROM utilisateurs --"]


@pytest.mark.parametrize("chaine", CHAINES_HOSTILES)
def test_recherche_resiste_aux_injections(agent, chaine):
    r = agent.get("/patients", params={"q": chaine})
    assert r.status_code in (200, 422), r.text
    assert "cle_hash" not in r.text and "argon2" not in r.text.lower()
    # Aucune table n'a été touchée : la recherche normale fonctionne toujours.
    if r.status_code == 200:
        assert r.json()["total"] == 0  # le texte est cherché tel quel, il n'élargit jamais la recherche
    assert agent.get("/patients", params={"q": "PAT"}).json()["total"] > 100


@pytest.mark.parametrize("chaine", CHAINES_HOSTILES)
def test_connexion_resiste_aux_injections(anonyme, chaine):
    r = anonyme.post("/auth/connexion", json={"role": "Agent d'accueil", "identifiant": chaine[:12], "cle": chaine})
    assert r.status_code in (401, 422) and "detail" in r.json() and "code_erreur" in r.json()


@pytest.mark.parametrize("chaine", CHAINES_HOSTILES)
def test_filtres_stats_resistent_aux_injections(responsable, chaine):
    r = responsable.get("/stats/kpi", params={"specialite": chaine})
    assert r.status_code == 200 and r.json()["total_rdv"] == 0  # texte comparé tel quel, jamais interprété
    assert responsable.get("/stats/kpi", params={"medecin_id": chaine}).status_code in (404, 422)


def test_donnees_hostiles_stockees_telles_quelles_et_renvoyees_en_json(agent):
    """Un nom contenant du HTML est enregistré comme texte ; l'API le renvoie en JSON (échappé), l'interface Streamlit n'exécute pas le HTML."""
    r = agent.post("/patients", json={"nom": "<script>alert(1)</script>", "prenom": "Test", "sexe": "F", "date_naissance": "1990-01-01", "telephone": "770000000"})
    assert r.status_code in (201, 422), r.text
    if r.status_code == 201:
        assert r.headers["content-type"].startswith("application/json") and r.headers["x-content-type-options"] == "nosniff"
