"""Client HTTP centralisé de l'interface MédiRDV.

Tous les appels à l'API passent par ce module. Chaque appel est encapsulé dans un bloc
``try/except requests.exceptions.RequestException`` et toute défaillance (serveur arrêté,
délai dépassé, erreur renvoyée par l'API) est convertie en une seule exception, ``ErreurApi``,
que l'interface affiche sous forme de bannière sobre.

Configuration par variables d'environnement :
- MEDIRDV_API_URL : adresse de l'API (défaut : http://127.0.0.1:8000) ;
- MEDIRDV_API_DELAI_S : délai d'attente maximal d'une requête en secondes (défaut : 10).
"""
import logging
import os
import threading
from datetime import date, datetime, time
from typing import Any

import requests

logger = logging.getLogger(__name__)

URL_API = os.getenv("MEDIRDV_API_URL", "http://127.0.0.1:8000").rstrip("/")
DELAI_ATTENTE_S = float(os.getenv("MEDIRDV_API_DELAI_S", "10"))


# Jeton de session de l'utilisateur dont le script est en cours d'exécution. Chaque session Streamlit
# s'exécute dans son propre fil ; l'interface (re)définit le jeton au début de chaque exécution.
_contexte = threading.local()


def definir_jeton(jeton: str | None) -> None:
    """Définit (ou efface) le jeton envoyé avec les requêtes de la session courante."""
    _contexte.jeton = jeton


def _en_tetes() -> dict[str, str]:
    jeton = getattr(_contexte, "jeton", None)
    return {"Authorization": f"Bearer {jeton}"} if jeton else {}


