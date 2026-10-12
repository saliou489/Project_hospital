"""Tests unitaires de fonctions pures (aucune base nécessaire)."""
from datetime import date

import pytest
from pydantic import ValidationError

from backend.qualite import cle_identite
from backend.routes.stats import calculer_taux_absence
from backend.schemas import PatientCreation, RdvCreation, valider_telephone
from backend.securite import (
    EMPREINTE_FACTICE,
    empreinte_session,
    generer_cle,
    hacher_cle,
    normaliser_cle,
    verifier_cle,
)


def test_taux_absence_rg09():
    assert calculer_taux_absence(90, 10) == 10.0
    assert calculer_taux_absence(0, 0) is None
    assert calculer_taux_absence(0, 5) == 100.0


def test_cle_generee_format():
    cle = generer_cle()
    assert len(cle.split("-")) == 3 and all(len(b) == 4 for b in cle.split("-"))
    assert generer_cle() != cle


def test_cle_insensible_casse_espaces_tirets():
    assert normaliser_cle(" ab-cd 12 ") == "ABCD12"
    empreinte = hacher_cle("ABCD-EFGH-JKLM")
    assert verifier_cle(empreinte, "abcd efgh jklm")
    assert not verifier_cle(empreinte, "ABCD-EFGH-JKLN")
    assert not verifier_cle("pas-une-empreinte", "x")


def test_empreinte_session_change_avec_la_cle():
    a, b = hacher_cle("AAAA-BBBB-CCCC"), hacher_cle("AAAA-BBBB-CCCC")
    assert a != b  # sel différent
    assert empreinte_session(a) != empreinte_session(b)
    assert empreinte_session(a) == empreinte_session(a)
    assert EMPREINTE_FACTICE


@pytest.mark.parametrize("tel", ["771234567", "781234567", "761234567", "701234567", "751234567", "331234567"])
def test_telephones_valides(tel):
    assert valider_telephone(tel) == tel


@pytest.mark.parametrize("tel", ["12345", "791234567", "77123456", "7712345678", "abcdefghi"])
def test_telephones_invalides(tel):
    with pytest.raises(ValueError):
        valider_telephone(tel)


def test_cle_identite_ignore_accents_casse_ordre():
    a = cle_identite("Diop", "Aïssatou", date(1990, 1, 1))
    assert a == cle_identite("DIOP", "aissatou", date(1990, 1, 1))
    assert a == cle_identite("Aïssatou", "Diop", date(1990, 1, 1))
    assert a != cle_identite("Diop", "Aïssatou", date(1990, 1, 2))


def test_schema_patient_normalise_les_noms():
    p = PatientCreation(nom="  diop ", prenom="aissatou", sexe="F", date_naissance="1990-01-01", telephone="771234567")
    assert p.nom == "Diop" or p.nom.lower() == "diop"


def test_schema_rdv_refuse_heure_avec_secondes():
    with pytest.raises(ValidationError):
        RdvCreation(patient_id="PAT00001", medecin_id="MED001", date_rdv="2999-01-01", heure_rdv="10:00:30")
