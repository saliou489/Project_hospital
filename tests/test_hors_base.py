"""Tests de bout en bout des outils en ligne de commande, sur une base SQLite jetable (sous-processus)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

RACINE = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path):
    e = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path}/t.db", "MEDIRDV_SECRET_JWT": "x" * 48, "LOG_LEVEL": "WARNING", "MEDIRDV_DONNEES_DEMO": "non", "MEDIRDV_DOSSIER_CLES": str(tmp_path)}
    e.pop("TEST_DATABASE_URL", None)
    return e


def lancer(env, *args):
    return subprocess.run([sys.executable, "-m", *args], cwd=RACINE, env=env, capture_output=True, text=True, timeout=300)


def test_migrations_creent_le_schema(env, tmp_path):
    r = lancer(env, "backend.initialiser")
    assert r.returncode == 0, r.stderr
    insp = inspect(create_engine(env["DATABASE_URL"]))
    tables = set(insp.get_table_names())
    assert {"patients", "rendez_vous", "medecins", "agents", "utilisateurs", "pointages", "journal_actions", "alembic_version"} <= tables


def test_initialiser_est_idempotent(env):
    assert lancer(env, "backend.initialiser").returncode == 0
    assert lancer(env, "backend.initialiser").returncode == 0


def test_base_ancienne_sans_alembic_est_reconnue(env):
    """Une base créée avant Alembic (schéma de la révision 0001, sans table alembic_version) est rattachée puis migrée."""
    script = (
        "from alembic.config import Config; from alembic import command; "
        "c = Config('alembic.ini'); c.set_main_option('script_location', 'alembic'); command.upgrade(c, '0001')"
    )
    assert subprocess.run([sys.executable, "-c", script], cwd=RACINE, env=env, capture_output=True, text=True).returncode == 0
    moteur = create_engine(env["DATABASE_URL"])
    with moteur.begin() as c:
        c.execute(text("DROP TABLE alembic_version"))
    assert lancer(env, "backend.initialiser").returncode == 0
    with moteur.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() != "0001"
        assert "plages_consultation" in inspect(moteur).get_table_names()


def test_generateur_refuse_d_ecraser_sans_remplacer(env):
    assert lancer(env, "backend.initialiser").returncode == 0
    r = lancer(env, "backend.generer_donnees", "--passes", "10", "--futurs", "5", "--patients", "10")
    assert r.returncode != 0  # le CSV importé contient déjà des rendez-vous


def test_generateur_complet_et_anomalies(env, tmp_path):
    assert lancer(env, "backend.initialiser").returncode == 0
    r = lancer(env, "backend.generer_donnees", "--passes", "1200", "--futurs", "100", "--patients", "250", "--remplacer",
               "--anomalies", "--fichier-anomalies", str(tmp_path / "an.csv"), "--graine", "5")
    assert r.returncode == 0, r.stderr + r.stdout
    assert (tmp_path / "an.csv").exists()
    with create_engine(env["DATABASE_URL"]).connect() as c:
        assert c.execute(text("SELECT count(*) FROM patients")).scalar() >= 250
    assert lancer(env, "backend.generer_donnees", "--passes", "10", "--futurs", "5", "--patients", "5").returncode != 0


def test_anomalies_refusees_sur_petit_volume(env):
    assert lancer(env, "backend.initialiser").returncode == 0
    r = lancer(env, "backend.generer_donnees", "--passes", "300", "--futurs", "50", "--patients", "60", "--remplacer", "--anomalies")
    assert r.returncode != 0 and "au moins 200 patients" in (r.stderr + r.stdout)