class ErreurApi(Exception):
    """Erreur renvoyée par l'API ou indisponibilité du serveur.

    Attributs :
    - message : texte en français destiné à l'utilisateur ;
    - code_erreur : code renvoyé par l'API (ex. CONFLIT_MEDECIN) ou code local ;
    - statut_http : code HTTP, ou None si le serveur n'a pas répondu ;
    - indisponible : True si le serveur est injoignable ou trop lent (repli à prévoir).
    """

    def __init__(
        self,
        message: str,
        code_erreur: str = "ERREUR_API",
        statut_http: int | None = None,
        indisponible: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code_erreur = code_erreur
        self.statut_http = statut_http
        self.indisponible = indisponible


def _nettoyer(donnees: dict[str, Any], garder_nulls: bool = False) -> dict[str, Any]:
    """Convertit dates et heures au format attendu par l'API ; retire les valeurs nulles sauf si on veut les envoyer."""
    resultat: dict[str, Any] = {}
    for cle, valeur in donnees.items():
        if valeur is None and not garder_nulls:
            continue
        if isinstance(valeur, date):
            valeur = valeur.isoformat()
        elif isinstance(valeur, time):
            valeur = valeur.strftime("%H:%M")
        resultat[cle] = valeur
    return resultat


def _appeler(
    methode: str,
    chemin: str,
    *,
    params: dict[str, Any] | None = None,
    corps: dict[str, Any] | None = None,
    garder_nulls: bool = False,
    brut: bool = False,
) -> Any:
    """Émet une requête vers l'API et retourne le JSON de la réponse, ou lève ErreurApi."""
    url = f"{URL_API}{chemin}"
    try:
        reponse = requests.request(
            methode,
            url,
            params=_nettoyer(params) if params else None,
            json=_nettoyer(corps, garder_nulls) if corps is not None else None,
            headers=_en_tetes(),
            timeout=DELAI_ATTENTE_S,
        )
    except requests.exceptions.Timeout as exc:
        logger.warning("Délai dépassé pour %s %s", methode, chemin)
        raise ErreurApi(
            "Le serveur met trop de temps à répondre.", "API_DELAI_DEPASSE", indisponible=True
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        logger.warning("Serveur injoignable pour %s %s", methode, chemin)
        raise ErreurApi(
            f"Le serveur MédiRDV ne répond pas ({URL_API}).", "API_INJOIGNABLE", indisponible=True
        ) from exc
    except requests.exceptions.RequestException as exc:
        logger.warning("Échec de la requête %s %s : %s", methode, chemin, type(exc).__name__)
        raise ErreurApi("La requête vers le serveur a échoué.", "API_REQUETE_ECHOUEE", indisponible=True) from exc

    if reponse.ok:
        if brut:
            return reponse.content  # fichier à télécharger (export CSV ou PDF)
        if reponse.status_code == 204 or not reponse.content:
            return None  # suppression réussie : pas de corps
        try:
            return reponse.json()
        except ValueError as exc:
            raise ErreurApi("Réponse inattendue du serveur.", "REPONSE_INVALIDE", reponse.status_code) from exc

    # Réponse d'erreur : l'API renvoie {"detail": ..., "code_erreur": ...}.
    try:
        corps_erreur = reponse.json()
        detail = str(corps_erreur.get("detail") or "")
        code = str(corps_erreur.get("code_erreur") or "ERREUR_HTTP")
    except (ValueError, AttributeError):
        detail, code = "", "ERREUR_HTTP"
    if not detail:
        detail = f"Le serveur a répondu par une erreur (code HTTP {reponse.status_code})."
    raise ErreurApi(detail, code, reponse.status_code, indisponible=reponse.status_code in (502, 503, 504))


# --------------------------------------------------------------------------------------
# Système et référentiels
# --------------------------------------------------------------------------------------


def verifier_sante() -> dict:
    return _appeler("GET", "/sante")


# --------------------------------------------------------------------------------------
# Authentification
# --------------------------------------------------------------------------------------


def connexion(role: str, identifiant: str, cle: str) -> dict:
    return _appeler("POST", "/auth/connexion", corps={"role": role, "identifiant": identifiant, "cle": cle})


def profil_connecte() -> dict:
    return _appeler("GET", "/auth/moi")


def changer_cle(cle_actuelle: str, nouvelle_cle: str) -> dict:
    """Change sa propre clé ; retourne un nouveau jeton (les autres sessions du compte sont fermées)."""
    return _appeler("POST", "/auth/changer-cle", corps={"cle_actuelle": cle_actuelle, "nouvelle_cle": nouvelle_cle})


def lister_agents() -> list[dict]:
    return _appeler("GET", "/referentiels/agents")


def lister_medecins(specialite: str | None = None) -> list[dict]:
    return _appeler("GET", "/referentiels/medecins", params={"specialite": specialite})


# --------------------------------------------------------------------------------------
# Patients
# --------------------------------------------------------------------------------------


def rechercher_patients(texte: str, limite: int = 20, decalage: int = 0) -> dict:
    parametres: dict = {"limite": limite, "decalage": decalage}
    if texte:
        parametres["q"] = texte
    return _appeler("GET", "/patients", params=parametres)


def obtenir_patient(patient_id: str) -> dict:
    return _appeler("GET", f"/patients/{patient_id}")


def creer_patient(
    nom: str,
    prenom: str,
    sexe: str,
    date_naissance: date | None,
    telephone: str,
    quartier: str | None = None,
    confirmer_doublon: bool = False,
) -> dict:
    return _appeler(
        "POST",
        "/patients",
        corps={
            "nom": nom,
            "prenom": prenom,
            "sexe": sexe,
            "date_naissance": date_naissance,
            "telephone": telephone,
            "quartier": quartier or None,
            "confirmer_doublon": confirmer_doublon,
        },
    )


def verifier_doublon(nom: str, prenom: str, date_naissance: date) -> list[dict]:
    """Fiches déjà enregistrées avec la même identité (liste vide si aucune)."""
    return _appeler(
        "POST", "/patients/verification-doublon", corps={"nom": nom, "prenom": prenom, "date_naissance": date_naissance}
    )


def modifier_patient(patient_id: str, champs: dict[str, Any], confirmer_doublon: bool = False) -> dict:
    """Modifie les champs indiqués ; une valeur None est envoyée telle quelle (ex. vider le quartier)."""
    return _appeler(
        "PATCH",
        f"/patients/{patient_id}",
        corps={**champs, "confirmer_doublon": confirmer_doublon},
        garder_nulls=True,
    )


def historique_patient(patient_id: str) -> dict:
    return _appeler("GET", f"/patients/{patient_id}/historique")


# --------------------------------------------------------------------------------------
# Rendez-vous
# --------------------------------------------------------------------------------------


def lister_rdv(
    *,
    medecin_id: str | None = None,
    patient_id: str | None = None,
    specialite: str | None = None,
    statuts: list[str] | None = None,
    date_debut: date | None = None,
    date_fin: date | None = None,
    limite: int = 500,
) -> dict:
    params: dict[str, Any] = {
        "medecin_id": medecin_id,
        "patient_id": patient_id,
        "specialite": specialite,
        "date_debut": date_debut,
        "date_fin": date_fin,
        "limite": limite,
    }
    if statuts:
        params["statuts"] = statuts  # requests répète le paramètre pour chaque valeur
    return _appeler("GET", "/rdv", params=params)


def creer_rdv(
    patient_id: str,
    medecin_id: str,
    date_rdv: date,
    heure_rdv: time,
    canal: str | None = None,
) -> dict:
    return _appeler(
        "POST",
        "/rdv",
        corps={
            "patient_id": patient_id,
            "medecin_id": medecin_id,
            "date_rdv": date_rdv,
            "heure_rdv": heure_rdv,
            "canal": canal,
        },
    )


def modifier_rdv(
    rdv_id: str,
    *,
    statut: str | None = None,
    motif_annulation: str | None = None,
    date_rdv: date | None = None,
    heure_rdv: time | None = None,
) -> dict:
    return _appeler(
        "PATCH",
        f"/rdv/{rdv_id}",
        corps={
            "statut": statut,
            "motif_annulation": motif_annulation,
            "date_rdv": date_rdv,
            "heure_rdv": heure_rdv,
        },
    )


# --------------------------------------------------------------------------------------
# Statistiques
# --------------------------------------------------------------------------------------


def indicateurs(
    date_debut: date | None = None,
    date_fin: date | None = None,
    specialite: str | None = None,
    medecin_id: str | None = None,
) -> dict:
    return _appeler(
        "GET",
        "/stats/kpi",
        params={"date_debut": date_debut, "date_fin": date_fin, "specialite": specialite, "medecin_id": medecin_id},
    )


def absence_par_specialite(
    date_debut: date | None = None, date_fin: date | None = None, medecin_id: str | None = None
) -> list[dict]:
    return _appeler(
        "GET",
        "/stats/absence/par-specialite",
        params={"date_debut": date_debut, "date_fin": date_fin, "medecin_id": medecin_id},
    )


def absence_mensuelle(
    date_debut: date | None = None,
    date_fin: date | None = None,
    specialite: str | None = None,
    medecin_id: str | None = None,
) -> list[dict]:
    return _appeler(
        "GET",
        "/stats/absence/mensuelle",
        params={"date_debut": date_debut, "date_fin": date_fin, "specialite": specialite, "medecin_id": medecin_id},
    )


# --------------------------------------------------------------------------------------
# Pointage (tous les employés)
# --------------------------------------------------------------------------------------


def pointage_aujourdhui() -> dict:
    return _appeler("GET", "/pointage/aujourdhui")


def pointer_arrivee() -> dict:
    return _appeler("POST", "/pointage/arrivee")


def pointer_depart() -> dict:
    return _appeler("POST", "/pointage/depart")


# --------------------------------------------------------------------------------------
# Administration (rôle Administrateur)
# --------------------------------------------------------------------------------------


def lister_utilisateurs(role: str | None = None, actif: bool | None = None, q: str | None = None) -> dict:
    return _appeler("GET", "/admin/utilisateurs", params={"role": role, "actif": actif, "q": q, "limite": 500})


def creer_utilisateur(
    role: str,
    nom_affichage: str | None = None,
    nom_medecin: str | None = None,
    specialite: str | None = None,
    duree_consultation_min: int | None = None,
) -> dict:
    # La durée vient de la spécialité : le paramètre est conservé pour compatibilité mais n'est plus envoyé.
    return _appeler(
        "POST",
        "/admin/utilisateurs",
        corps={
            "role": role,
            "nom_affichage": nom_affichage or None,
            "nom_medecin": nom_medecin or None,
            "specialite": specialite,
        },
    )


def regenerer_cle(identifiant: str) -> dict:
    return _appeler("POST", f"/admin/utilisateurs/{identifiant}/regenerer-cle")


def desactiver_utilisateur(identifiant: str, retirer_du_planning: bool = False) -> dict:
    return _appeler(
        "POST", f"/admin/utilisateurs/{identifiant}/desactiver", corps={"retirer_du_planning": retirer_du_planning}
    )


def reactiver_utilisateur(identifiant: str) -> dict:
    return _appeler("POST", f"/admin/utilisateurs/{identifiant}/reactiver")


def consulter_journal(
    utilisateur_id: str | None = None,
    action: str | None = None,
    date_debut: date | None = None,
    date_fin: date | None = None,
    limite: int = 200,
) -> dict:
    return _appeler(
        "GET",
        "/admin/journal",
        params={
            "utilisateur_id": utilisateur_id,
            "action": action,
            "date_debut": date_debut,
            "date_fin": date_fin,
            "limite": limite,
        },
    )


def lister_pointages(
    date_debut: date | None = None, date_fin: date | None = None, utilisateur_id: str | None = None
) -> dict:
    return _appeler(
        "GET",
        "/admin/pointages",
        params={"date_debut": date_debut, "date_fin": date_fin, "utilisateur_id": utilisateur_id, "limite": 500},
    )


def corriger_pointage(pointage_id: int, arrivee: time | None = None, depart: time | None = None) -> dict:
    return _appeler("PATCH", f"/admin/pointages/{pointage_id}", corps={"arrivee": arrivee, "depart": depart})


# --------------------------------------------------------------------------------------
# Qualité des données
# --------------------------------------------------------------------------------------


def lister_doublons() -> dict:
    return _appeler("GET", "/qualite/doublons")


def rapport_qualite(exemples: int = 20) -> dict:
    return _appeler("GET", "/qualite/rapport", params={"exemples": exemples})


# --------------------------------------------------------------------------------------
# Planning : spécialités, plages, jours fermés, créneaux, indisponibilités
# --------------------------------------------------------------------------------------


def lister_specialites(actives_seulement: bool = False) -> list[dict]:
    return _appeler("GET", "/referentiels/specialites", params={"actives_seulement": actives_seulement})


def creer_specialite(nom: str, duree_standard_min: int, couleur: str) -> dict:
    return _appeler(
        "POST",
        "/referentiels/specialites",
        corps={"nom": nom, "duree_standard_min": duree_standard_min, "couleur": couleur},
    )


def modifier_specialite(nom: str, champs: dict[str, Any]) -> dict:
    return _appeler("PATCH", f"/referentiels/specialites/{nom}", corps=champs)


def lire_plages(medecin_id: str) -> dict:
    return _appeler("GET", f"/referentiels/medecins/{medecin_id}/plages")


def remplacer_plages(medecin_id: str, plages: list[dict]) -> dict:
    """Remplace toutes les plages hebdomadaires du médecin (heures au format HH:MM)."""
    return _appeler("PUT", f"/referentiels/medecins/{medecin_id}/plages", corps={"plages": plages})


def lister_feries(annee: int | None = None) -> list[dict]:
    return _appeler("GET", "/referentiels/feries", params={"annee": annee})


def ajouter_ferie(jour: date, libelle: str, type_fermeture: str) -> dict:
    return _appeler("POST", "/referentiels/feries", corps={"jour": jour, "libelle": libelle, "type": type_fermeture})


def retirer_ferie(jour: date) -> None:
    _appeler("DELETE", f"/referentiels/feries/{jour.isoformat()}")


def lister_creneaux(medecin_id: str, date_debut: date, date_fin: date, etat: str | None = None) -> list[dict]:
    return _appeler(
        "GET",
        "/planning/creneaux",
        params={"medecin_id": medecin_id, "date_debut": date_debut, "date_fin": date_fin, "etat": etat},
    )


def premiers_creneaux(
    specialite: str | None = None,
    medecin_id: str | None = None,
    date_debut: date | None = None,
    date_fin: date | None = None,
    patient_id: str | None = None,
    limite: int = 5,
) -> list[dict]:
    return _appeler(
        "GET",
        "/planning/premiers-creneaux",
        params={
            "specialite": specialite,
            "medecin_id": medecin_id,
            "date_debut": date_debut,
            "date_fin": date_fin,
            "patient_id": patient_id,
            "limite": limite,
        },
    )


def lister_indisponibilites(medecin_id: str | None = None, actives_seulement: bool = True) -> list[dict]:
    return _appeler(
        "GET",
        "/planning/indisponibilites",
        params={"medecin_id": medecin_id, "actives_seulement": actives_seulement},
    )


def declarer_indisponibilite(medecin_id: str, debut: datetime, fin: datetime, motif: str) -> dict:
    return _appeler(
        "POST",
        "/planning/indisponibilites",
        corps={"medecin_id": medecin_id, "debut": debut.replace(microsecond=0), "fin": fin.replace(microsecond=0), "motif": motif},
    )


def lever_indisponibilite(identifiant: int) -> dict:
    return _appeler("POST", f"/planning/indisponibilites/{identifiant}/lever")


def rdv_impactes(identifiant: int) -> list[dict]:
    return _appeler("GET", f"/planning/indisponibilites/{identifiant}/rdv-impactes")


def taux_occupation(
    date_debut: date | None = None,
    date_fin: date | None = None,
    specialite: str | None = None,
    medecin_id: str | None = None,
) -> dict:
    return _appeler(
        "GET",
        "/stats/occupation",
        params={"date_debut": date_debut, "date_fin": date_fin, "specialite": specialite, "medecin_id": medecin_id},
    )


def historique_rdv(rdv_id: str) -> list[dict]:
    return _appeler("GET", f"/rdv/{rdv_id}/historique")


# --------------------------------------------------------------------------------------
# Analyse (bloc 3) : indicateurs du jour, carte de chaleur, délais, annulations, évolution
# --------------------------------------------------------------------------------------


def _filtres(date_debut, date_fin, specialite, medecin_id) -> dict[str, Any]:
    return {"date_debut": date_debut, "date_fin": date_fin, "specialite": specialite, "medecin_id": medecin_id}


def indicateurs_du_jour(jour: date | None = None, specialite: str | None = None, medecin_id: str | None = None) -> dict:
    return _appeler("GET", "/stats/jour", params={"jour": jour, "specialite": specialite, "medecin_id": medecin_id})


def carte_chaleur(date_debut=None, date_fin=None, specialite=None, medecin_id=None, inclure_annules=False) -> dict:
    return _appeler(
        "GET",
        "/stats/carte-chaleur",
        params={**_filtres(date_debut, date_fin, specialite, medecin_id), "inclure_annules": inclure_annules},
    )


def delai_attente(date_debut=None, date_fin=None, specialite=None, medecin_id=None) -> dict:
    return _appeler("GET", "/stats/delai-attente", params=_filtres(date_debut, date_fin, specialite, medecin_id))


def taux_annulation(date_debut=None, date_fin=None, specialite=None, medecin_id=None) -> dict:
    return _appeler("GET", "/stats/annulation", params=_filtres(date_debut, date_fin, specialite, medecin_id))


def evolution_mensuelle(date_debut=None, date_fin=None, specialite=None, medecin_id=None) -> list[dict]:
    return _appeler("GET", "/stats/evolution-mensuelle", params=_filtres(date_debut, date_fin, specialite, medecin_id))


def prevision(specialite: str | None = None, horizon: int = 4) -> dict:
    return _appeler("GET", "/stats/prevision", params={"specialite": specialite, "horizon": horizon})


def rdv_a_relancer(jours: int = 7, tous: bool = False, specialite: str | None = None) -> dict:
    return _appeler("GET", "/risque-absence/a-relancer", params={"jours": jours, "tous": tous, "specialite": specialite})


def evaluation_risque() -> dict:
    return _appeler("GET", "/risque-absence/evaluation")


def recalculer_risque() -> dict:
    return _appeler("POST", "/risque-absence/recalculer")


def exporter_indicateurs(format: str, date_debut=None, date_fin=None, specialite=None, medecin_id=None) -> bytes:
    """Fichier CSV ou PDF des indicateurs de la période (octets)."""
    return _appeler("GET", "/export/indicateurs", params={"format": format, **_filtres(date_debut, date_fin, specialite, medecin_id)}, brut=True)
