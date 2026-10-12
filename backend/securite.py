"""Sécurité de MédiRDV : clés personnelles, hachage Argon2, jetons JWT et contrôle d'accès par rôle.

Principes :
- chaque employé se connecte avec son identifiant (MED016, AGT01, OPS01, DIR01, ADM01) et une clé
  personnelle générée par le système (12 caractères, ex. K7M2-P9QX-H4TD) ;
- la clé n'est jamais stockée : seule son empreinte Argon2 l'est (ENF-04) ;
- après connexion, l'API délivre un jeton JWT signé ; chaque requête le présente en en-tête
  ``Authorization: Bearer <jeton>`` et le compte est relu en base à chaque appel, si bien qu'une
  désactivation de compte prend effet immédiatement ;
- le jeton est lié à la clé : il porte l'empreinte de la clé du moment. Dès que la clé change (régénérée par
  l'administrateur ou modifiée par l'employé), tous les jetons émis avant sont refusés ;
- le rôle vient du compte, jamais du client : les droits sont vérifiés côté serveur (ENF-05).

Réglages par variables d'environnement (valeurs par défaut entre parenthèses) :
- MEDIRDV_SECRET_JWT : secret de signature des jetons (à défaut, généré une fois dans ``.secret_jwt``) ;
- MEDIRDV_SESSION_HEURES : durée d'une session (8) ;
- MEDIRDV_MAX_ESSAIS : essais de clé ratés avant verrouillage (5) ;
- MEDIRDV_VERROUILLAGE_MIN : durée du verrouillage en minutes (5).
"""
import hashlib
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from backend.database import RACINE_PROJET, get_db
from backend.exceptions import AccesRefuseError, NonAuthentifieError
from backend.models import RoleUtilisateur, Utilisateur

logger = logging.getLogger(__name__)

# Alphabet sans I, O, 0 et 1 pour éviter les confusions à la lecture.
ALPHABET_CLE = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
LONGUEUR_CLE = 12  # au-dessus du minimum de 10 caractères exigé par ENF-04
LONGUEUR_MIN_CLE = 10  # ENF-04 : politique de 10 caractères minimum

MAX_ESSAIS = int(os.getenv("MEDIRDV_MAX_ESSAIS", "5"))
DUREE_VERROUILLAGE_MIN = int(os.getenv("MEDIRDV_VERROUILLAGE_MIN", "5"))
DUREE_SESSION_HEURES = float(os.getenv("MEDIRDV_SESSION_HEURES", "8"))
ALGORITHME_JWT = "HS256"

_hacheur = PasswordHasher()
_secret_en_memoire: str | None = None


# --------------------------------------------------------------------------------------
# Clés personnelles
# --------------------------------------------------------------------------------------


def generer_cle() -> str:
    """Génère une clé aléatoire lisible, au format XXXX-XXXX-XXXX."""
    brute = "".join(secrets.choice(ALPHABET_CLE) for _ in range(LONGUEUR_CLE))
    return "-".join(brute[i : i + 4] for i in range(0, LONGUEUR_CLE, 4))


def normaliser_cle(saisie: str) -> str:
    """Ignore la casse, les espaces et les tirets saisis par l'utilisateur."""
    return re.sub(r"[\s-]", "", str(saisie)).upper()


def hacher_cle(cle: str) -> str:
    """Retourne l'empreinte Argon2 de la clé (la clé en clair n'est jamais conservée)."""
    return _hacheur.hash(normaliser_cle(cle))


def verifier_cle(empreinte: str, cle_saisie: str) -> bool:
    """Vrai si la clé saisie correspond à l'empreinte enregistrée."""
    try:
        return _hacheur.verify(empreinte, normaliser_cle(cle_saisie))
    except (VerificationError, InvalidHashError):
        return False


def empreinte_session(cle_hash: str) -> str:
    """Identifiant court de la clé en vigueur : il change dès que la clé change, ce qui invalide les anciens jetons."""
    return hashlib.sha256(cle_hash.encode("utf-8")).hexdigest()[:16]


# Empreinte factice : permet de dépenser le même temps de calcul pour un identifiant inconnu
# (on ne révèle pas, par le temps de réponse, qu'un identifiant n'existe pas).
EMPREINTE_FACTICE = _hacheur.hash("FACTICE-SANS-COMPTE")


