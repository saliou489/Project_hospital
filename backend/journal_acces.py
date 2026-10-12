"""Journal d'accès HTTP (ENF-16) : horodaté, et sans donnée personnelle en clair.

Par défaut, uvicorn écrit chaque requête sans date et avec sa chaîne de requête complète. Or l'interface
recherche les patients par « ?q=nom, prénom ou téléphone » : le nom d'un patient se retrouverait dans les journaux.
Ce module (1) masque la valeur de tous les paramètres de requête, en gardant leur nom, et (2) ajoute la date
et l'heure à chaque ligne.
"""
import logging
from urllib.parse import parse_qsl, urlsplit

FORMAT_ACCES = "%(asctime)s %(levelname)s uvicorn.access : %(message)s"


def masquer_requete(chemin: str) -> str:
    """Remplace la valeur de chaque paramètre par « * » : ``/patients?q=Dia&limite=20`` -> ``/patients?q=*&limite=*``."""
    morceaux = urlsplit(chemin)
    if not morceaux.query:
        return chemin
    noms = [nom for nom, _ in parse_qsl(morceaux.query, keep_blank_values=True)]
    return f"{morceaux.path}?" + "&".join(f"{nom}=*" for nom in noms)


class FiltreMasquage(logging.Filter):
    """Masque la chaîne de requête dans les enregistrements du journal d'accès d'uvicorn."""

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn fournit (adresse client, méthode, chemin, version HTTP, code de statut).
        if isinstance(record.args, tuple) and len(record.args) == 5 and isinstance(record.args[2], str):
            argument = list(record.args)
            argument[2] = masquer_requete(argument[2])
            record.args = tuple(argument)
        return True


def configurer_journal_acces() -> None:
    """À appeler une fois au chargement de l'application ; sans effet si le journal est déjà configuré."""
    journal = logging.getLogger("uvicorn.access")
    if any(isinstance(f, FiltreMasquage) for f in journal.filters):
        return
    journal.addFilter(FiltreMasquage())
    for gestionnaire in journal.handlers:
        gestionnaire.setFormatter(logging.Formatter(FORMAT_ACCES))
