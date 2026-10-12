"""Authentification, verrouillage, changement de clé, droits par rôle."""
import jwt

from tests.conftest import AGENT, role_de


def test_sante(anonyme):
    assert anonyme.get("/sante").status_code == 200


def test_connexion_reussie(connexion, cles):
    client = connexion("AGT02")
    moi = client.get("/auth/moi")
    assert moi.status_code == 200
    assert moi.json()["id"] == "AGT02"


def test_mauvaise_cle(anonyme):
    r = anonyme.post("/auth/connexion", json={"role": AGENT, "identifiant": "AGT03", "cle": "mauvaise-cle-123"})
    assert r.status_code == 401
    assert r.json()["code_erreur"] == "IDENTIFIANTS_INVALIDES"


def test_mauvais_role(anonyme, cles):
    r = anonyme.post("/auth/connexion", json={"role": "Direction", "identifiant": "AGT03", "cle": cles["AGT03"]})
    assert r.status_code == 401


def test_identifiant_inconnu_meme_message(anonyme):
    r = anonyme.post("/auth/connexion", json={"role": AGENT, "identifiant": "AGT99", "cle": "n-importe-quoi-12"})
    assert r.status_code == 401
    assert r.json()["code_erreur"] == "IDENTIFIANTS_INVALIDES"


def test_verrouillage_apres_cinq_echecs(anonyme, cles):
    for _ in range(5):
        anonyme.post("/auth/connexion", json={"role": AGENT, "identifiant": "AGT04", "cle": "mauvaise-cle-123"})
    r = anonyme.post("/auth/connexion", json={"role": AGENT, "identifiant": "AGT04", "cle": cles["AGT04"]})
    assert r.status_code == 401
    assert r.json()["code_erreur"] == "COMPTE_VERROUILLE"
    assert "5 minute" in r.json()["detail"]


def test_sans_jeton(anonyme):
    r = anonyme.get("/patients")
    assert r.status_code == 401
    assert r.json()["code_erreur"] == "NON_AUTHENTIFIE"


def test_jeton_falsifie(anonyme):
    r = anonyme.get("/patients", headers={"Authorization": "Bearer abc.def.ghi"})
    assert r.status_code == 401


def test_jeton_signe_autrement_refuse(anonyme):
    faux = jwt.encode({"sub": "ADM01"}, "autre-secret-suffisamment-long-0123456789abcdef", algorithm="HS256")
    assert anonyme.get("/admin/utilisateurs", headers={"Authorization": f"Bearer {faux}"}).status_code == 401


def test_changement_de_cle_invalide_autres_sessions(connexion, cles, anonyme):
    session_a = connexion("AGT05")
    session_b = connexion("AGT05")
    nouvelle = "NouvelleCle-Solide-2026"
    r = session_a.post("/auth/changer-cle", json={"cle_actuelle": cles["AGT05"], "nouvelle_cle": nouvelle})
    assert r.status_code == 200, r.text
    assert session_b.get("/auth/moi").status_code == 401  # ancienne session fermée
    session_a.headers["Authorization"] = f"Bearer {r.json()['jeton']}"
    assert session_a.get("/auth/moi").status_code == 200
    cles["AGT05"] = nouvelle


def test_changement_cle_trop_courte(connexion, cles):
    r = connexion("AGT06").post("/auth/changer-cle", json={"cle_actuelle": cles["AGT06"], "nouvelle_cle": "court"})
    assert r.status_code == 422


def test_changement_cle_actuelle_fausse(connexion):
    r = connexion("AGT07").post("/auth/changer-cle", json={"cle_actuelle": "pas-la-bonne-cle", "nouvelle_cle": "AutreCle-Solide-2026"})
    assert r.status_code == 400
    assert r.json()["code_erreur"] == "CLE_ACTUELLE_INCORRECTE"


def test_droits_par_role(agent, admin, responsable, direction, connexion):
    assert agent.get("/admin/utilisateurs").status_code == 403
    assert admin.get("/admin/utilisateurs").status_code == 200
    assert agent.get("/qualite/rapport").status_code == 403
    assert direction.get("/stats/kpi").status_code == 200
    assert responsable.get("/qualite/doublons").status_code == 200


def test_role_de():
    assert role_de("ADM01") == "Administrateur"
