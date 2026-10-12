"""ENF-16 : le journal d'accès ne contient ni donnée personnelle ni ligne sans horodatage."""
import logging

from backend.journal_acces import FORMAT_ACCES, FiltreMasquage, masquer_requete


def test_valeurs_de_requete_masquees():
    assert masquer_requete("/patients?limite=20&decalage=0&q=Dia") == "/patients?limite=*&decalage=*&q=*"
    assert masquer_requete("/patients?q=77%20123%2045%2067") == "/patients?q=*"


def test_chemin_sans_requete_inchange():
    assert masquer_requete("/patients/PAT00404") == "/patients/PAT00404"


def test_ligne_de_journal_masquee_et_horodatee():
    enregistrement = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/patients?q=Diouf%20Awa", "1.1", 200), None,
    )
    assert FiltreMasquage().filter(enregistrement)
    ligne = logging.Formatter(FORMAT_ACCES).format(enregistrement)
    assert "Diouf" not in ligne and "q=*" in ligne
    assert ligne[:4].isdigit()  # commence par l'année : la ligne est horodatée
