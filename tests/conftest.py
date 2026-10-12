"""Configuration des tests.

Par défaut les tests s'exécutent sur une base SQLite temporaire. Pour les lancer sur PostgreSQL (comme en production) :

    TEST_DATABASE_URL=postgresql://utilisateur:motdepasse@hote:5432/base_de_test  pytest

ATTENTION : la base indiquée est vidée au début de la session de tests. N'utilisez jamais la base réelle.

Le jeu de données est créé une seule fois par session : import du CSV (médecins, agents), génération de 150 patients et
de 1 000 rendez-vous (graine fixée), puis création des 45 comptes. Les variables d'environnement sont positionnées AVANT
l'import de l'application, qui les lit au chargement.
"""
import os
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path

_DOSSIER_TEMPORAIRE = tempfile.mkdtemp(prefix="medirdv_tests_")
os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL") or f"sqlite:///{_DOSSIER_TEMPORAIRE}/test.db"
os.environ["MEDIRDV_SECRET_JWT"] = "secret-de-test-assez-long-pour-hs256-0123456789-abcdef"
os.environ["MEDIRDV_MAX_ESSAIS"] = "5"
os.environ["MEDIRDV_VERROUILLAGE_MIN"] = "5"
os.environ["MEDIRDV_SESSION_HEURES"] = "8"
os.environ["LOG_LEVEL"] = "WARNING"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select, text  # noqa: E402

from backend import seed_data  # noqa: E402
from backend.creer_comptes import creer_comptes  # noqa: E402
from backend.database import Base, SessionLocale, engine, initialiser_base  # noqa: E402
from backend.generer_donnees import generer  # noqa: E402
from backend.main import app  # noqa: E402
from backend.models import (  # noqa: E402
    Canal,
    Patient,
    RendezVous,
    Sexe,
    StatutRdv,
    MotifAnnulation,
)

RACINE = Path(__file__).resolve().parent.parent
AGENT, MEDECIN, RESPONSABLE, DIRECTION, ADMIN = (
    "Agent d'accueil",
    "Médecin",
    "Responsable des opérations",
    "Direction",
    "Administrateur",
)
ROLES = {"AGT": AGENT, "MED": MEDECIN, "OPS": RESPONSABLE, "DIR": DIRECTION, "ADM": ADMIN}


@pytest.fixture(scope="session")
def cles() -> dict[str, str]:
    """Base neuve et peuplée ; retourne les clés de connexion {identifiant: clé}."""
    Base.metadata.drop_all(engine)
    with engine.begin() as connexion:
        connexion.execute(text("DROP TABLE IF EXISTS alembic_version"))
    initialiser_base()
    seed_data.executer_import(RACINE / "data" / "rendez_vous_medirdv_propre.csv", Path(_DOSSIER_TEMPORAIRE) / "rejets.csv", False)
    with SessionLocale() as db:
        generer(db, passes=800, futurs=200, nb_patients=150, graine=11, reference=date.today(), remplacer=True)
        return {c["identifiant"]: c["cle"] for c in creer_comptes(db)}


def role_de(identifiant: str) -> str:
    return ROLES[identifiant[:3]]


@pytest.fixture(scope="session")
def anonyme(cles) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="session")
def connexion(cles):
    """Fabrique un client authentifié : connexion('AGT01') -> TestClient avec l'en-tête Authorization."""

    def _connecter(identifiant: str, cle: str | None = None) -> TestClient:
        client = TestClient(app, raise_server_exceptions=False)
        reponse = client.post("/auth/connexion", json={"role": role_de(identifiant), "identifiant": identifiant, "cle": cle or cles[identifiant]})
        assert reponse.status_code == 200, reponse.text
        client.headers["Authorization"] = f"Bearer {reponse.json()['jeton']}"
        return client

    return _connecter


@pytest.fixture(scope="session")
def agent(connexion) -> TestClient:
    return connexion("AGT01")


@pytest.fixture(scope="session")
def admin(connexion) -> TestClient:
    return connexion("ADM01")


@pytest.fixture(scope="session")
def responsable(connexion) -> TestClient:
    return connexion("OPS01")


@pytest.fixture(scope="session")
def direction(connexion) -> TestClient:
    return connexion("DIR01")


@pytest.fixture(scope="session")
def medecins(agent) -> list[dict]:
    return agent.get("/referentiels/medecins").json()


# ----------------------------------------------------------------------------------------------------
# Générateurs de données uniques (les tests partagent la même base)
# ----------------------------------------------------------------------------------------------------

class _Compteur:
    valeur = 0

    @classmethod
    def suivant(cls) -> int:
        cls.valeur += 1
        return cls.valeur


