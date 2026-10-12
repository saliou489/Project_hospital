"""Endpoints d'authentification : connexion par identifiant et clé personnelle, profil connecté."""
import logging
import math
from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.database import get_db, transaction_ecriture
from backend.exceptions import NonAuthentifieError, RegleMetierError
from backend.journal import journaliser
from backend.models import Utilisateur, maintenant
from backend.schemas import ChangementCle, ConnexionDemande, ConnexionReponse, ErreurReponse, UtilisateurLecture
from backend.securite import (
    DUREE_VERROUILLAGE_MIN,
    EMPREINTE_FACTICE,
    MAX_ESSAIS,
    creer_jeton,
    hacher_cle,
    normaliser_cle,
    utilisateur_courant,
    verifier_cle,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentification"])

REPONSES_ERREURS = {
    401: {"model": ErreurReponse, "description": "Identifiant, clé ou rôle incorrect, compte verrouillé ou session invalide"},
    422: {"model": ErreurReponse, "description": "Données invalides"},
    500: {"model": ErreurReponse, "description": "Erreur interne"},
}

def _compter_echec(utilisateur: Utilisateur, instant) -> None:
    """Compte un essai de clé raté ; au bout de MAX_ESSAIS, le compte est verrouillé temporairement."""
    utilisateur.essais_echoues += 1
    if utilisateur.essais_echoues >= MAX_ESSAIS:
        utilisateur.essais_echoues = 0
        utilisateur.verrouille_jusqua = instant + timedelta(minutes=DUREE_VERROUILLAGE_MIN)


# Message unique : on ne révèle pas lequel des trois éléments (identifiant, clé, rôle) est faux.
MESSAGE_IDENTIFIANTS = "Identifiant, clé ou rôle incorrect."


@router.post(
    "/connexion",
    response_model=ConnexionReponse,
    summary="Se connecter avec son identifiant et sa clé personnelle",
    responses=REPONSES_ERREURS,
)
def connexion(donnees: ConnexionDemande, db: Session = Depends(get_db)) -> ConnexionReponse:
    """Vérifie l'identifiant, la clé et le rôle choisi (qui doit être celui du compte), puis délivre un jeton.

    Après plusieurs clés erronées consécutives, le compte est verrouillé temporairement.
    """
    identifiant = donnees.identifiant.strip().upper()
    utilisateur = db.get(Utilisateur, identifiant)
    instant = maintenant()

    # Compte verrouillé : la clé n'est même pas examinée.
    if utilisateur is not None and utilisateur.verrouille_jusqua and utilisateur.verrouille_jusqua > instant:
        minutes = math.ceil((utilisateur.verrouille_jusqua - instant).total_seconds() / 60)
        raise NonAuthentifieError(
            f"Trop d'essais échoués : ce compte est verrouillé pour encore {minutes} minute(s).",
            "COMPTE_VERROUILLE",
        )

    # Le calcul d'empreinte est fait même si l'identifiant n'existe pas (temps de réponse identique).
    cle_valide = verifier_cle(utilisateur.cle_hash if utilisateur is not None else EMPREINTE_FACTICE, donnees.cle)
    acces_autorise = (
        utilisateur is not None and cle_valide and utilisateur.actif and utilisateur.role == donnees.role
    )

    if acces_autorise:
        with transaction_ecriture(db):
            utilisateur.essais_echoues = 0
            utilisateur.verrouille_jusqua = None
            utilisateur.derniere_connexion = instant
            journaliser(db, utilisateur, "CONNEXION_REUSSIE", cible=utilisateur.id)
        jeton, expire_le = creer_jeton(utilisateur)
        return ConnexionReponse(
            jeton=jeton, expire_le=expire_le, utilisateur=UtilisateurLecture.model_validate(utilisateur)
        )

    # Échec : l'essai est enregistré AVANT de renvoyer l'erreur (sinon le rollback l'effacerait).
    if utilisateur is not None:
        with transaction_ecriture(db):
            if not cle_valide:
                _compter_echec(utilisateur, instant)
                if utilisateur.verrouille_jusqua:
                    journaliser(db, None, "COMPTE_VERROUILLE", cible=utilisateur.id, detail=f"{DUREE_VERROUILLAGE_MIN} minutes")
            journaliser(db, None, "CONNEXION_ECHEC", cible=utilisateur.id)
    raise NonAuthentifieError(MESSAGE_IDENTIFIANTS, "IDENTIFIANTS_INVALIDES")


@router.get(
    "/moi",
    response_model=UtilisateurLecture,
    summary="Profil de l'utilisateur connecté",
    responses=REPONSES_ERREURS,
)
def profil_connecte(utilisateur: Utilisateur = Depends(utilisateur_courant)) -> UtilisateurLecture:
    return UtilisateurLecture.model_validate(utilisateur)


@router.post(
    "/changer-cle",
    response_model=ConnexionReponse,
    summary="Changer sa propre clé (les autres sessions du compte sont fermées)",
    responses=REPONSES_ERREURS,
)
def changer_cle(
    donnees: ChangementCle,
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(utilisateur_courant),
) -> ConnexionReponse:
    """L'employé change sa clé en prouvant qu'il connaît l'actuelle.

    Les essais de clé actuelle erronés comptent comme à la connexion (verrouillage temporaire). La session en cours
    reçoit un nouveau jeton ; tous les autres jetons du compte cessent de fonctionner, car ils sont liés à l'ancienne clé.
    """
    instant = maintenant()
    if utilisateur.verrouille_jusqua and utilisateur.verrouille_jusqua > instant:
        minutes = math.ceil((utilisateur.verrouille_jusqua - instant).total_seconds() / 60)
        raise RegleMetierError(
            f"Trop d'essais échoués : ce compte est verrouillé pour encore {minutes} minute(s).", "COMPTE_VERROUILLE"
        )
    if not verifier_cle(utilisateur.cle_hash, donnees.cle_actuelle):
        with transaction_ecriture(db):
            _compter_echec(utilisateur, instant)
            journaliser(db, utilisateur, "CLE_MODIFICATION_ECHEC", cible=utilisateur.id)
        raise RegleMetierError("La clé actuelle est incorrecte.", "CLE_ACTUELLE_INCORRECTE")

    nouvelle = normaliser_cle(donnees.nouvelle_cle)
    if nouvelle == normaliser_cle(donnees.cle_actuelle):
        raise RegleMetierError("La nouvelle clé doit être différente de l'ancienne.", "NOUVELLE_CLE_IDENTIQUE")
    if utilisateur.id.upper() in nouvelle:
        raise RegleMetierError("La nouvelle clé ne doit pas contenir votre identifiant.", "NOUVELLE_CLE_TROP_SIMPLE")

    empreinte = hacher_cle(donnees.nouvelle_cle)  # calcul coûteux : fait avant la transaction
    with transaction_ecriture(db):
        utilisateur.cle_hash = empreinte
        utilisateur.essais_echoues = 0
        utilisateur.verrouille_jusqua = None
        journaliser(db, utilisateur, "CLE_MODIFIEE", cible=utilisateur.id)
    jeton, expire_le = creer_jeton(utilisateur)
    return ConnexionReponse(jeton=jeton, expire_le=expire_le, utilisateur=UtilisateurLecture.model_validate(utilisateur))
