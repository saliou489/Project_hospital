"""Sauvegarde et restauration (ENF-10) : restauration testée, intégrité vérifiée, d'un moteur à l'autre."""
import json
import os
import stat
import zipfile
from datetime import datetime

import pytest
from sqlalchemy import create_engine, func, select, text

from backend import sauvegarde
from backend.database import Base, engine
from backend.models import RendezVous, Utilisateur


def _moteur_vide(tmp_path, nom="cible.db"):
    """Base SQLite neuve, avec le schéma complet et la même révision que la base source."""
    cible = create_engine(f"sqlite:///{tmp_path / nom}")
    Base.metadata.create_all(cible)
    with engine.connect() as source:
        revision = sauvegarde._revision(source)
    with cible.begin() as c:
        c.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        c.execute(text("INSERT INTO alembic_version VALUES (:r)"), {"r": revision})
    return cible


def _comptes(moteur):
    with moteur.connect() as c:
        return {t.name: c.execute(select(func.count()).select_from(t)).scalar() for t in sauvegarde._tables()}


@pytest.fixture(scope="module")
def fichier(cles, tmp_path_factory):
    return sauvegarde.sauvegarder(engine, tmp_path_factory.mktemp("sauvegardes"))


def test_manifeste_et_comptes(fichier):
    manifeste, contenus = sauvegarde.lire_et_verifier(fichier)
    assert manifeste["tables"]["rendez_vous"]["lignes"] == _comptes(engine)["rendez_vous"] > 500
    assert manifeste["total_lignes"] == sum(_comptes(engine).values()) and manifeste["revision_schema"]
    assert set(contenus) == {t.name for t in sauvegarde._tables()}
    if os.name == "posix":
        assert stat.S_IMODE(fichier.stat().st_mode) == 0o600  # contient des données personnelles


def test_restauration_identique_dans_une_base_vide(fichier, tmp_path):
    cible = _moteur_vide(tmp_path)
    restaures = sauvegarde.restaurer(cible, fichier)
    assert restaures == _comptes(cible) == _comptes(engine)
    # Contenu identique : une nouvelle sauvegarde de la base restaurée a les mêmes empreintes que l'originale.
    copie = sauvegarde.sauvegarder(cible, tmp_path / "copie")
    original, _ = sauvegarde.lire_et_verifier(fichier)
    restauree, _ = sauvegarde.lire_et_verifier(copie)
    assert {n: t["sha256"] for n, t in original["tables"].items()} == {n: t["sha256"] for n, t in restauree["tables"].items()}
    with cible.connect() as c:  # types et contraintes bien rétablis
        rdv = c.execute(select(RendezVous).limit(1)).first()
        assert isinstance(rdv.debut, datetime) and rdv.statut.value
        assert c.execute(select(func.count()).select_from(Utilisateur).where(Utilisateur.cle_hash.like("$argon2%"))).scalar() > 40


def test_restauration_refuse_une_base_non_vide(fichier, tmp_path):
    cible = _moteur_vide(tmp_path)
    sauvegarde.restaurer(cible, fichier)
    avant = _comptes(cible)
    with pytest.raises(sauvegarde.SauvegardeError, match="déjà"):
        sauvegarde.restaurer(cible, fichier)
    assert _comptes(cible) == avant  # rien n'a été écrasé


def test_remplacer_efface_puis_recharge(fichier, tmp_path):
    cible = _moteur_vide(tmp_path)
    sauvegarde.restaurer(cible, fichier)
    with cible.begin() as c:
        c.execute(text("DELETE FROM scores_risque"))
        c.execute(text("DELETE FROM historique_rdv"))
        c.execute(text("DELETE FROM pointages"))
        c.execute(text("DELETE FROM rendez_vous WHERE id IN (SELECT id FROM rendez_vous LIMIT 50)"))
    assert _comptes(cible)["rendez_vous"] < _comptes(engine)["rendez_vous"]
    sauvegarde.restaurer(cible, fichier, remplacer=True)
    assert _comptes(cible) == _comptes(engine)