def lettres(n: int) -> str:
    """Convertit un entier en suite de lettres (les noms n'acceptent pas les chiffres)."""
    texte = ""
    n += 1
    while n:
        n, reste = divmod(n - 1, 26)
        texte = chr(97 + reste) + texte
    return texte


def corps_patient(**surcharges) -> dict:
    """Identité unique à chaque appel : aucun doublon accidentel entre tests."""
    n = _Compteur.suivant()
    corps = {
        "nom": "Test" + lettres(n),
        "prenom": "Essai" + lettres(n * 7 + 3),
        "sexe": "F",
        "date_naissance": "1990-05-17",
        "telephone": f"77{(5000000 + n):07d}",
        "quartier": "Fann",
    }
    corps.update(surcharges)
    return corps


@pytest.fixture
def nouveau_patient(agent):
    def _creer(**surcharges) -> str:
        reponse = agent.post("/patients", json=corps_patient(**surcharges))
        assert reponse.status_code == 201, reponse.text
        return reponse.json()["id"]

    return _creer


def _jours_ouverts(depuis: int = 30, nombre: int = 120) -> list[date]:
    """Jours à venir ouverts (lundi-samedi, hors jours fériés calculables) : ceux des plages par défaut des médecins."""
    from backend.planning import jours_feries_calculables

    feries = {j for annee in (date.today().year, date.today().year + 1) for j, _ in jours_feries_calculables(annee)}
    jours = []
    jour = date.today() + timedelta(days=depuis)
    while len(jours) < nombre:
        if jour.weekday() < 6 and jour not in feries:
            jours.append(jour)
        jour += timedelta(days=1)
    return jours


HEURES_OUVERTES = (8, 9, 10, 11, 14, 15, 16, 17)  # sur la grille de 15, 20 et 30 minutes, dans les plages par défaut


def creneau_unique(*medecin_ids: str) -> tuple[date, time]:
    """Un créneau ouvert (RG-03), jamais utilisé par un autre test, et libre pour chacun des médecins indiqués."""
    jours = _jours_ouverts()
    while True:
        n = _Compteur.suivant()
        jour, heure = jours[(n // len(HEURES_OUVERTES)) % len(jours)], time(HEURES_OUVERTES[n % len(HEURES_OUVERTES)], 0)
        if not medecin_ids:
            return jour, heure
        with SessionLocale() as db:
            pris = db.scalar(
                select(func.count()).select_from(RendezVous).where(
                    RendezVous.medecin_id.in_(medecin_ids), RendezVous.debut == datetime.combine(jour, heure),
                    RendezVous.statut.in_((StatutRdv.PLANIFIE, StatutRdv.CONFIRME, StatutRdv.HONORE)),
                )
            )  # fmt: skip
        if not pris:
            return jour, heure


def corps_rdv(patient_id: str, medecin_id: str, jour: date, heure: time, **extra) -> dict:
    return {"patient_id": patient_id, "medecin_id": medecin_id, "date_rdv": jour.isoformat(), "heure_rdv": heure.strftime("%H:%M"), **extra}


def inserer_rdv(patient_id: str, medecin_id: str, debut: datetime, statut=StatutRdv.PLANIFIE, duree=15, **extra) -> str:
    """Insère directement un rendez-vous (y compris dans le passé ou avec des anomalies), hors des règles de l'API."""
    with SessionLocale() as db:
        identifiant = f"RDV9{_Compteur.suivant():04d}"
        valeurs = dict(
            id=identifiant, patient_id=patient_id, medecin_id=medecin_id, agent_id="AGT01",
            date_prise=min(date.today(), debut.date() - timedelta(days=2)), debut=debut, fin=debut + timedelta(minutes=duree),
            statut=statut, confirme=False, canal=Canal.TELEPHONE,
            motif_annulation=MotifAnnulation.PATIENT if statut == StatutRdv.ANNULE else None,
        )  # fmt: skip
        valeurs.update(extra)
        db.add(RendezVous(**valeurs))
        db.commit()
        return identifiant


def inserer_patient(**surcharges) -> str:
    """Insère directement une fiche patient (permet des valeurs interdites par l'API)."""
    n = _Compteur.suivant()
    with SessionLocale() as db:
        identifiant = f"PAT9{n:04d}"
        valeurs = dict(
            id=identifiant, nom="Insere" + lettres(n), prenom="Direct" + lettres(n + 40), sexe=Sexe.FEMININ,
            date_naissance=date(1985, 3, 3), telephone=f"78{(6000000 + n):07d}", quartier="Yoff",
        )  # fmt: skip
        valeurs.update(surcharges)
        db.add(Patient(**valeurs))
        db.commit()
        return identifiant