# --------------------------------------------------------------------------------------
# Jetons JWT
# --------------------------------------------------------------------------------------


def _secret_jwt() -> str:
    """Secret de signature : variable d'environnement, sinon fichier local ``.secret_jwt`` créé au premier usage."""
    global _secret_en_memoire
    if _secret_en_memoire:
        return _secret_en_memoire
    secret = os.getenv("MEDIRDV_SECRET_JWT", "").strip()
    if not secret:
        fichier = RACINE_PROJET / ".secret_jwt"
        if fichier.is_file():
            secret = fichier.read_text(encoding="utf-8").strip()
        if not secret:
            secret = secrets.token_urlsafe(48)
            fichier.write_text(secret, encoding="utf-8")
            try:
                fichier.chmod(0o600)
            except OSError:  # systèmes de fichiers sans droits Unix (Windows)
                pass
            logger.info("Secret de signature des jetons créé dans %s.", fichier.name)
    _secret_en_memoire = secret
    return secret


def creer_jeton(utilisateur: Utilisateur) -> tuple[str, datetime]:
    """Retourne (jeton, date d'expiration en heure de Dakar sans fuseau)."""
    emission = datetime.now(timezone.utc)
    expiration = emission + timedelta(hours=DUREE_SESSION_HEURES)
    charge = {
        "sub": utilisateur.id,
        "role": utilisateur.role.value,
        "ck": empreinte_session(utilisateur.cle_hash),
        "iat": emission,
        "exp": expiration,
    }
    jeton = jwt.encode(charge, _secret_jwt(), algorithm=ALGORITHME_JWT)
    return jeton, expiration.replace(tzinfo=None)


def decoder_jeton(jeton: str) -> dict:
    """Décode et vérifie un jeton ; lève NonAuthentifieError s'il est expiré ou invalide."""
    try:
        return jwt.decode(
            jeton, _secret_jwt(), algorithms=[ALGORITHME_JWT], options={"require": ["exp", "sub", "role", "ck"]}
        )
    except jwt.ExpiredSignatureError:
        raise NonAuthentifieError("Votre session a expiré : reconnectez-vous.", "SESSION_EXPIREE") from None
    except jwt.InvalidTokenError:
        raise NonAuthentifieError("Session invalide : reconnectez-vous.", "SESSION_INVALIDE") from None


# --------------------------------------------------------------------------------------
# Dépendances FastAPI : utilisateur connecté et contrôle des rôles
# --------------------------------------------------------------------------------------

schema_bearer = HTTPBearer(auto_error=False, description="Jeton obtenu par POST /auth/connexion")


def utilisateur_courant(
    identifiants: HTTPAuthorizationCredentials | None = Depends(schema_bearer),
    db: Session = Depends(get_db),
) -> Utilisateur:
    """Retourne le compte connecté, relu en base (compte désactivé ou rôle modifié : session refusée)."""
    if identifiants is None or not identifiants.credentials:
        raise NonAuthentifieError("Authentification requise : connectez-vous.", "NON_AUTHENTIFIE")
    charge = decoder_jeton(identifiants.credentials)
    utilisateur = db.get(Utilisateur, charge["sub"])
    if utilisateur is None or not utilisateur.actif or utilisateur.role.value != charge["role"]:
        raise NonAuthentifieError("Session invalide ou compte désactivé : reconnectez-vous.", "SESSION_INVALIDE")
    if charge["ck"] != empreinte_session(utilisateur.cle_hash):
        raise NonAuthentifieError(
            "Votre clé a changé : reconnectez-vous avec la clé en vigueur.", "SESSION_INVALIDE"
        )
    return utilisateur


def exiger_roles(*roles: RoleUtilisateur):
    """Fabrique une dépendance qui n'accepte que les rôles indiqués (HTTP 403 pour les autres)."""

    def verifier(utilisateur: Utilisateur = Depends(utilisateur_courant)) -> Utilisateur:
        if utilisateur.role not in roles:
            raise AccesRefuseError(
                f"Votre rôle ({utilisateur.role.value}) ne permet pas cette action.", "ACCES_REFUSE"
            )
        return utilisateur

    return verifier