def test_fichier_altere_refuse(fichier, tmp_path):
    abime = tmp_path / "abime.zip"
    with zipfile.ZipFile(fichier) as source, zipfile.ZipFile(abime, "w") as sortie:
        for nom in source.namelist():
            donnees = source.read(nom)
            if nom == "patients.jsonl":
                donnees = donnees.replace(b"PAT00001", b"PAT99999", 1)  # une modification discrète
            sortie.writestr(nom, donnees)
    with pytest.raises(sauvegarde.SauvegardeError, match="altérée"):
        sauvegarde.lire_et_verifier(abime)
    (tmp_path / "pas_un_zip.zip").write_bytes(b"ceci n'est pas une sauvegarde")
    with pytest.raises(sauvegarde.SauvegardeError, match="illisible"):
        sauvegarde.lire_et_verifier(tmp_path / "pas_un_zip.zip")


def test_schema_incompatible_refuse(fichier, tmp_path):
    cible = _moteur_vide(tmp_path)
    with cible.begin() as c:
        c.execute(text("UPDATE alembic_version SET version_num = '0001'"))
    with pytest.raises(sauvegarde.SauvegardeError, match="Schéma incompatible"):
        sauvegarde.restaurer(cible, fichier)
    assert sum(_comptes(cible).values()) == 0


def test_une_restauration_qui_echoue_laisse_la_base_intacte(fichier, tmp_path):
    """Si l'insertion échoue en cours de route, tout est annulé (une seule transaction)."""
    cible = _moteur_vide(tmp_path)
    mauvais = tmp_path / "mauvais.zip"
    manifeste, contenus = sauvegarde.lire_et_verifier(fichier)
    contenus["rendez_vous"] = contenus["rendez_vous"].replace(b'"statut": "Honor', b'"statut": "Inconnu', 1)
    import hashlib

    manifeste["tables"]["rendez_vous"]["sha256"] = hashlib.sha256(contenus["rendez_vous"]).hexdigest()
    with zipfile.ZipFile(mauvais, "w") as z:
        z.writestr("manifeste.json", json.dumps(manifeste))
        for nom, donnees in contenus.items():
            z.writestr(f"{nom}.jsonl", donnees)
    with pytest.raises(Exception):  # noqa: B017 - ValueError ou LookupError selon la valeur refusée
        sauvegarde.restaurer(cible, mauvais)
    assert sum(_comptes(cible).values()) == 0


def test_retention_garde_les_plus_recentes_et_protege_les_sauvegardes_de_securite(tmp_path):
    for jour in range(1, 6):
        (tmp_path / f"medirdv_202610{jour:02d}_030000.zip").write_bytes(b"x")
    protegee = tmp_path / "medirdv_20261001_040000_avant_restauration.zip"
    protegee.write_bytes(b"x")
    supprimees = sauvegarde.appliquer_retention(tmp_path, 2)
    assert sorted(f.name for f in supprimees) == ["medirdv_20261001_030000.zip", "medirdv_20261002_030000.zip", "medirdv_20261003_030000.zip"]
    assert protegee.exists() and (tmp_path / "medirdv_20261005_030000.zip").exists()


def test_ligne_de_commande(fichier, capsys):
    assert sauvegarde.principal(["verifier", str(fichier)]) == 0
    assert "Sauvegarde valide" in capsys.readouterr().out
    assert sauvegarde.principal(["verifier", str(fichier) + ".absent"]) == 1
    assert "illisible" in capsys.readouterr().err
    assert sauvegarde.principal(["lister", "--dossier", str(fichier.parent)]) == 0
    assert fichier.name in capsys.readouterr().out


def test_restauration_sur_la_base_de_test_elle_meme_et_sequences(fichier):
    """Remplacement sur place (PostgreSQL ou SQLite) : mêmes données, et les identifiants générés repartent après le maximum."""
    from backend.database import SessionLocale
    from backend.models import JournalAction

    avant = _comptes(engine)
    sauvegarde.restaurer(engine, fichier, remplacer=True)
    assert _comptes(engine) == avant
    with SessionLocale() as db:
        dernier = db.scalar(select(func.max(JournalAction.id)))
        db.add(JournalAction(action="TEST_SAUVEGARDE"))
        db.commit()
        nouveau = db.scalar(select(JournalAction.id).where(JournalAction.action == "TEST_SAUVEGARDE"))
        assert nouveau > (dernier or 0)
        db.query(JournalAction).filter(JournalAction.action == "TEST_SAUVEGARDE").delete()
        db.commit()
