"""Administration des comptes, journal, pointage."""


def test_liste_utilisateurs(admin):
    r = admin.get("/admin/utilisateurs")
    assert r.status_code == 200
    assert len(r.json()) >= 40 or len(r.json().get("elements", [])) >= 40


def test_creation_compte_et_connexion(admin, anonyme):
    r = admin.post("/admin/utilisateurs", json={"role": "Agent d'accueil", "nom_affichage": "Agent Test"})
    assert r.status_code == 201, r.text
    ident, cle = r.json()["utilisateur"]["id"], r.json()["cle"]
    assert len(cle) >= 10
    c = anonyme.post("/auth/connexion", json={"role": "Agent d'accueil", "identifiant": ident, "cle": cle})
    assert c.status_code == 200


def test_regeneration_invalide_ancienne_cle(admin, anonyme):
    r = admin.post("/admin/utilisateurs", json={"role": "Agent d'accueil", "nom_affichage": "Agent Regen"}).json()
    ident, ancienne = r["utilisateur"]["id"], r["cle"]
    nouvelle = admin.post(f"/admin/utilisateurs/{ident}/regenerer-cle").json()["cle"]
    assert nouvelle != ancienne
    ko = anonyme.post("/auth/connexion", json={"role": "Agent d'accueil", "identifiant": ident, "cle": ancienne})
    assert ko.status_code == 401


def test_desactivation_reactivation(admin, anonyme):
    r = admin.post("/admin/utilisateurs", json={"role": "Agent d'accueil", "nom_affichage": "Agent Off"}).json()
    ident, cle = r["utilisateur"]["id"], r["cle"]
    assert admin.post(f"/admin/utilisateurs/{ident}/desactiver", json={}).status_code == 200
    assert anonyme.post("/auth/connexion", json={"role": "Agent d'accueil", "identifiant": ident, "cle": cle}).status_code == 401
    assert admin.post(f"/admin/utilisateurs/{ident}/reactiver").status_code == 200
    assert anonyme.post("/auth/connexion", json={"role": "Agent d'accueil", "identifiant": ident, "cle": cle}).status_code == 200


def test_auto_desactivation_interdite(admin):
    r = admin.post("/admin/utilisateurs/ADM01/desactiver", json={})
    assert r.status_code == 400
    assert r.json()["code_erreur"] == "AUTO_DESACTIVATION_INTERDITE"


def test_utilisateur_introuvable(admin):
    assert admin.post("/admin/utilisateurs/XXX99/desactiver", json={}).status_code == 404


def test_journal_trace_les_actions(admin):
    r = admin.get("/admin/journal")
    assert r.status_code == 200
    texte = r.text
    assert "CONNEXION_REUSSIE" in texte


def test_pointage_cycle(connexion):
    c = connexion("AGT08")
    assert c.get("/pointage/aujourdhui").status_code == 200
    assert c.post("/pointage/depart").status_code in (400, 409)  # départ avant arrivée
    assert c.post("/pointage/arrivee").status_code in (200, 201)
    assert c.post("/pointage/arrivee").status_code == 409
    assert c.post("/pointage/depart").status_code in (200, 201)
    assert c.post("/pointage/depart").status_code == 409
    assert c.get("/pointage/mes-pointages").status_code == 200


def test_pointages_admin(admin):
    assert admin.get("/admin/pointages").status_code == 200


def test_pointages_interdits_agent(agent):
    assert agent.get("/admin/pointages").status_code == 403
