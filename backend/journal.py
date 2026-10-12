"""Journal des actions (EF-32) : qui a créé, modifié ou annulé quoi, et quand.

L'entrée est ajoutée à la session de l'action elle-même : elle est validée ou annulée en même
temps qu'elle, si bien que le journal ne contient jamais une action qui n'a pas eu lieu.
Il ne contient que des identifiants et des libellés techniques, jamais de donnée personnelle en clair.
"""
from sqlalchemy.orm import Session

from backend.models import JournalAction, Utilisateur


def journaliser(
    db: Session,
    utilisateur: Utilisateur | None,
    action: str,
    cible: str | None = None,
    detail: str | None = None,
) -> None:
    """Ajoute une entrée au journal (sans valider la transaction : c'est le rôle de l'appelant)."""
    db.add(
        JournalAction(
            utilisateur_id=utilisateur.id if utilisateur is not None else None,
            role=utilisateur.role.value if utilisateur is not None else None,
            action=action,
            cible=cible,
            detail=detail[:300] if detail else None,
        )
    )
