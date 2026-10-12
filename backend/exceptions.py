"""Exceptions métier et gestionnaires d'erreurs globaux de l'API MédiRDV.

Toutes les erreurs renvoyées au client suivent le même format JSON :

    {"detail": "Description explicite du problème", "code_erreur": "TYPE_ERREUR"}

Codes HTTP utilisés :
- 400 : violation d'une règle métier (ex. motif d'annulation manquant) ;
- 401 : authentification absente, invalide ou expirée ;
- 403 : rôle non autorisé pour l'action demandée ;
- 404 : patient, médecin, agent ou rendez-vous inexistant ;
- 409 : conflit d'agenda / double réservation (médecin ou patient) ;
- 422 : format de données invalide (téléphone, date, etc.) ;
- 500 : erreur interne, masquée au client et journalisée en détail côté serveur.
"""
import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------
# Exceptions métier
# --------------------------------------------------------------------------------------


class ErreurMetier(Exception):
    """Classe de base des erreurs prévues, traduites en réponse HTTP uniforme."""

    statut_http: int = 400
    code_defaut: str = "REGLE_METIER_VIOLEE"

    def __init__(self, detail: str, code_erreur: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code_erreur = code_erreur or self.code_defaut


class RegleMetierError(ErreurMetier):
    """Violation d'une règle métier (HTTP 400)."""

    statut_http = 400
    code_defaut = "REGLE_METIER_VIOLEE"


class RessourceIntrouvableError(ErreurMetier):
    """Patient, médecin, agent ou rendez-vous inexistant (HTTP 404)."""

    statut_http = 404
    code_defaut = "RESSOURCE_INTROUVABLE"


class ConflitError(ErreurMetier):
    """Conflit d'agenda ou double réservation (HTTP 409)."""

    statut_http = 409
    code_defaut = "CONFLIT"


class NonAuthentifieError(ErreurMetier):
    """Authentification absente, invalide ou expirée (HTTP 401)."""

    statut_http = 401
    code_defaut = "NON_AUTHENTIFIE"


class AccesRefuseError(ErreurMetier):
    """Utilisateur authentifié mais non autorisé à effectuer l'action (HTTP 403)."""

    statut_http = 403
    code_defaut = "ACCES_REFUSE"


class DonneesInvalidesError(ErreurMetier):
    """Donnée de format invalide détectée hors des schémas Pydantic (HTTP 422)."""

    statut_http = 422
    code_defaut = "DONNEES_INVALIDES"


# --------------------------------------------------------------------------------------
# Traduction des erreurs de validation Pydantic
# --------------------------------------------------------------------------------------

LIBELLES_CHAMPS = {
    "nom": "nom",
    "prenom": "prénom",
    "sexe": "sexe",
    "date_naissance": "date de naissance",
    "telephone": "téléphone",
    "quartier": "quartier",
    "patient_id": "identifiant du patient",
    "medecin_id": "identifiant du médecin",
    "agent_id": "identifiant de l'agent",
    "date_prise": "date de prise de rendez-vous",
    "date_rdv": "date du rendez-vous",
    "heure_rdv": "heure du rendez-vous",
    "canal": "canal",
    "statut": "statut",
    "statuts": "statut",
    "motif_annulation": "motif d'annulation",
    "limite": "limite",
    "decalage": "décalage",
    "q": "recherche",
    "date_debut": "date de début",
    "date_fin": "date de fin",
    "specialite": "spécialité",
    "role": "rôle",
    "identifiant": "identifiant",
    "cle": "clé",
    "nom_affichage": "nom affiché",
    "nom_medecin": "nom du médecin",
    "duree_consultation_min": "durée de consultation",
    "retirer_du_planning": "retrait du planning",
    "arrivee": "heure d'arrivée",
    "depart": "heure de départ",
    "actif": "état du compte",
}

EXEMPLES_IDENTIFIANTS = {
    "patient_id": "PAT00123",
    "medecin_id": "MED014",
    "agent_id": "AGT07",
}

MESSAGES_PAR_TYPE = {
    "missing": "ce champ est obligatoire",
    "string_too_short": "valeur trop courte",
    "string_too_long": "valeur trop longue",
    "string_type": "texte attendu",
    "date_parsing": "date invalide (format attendu : AAAA-MM-JJ)",
    "date_type": "date invalide (format attendu : AAAA-MM-JJ)",
    "date_from_datetime_parsing": "date invalide (format attendu : AAAA-MM-JJ)",
    "time_parsing": "heure invalide (format attendu : HH:MM)",
    "time_type": "heure invalide (format attendu : HH:MM)",
    "int_parsing": "nombre entier attendu",
    "int_type": "nombre entier attendu",
    "greater_than_equal": "valeur trop petite",
    "less_than_equal": "valeur trop grande",
    "bool_parsing": "valeur booléenne attendue",
    "extra_forbidden": "champ non reconnu",
    "json_invalid": "le corps de la requête n'est pas un JSON valide",
    "model_attributes_type": "le corps de la requête est invalide",
    "dict_type": "le corps de la requête est invalide",
    "model_type": "le corps de la requête est invalide",
}

# Erreurs levées par les schémas Pydantic qui correspondent à une violation de règle métier :
# elles sont renvoyées en HTTP 400 (et non 422) avec leur propre code d'erreur.
ERREURS_REGLE_METIER = {
    "motif_annulation_manquant": "MOTIF_ANNULATION_MANQUANT",
    "motif_sans_annulation": "MOTIF_SANS_ANNULATION",
}

PARTIES_IGNOREES = ("body", "query", "path", "header", "cookie")


def _majuscule(texte: str) -> str:
    return texte[:1].upper() + texte[1:]


def _libelle_champ(localisation) -> str | None:
    """Retourne le libellé français du champ visé par une erreur, ou None si l'erreur est globale."""
    parties = [p for p in localisation if p not in PARTIES_IGNOREES and not isinstance(p, int)]
    if not parties:
        return None
    nom = str(parties[-1])
    return LIBELLES_CHAMPS.get(nom, nom)


def _message_validation(erreur: dict) -> str:
    """Traduit une erreur de validation Pydantic en message français lisible."""
    type_erreur = erreur.get("type", "")
    localisation = erreur.get("loc", ())
    nom_champ = str(localisation[-1]) if localisation else ""

    if type_erreur == "value_error":
        message = str(erreur.get("msg", "")).removeprefix("Value error, ")
    elif type_erreur == "string_pattern_mismatch" and nom_champ in EXEMPLES_IDENTIFIANTS:
        message = f"identifiant au format incorrect (exemple : {EXEMPLES_IDENTIFIANTS[nom_champ]})"
    elif type_erreur == "enum":
        attendu = str(erreur.get("ctx", {}).get("expected", "")).replace(" or ", " ou ")
        message = f"valeur non autorisée (valeurs possibles : {attendu})" if attendu else "valeur non autorisée"
    elif type_erreur in MESSAGES_PAR_TYPE:
        message = MESSAGES_PAR_TYPE[type_erreur]
    elif type_erreur in ERREURS_REGLE_METIER:
        message = str(erreur.get("msg", ""))
    else:
        message = "valeur invalide"

    libelle = _libelle_champ(localisation)
    if libelle:
        return f"« {libelle} » : {message}"
    return _majuscule(message)


def reponse_erreur(statut_http: int, detail: str, code_erreur: str) -> JSONResponse:
    """Construit la réponse d'erreur au format JSON uniforme."""
    return JSONResponse(status_code=statut_http, content={"detail": detail, "code_erreur": code_erreur})


# --------------------------------------------------------------------------------------
# Enregistrement des gestionnaires globaux
# --------------------------------------------------------------------------------------

MESSAGES_HTTP = {
    401: ("NON_AUTHENTIFIE", "Authentification requise : connectez-vous."),
    403: ("ACCES_REFUSE", "Votre rôle ne permet pas cette action."),
    404: ("RESSOURCE_INTROUVABLE", "La ressource demandée est introuvable."),
    405: ("METHODE_NON_AUTORISEE", "Cette méthode HTTP n'est pas autorisée pour cette ressource."),
}


def enregistrer_gestionnaires(app: FastAPI) -> None:
    """Enregistre les gestionnaires d'erreurs globaux sur l'application."""

    @app.exception_handler(ErreurMetier)
    async def gerer_erreur_metier(requete: Request, exc: ErreurMetier) -> JSONResponse:
        logger.info("%s %s -> %d %s", requete.method, requete.url.path, exc.statut_http, exc.code_erreur)
        return reponse_erreur(exc.statut_http, exc.detail, exc.code_erreur)

    @app.exception_handler(RequestValidationError)
    async def gerer_validation(requete: Request, exc: RequestValidationError) -> JSONResponse:
        erreurs = exc.errors()
        # Une erreur de règle métier levée par un schéma (ex. RG-05) donne un HTTP 400.
        for erreur in erreurs:
            if erreur.get("type") in ERREURS_REGLE_METIER:
                code = ERREURS_REGLE_METIER[erreur["type"]]
                logger.info("%s %s -> 400 %s", requete.method, requete.url.path, code)
                return reponse_erreur(400, _majuscule(str(erreur.get("msg", ""))), code)
        messages = [_message_validation(erreur) for erreur in erreurs]
        logger.info("%s %s -> 422 DONNEES_INVALIDES", requete.method, requete.url.path)
        return reponse_erreur(422, "Données invalides. " + " ; ".join(messages), "DONNEES_INVALIDES")

    @app.exception_handler(StarletteHTTPException)
    async def gerer_erreur_http(requete: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, message = MESSAGES_HTTP.get(exc.status_code, ("ERREUR_HTTP", "La requête n'a pas pu être traitée."))
        return reponse_erreur(exc.status_code, message, code)

    @app.exception_handler(IntegrityError)
    async def gerer_integrite(requete: Request, exc: IntegrityError) -> JSONResponse:
        # Le message du pilote contient les noms de colonnes, jamais les valeurs (hide_parameters).
        origine = str(exc.orig).lower() if exc.orig is not None else ""
        logger.warning("Violation de contrainte sur %s %s : %s", requete.method, requete.url.path, origine)
        if "unique" in origine or "duplicate" in origine:
            return reponse_erreur(
                409,
                "L'opération entre en conflit avec une autre modification enregistrée au même instant. "
                "Actualisez les données puis recommencez.",
                "CONFLIT_CONCURRENT",
            )
        return reponse_erreur(
            400,
            "L'opération viole une contrainte d'intégrité des données (référence ou valeur non autorisée).",
            "CONTRAINTE_INTEGRITE",
        )

    @app.exception_handler(SQLAlchemyError)
    async def gerer_erreur_base(requete: Request, exc: SQLAlchemyError) -> JSONResponse:
        reference = uuid.uuid4().hex[:8]
        logger.error(
            "Erreur de base de données [%s] sur %s %s", reference, requete.method, requete.url.path, exc_info=exc
        )
        return reponse_erreur(
            500,
            "Une erreur est survenue lors de l'accès à la base de données. Réessayez dans quelques instants ; "
            f"si le problème persiste, communiquez la référence {reference} à l'administrateur.",
            "ERREUR_BASE_DONNEES",
        )

    @app.exception_handler(Exception)
    async def gerer_erreur_interne(requete: Request, exc: Exception) -> JSONResponse:
        reference = uuid.uuid4().hex[:8]
        # Trace complète côté serveur uniquement ; le client ne reçoit qu'une référence.
        logger.error("Erreur interne [%s] sur %s %s", reference, requete.method, requete.url.path, exc_info=exc)
        return reponse_erreur(
            500,
            "Une erreur interne est survenue. Réessayez ; si le problème persiste, "
            f"communiquez la référence {reference} à l'administrateur.",
            "ERREUR_INTERNE",
        )
