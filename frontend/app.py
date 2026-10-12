"""Interface Streamlit de MédiRDV : une page de connexion, puis un menu propre à chaque rôle.

Lancement (depuis la racine du projet) :

    streamlit run frontend/app.py

Variables d'environnement :
- MEDIRDV_API_URL : adresse de l'API (défaut : http://127.0.0.1:8000) ;
- NOM_CLINIQUE : nom affiché dans le menu latéral (défaut : BAOBAB).

Les droits sont décidés par l'API : le rôle vient du compte, jamais du navigateur, et chaque route
vérifie le jeton de session. L'interface se contente de n'afficher que les pages utiles à chaque rôle.

Résilience : tous les appels passent par ``api_client`` (try/except RequestException). Si le serveur est
indisponible, l'interface affiche une bannière, conserve les dernières listes de référence connues et
désactive les enregistrements tant que le service ne répond pas. Si la session expire, l'utilisateur est
ramené à la page de connexion.
"""
import base64
import csv
import io
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

import api_client  # noqa: E402
from api_client import ErreurApi  # noqa: E402

NOM_CLINIQUE = os.getenv("NOM_CLINIQUE", "BAOBAB")
DOSSIER_IMAGES = Path(__file__).resolve().parent / "assets"
ICONE_ONGLET = DOSSIER_IMAGES / "favicon.png"

st.set_page_config(
    page_title=NOM_CLINIQUE,
    page_icon=str(ICONE_ONGLET) if ICONE_ONGLET.is_file() else None,
    layout="wide",
)

DUREE_VALIDITE_LISTES_S = 60

ROLES = ["Agent d'accueil", "Médecin", "Responsable des opérations", "Direction", "Administrateur"]
MENUS = {
    "Agent d'accueil": [
        "Nouveau rendez-vous", "Rendez-vous d'un patient", "Rendez-vous à relancer", "Fiche patient", "Doublons de patients",
    ],
    "Médecin": ["Mon planning", "Mes indisponibilités"],
    "Responsable des opérations": [
        "Tableau de bord", "Rendez-vous à relancer", "Plannings des médecins", "Spécialités et horaires",
        "Indisponibilités", "Doublons de patients", "Qualité des données",
    ],
    "Direction": ["Synthèse mensuelle"],
    "Administrateur": [
        "Comptes", "Journal des actions", "Pointages", "Tableau de bord", "Rendez-vous à relancer", "Plannings des médecins",
        "Spécialités et horaires", "Indisponibilités", "Fiche patient", "Doublons de patients", "Qualité des données",
    ],
}
CANAUX = ["Téléphone", "Guichet", "Recommandation médecin"]
MOTIFS_ANNULATION = ["Patient", "Médecin", "Clinique", "Autre"]
STATUTS_OCCUPANT_UN_CRENEAU = ["Planifié", "Confirmé", "Honoré"]
STATUTS_PLANNING = ["Planifié", "Confirmé", "Honoré", "Absent"]
CODES_SESSION_PERDUE = {"SESSION_EXPIREE", "SESSION_INVALIDE", "NON_AUTHENTIFIE"}
LIBELLES_ACTIONS = {
    "RDV_CREE": "Rendez-vous créé",
    "RDV_STATUT": "Statut de rendez-vous modifié",
    "RDV_ANNULE": "Rendez-vous annulé",
    "RDV_DEPLACE": "Rendez-vous déplacé",
    "PATIENT_CREE": "Fiche patient créée",
    "PATIENT_MODIFIE": "Fiche patient modifiée",
    "CONNEXION_REUSSIE": "Connexion réussie",
    "CONNEXION_ECHEC": "Connexion échouée",
    "COMPTE_VERROUILLE": "Compte verrouillé",
    "COMPTE_CREE": "Compte créé",
    "CLE_REGENEREE": "Clé régénérée par l'administrateur",
    "CLE_MODIFIEE": "Clé modifiée par l'employé",
    "CLE_MODIFICATION_ECHEC": "Échec de changement de clé",
    "DONNEES_GENEREES": "Données de démonstration générées",
    "COMPTE_DESACTIVE": "Compte désactivé",
    "COMPTE_REACTIVE": "Compte réactivé",
    "POINTAGE_CORRIGE": "Pointage corrigé",
    "COMPTES_CREES_EN_MASSE": "Comptes créés en masse",
    "RDV_ABSENCE_AUTO": "Absence enregistrée automatiquement",
    "SPECIALITE_CREEE": "Spécialité créée",
    "SPECIALITE_MODIFIEE": "Spécialité modifiée",
    "PLAGES_MODIFIEES": "Horaires d'un médecin modifiés",
    "FERIE_AJOUTE": "Jour férié ou fermeture ajouté",
    "FERIE_RETIRE": "Jour férié ou fermeture retiré",
    "INDISPONIBILITE_DECLAREE": "Indisponibilité déclarée",
    "INDISPONIBILITE_LEVEE": "Indisponibilité levée",
    "EXPORT_INDICATEURS": "Export des indicateurs",
}
VUES_TABLEAU_DE_BORD = ["Absences", "Demande", "Annulations", "Prévision", "Risque d'absence"]
LIBELLES_METHODES = {
    "moyenne_mobile": "Moyenne mobile (référence)",
    "holt": "Lissage de Holt",
    "regression": "Régression calendaire",
}
JOURS_COURTS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
JOURS_LONGS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]


# --------------------------------------------------------------------------------------
# Utilitaires d'affichage
# --------------------------------------------------------------------------------------


def aujourdhui() -> date:
    """Date du jour à Dakar (UTC+0 toute l'année)."""
    return datetime.now(timezone.utc).date()


def format_date(valeur: str) -> str:
    return date.fromisoformat(valeur).strftime("%d/%m/%Y")


def format_heure(valeur: str) -> str:
    return valeur[:5]


def format_horodatage(valeur: str | None) -> str:
    return datetime.fromisoformat(valeur).strftime("%d/%m/%Y %H:%M") if valeur else ""


def heure_de_horodatage(valeur: str | None) -> str:
    return datetime.fromisoformat(valeur).strftime("%H:%M") if valeur else ""


def format_pourcentage(valeur: float | None) -> str:
    """Un taux absent signifie « non calculable » (aucun rendez-vous honoré ou absent), pas 0 %."""
    return "Non calculable" if valeur is None else f"{valeur:.1f} %".replace(".", ",")


def libelle_patient(patient: dict) -> str:
    telephone = patient.get("telephone") or "téléphone non renseigné"
    return f"{patient['nom']} {patient['prenom']} - {telephone} - {patient['id']}"


def libelle_medecin(medecin: dict) -> str:
    return f"{medecin['nom']} ({medecin['specialite']})"


def heure_de_fin(rdv: dict) -> str:
    debut = datetime.combine(date.fromisoformat(rdv["date_rdv"]), time.fromisoformat(rdv["heure_rdv"]))
    return (debut + timedelta(minutes=rdv["duree_min"])).strftime("%H:%M")


@st.cache_resource
def _logo_en_base64() -> str | None:
    chemin = DOSSIER_IMAGES / "logo_baobab.png"
    return base64.b64encode(chemin.read_bytes()).decode() if chemin.is_file() else None


def afficher_logo(largeur: int) -> None:
    """Logo de la clinique sur une pastille blanche : il reste lisible en thème clair comme en thème sombre."""
    logo = _logo_en_base64()
    if logo is None:
        return
    st.markdown(
        '<div style="background:#ffffff;border-radius:8px;padding:6px 10px;display:inline-block;max-width:100%;">'
        f'<img src="data:image/png;base64,{logo}" alt="Logo {NOM_CLINIQUE}" '
        f'style="width:{largeur}px;max-width:100%;display:block;"></div>',
        unsafe_allow_html=True,
    )


def afficher_erreur(erreur: ErreurApi) -> None:
    """Bannière sobre : avertissement pour une saisie à corriger, erreur pour un refus ou une panne."""
    if erreur.indisponible:
        st.error(f"Service momentanément indisponible. {erreur.message} Réessayez dans quelques instants.")
    elif erreur.statut_http in (400, 422):
        st.warning(erreur.message)
    else:
        st.error(erreur.message)
    st.caption(f"Code : {erreur.code_erreur}")


def fermer_session(message: str | None = None) -> None:
    """Efface tout l'état de la session (compte, jeton, saisies) et prépare un message pour la page de connexion."""
    st.session_state.clear()
    if message:
        st.session_state["_message_connexion"] = message


def appel(fonction, *args, **kwargs):
    """Exécute un appel API ; en cas d'échec, affiche la bannière adaptée et retourne None.

    Si la session a expiré (ou si le compte a été désactivé), l'utilisateur est ramené à la connexion.
    """
    try:
        return fonction(*args, **kwargs)
    except ErreurApi as erreur:
        if erreur.code_erreur in CODES_SESSION_PERDUE:
            fermer_session(erreur.message)
            st.rerun()
        afficher_erreur(erreur)
        return None


def tableau(lignes: list[dict]) -> None:
    if lignes:
        st.dataframe(pd.DataFrame(lignes), hide_index=True)
    else:
        st.info("Aucun élément à afficher.")


def afficher_message_en_attente() -> None:
    """Affiche (une seule fois) les messages préparés avant un rechargement de page."""
    succes = st.session_state.pop("_succes", None)
    if succes:
        st.success(succes)
    alerte = st.session_state.pop("_alerte", None)
    if alerte:
        st.warning(alerte)


# --------------------------------------------------------------------------------------
# Listes de référence avec repli
# --------------------------------------------------------------------------------------


def charger_referentiels(forcer: bool = False) -> tuple[dict | None, bool]:
    """Retourne (listes, hors_ligne).

    La liste des médecins est rechargée toutes les 60 secondes. Si l'API est indisponible, la dernière
    liste connue est conservée (hors_ligne = True) ; s'il n'en existe aucune, la valeur retournée est None.
    """
    memoire = st.session_state.get("referentiels")
    if memoire and not forcer:
        age = (datetime.now() - memoire["horodatage"]).total_seconds()
        if age < DUREE_VALIDITE_LISTES_S:
            return memoire, False
    try:
        listes = {"medecins": api_client.lister_medecins(), "horodatage": datetime.now()}
    except ErreurApi as erreur:
        if erreur.code_erreur in CODES_SESSION_PERDUE:
            fermer_session(erreur.message)
            st.rerun()
        if memoire:
            st.warning(
                f"Le service est indisponible : les listes affichées datent de {memoire['horodatage']:%H:%M}. "
                "Les enregistrements sont désactivés tant que le service ne répond pas."
            )
            st.caption(f"Détail : {erreur.message}")
            return memoire, True
        afficher_erreur(erreur)
        return None, True
    st.session_state["referentiels"] = listes
    return listes, False


# --------------------------------------------------------------------------------------
# Connexion et pointage
# --------------------------------------------------------------------------------------


def page_connexion() -> None:
    """Page unique de connexion : rôle, identifiant et clé personnelle."""
    colonne_gauche, colonne_centrale, _ = st.columns([1, 2, 1])
    with colonne_centrale:
        afficher_logo(420)
        st.subheader(NOM_CLINIQUE)
        st.caption("Gestion des rendez-vous")
        message = st.session_state.pop("_message_connexion", None)
        if message:
            st.info(message)
        try:
            api_client.verifier_sante()
        except ErreurApi as erreur:
            afficher_erreur(erreur)
        with st.form("formulaire_connexion"):
            role = st.selectbox("Rôle", ROLES, key="role_connexion")
            identifiant = st.text_input("Identifiant", placeholder="Exemple : MED016, AGT01, OPS01", max_chars=12)
            cle = st.text_input("Clé personnelle", type="password", placeholder="XXXX-XXXX-XXXX", max_chars=64)
            envoye = st.form_submit_button("Se connecter")
        if not envoye:
            return
        if not identifiant.strip() or not cle.strip():
            st.warning("Renseignez l'identifiant et la clé personnelle.")
            return
        try:
            reponse = api_client.connexion(role, identifiant.strip(), cle)
        except ErreurApi as erreur:
            afficher_erreur(erreur)
            return
        st.session_state["jeton"] = reponse["jeton"]
        st.session_state["utilisateur"] = reponse["utilisateur"]
        st.rerun()


def bloc_pointage(hors_ligne: bool) -> None:
    """Pointage de l'employé connecté : une heure d'arrivée et une heure de départ par jour."""
    st.markdown("**Pointage**")
    etat = appel(api_client.pointage_aujourdhui)
    if etat is None:
        return
    if etat["arrivee"] is None:
        st.caption("Arrivée non pointée aujourd'hui.")
        if st.button("Pointer mon arrivée", disabled=hors_ligne, key="pointer_arrivee"):
            if appel(api_client.pointer_arrivee):
                st.rerun()
    elif etat["depart"] is None:
        st.caption(f"Arrivée pointée à {heure_de_horodatage(etat['arrivee'])}.")
        if st.button("Pointer mon départ", disabled=hors_ligne, key="pointer_depart"):
            if appel(api_client.pointer_depart):
                st.rerun()
    else:
        st.caption(
            f"Arrivée {heure_de_horodatage(etat['arrivee'])}, départ {heure_de_horodatage(etat['depart'])}."
        )


def bloc_mon_compte(hors_ligne: bool) -> None:
    """Changement de sa propre clé : la session en cours continue, les autres sessions du compte sont fermées."""
    with st.expander("Mon compte"):
        st.caption("Au moins 10 caractères. Vos autres sessions seront fermées.")
        with st.form("formulaire_changement_cle", clear_on_submit=True):
            actuelle = st.text_input("Clé actuelle", type="password", key="cle_actuelle", max_chars=64)
            nouvelle = st.text_input("Nouvelle clé", type="password", key="nouvelle_cle", max_chars=64)
            confirmation = st.text_input("Confirmer la nouvelle clé", type="password", key="confirmation_cle", max_chars=64)
            envoye = st.form_submit_button("Changer ma clé", disabled=hors_ligne)
        if not envoye:
            return
        if not actuelle or not nouvelle:
            st.warning("Renseignez la clé actuelle et la nouvelle clé.")
        elif nouvelle != confirmation:
            st.warning("La confirmation ne correspond pas à la nouvelle clé.")
        else:
            reponse = appel(api_client.changer_cle, actuelle, nouvelle)
            if reponse:
                st.session_state["jeton"] = reponse["jeton"]
                api_client.definir_jeton(reponse["jeton"])
                st.success("Clé modifiée. Utilisez la nouvelle clé à votre prochaine connexion.")


# --------------------------------------------------------------------------------------
# Composants communs aux pages
# --------------------------------------------------------------------------------------


def selection_patient(cle: str) -> str | None:
    """Recherche un patient puis retourne l'identifiant choisi (ou None)."""
    texte = st.text_input(
        "Rechercher un patient (nom, prénom, téléphone ou identifiant)", key=f"recherche_{cle}", max_chars=60
    ).strip()
    if len(texte) < 2:
        st.caption("Saisissez au moins 2 caractères.")
        return None
    resultat = appel(api_client.rechercher_patients, texte, 20)
    if resultat is None:
        return None
    if not resultat["elements"]:
        st.info("Aucun patient ne correspond à cette recherche.")
        return None
    if resultat["total"] > len(resultat["elements"]):
        st.caption(f"{resultat['total']} résultats : affinez la recherche pour voir les autres.")
    libelles = {p["id"]: libelle_patient(p) for p in resultat["elements"]}
    return st.selectbox("Patient", options=list(libelles), format_func=libelles.get, key=f"choix_{cle}")


def tableau_patients(cle: str, taille_page: int = 15, action: str = "ouvrir la fiche") -> str | None:
    """Tableau paginé de tous les patients, filtrable ; retourne l'identifiant de la ligne sélectionnée (ou None)."""
    texte = st.text_input(
        "Filtrer (nom, prénom, téléphone ou identifiant)", key=f"recherche_{cle}", max_chars=60,
        help="Laissez vide pour voir tous les patients.",
    ).strip()  # fmt: skip
    if texte and len(texte) < 2:
        st.caption("Saisissez au moins 2 caractères pour filtrer.")
        texte = ""
    if st.session_state.get(f"_filtre_{cle}") != texte:  # nouveau filtre : retour à la première page
        st.session_state[f"_filtre_{cle}"] = texte
        st.session_state[f"page_{cle}"] = 1
    premiere = appel(api_client.rechercher_patients, texte, taille_page, 0)
    if premiere is None:
        return None
    total = premiere["total"]
    if total == 0:
        st.info("Aucun patient ne correspond à cette recherche.")
        return None
    nb_pages = max(1, -(-total // taille_page))
    colonne_page, colonne_total = st.columns([1, 3])
    page = colonne_page.number_input("Page", min_value=1, max_value=nb_pages, step=1, key=f"page_{cle}")
    colonne_total.caption(f"{total} patient(s) - page {page} sur {nb_pages}. Cliquez sur une ligne pour {action}.")
    resultat = premiere if page == 1 else appel(api_client.rechercher_patients, texte, taille_page, (page - 1) * taille_page)
    if resultat is None:
        return None
    lignes = resultat["elements"]
    table = pd.DataFrame(
        [
            {
                "Identifiant": p["id"], "Nom": p["nom"], "Prénom": p["prenom"], "Sexe": p["sexe"],
                "Naissance": p["date_naissance"] or "", "Téléphone": p["telephone"] or "", "Quartier": p["quartier"] or "",
            }
            for p in lignes
        ]
    )  # fmt: skip
    selection = st.dataframe(
        table, hide_index=True, use_container_width=True, on_select="rerun", selection_mode="single-row",
        key=f"tableau_{cle}_{page}_{texte}",
    )  # fmt: skip
    if selection.selection.rows:
        return lignes[selection.selection.rows[0]]["id"]
    if total == 1:  # un seul résultat (ex. arrivée depuis « Ouvrir la fiche ») : ouverture directe
        return lignes[0]["id"]
    return None


def afficher_alerte_recidiviste(alerte: dict) -> None:
    """RG-08 : avertissement contextuel pour l'agent (non bloquant)."""
    if alerte["actif"]:
        st.warning(alerte["message"])


def libelle_creneau(creneau: dict, avec_medecin: bool = True) -> str:
    jour = date.fromisoformat(creneau["date_rdv"])
    texte = f"{JOURS_COURTS[jour.weekday()]} {jour:%d/%m/%Y} à {format_heure(creneau['heure_rdv'])}"
    return f"{texte} - {creneau['medecin_nom']}" if avec_medecin else texte


def _attribuer(candidats: list[dict]) -> dict:
    """Parmi les médecins libres au même créneau, en répartit la charge de façon stable (sans favoriser le premier)."""
    candidats = sorted(candidats, key=lambda c: c["medecin_id"])
    graine = candidats[0]["date_rdv"] + candidats[0]["heure_rdv"]
    return candidats[sum(map(ord, graine)) % len(candidats)]


def choix_creneau(
    cle: str,
    specialite: str,
    medecin_id: str | None,
    patient_id: str | None,
    medecins_ids: list[str] | None = None,
) -> tuple[str, date, time] | None:
    """EF-10 et EF-11 : l'utilisateur ne tape jamais une heure, il choisit un créneau libre.

    Si ``medecin_id`` est fourni (report d'un rendez-vous), seuls ses créneaux sont proposés. Sinon, les créneaux sont
    regroupés par spécialité : un créneau est proposé une seule fois, et un médecin libre à cette heure est attribué.
    Retourne (médecin, date, heure) ou None tant qu'aucun créneau n'est choisi.
    """
    mode = st.radio(
        "Recherche du créneau",
        ["Premiers créneaux libres", "Choisir un jour"],
        horizontal=True,
        key=f"mode_creneau_{cle}",
    )
    if mode == "Premiers créneaux libres":
        brut = appel(
            api_client.premiers_creneaux,
            specialite=specialite,
            medecin_id=medecin_id,
            patient_id=patient_id,
            limite=50 if medecin_id is None else 8,
        )
        if brut is None:
            return None
        par_heure: dict[tuple[str, str], list[dict]] = {}
        for c in brut:
            par_heure.setdefault((c["date_rdv"], c["heure_rdv"]), []).append(c)
        creneaux = [_attribuer(v) for v in par_heure.values()][:8]
        if not creneaux:
            st.info("Aucun créneau libre dans les 90 prochains jours pour cette sélection.")
            return None
        choix = st.radio(
            "Créneaux disponibles",
            list(range(len(creneaux))),
            format_func=lambda i: libelle_creneau(creneaux[i], avec_medecin=False),
            key=f"premiers_{cle}_{medecin_id}_{patient_id}_{specialite}",
        )
        retenu = creneaux[choix]
        return retenu["medecin_id"], date.fromisoformat(retenu["date_rdv"]), time.fromisoformat(retenu["heure_rdv"])

    ids = [medecin_id] if medecin_id else list(medecins_ids or [])
    if not ids:
        st.info("Aucun médecin n'est disponible pour cette spécialité.")
        return None
    jour = st.date_input(
        "Jour", value=aujourdhui(), min_value=aujourdhui(), format="DD/MM/YYYY", key=f"jour_creneau_{cle}"
    )
    par_heure_jour: dict[str, list[dict]] = {}
    total_creneaux = 0
    for identifiant in ids:
        creneaux = appel(api_client.lister_creneaux, identifiant, jour, jour)
        if creneaux is None:
            return None
        total_creneaux += len(creneaux)
        for c in creneaux:
            if c["etat"] == "libre":
                par_heure_jour.setdefault(c["heure_rdv"], []).append(c)
    if not total_creneaux:
        st.info(f"Aucune consultation le {jour:%d/%m/%Y} (hors horaires, jour férié ou fermeture).")
        return None
    if not par_heure_jour:
        st.info("Aucun créneau libre ce jour-là : choisissez un autre jour.")
        return None
    st.caption(f"{len(par_heure_jour)} horaire(s) disponible(s) le {jour:%d/%m/%Y}.")
    heures = sorted(par_heure_jour)
    heure = st.selectbox(
        "Créneau libre",
        heures,
        format_func=lambda h: f"{format_heure(h)} - {format_heure(par_heure_jour[h][0]['heure_fin'])}",
        key=f"heure_creneau_{cle}_{jour}_{specialite}",
    )
    retenu = _attribuer(par_heure_jour[heure])
    return retenu["medecin_id"], jour, time.fromisoformat(heure)


# --------------------------------------------------------------------------------------
# Rôle Agent d'accueil
# --------------------------------------------------------------------------------------


def _ligne_doublon(patient: dict) -> dict:
    return {
        "Identifiant": patient["id"],
        "Nom": patient["nom"],
        "Prénom": patient["prenom"],
        "Naissance": format_date(patient["date_naissance"]) if patient["date_naissance"] else "",
        "Téléphone": patient["telephone"] or "",
        "Quartier": patient["quartier"] or "",
    }


def _creer_fiche(donnees: dict, confirmer: bool) -> None:
    """Crée la fiche ; en cas de succès, la recherche est pré-remplie avec la nouvelle fiche."""
    patient = appel(api_client.creer_patient, **donnees, confirmer_doublon=confirmer)
    if patient:
        st.session_state["_prefill_recherche_nouveau"] = patient["id"]
        st.session_state["_succes"] = f"Fiche patient {patient['id']} créée : {patient['nom']} {patient['prenom']}."
        st.rerun()


def formulaire_creation_patient() -> None:
    """Création d'une fiche patient. L'identité est comparée aux fiches existantes AVANT la création (EF-04)."""
    en_attente = st.session_state.get("_creation_en_attente")
    if en_attente:
        st.warning(
            "Une fiche de même identité (nom, prénom et date de naissance) existe déjà. "
            "Sélectionnez-la dans la recherche, ou confirmez si c'est une autre personne."
        )
        tableau([_ligne_doublon(p) for p in en_attente["doublons"]])
        colonne_confirmer, colonne_annuler, _ = st.columns([4, 2, 1])
        if colonne_confirmer.button("Créer quand même (autre personne)", key="confirmer_creation_doublon"):
            st.session_state.pop("_creation_en_attente")
            _creer_fiche(en_attente["donnees"], confirmer=True)
        if colonne_annuler.button("Annuler", key="annuler_creation_doublon"):
            st.session_state.pop("_creation_en_attente")
            st.rerun()
        return

    with st.form("formulaire_patient"):
        colonne_nom, colonne_prenom = st.columns(2)
        nom = colonne_nom.text_input("Nom", max_chars=80)
        prenom = colonne_prenom.text_input("Prénom", max_chars=80)
        sexe = st.radio("Sexe", ["Féminin", "Masculin"], horizontal=True)
        naissance = st.date_input(
            "Date de naissance",
            value=None,
            min_value=date(1900, 1, 1),
            max_value=aujourdhui(),
            format="DD/MM/YYYY",
        )
        telephone = st.text_input(
            "Téléphone",
            max_chars=20,
            help="9 chiffres sans espace, commençant par 77, 78, 76, 70, 75 ou 33.",
        )
        quartier = st.text_input("Quartier (facultatif)", max_chars=60)
        envoye = st.form_submit_button("Créer la fiche patient")
    if not envoye:
        return
    if naissance is None:
        st.warning("Renseignez la date de naissance.")
        return
    donnees = {
        "nom": nom,
        "prenom": prenom,
        "sexe": "F" if sexe == "Féminin" else "M",
        "date_naissance": naissance,
        "telephone": telephone,
        "quartier": quartier or None,
    }
    doublons = appel(api_client.verifier_doublon, nom, prenom, naissance)
    if doublons is None:
        return
    if doublons:
        st.session_state["_creation_en_attente"] = {"donnees": donnees, "doublons": doublons}
        st.rerun()
    _creer_fiche(donnees, confirmer=False)


def page_nouveau_rdv(listes: dict, hors_ligne: bool) -> None:
    st.header("Nouveau rendez-vous")
    # Le champ de recherche est pré-rempli avec la fiche qui vient d'être créée (avant son affichage).
    if "_prefill_recherche_nouveau" in st.session_state:
        st.session_state["recherche_nouveau"] = st.session_state.pop("_prefill_recherche_nouveau")
    afficher_message_en_attente()

    st.subheader("1. Patient")
    patient_id = selection_patient("nouveau")
    with st.expander("Créer une fiche patient", expanded="_creation_en_attente" in st.session_state):
        formulaire_creation_patient()
    patient = None
    if patient_id:
        patient = appel(api_client.obtenir_patient, patient_id)
        if patient:
            afficher_alerte_recidiviste(patient["alerte_recidiviste"])

    st.subheader("2. Rendez-vous")
    medecins = listes["medecins"]
    specialite = st.selectbox("Spécialité", sorted({m["specialite"] for m in medecins}), key="specialite_nouveau")
    medecins_de_la_specialite = {m["id"]: m for m in medecins if m["specialite"] == specialite}
    choix = (
        choix_creneau("nouveau", specialite, None, patient_id, list(medecins_de_la_specialite)) if specialite else None
    )
    canal = st.selectbox("Canal de prise de rendez-vous", ["Non précisé"] + CANAUX, key="canal_nouveau")
    if choix:
        medecin_id, jour, heure = choix
        st.caption(
            f"Créneau retenu : {JOURS_COURTS[jour.weekday()]} {jour:%d/%m/%Y} à {heure:%H:%M} - "
            f"{specialite}, avec {medecins_de_la_specialite.get(medecin_id, {}).get('nom', medecin_id)}."
        )

    if patient is None:
        st.caption("Sélectionnez un patient pour activer l'enregistrement.")
    if st.button("Enregistrer le rendez-vous", disabled=(patient is None or hors_ligne or not choix), key="enregistrer"):
        reponse = appel(
            api_client.creer_rdv,
            patient_id,
            medecin_id,
            jour,
            heure,
            None if canal == "Non précisé" else canal,
        )
        if reponse:
            rdv = reponse["rdv"]
            st.session_state["_resultat_rdv"] = {
                "message": (
                    f"Rendez-vous {rdv['id']} enregistré : {rdv['medecin_nom']}, le {format_date(rdv['date_rdv'])} "
                    f"à {format_heure(rdv['heure_rdv'])} ({rdv['duree_min']} minutes)."
                ),
                "avertissements": [a["message"] for a in reponse["avertissements"]],
            }
            st.rerun()  # recharge la page pour que la liste des créneaux libres soit à jour
    resultat = st.session_state.pop("_resultat_rdv", None)
    if resultat:
        st.success(resultat["message"])
        for message in resultat["avertissements"]:
            st.warning(message)  # RG-08 : alerte non bloquante


def page_rendez_vous_patient(hors_ligne: bool) -> None:
    st.header("Rendez-vous d'un patient")
    afficher_message_en_attente()
    patient_id = tableau_patients("gestion", action="voir ses rendez-vous")
    if not patient_id:
        return
    historique = appel(api_client.historique_patient, patient_id)
    if historique is None:
        return

    afficher_alerte_recidiviste(historique["alerte_recidiviste"])
    colonnes = st.columns(4)
    colonnes[0].metric("Rendez-vous", historique["total_rdv"])
    colonnes[1].metric("Honorés", historique["honores"])
    colonnes[2].metric("Absences", historique["absents"])
    colonnes[3].metric("Taux d'absence", format_pourcentage(historique["taux_absence_pct"]))
    st.caption(
        "Taux d'absence = absences / (honorés + absences). Les rendez-vous planifiés, confirmés et annulés "
        "sont exclus du calcul."
    )
    rendez_vous = historique["rendez_vous"]
    tableau(
        [
            {
                "Identifiant": r["id"],
                "Date": format_date(r["date_rdv"]),
                "Heure": format_heure(r["heure_rdv"]),
                "Médecin": r["medecin_nom"],
                "Spécialité": r["specialite"],
                "Statut": r["statut"],
                "Motif d'annulation": r["motif_annulation"] or "",
            }
            for r in rendez_vous
        ]
    )

    if rendez_vous:
        with st.expander("Historique des modifications d'un rendez-vous"):
            rdv_histo = st.selectbox(
                "Rendez-vous",
                [r["id"] for r in rendez_vous],
                format_func=lambda i: next(
                    f"{i} - {format_date(r['date_rdv'])} {format_heure(r['heure_rdv'])} - {r['medecin_nom']}"
                    for r in rendez_vous
                    if r["id"] == i
                ),
                key="rdv_historique",
            )
            lignes = appel(api_client.historique_rdv, rdv_histo)
            if lignes is not None:
                tableau(
                    [
                        {
                            "Date": format_horodatage(h["horodatage"]),
                            "Par": h["utilisateur_id"] or "Système",
                            "Action": LIBELLES_ACTIONS.get(h["action"], h["action"]),
                            "Champ": h["champ"] or "",
                            "Avant": h["avant"] or "",
                            "Après": h["apres"] or "",
                        }
                        for h in lignes
                    ]
                )

    modifiables = {r["id"]: r for r in rendez_vous if r["statut"] in ("Planifié", "Confirmé")}
    st.subheader("Modifier un rendez-vous")
    if not modifiables:
        st.caption("Aucun rendez-vous planifié ou confirmé à modifier.")
        return
    rdv_id = st.selectbox(
        "Rendez-vous",
        list(modifiables),
        format_func=lambda i: (
            f"{i} - {format_date(modifiables[i]['date_rdv'])} {format_heure(modifiables[i]['heure_rdv'])} - "
            f"{modifiables[i]['medecin_nom']} - {modifiables[i]['statut']}"
        ),
        key="rdv_a_modifier",
    )
    action = st.radio("Action", ["Confirmer", "Reporter", "Annuler"], horizontal=True, key="action_rdv")
    nouvelle_date = nouvelle_heure = motif = None
    if action == "Reporter":
        courant = modifiables[rdv_id]
        choix = choix_creneau(f"report_{rdv_id}", courant["specialite"], courant["medecin_id"], patient_id)
        if choix:
            _, nouvelle_date, nouvelle_heure = choix
    elif action == "Annuler":
        motif = st.selectbox("Motif d'annulation", MOTIFS_ANNULATION, key="motif_annulation")

    if st.button(
        "Appliquer",
        disabled=hors_ligne or (action == "Reporter" and nouvelle_date is None),
        key="appliquer_action",
    ):
        if action == "Confirmer":
            resultat = appel(api_client.modifier_rdv, rdv_id, statut="Confirmé")
            message = f"Rendez-vous {rdv_id} confirmé."
        elif action == "Reporter":
            resultat = appel(api_client.modifier_rdv, rdv_id, date_rdv=nouvelle_date, heure_rdv=nouvelle_heure)
            message = f"Rendez-vous {rdv_id} reporté au {nouvelle_date:%d/%m/%Y} à {nouvelle_heure:%H:%M}."
        else:
            resultat = appel(api_client.modifier_rdv, rdv_id, statut="Annulé", motif_annulation=motif)
            message = f"Rendez-vous {rdv_id} annulé (motif : {motif})."
        if resultat:
            st.session_state["_succes"] = message
            st.rerun()


def page_fiche_patient(hors_ligne: bool) -> None:
    """Modification d'une fiche patient (EF-03) : seuls les champs changés sont envoyés ; chaque modification est tracée."""
    st.header("Fiche patient")
    afficher_message_en_attente()
    patient_id = tableau_patients("fiche")
    if not patient_id:
        return
    st.divider()
    fiche = appel(api_client.obtenir_patient, patient_id)
    if fiche is None:
        return
    afficher_alerte_recidiviste(fiche["alerte_recidiviste"])

    en_attente = st.session_state.get("_modif_doublon")
    if en_attente and en_attente["patient_id"] != patient_id:
        st.session_state.pop("_modif_doublon")
        en_attente = None
    if en_attente:
        st.warning(en_attente["message"])
        colonne_confirmer, colonne_annuler, _ = st.columns([4, 2, 1])
        if colonne_confirmer.button("Enregistrer quand même (autre personne)", disabled=hors_ligne, key="confirmer_modif_doublon"):
            st.session_state.pop("_modif_doublon")
            if appel(api_client.modifier_patient, patient_id, en_attente["champs"], True):
                st.session_state["_succes"] = f"Fiche {patient_id} modifiée (doublon confirmé)."
                st.rerun()
        if colonne_annuler.button("Annuler", key="annuler_modif_doublon"):
            st.session_state.pop("_modif_doublon")
            st.rerun()
        return

    st.caption(f"Identifiant : {fiche['id']} (non modifiable)")
    with st.form(f"formulaire_fiche_{patient_id}"):
        colonne_nom, colonne_prenom = st.columns(2)
        nom = colonne_nom.text_input("Nom", value=fiche["nom"], max_chars=80, key=f"fiche_nom_{patient_id}")
        prenom = colonne_prenom.text_input("Prénom", value=fiche["prenom"], max_chars=80, key=f"fiche_prenom_{patient_id}")
        sexe = st.radio(
            "Sexe", ["Féminin", "Masculin"], index=0 if fiche["sexe"] == "F" else 1, horizontal=True,
            key=f"fiche_sexe_{patient_id}",
        )  # fmt: skip
        naissance = st.date_input(
            "Date de naissance",
            value=date.fromisoformat(fiche["date_naissance"]) if fiche["date_naissance"] else None,
            min_value=date(1900, 1, 1),
            max_value=aujourdhui(),
            format="DD/MM/YYYY",
            key=f"fiche_naissance_{patient_id}",
        )
        telephone = st.text_input(
            "Téléphone",
            value=fiche["telephone"] or "",
            max_chars=20,
            help="9 chiffres sans espace, commençant par 77, 78, 76, 70, 75 ou 33.",
            key=f"fiche_telephone_{patient_id}",
        )
        quartier = st.text_input("Quartier (facultatif)", value=fiche["quartier"] or "", max_chars=60, key=f"fiche_quartier_{patient_id}")
        envoye = st.form_submit_button("Enregistrer les modifications", disabled=hors_ligne)
    if not envoye:
        return

    champs: dict = {}
    if nom.strip() != fiche["nom"]:
        champs["nom"] = nom.strip()
    if prenom.strip() != fiche["prenom"]:
        champs["prenom"] = prenom.strip()
    if ("F" if sexe == "Féminin" else "M") != fiche["sexe"]:
        champs["sexe"] = "F" if sexe == "Féminin" else "M"
    if naissance is None and fiche["date_naissance"]:
        st.warning("La date de naissance ne peut pas être vidée.")
        return
    if naissance is not None and naissance.isoformat() != fiche["date_naissance"]:
        champs["date_naissance"] = naissance
    if telephone.strip() != (fiche["telephone"] or ""):
        if not telephone.strip():
            st.warning("Le téléphone ne peut pas être vidé.")
            return
        champs["telephone"] = telephone.strip()
    if (quartier.strip() or None) != fiche["quartier"]:
        champs["quartier"] = quartier.strip() or None
    if not champs:
        st.info("Aucune modification à enregistrer.")
        return
    try:
        api_client.modifier_patient(patient_id, champs)
    except ErreurApi as erreur:
        if erreur.code_erreur == "PATIENT_DOUBLON":
            st.session_state["_modif_doublon"] = {"patient_id": patient_id, "champs": champs, "message": erreur.message}
            st.rerun()
        if erreur.code_erreur in CODES_SESSION_PERDUE:
            fermer_session(erreur.message)
            st.rerun()
        afficher_erreur(erreur)
        return
    libelles = {"nom": "nom", "prenom": "prénom", "sexe": "sexe", "date_naissance": "date de naissance", "telephone": "téléphone", "quartier": "quartier"}
    st.session_state["_succes"] = f"Fiche {patient_id} modifiée : {', '.join(libelles[c] for c in champs)}. La modification est tracée dans le journal."
    st.rerun()


def _ouvrir_fiche(role: str, patient_id: str) -> None:
    """Rappel du bouton « Ouvrir la fiche » : bascule vers la page Fiche patient avec la recherche pré-remplie."""
    st.session_state[f"menu_{role}"] = "Fiche patient"
    st.session_state["recherche_fiche"] = patient_id


def page_doublons(role: str, peut_corriger: bool) -> None:
    st.header("Doublons de patients")
    donnees = appel(api_client.lister_doublons)
    if donnees is None:
        return
    st.caption(
        "Deux fiches sont des doublons potentiels lorsqu'elles ont la même date de naissance et les mêmes nom et prénom "
        "(accents, majuscules, tirets et ordre du nom et du prénom ignorés)."
    )
    if not donnees["groupes"]:
        st.success("Aucun doublon détecté.")
        return
    st.metric("Groupes de doublons", donnees["nb_groupes"])
    lignes = [
        {"Groupe": g["numero"], **_ligne_doublon(p), "Rendez-vous": p["nb_rdv"]}
        for g in donnees["groupes"]
        for p in g["patients"]
    ]
    if len(lignes) > 300:
        st.caption(f"{donnees['nb_patients']} fiches concernées : les 300 premières sont affichées.")
    tableau(lignes[:300])
    st.caption("Aucune fiche n'est supprimée : on corrige la fiche erronée (nom, prénom, date de naissance) depuis l'écran Fiche patient.")
    if peut_corriger:
        st.subheader("Corriger une fiche")
        indexees = {ligne["Identifiant"]: ligne for ligne in lignes}
        identifiant = st.selectbox(
            "Fiche",
            list(indexees),
            format_func=lambda i: f"Groupe {indexees[i]['Groupe']} - {i} - {indexees[i]['Nom']} {indexees[i]['Prénom']}",
            key="doublon_a_corriger",
        )
        st.button("Ouvrir la fiche", key="ouvrir_fiche_doublon", on_click=_ouvrir_fiche, args=(role, identifiant))


def page_qualite() -> None:
    """Rapport de qualité : valeurs manquantes, doublons et incohérences détectés dans la base (section 7.3)."""
    st.header("Qualité des données")
    rapport = appel(api_client.rapport_qualite, 200)
    if rapport is None:
        return
    st.caption(
        f"Rapport généré le {format_horodatage(rapport['genere_le'])} sur {rapport['patients']} patients et "
        f"{rapport['rendez_vous']} rendez-vous. Il ne modifie aucune donnée."
    )
    colonnes = st.columns(4)
    colonnes[0].metric("Contrôles en erreur", rapport["erreurs"])
    colonnes[1].metric("Avertissements", rapport["avertissements"])
    colonnes[2].metric("Informations", rapport["informations"])
    colonnes[3].metric("Contrôles sans anomalie", rapport["controles_sans_anomalie"])
    if rapport["erreurs"]:
        st.error("Des incohérences sont à corriger en priorité (contrôles de gravité « Erreur »).")
    else:
        st.success("Aucune erreur détectée.")

    gravites = {"erreur": "Erreur", "avertissement": "Avertissement", "information": "Information"}
    toutes = st.checkbox("Afficher aussi les contrôles sans anomalie", value=False, key="qualite_toutes")
    controles = [c for c in rapport["controles"] if toutes or c["nombre"] > 0]
    tableau(
        [
            {
                "Gravité": gravites[c["gravite"]],
                "Anomalies": c["nombre"],
                "Part": "" if c["pourcentage"] is None else f"{c['pourcentage']:.1f} %".replace(".", ","),
                "Contrôle": c["libelle"],
                "Famille": c["famille"],
                "Exemples": ", ".join(c["exemples"][:5]),
            }
            for c in controles
        ]
    )
    for controle in controles:
        if controle["nombre"]:
            with st.expander(f"{controle['libelle']} - {controle['nombre']}"):
                st.caption(f"Gravité : {gravites[controle['gravite']]}. Code : {controle['code']}.")
                suite = " (liste limitée)" if controle["nombre"] > len(controle["exemples"]) else ""
                st.text(", ".join(controle["exemples"][:200]) + suite)

    sortie = io.StringIO()
    ecrivain = csv.writer(sortie)
    ecrivain.writerow(["code", "famille", "controle", "gravite", "nombre_total", "identifiant"])
    for controle in rapport["controles"]:
        for identifiant in controle["exemples"] or [""]:
            if controle["nombre"] or not identifiant:
                ecrivain.writerow([controle["code"], controle["famille"], controle["libelle"], controle["gravite"], controle["nombre"], identifiant])
    st.download_button(
        "Télécharger le rapport (CSV)",
        data=("\ufeff" + sortie.getvalue()).encode("utf-8"),
        file_name=f"rapport_qualite_{aujourdhui():%Y%m%d}.csv",
        mime="text/csv",
        key="telecharger_rapport",
    )
    st.caption("Le fichier cite jusqu'à 200 identifiants par contrôle.")


# --------------------------------------------------------------------------------------
# Plannings : médecin (le sien), responsable des opérations et administrateur (tous)
# --------------------------------------------------------------------------------------


def page_planning_medecin(medecin: dict, hors_ligne: bool, titre: str | None = "Mon planning") -> None:
    if titre:
        st.header(titre)
    afficher_message_en_attente()
    st.caption(libelle_medecin(medecin))
    jour_courant = aujourdhui()
    # EF-15 : trois vues du même planning (jour, semaine, liste sur une période libre).
    vue = st.radio("Vue", ["Jour", "Semaine", "Liste"], horizontal=True, key="vue_planning")
    if vue == "Jour":
        jour = st.date_input("Jour", value=jour_courant, format="DD/MM/YYYY", key="jour_planning")
        periode = (jour, jour)
    elif vue == "Semaine":
        jour = st.date_input("Semaine contenant le", value=jour_courant, format="DD/MM/YYYY", key="semaine_planning")
        lundi = jour - timedelta(days=jour.weekday())
        periode = (lundi, lundi + timedelta(days=6))
        st.caption(f"Semaine du {periode[0]:%d/%m/%Y} au {periode[1]:%d/%m/%Y}.")
    else:
        periode = st.date_input(
            "Période",
            value=(jour_courant, jour_courant + timedelta(days=6)),
            format="DD/MM/YYYY",
            key="periode_medecin",
        )
    if not isinstance(periode, (tuple, list)) or len(periode) != 2:
        st.info("Sélectionnez une date de début et une date de fin.")
        return
    donnees = appel(
        api_client.lister_rdv,
        medecin_id=medecin["id"],
        statuts=STATUTS_PLANNING,
        date_debut=periode[0],
        date_fin=periode[1],
    )
    if donnees is None:
        return
    rendez_vous = donnees["elements"]
    if donnees["total"] > len(rendez_vous):
        st.caption(f"{donnees['total']} rendez-vous sur la période : seuls les {len(rendez_vous)} premiers sont affichés.")
    if vue == "Semaine":
        _grille_semaine(rendez_vous, periode[0])
    else:
        tableau(
            [
                {
                    "Date": format_date(r["date_rdv"]),
                    "Heure": format_heure(r["heure_rdv"]),
                    "Durée (min)": r["duree_min"],
                    "Patient": r["patient_nom"],
                    "Statut": r["statut"],
                    "Canal": r["canal"] or "",
                }
                for r in rendez_vous
            ]
        )

    st.subheader("Clôturer un rendez-vous")
    a_cloturer = {
        r["id"]: r
        for r in rendez_vous
        if r["statut"] in ("Planifié", "Confirmé") and date.fromisoformat(r["date_rdv"]) <= jour_courant
    }
    if not a_cloturer:
        st.caption("Aucun rendez-vous du jour ou antérieur en attente d'être marqué honoré ou absent.")
        return
    rdv_id = st.selectbox(
        "Rendez-vous",
        list(a_cloturer),
        format_func=lambda i: (
            f"{format_date(a_cloturer[i]['date_rdv'])} {format_heure(a_cloturer[i]['heure_rdv'])} - "
            f"{a_cloturer[i]['patient_nom']}"
        ),
        key="rdv_a_cloturer",
    )
    colonne_honore, colonne_absent, _ = st.columns([2, 2, 3])
    nouveau_statut = None
    if colonne_honore.button("Marquer honoré", disabled=hors_ligne, key="marquer_honore"):
        nouveau_statut = "Honoré"
    if colonne_absent.button("Marquer absent", disabled=hors_ligne, key="marquer_absent"):
        nouveau_statut = "Absent"
    if nouveau_statut and appel(api_client.modifier_rdv, rdv_id, statut=nouveau_statut):
        st.session_state["_succes"] = f"Rendez-vous {rdv_id} marqué « {nouveau_statut} »."
        st.rerun()


def _grille_semaine(rendez_vous: list[dict], lundi: date) -> None:
    """Vue semaine : une ligne par heure, une colonne par jour (du lundi au samedi, dimanche si besoin)."""
    if not rendez_vous:
        st.info("Aucun rendez-vous cette semaine.")
        return
    jours = [lundi + timedelta(days=i) for i in range(7)]
    if not any(date.fromisoformat(r["date_rdv"]) == jours[6] for r in rendez_vous):
        jours = jours[:6]  # le dimanche n'apparaît que s'il porte un rendez-vous
    colonnes = {j: f"{JOURS_COURTS[j.weekday()]} {j:%d/%m}" for j in jours}
    grille: dict[str, dict[str, str]] = {}
    for r in rendez_vous:
        jour = date.fromisoformat(r["date_rdv"])
        if jour in colonnes:
            grille.setdefault(format_heure(r["heure_rdv"]), {})[colonnes[jour]] = f"{r['patient_nom']} ({r['statut']})"
    tableau([{"Heure": heure, **{c: grille[heure].get(c, "") for c in colonnes.values()}} for heure in sorted(grille)])


def page_plannings_medecins(listes: dict, hors_ligne: bool) -> None:
    st.header("Plannings des médecins")
    medecins = {m["id"]: m for m in listes["medecins"]}
    if not medecins:
        st.info("Aucun médecin n'est enregistré : il n'y a aucun planning à afficher.")
        return
    # EF-15 : filtre par spécialité, puis par médecin.
    specialite = st.selectbox(
        "Spécialité", ["Toutes"] + sorted({m["specialite"] for m in medecins.values()}), key="specialite_consultee"
    )
    candidats = {i: m for i, m in medecins.items() if specialite == "Toutes" or m["specialite"] == specialite}
    medecin_id = st.selectbox(
        "Médecin", list(candidats), format_func=lambda i: libelle_medecin(candidats[i]), key="medecin_consulte"
    )
    page_planning_medecin(candidats[medecin_id], hors_ligne, titre=None)


# --------------------------------------------------------------------------------------
# Tableau de bord (responsable des opérations, administrateur) et synthèse (Direction)
# --------------------------------------------------------------------------------------


def _indicateurs_du_jour(jour_courant: date) -> None:
    """EF-20 : indicateurs clés d'aujourd'hui, indépendants des filtres de période."""
    st.subheader(f"Aujourd'hui, {jour_courant:%d/%m/%Y}")
    try:
        jour = api_client.indicateurs_du_jour(jour_courant)
    except ErreurApi as erreur:
        if erreur.code_erreur in CODES_SESSION_PERDUE:
            fermer_session(erreur.message)
            st.rerun()
        st.caption("Les indicateurs du jour sont momentanément indisponibles.")
        return
    colonnes = st.columns(5)
    colonnes[0].metric("Rendez-vous prévus", jour["prevus"])
    colonnes[1].metric("À venir", jour["a_venir"])
    colonnes[2].metric("Honorés", jour["honores"])
    colonnes[3].metric("Absents", jour["absents"])
    colonnes[4].metric("Taux d'occupation", format_pourcentage(jour["taux_occupation_pct"]))


def page_tableau_de_bord(listes: dict, complet: bool = True) -> None:
    st.header("Tableau de bord" if complet else "Synthèse mensuelle")
    medecins = listes["medecins"]
    jour_courant = aujourdhui()
    _indicateurs_du_jour(jour_courant)
    vue = st.radio("Analyse", VUES_TABLEAU_DE_BORD, horizontal=True, key="vue_tableau_de_bord")
    if vue == "Risque d'absence":
        _vue_risque_absence()  # indépendante de la période : pas de filtres ni de taux d'absence ici
        return
    specialite, medecin_id = "Toutes", None
    if complet:
        colonne_periode, colonne_specialite, colonne_medecin = st.columns(3)
    else:
        colonne_periode = st.columns(3)[0]
    periode = colonne_periode.date_input(
        "Période",
        value=(jour_courant - timedelta(days=365), jour_courant),
        format="DD/MM/YYYY",
        key="periode_direction",
    )
    if complet:
        specialite = colonne_specialite.selectbox(
            "Spécialité", ["Toutes"] + sorted({m["specialite"] for m in medecins}), key="specialite_direction"
        )
        medecins_filtres = {m["id"]: m for m in medecins if specialite == "Toutes" or m["specialite"] == specialite}
        medecin_id = colonne_medecin.selectbox(
            "Médecin",
            [None] + list(medecins_filtres),
            format_func=lambda i: "Tous" if i is None else medecins_filtres[i]["nom"],
            key="medecin_direction",
        )
    if not isinstance(periode, (tuple, list)) or len(periode) != 2:
        st.info("Sélectionnez une date de début et une date de fin.")
        return
    date_debut, date_fin = periode
    filtre_specialite = None if specialite == "Toutes" else specialite

    kpi = appel(api_client.indicateurs, date_debut, date_fin, filtre_specialite, medecin_id)
    if kpi is None:
        st.caption("Les indicateurs ne peuvent pas être affichés tant que le service est indisponible.")
        return
    colonnes = st.columns(6)
    colonnes[0].metric("Rendez-vous", kpi["total_rdv"])
    colonnes[1].metric("Honorés", kpi["honores"])
    colonnes[2].metric("Absents", kpi["absents"])
    colonnes[3].metric("Taux d'absence", format_pourcentage(kpi["taux_absence_pct"]))
    colonnes[4].metric("Annulés", kpi["repartition_statuts"]["Annulé"])
    try:  # indicateur secondaire : son échec ne doit pas masquer les autres chiffres
        occupation = api_client.taux_occupation(date_debut, min(date_fin, jour_courant), filtre_specialite, medecin_id)
    except ErreurApi as erreur:
        if erreur.code_erreur in CODES_SESSION_PERDUE:
            fermer_session(erreur.message)
            st.rerun()
        occupation = None
        st.caption("Le taux d'occupation est momentanément indisponible ; les autres indicateurs restent valables.")
    colonnes[5].metric("Taux d'occupation", format_pourcentage(occupation["taux_occupation_pct"]) if occupation else "-")
    st.caption(
        f"Taux d'absence = absents / (honorés + absents) = {kpi['absents']} / {kpi['base_calcul_absence']}. "
        "Les rendez-vous planifiés, confirmés et annulés sont exclus du calcul. "
        "Taux d'occupation = créneaux honorés ou absents / créneaux ouverts (jours fériés, fermetures et "
        "indisponibilités exclus), jusqu'à aujourd'hui."
    )
    if complet:
        _bloc_export(date_debut, date_fin, filtre_specialite, medecin_id)
    if complet and occupation and occupation["par_medecin"]:
        with st.expander("Taux d'occupation par médecin"):
            tableau(
                [
                    {
                        "Médecin": o["medecin_nom"],
                        "Spécialité": o["specialite"],
                        "Créneaux ouverts": o["creneaux_ouverts"],
                        "Créneaux occupés": o["creneaux_occupes"],
                        "Taux d'occupation": format_pourcentage(o["taux_occupation_pct"]),
                    }
                    for o in occupation["par_medecin"]
                ]
            )

    if vue == "Absences":
        _vue_absences(date_debut, date_fin, specialite, filtre_specialite, medecin_id)
    elif vue == "Demande":
        _vue_demande(date_debut, date_fin, filtre_specialite, medecin_id)
    elif vue == "Annulations":
        _vue_annulations(date_debut, date_fin, filtre_specialite, medecin_id)
    else:
        _vue_prevision(filtre_specialite)


def page_a_relancer(listes: dict, role: str) -> None:
    """EF-25 : rendez-vous dont le risque d'absence dépasse le seuil de relance. Aide à la relance, jamais une contrainte."""
    st.header("Rendez-vous à relancer")
    st.caption(
        "Le score estime la probabilité qu'un patient ne vienne pas. C'est une aide pour décider qui rappeler : "
        "il ne bloque aucune prise de rendez-vous et le patient ne le voit jamais."
    )
    colonnes = st.columns(3)
    jours = colonnes[0].slider("Rendez-vous des prochains jours", 1, 14, 3, key="relance_jours")
    specialite = colonnes[1].selectbox(
        "Spécialité", ["Toutes"] + sorted({m["specialite"] for m in listes["medecins"]}), key="relance_specialite"
    )
    tous = colonnes[2].checkbox("Inclure ceux sous le seuil", key="relance_tous")
    donnees = appel(api_client.rdv_a_relancer, jours, tous, None if specialite == "Toutes" else specialite)
    if donnees is None:
        return
    if donnees["calcule_le"] is None:
        st.info(
            "Aucun score n'a encore été calculé : le module analytique s'exécute chaque nuit."
            + (" Vous pouvez lancer le calcul maintenant." if role in ("Responsable des opérations", "Administrateur") else "")
        )
    else:
        st.caption(
            f"Scores calculés le {format_date(donnees['calcule_le'][:10])} à {donnees['calcule_le'][11:16].replace(':', ' h ')}. "
            f"Seuil de relance : {format_pourcentage(donnees['seuil_pct'])} de risque."
        )
    if role in ("Responsable des opérations", "Administrateur") and st.button("Recalculer maintenant", key="recalcul_risque"):
        bilan = appel(api_client.recalculer_risque)
        if bilan is not None:
            if bilan["statut"] == "ok":
                st.session_state["_succes"] = f"Scores recalculés pour {bilan['scores']} rendez-vous à venir."
            else:
                st.session_state["_alerte"] = bilan.get("message") or "Les données sont insuffisantes pour calculer les scores."
            st.rerun()
    if not donnees["rendez_vous"]:
        if donnees["calcule_le"]:
            st.success("Aucun rendez-vous à relancer sur cette période.")
        return
    st.metric("Rendez-vous concernés", donnees["nombre"])
    tableau(
        [
            {
                "Risque": format_pourcentage(r["score_pct"]),
                "Rendez-vous": f"{format_date(r['date_rdv'])} à {r['heure_rdv'][:5].replace(':', ' h ')}",
                "Patient": r["patient"],
                "Téléphone": r["telephone"] or "non renseigné",
                "Médecin": r["medecin"],
                "Spécialité": r["specialite"],
                "Statut": r["statut"],
                "Raisons principales": r["facteurs"] or "-",
            }
            for r in donnees["rendez_vous"]
        ]
    )


def _vue_risque_absence() -> None:
    st.subheader("Score de risque d'absence")
    ev = appel(api_client.evaluation_risque)
    if ev is None:
        return
    retenu = ev["modeles"][ev["retenu"]]
    h = ev["hypotheses"]
    st.caption(
        f"Évalué le {format_date(ev['calcule_le'][:10])} sur les rendez-vous les plus récents, mis de côté pendant "
        f"l'apprentissage : {ev['nb_test']} rendez-vous clos à partir du {format_date(ev['date_coupure'][:10])}, "
        f"{ev['nb_apprentissage']} pour apprendre. Taux d'absence : {format_pourcentage(ev['taux_absence_apprentissage'] * 100)} "
        f"à l'apprentissage, {format_pourcentage(ev['taux_absence_test'] * 100)} sur la période de test."
    )
    colonnes = st.columns(4)
    colonnes[0].metric("Modèle retenu", "Logistique" if ev["retenu"] == "logistique" else "Forêt")
    colonnes[1].metric("AUC", f"{retenu['auc']:.3f}".replace(".", ","))
    colonnes[2].metric("Précision au seuil", format_pourcentage(None if retenu["precision"] is None else retenu["precision"] * 100))
    colonnes[3].metric("Rappel au seuil", format_pourcentage(None if retenu["rappel"] is None else retenu["rappel"] * 100))
    st.caption("Une AUC de 0,5 correspond au hasard, de 1 à un classement parfait.")

    tableau(
        [
            {
                "Modèle": m["libelle"],
                "AUC": round(m["auc"], 3),
                "Précision moyenne": round(m["precision_moyenne"], 3),
                "Précision au seuil (%)": None if m["precision"] is None else round(m["precision"] * 100, 1),
                "Rappel au seuil (%)": None if m["rappel"] is None else round(m["rappel"] * 100, 1),
                "Part des rendez-vous relancés (%)": round(m["part_relancee"] * 100, 1),
            }
            for m in ev["modeles"].values()
        ]
    )

    st.markdown("**Matrice de confusion au seuil retenu**")
    (vn, fp), (fn, vp) = retenu["matrice"]
    tableau(
        [
            {"Réalité": "Venu", "Non relancé": vn, "Relancé": fp},
            {"Réalité": "Absent", "Non relancé": fn, "Relancé": vp},
        ]
    )

    st.markdown("**Choix du seuil : coût d'un appel contre coût d'un créneau perdu**")
    st.markdown(
        f"Relancer un patient coûte environ {h['cout_relance']:,.0f} FCFA (temps d'agent). Un créneau perdu coûte "
        f"{h['cout_creneau']:,.0f} FCFA, et une relance évite l'absence dans {h['efficacite'] * 100:.0f} % des cas. "
        f"L'appel est rentable dès que le risque dépasse {h['cout_relance']:,.0f} / ({h['efficacite']:.2f} × {h['cout_creneau']:,.0f}) "
        f"= **{ev['seuil'] * 100:.1f} %**. Ces trois montants sont des hypothèses à faire valider par la clinique.".replace(",", " ")
    )
    st.altair_chart(_graphique_seuils(ev["courbe_seuils"], ev["seuil"]))
    gains = ev["strategies"]
    tableau(
        [
            {"Stratégie sur la période de test": "Ne relancer personne", "Gain net estimé (FCFA)": f"{gains['ne_relancer_personne']:,.0f}".replace(",", " ")},
            {"Stratégie sur la période de test": "Relancer tout le monde", "Gain net estimé (FCFA)": f"{gains['relancer_tout_le_monde']:,.0f}".replace(",", " ")},
            {"Stratégie sur la période de test": "Relancer selon le score", "Gain net estimé (FCFA)": f"{gains['relancer_selon_le_score']:,.0f}".replace(",", " ")},
        ]
    )
    with st.expander("Limites de cette analyse"):
        for limite in ev["limites"]:
            st.markdown(f"- {limite}")


def _graphique_seuils(courbe: list[dict], seuil: float) -> alt.Chart:
    donnees = pd.DataFrame(courbe)
    donnees["Seuil (%)"] = donnees["seuil"] * 100
    base = alt.Chart(donnees).encode(x=alt.X("Seuil (%):Q", title="Seuil de relance (% de risque)"))
    ligne = base.mark_line(point=True).encode(y=alt.Y("gain_net:Q", title="Gain net estimé (FCFA)"))
    repere = alt.Chart(pd.DataFrame({"Seuil (%)": [seuil * 100]})).mark_rule(strokeDash=[4, 4]).encode(x="Seuil (%):Q")
    return (ligne + repere).properties(height=260)


def _bloc_export(date_debut, date_fin, filtre_specialite, medecin_id) -> None:
    """EF-26 : le fichier est préparé à la demande (inutile de le calculer à chaque affichage), avec les filtres de l'écran."""
    with st.expander("Exporter ces indicateurs"):
        format_choisi = st.radio("Format", ["CSV (Excel)", "PDF"], horizontal=True, key="export_format")
        extension = "csv" if format_choisi.startswith("CSV") else "pdf"
        cle = (extension, date_debut, date_fin, filtre_specialite, medecin_id)
        if st.button("Préparer le fichier", key="export_preparer"):
            contenu = appel(api_client.exporter_indicateurs, extension, date_debut, date_fin, filtre_specialite, medecin_id)
            if contenu is not None:
                st.session_state["_export"] = (cle, contenu)
        pret = st.session_state.get("_export")
        if pret and pret[0] == cle:
            st.download_button(
                f"Télécharger le fichier {extension.upper()}", pret[1], file_name=f"indicateurs_medirdv.{extension}",
                mime="text/csv" if extension == "csv" else "application/pdf", key="export_telecharger",
            )
            st.caption("Les filtres de l'écran (période, spécialité, médecin) sont appliqués. L'export est enregistré dans le journal.")
        elif pret:
            st.caption("Les filtres ont changé : préparez à nouveau le fichier.")


def _vue_absences(date_debut, date_fin, specialite, filtre_specialite, medecin_id) -> None:
    st.subheader("Taux d'absence par spécialité")
    if specialite == "Toutes":
        par_specialite = appel(api_client.absence_par_specialite, date_debut, date_fin, medecin_id)
        if par_specialite is not None:
            tableau(
                [
                    {
                        "Spécialité": s["specialite"],
                        "Rendez-vous": s["total_rdv"],
                        "Honorés": s["honores"],
                        "Absents": s["absents"],
                        "Taux d'absence (%)": s["taux_absence_pct"],
                    }
                    for s in par_specialite
                ]
            )
            if par_specialite:
                # Apostrophe typographique volontaire : une apostrophe droite dans le nom d'une colonne
                # empêche l'affichage du graphique.
                st.bar_chart(
                    pd.DataFrame(
                        {"Taux d’absence (%)": [s["taux_absence_pct"] for s in par_specialite]},
                        index=[s["specialite"] for s in par_specialite],
                    )
                )
    else:
        st.caption("La comparaison entre spécialités s'affiche lorsque « Toutes » est sélectionné.")

    st.subheader("Évolution mensuelle")
    mensuel = appel(api_client.evolution_mensuelle, date_debut, date_fin, filtre_specialite, medecin_id)
    if mensuel is None:
        return
    if not mensuel:
        st.info("Aucun rendez-vous sur cette période.")
        return
    donnees = pd.DataFrame(mensuel)
    st.caption("Taux d'absence mensuel (%) par spécialité")
    # Un mois sans rendez-vous honoré ni absent n'a pas de taux (non calculable) : il n'est pas tracé,
    # afin de ne pas être confondu avec un taux de 0 %.
    taux = donnees.pivot(index="periode", columns="specialite", values="taux_absence_pct")
    st.line_chart(taux)
    if donnees["taux_absence_pct"].isna().any():
        st.caption("Les mois sans rendez-vous honoré ni absent n'ont pas de taux calculable et ne sont pas tracés.")
    st.caption("Nombre de rendez-vous par mois et par spécialité")
    st.bar_chart(donnees.pivot_table(index="periode", columns="specialite", values="total_rdv", aggfunc="sum").fillna(0))


def _vue_demande(date_debut, date_fin, filtre_specialite, medecin_id) -> None:
    st.subheader("Carte de chaleur de la demande")
    inclure = st.checkbox("Inclure les rendez-vous annulés", key="chaleur_annules")
    carte = appel(api_client.carte_chaleur, date_debut, date_fin, filtre_specialite, medecin_id, inclure)
    if carte is not None:
        if not carte["cellules"]:
            st.info("Aucun rendez-vous sur cette période.")
        else:
            donnees = pd.DataFrame(carte["cellules"])
            heures = list(range(int(donnees["heure"].min()), int(donnees["heure"].max()) + 1))
            jours = [j for j in range(7) if j < 6 or (donnees["jour_semaine"] == 6).any()]
            grille = pd.DataFrame([(j, h) for j in jours for h in heures], columns=["jour_semaine", "heure"])
            grille = grille.merge(donnees, how="left", on=["jour_semaine", "heure"]).fillna({"nombre": 0})
            grille["Jour"] = grille["jour_semaine"].map(lambda j: JOURS_LONGS[j])
            grille["Heure"] = grille["heure"].map(lambda h: f"{h:02d}h")
            base = alt.Chart(grille).encode(
                x=alt.X("Heure:O", title="Heure de début"),
                y=alt.Y("Jour:N", sort=[JOURS_LONGS[j] for j in jours], title=None),
            )
            carte_graphique = base.mark_rect().encode(
                color=alt.Color("nombre:Q", scale=alt.Scale(scheme="greens"), title="Rendez-vous"),
                tooltip=["Jour", "Heure", alt.Tooltip("nombre:Q", title="Rendez-vous")],
            ) + base.mark_text(fontSize=11).encode(
                text=alt.Text("nombre:Q", format="d"),
                color=alt.condition(alt.datum.nombre > 0.6 * float(grille["nombre"].max()), alt.value("white"), alt.value("#1f2d2a")),
            )
            st.altair_chart(carte_graphique.properties(height=46 * len(jours)))
            st.caption(
                f"{carte['total']} rendez-vous, "
                f"{'annulés inclus' if carte['inclut_annules'] else 'annulés exclus'}. "
                "Plus la case est foncée, plus la demande est forte à ce jour et à cette heure."
            )
            with st.expander("Rendez-vous par spécialité et jour de la semaine"):
                par_spec = pd.DataFrame(carte["par_specialite"])
                if not par_spec.empty:
                    par_spec["Jour"] = par_spec["jour_semaine"].map(lambda j: JOURS_LONGS[j])
                    table = par_spec.pivot_table(index="specialite", columns="Jour", values="nombre", aggfunc="sum").fillna(0)
                    table = table[[j for j in JOURS_LONGS if j in table.columns]].astype(int)
                    table.index.name = "Spécialité"
                    st.dataframe(table)

    st.subheader("Délai d'attente")
    delai = appel(api_client.delai_attente, date_debut, date_fin, filtre_specialite, medecin_id)
    if delai is None:
        return
    if delai["mediane_jours"] is None:
        st.info("Aucun rendez-vous sur cette période.")
        return
    colonne_mediane, colonne_nombre = st.columns(2)
    colonne_mediane.metric("Délai médian", f"{delai['mediane_jours']:g} jours".replace(".", ","))
    colonne_nombre.metric("Rendez-vous pris en compte", delai["nombre_rdv"])
    st.caption("Délai = jours entre la prise du rendez-vous et sa date. Les rendez-vous annulés sont exclus.")
    tableau(
        [
            {
                "Spécialité": d["specialite"],
                "Rendez-vous": d["nombre_rdv"],
                "Délai médian (jours)": d["mediane_jours"],
                "Délai moyen (jours)": d["moyenne_jours"],
                "9 rendez-vous sur 10 sous (jours)": d["p90_jours"],
            }
            for d in delai["par_specialite"]
        ]
    )
    if len(delai["par_specialite"]) > 1:
        st.bar_chart(
            pd.DataFrame(
                {"Délai médian (jours)": [d["mediane_jours"] for d in delai["par_specialite"]]},
                index=[d["specialite"] for d in delai["par_specialite"]],
            )
        )


def _vue_annulations(date_debut, date_fin, filtre_specialite, medecin_id) -> None:
    st.subheader("Taux d'annulation")
    annul = appel(api_client.taux_annulation, date_debut, date_fin, filtre_specialite, medecin_id)
    if annul is None:
        return
    if not annul["total_cree"]:
        st.info("Aucun rendez-vous sur cette période.")
        return
    colonnes = st.columns(3)
    colonnes[0].metric("Taux d'annulation", format_pourcentage(annul["taux_annulation_pct"]))
    colonnes[1].metric("Annulés", annul["annules"])
    colonnes[2].metric("Rendez-vous créés", annul["total_cree"])
    st.caption("Taux d'annulation = annulés / total des rendez-vous créés, tous statuts confondus.")

    st.subheader("Par motif")
    if annul["par_motif"]:
        tableau([{"Motif": m["motif"], "Annulés": m["nombre"], "Part des rendez-vous créés (%)": m["part_pct"]} for m in annul["par_motif"]])
        st.bar_chart(pd.DataFrame({"Annulés": [m["nombre"] for m in annul["par_motif"]]}, index=[m["motif"] for m in annul["par_motif"]]))
    else:
        st.info("Aucune annulation sur cette période.")

    st.subheader("Par mois")
    mois = pd.DataFrame(annul["par_mois"])
    mois["Taux (%)"] = mois["taux_annulation_pct"]
    st.line_chart(mois.set_index("periode")[["Taux (%)"]])
    motifs_mois = pd.DataFrame(
        [{"periode": m["periode"], **m["par_motif"]} for m in annul["par_mois"]]
    ).set_index("periode").fillna(0)
    if not motifs_mois.empty:
        st.caption("Annulations par mois et par motif")
        st.bar_chart(motifs_mois)

    if not filtre_specialite:
        st.subheader("Par spécialité")
        tableau(
            [
                {
                    "Spécialité": x["specialite"],
                    "Créés": x["total_cree"],
                    "Annulés": x["annules"],
                    "Taux d'annulation (%)": x["taux_annulation_pct"],
                }
                for x in annul["par_specialite"]
            ]
        )


def _graphique_prevision(spec: dict) -> alt.Chart:
    """Historique (trait plein), prévision (pointillés) et intervalle d'incertitude (zone) d'une série hebdomadaire."""
    histo = pd.DataFrame(spec["historique"]).rename(columns={"volume": "Rendez-vous"})
    histo["semaine"] = pd.to_datetime(histo["semaine"])
    prev = pd.DataFrame(spec["prevision"])
    prev["semaine"] = pd.to_datetime(prev["semaine"])
    # La prévision part de la dernière semaine observée pour que les deux traits se rejoignent.
    liaison = pd.DataFrame({"semaine": [histo["semaine"].iloc[-1]], "prevision": [float(histo["Rendez-vous"].iloc[-1])]})
    trait_prevision = pd.concat([liaison, prev[["semaine", "prevision"]]])
    x = alt.X("semaine:T", title="Semaine (lundi)", axis=alt.Axis(format="%d/%m"))
    y = alt.Y("Rendez-vous:Q", title="Rendez-vous par semaine", scale=alt.Scale(zero=True))
    zone = alt.Chart(prev).mark_area(opacity=0.25, color="#2E7D6B").encode(
        x=x, y=alt.Y("bas:Q", title="Rendez-vous par semaine"), y2="haut:Q"
    )
    ligne_histo = alt.Chart(histo).mark_line(color="#1f2d2a", point=alt.OverlayMarkDef(color="#1f2d2a", size=40)).encode(
        x=x, y=y, tooltip=[alt.Tooltip("semaine:T", title="Semaine", format="%d/%m/%Y"), "Rendez-vous"]
    )
    ligne_prev = alt.Chart(trait_prevision).mark_line(color="#2E7D6B", strokeDash=[6, 4], point=alt.OverlayMarkDef(color="#2E7D6B", size=40)).encode(
        x=x,
        y=alt.Y("prevision:Q", title="Rendez-vous par semaine"),
        tooltip=[alt.Tooltip("semaine:T", title="Semaine", format="%d/%m/%Y"), alt.Tooltip("prevision:Q", title="Prévision", format=".1f")],
    )
    return (zone + ligne_histo + ligne_prev).properties(height=300)


def _graphique_controle(spec: dict) -> alt.Chart:
    """Les 8 semaines tenues à l'écart : réel contre prévu par chaque méthode."""
    lignes = []
    for point in spec["backtest"]:
        lignes.append({"semaine": point["semaine"], "Série": "Réel", "Rendez-vous": point["reel"]})
        for methode, valeur in point["par_methode"].items():
            lignes.append({"semaine": point["semaine"], "Série": LIBELLES_METHODES.get(methode, methode), "Rendez-vous": valeur})
    donnees = pd.DataFrame(lignes)
    donnees["semaine"] = pd.to_datetime(donnees["semaine"])
    return (
        alt.Chart(donnees)
        .mark_line(point=True)
        .encode(
            x=alt.X("semaine:T", title="Semaine (lundi)", axis=alt.Axis(format="%d/%m")),
            y=alt.Y("Rendez-vous:Q", title="Rendez-vous par semaine", scale=alt.Scale(zero=False)),
            color=alt.Color("Série:N", title=None, scale=alt.Scale(range=["#1f2d2a", "#2E7D6B", "#C9822B", "#6B7FA3"])),
            strokeDash=alt.condition(alt.datum["Série"] == "Réel", alt.value([1, 0]), alt.value([5, 3])),
        )
        .properties(height=260)
    )


def _vue_prevision(filtre_specialite: str | None) -> None:
    st.subheader("Prévision du volume hebdomadaire de rendez-vous")
    donnees = appel(api_client.prevision, filtre_specialite, 4)
    if donnees is None:
        return
    retenue = donnees["methode_retenue"]
    libelles = {m["methode"]: m["libelle"] for m in donnees["methodes"]}
    st.caption(
        f"Quatre semaines à partir du {format_date(donnees['ensemble']['prevision'][0]['semaine'])}. "
        f"Méthode retenue : {libelles[retenue].lower()}, choisie parce qu'elle a l'erreur moyenne la plus faible "
        f"sur les {donnees['semaines_tenues_a_l_ecart']} dernières semaines mises de côté. "
        "L'intervalle couvre environ 90 % des cas."
    )
    serie = donnees["specialites"][0] if filtre_specialite else donnees["ensemble"]
    st.markdown(f"**{serie['specialite']}**")
    st.altair_chart(_graphique_prevision(serie))
    tableau(
        [
            {
                "Semaine du": format_date(p["semaine"]),
                "Prévision": p["prevision"],
                "Intervalle (90 %)": f"{p['bas']:g} à {p['haut']:g}".replace(".", ","),
                "Déjà enregistrés": p["deja_pris"],
            }
            for p in serie["prevision"]
        ]
    )
    st.caption(
        "Déjà enregistrés = rendez-vous déjà pris pour la semaine ; la prévision porte sur le volume final de la semaine."
    )

    if not filtre_specialite:
        st.subheader("Par spécialité")
        lignes = []
        for spec in donnees["specialites"]:
            ligne = {"Spécialité": spec["specialite"]}
            for p in spec["prevision"]:
                ligne[f"Semaine du {format_date(p['semaine'])[:5]}"] = p["prevision"]
            lignes.append(ligne)
        tableau(lignes)

    st.subheader("Qualité des méthodes")
    st.caption(
        f"Évaluation sur les {donnees['semaines_tenues_a_l_ecart']} dernières semaines, mises de côté puis prévues "
        f"à partir des {donnees['semaines_entrainement']} semaines précédentes. "
        "MAE = erreur moyenne en rendez-vous par semaine ; MAPE = erreur moyenne en pourcentage."
    )
    tableau(
        [
            {
                "Méthode": m["libelle"] + (" (retenue)" if m["methode"] == retenue else ""),
                "MAE (rdv / semaine)": m["mae"],
                "MAPE (%)": m["mape_pct"],
            }
            for m in donnees["methodes"]
        ]
    )
    with st.expander(f"Contrôle sur les semaines mises de côté - {serie['specialite']}"):
        st.altair_chart(_graphique_controle(serie))
    with st.expander("Limites de cette prévision"):
        for limite in donnees["limites"]:
            st.markdown(f"- {limite}")


# --------------------------------------------------------------------------------------
# Planning : spécialités, horaires, jours fermés (Responsable, Administrateur) et indisponibilités
# --------------------------------------------------------------------------------------


def _analyser_heure(texte: str) -> time | None:
    try:
        return datetime.strptime(str(texte).strip(), "%H:%M").time()
    except ValueError:
        return None


def onglet_specialites(role: str, hors_ligne: bool) -> None:
    specialites = appel(api_client.lister_specialites)
    if specialites is None:
        return
    tableau(
        [
            {
                "Spécialité": x["nom"],
                "Durée d'une consultation (min)": x["duree_standard_min"],
                "Couleur": x["couleur"],
                "Médecins": x["nb_medecins"],
                "État": "Active" if x["actif"] else "Désactivée",
            }
            for x in specialites
        ]
    )
    if role != "Administrateur":
        st.caption("Seul l'administrateur modifie les spécialités.")
        return

    st.subheader("Modifier une spécialité")
    if specialites:
        indexees = {x["nom"]: x for x in specialites}
        nom = st.selectbox("Spécialité", list(indexees), key="specialite_modifiee")
        courante = indexees[nom]
        colonne_duree, colonne_couleur, colonne_etat = st.columns(3)
        duree = colonne_duree.selectbox(
            "Durée d'une consultation (min)",
            [15, 20, 30],
            index=[15, 20, 30].index(courante["duree_standard_min"]),
            key=f"duree_{nom}",
        )
        couleur = colonne_couleur.color_picker("Couleur", value=courante["couleur"], key=f"couleur_{nom}")
        actif = colonne_etat.checkbox("Spécialité active", value=courante["actif"], key=f"actif_{nom}")
        st.caption(
            "Changer la durée régénère les créneaux futurs de tous les médecins de la spécialité ; "
            "les rendez-vous déjà pris ne sont pas déplacés."
        )
        if st.button("Enregistrer les modifications", disabled=hors_ligne, key="enregistrer_specialite"):
            champs = {}
            if duree != courante["duree_standard_min"]:
                champs["duree_standard_min"] = duree
            if couleur.upper() != courante["couleur"].upper():
                champs["couleur"] = couleur
            if actif != courante["actif"]:
                champs["actif"] = actif
            if not champs:
                st.info("Aucune modification à enregistrer.")
            elif appel(api_client.modifier_specialite, nom, champs):
                st.session_state["_succes"] = f"Spécialité {nom} mise à jour."
                st.session_state["_actualiser"] = True
                st.rerun()

    st.subheader("Créer une spécialité")
    colonne_nom, colonne_duree, colonne_couleur = st.columns(3)
    nouveau_nom = colonne_nom.text_input("Nom", key="nouvelle_specialite_nom", max_chars=60)
    nouvelle_duree = colonne_duree.selectbox("Durée d'une consultation (min)", [15, 20, 30], index=1, key="nouvelle_specialite_duree")
    nouvelle_couleur = colonne_couleur.color_picker("Couleur", value="#2E7D6B", key="nouvelle_specialite_couleur")
    if st.button("Créer la spécialité", disabled=hors_ligne or len(nouveau_nom.strip()) < 2, key="creer_specialite"):
        if appel(api_client.creer_specialite, nouveau_nom.strip(), nouvelle_duree, nouvelle_couleur):
            st.session_state["_succes"] = f"Spécialité {nouveau_nom.strip()} créée."
            st.rerun()


def onglet_horaires(listes: dict, hors_ligne: bool) -> None:
    medecins = {m["id"]: m for m in listes["medecins"]}
    if not medecins:
        st.info("Aucun médecin n'est enregistré.")
        return
    medecin_id = st.selectbox(
        "Médecin", list(medecins), format_func=lambda i: libelle_medecin(medecins[i]), key="medecin_horaires"
    )
    donnees = appel(api_client.lire_plages, medecin_id)
    if donnees is None:
        return
    version = st.session_state.get("_version_plages", 0)
    lignes = [
        {"Jour": JOURS_LONGS[p["jour_semaine"]], "Début": format_heure(p["debut"]), "Fin": format_heure(p["fin"])}
        for p in donnees["plages"]
    ]
    st.caption(
        "Une ligne par plage de consultation hebdomadaire (plusieurs plages par jour possibles, par exemple "
        "matin et après-midi). Les créneaux sont calculés à partir de ces plages et de la durée de la spécialité."
    )
    edite = st.data_editor(
        pd.DataFrame(lignes, columns=["Jour", "Début", "Fin"]),
        num_rows="dynamic",
        hide_index=True,
        column_config={
            "Jour": st.column_config.SelectboxColumn("Jour", options=JOURS_LONGS, required=True),
            "Début": st.column_config.TextColumn("Début (HH:MM)", required=True, max_chars=5),
            "Fin": st.column_config.TextColumn("Fin (HH:MM)", required=True, max_chars=5),
        },
        key=f"plages_{medecin_id}_{version}",
    )
    if st.button("Enregistrer les horaires", disabled=hors_ligne, key="enregistrer_plages"):
        plages, erreurs = [], []
        for numero, ligne in enumerate(edite.to_dict("records"), start=1):
            debut, fin = _analyser_heure(ligne["Début"]), _analyser_heure(ligne["Fin"])
            if ligne["Jour"] not in JOURS_LONGS or debut is None or fin is None:
                erreurs.append(f"Ligne {numero} : jour ou heure invalide (format HH:MM).")
                continue
            plages.append({"jour_semaine": JOURS_LONGS.index(ligne["Jour"]), "debut": debut, "fin": fin})
        if erreurs:
            for message in erreurs:
                st.error(message)
        else:
            reponse = appel(api_client.remplacer_plages, medecin_id, plages)
            if reponse is not None:
                st.session_state["_version_plages"] = version + 1
                st.session_state["_succes"] = "Horaires enregistrés. Les créneaux futurs sont recalculés."
                if reponse["rdv_hors_planning"]:
                    st.session_state["_alerte"] = (
                        f"{len(reponse['rdv_hors_planning'])} rendez-vous à venir tombent hors des nouveaux horaires "
                        f"et sont à reprogrammer : {', '.join(reponse['rdv_hors_planning'][:10])}"
                        + (" ..." if len(reponse["rdv_hors_planning"]) > 10 else "")
                    )
                st.rerun()


def onglet_jours_fermes(hors_ligne: bool) -> None:
    annees = [aujourdhui().year, aujourdhui().year + 1]
    annee = st.selectbox("Année", annees, key="annee_feries")
    feries = appel(api_client.lister_feries, annee)
    if feries is None:
        return
    tableau(
        [
            {"Date": format_date(f["jour"]), "Jour": JOURS_LONGS[date.fromisoformat(f["jour"]).weekday()], "Libellé": f["libelle"], "Type": f["type"]}
            for f in feries
        ]
    )
    st.caption(
        "Les jours fériés légaux sont générés automatiquement. Les fêtes religieuses dont la date dépend de "
        "l'observation de la lune sont à ajouter ici dès que la date est connue."
    )
    resultat = st.session_state.pop("_a_reprogrammer_ferie", None)
    if resultat:
        st.warning(resultat["message"])
        tableau(resultat["lignes"])

    st.subheader("Ajouter un jour férié ou une fermeture")
    colonne_jour, colonne_libelle, colonne_type = st.columns([1, 2, 1])
    jour = colonne_jour.date_input("Date", value=aujourdhui(), min_value=aujourdhui(), format="DD/MM/YYYY", key="date_ferie")
    libelle = colonne_libelle.text_input("Libellé", key="libelle_ferie", max_chars=120)
    type_fermeture = colonne_type.selectbox("Type", ["Jour férié", "Fermeture exceptionnelle"], key="type_ferie")
    if st.button("Ajouter", disabled=hors_ligne or len(libelle.strip()) < 2, key="ajouter_ferie"):
        reponse = appel(api_client.ajouter_ferie, jour, libelle.strip(), type_fermeture)
        if reponse:
            st.session_state["_succes"] = f"Le {jour:%d/%m/%Y} est désormais fermé : aucun créneau ne sera proposé."
            a_reprogrammer = reponse["rdv_a_reprogrammer"]
            if a_reprogrammer:
                st.session_state["_a_reprogrammer_ferie"] = {
                    "message": f"{len(a_reprogrammer)} rendez-vous prévus ce jour-là sont à reprogrammer (page « Rendez-vous d'un patient »).",
                    "lignes": [
                        {"Identifiant": r["id"], "Heure": format_heure(r["heure_rdv"]), "Patient": r["patient_nom"], "Médecin": r["medecin_nom"]}
                        for r in a_reprogrammer
                    ],
                }
            st.rerun()

    st.subheader("Retirer un jour")
    retirables = [f for f in feries if date.fromisoformat(f["jour"]) >= aujourdhui()]
    if not retirables:
        st.caption("Aucun jour à venir à retirer pour cette année.")
        return
    retire = st.selectbox(
        "Jour à retirer",
        [f["jour"] for f in retirables],
        format_func=lambda j: f"{format_date(j)} - {next(f['libelle'] for f in retirables if f['jour'] == j)}",
        key="ferie_retire",
    )
    if st.button("Retirer ce jour", disabled=hors_ligne, key="retirer_ferie"):
        try:
            api_client.retirer_ferie(date.fromisoformat(retire))
        except ErreurApi as erreur:
            afficher_erreur(erreur)
        else:
            st.session_state["_succes"] = f"Le {format_date(retire)} n'est plus fermé."
            st.rerun()


def page_specialites_horaires(listes: dict, hors_ligne: bool, role: str) -> None:
    st.header("Spécialités et horaires")
    afficher_message_en_attente()
    onglets = st.tabs(["Spécialités", "Horaires des médecins", "Jours fériés et fermetures"])
    with onglets[0]:
        onglet_specialites(role, hors_ligne)
    with onglets[1]:
        onglet_horaires(listes, hors_ligne)
    with onglets[2]:
        onglet_jours_fermes(hors_ligne)


def page_indisponibilites(listes: dict, hors_ligne: bool, utilisateur: dict) -> None:
    """EF-14 : un médecin déclare ses absences ; les créneaux concernés ne sont plus proposés."""
    st.header("Indisponibilités des médecins" if utilisateur["role"] != "Médecin" else "Mes indisponibilités")
    afficher_message_en_attente()
    medecins = {m["id"]: m for m in listes["medecins"]}
    if utilisateur["role"] == "Médecin":
        medecin_id = utilisateur["medecin_id"]
        medecins.setdefault(medecin_id, {"id": medecin_id, "nom": utilisateur["nom_affichage"], "specialite": ""})
    else:
        if not medecins:
            st.info("Aucun médecin n'est enregistré.")
            return
        medecin_id = st.selectbox(
            "Médecin", list(medecins), format_func=lambda i: libelle_medecin(medecins[i]), key="medecin_indispo"
        )

    resultat = st.session_state.pop("_a_reprogrammer_indispo", None)
    if resultat:
        st.warning(resultat["message"])
        tableau(resultat["lignes"])

    indispos = appel(api_client.lister_indisponibilites, medecin_id)
    if indispos is None:
        return
    st.subheader("Indisponibilités à venir")
    tableau(
        [
            {
                "N°": i["id"],
                "Début": format_horodatage(i["debut"]),
                "Fin": format_horodatage(i["fin"]),
                "Motif": i["motif"],
            }
            for i in indispos
        ]
    )
    if indispos:
        indexees = {i["id"]: i for i in indispos}
        numero = st.selectbox(
            "Indisponibilité",
            list(indexees),
            format_func=lambda n: f"n° {n} - {format_horodatage(indexees[n]['debut'])} au {format_horodatage(indexees[n]['fin'])}",
            key="indispo_choisie",
        )
        impactes = appel(api_client.rdv_impactes, numero)
        if impactes:
            st.warning(f"{len(impactes)} rendez-vous restent à reprogrammer pour cette période.")
            tableau(
                [
                    {
                        "Identifiant": r["id"],
                        "Date": format_date(r["date_rdv"]),
                        "Heure": format_heure(r["heure_rdv"]),
                        "Patient": r["patient_nom"],
                        "Statut": r["statut"],
                    }
                    for r in impactes
                ]
            )
            st.caption("Un agent d'accueil les reporte depuis « Rendez-vous d'un patient ».")
        elif impactes is not None:
            st.caption("Aucun rendez-vous n'est touché par cette indisponibilité.")
        if st.button("Lever cette indisponibilité", disabled=hors_ligne, key="lever_indispo"):
            if appel(api_client.lever_indisponibilite, numero):
                st.session_state["_succes"] = f"Indisponibilité n° {numero} levée : les créneaux sont de nouveau proposés."
                st.rerun()

    st.subheader("Déclarer une indisponibilité")
    maintenant = datetime.now(timezone.utc).replace(tzinfo=None)
    colonne_debut, colonne_heure_debut, colonne_fin, colonne_heure_fin = st.columns(4)
    jour_debut = colonne_debut.date_input("Du", value=aujourdhui(), min_value=aujourdhui(), format="DD/MM/YYYY", key="indispo_jour_debut")
    heure_debut = colonne_heure_debut.time_input("À partir de", value=time(8, 0), step=900, key="indispo_heure_debut")
    jour_fin = colonne_fin.date_input("Au", value=aujourdhui(), min_value=aujourdhui(), format="DD/MM/YYYY", key="indispo_jour_fin")
    heure_fin = colonne_heure_fin.time_input("Jusqu'à", value=time(18, 0), step=900, key="indispo_heure_fin")
    motif = st.text_input("Motif (congé, formation, urgence...)", key="indispo_motif", max_chars=200)
    debut, fin = datetime.combine(jour_debut, heure_debut), datetime.combine(jour_fin, heure_fin)
    if fin <= debut:
        st.caption("La fin doit suivre le début.")
    elif fin <= maintenant:
        st.caption("La période doit se terminer dans le futur.")
    valide = fin > debut and fin > maintenant and len(motif.strip()) >= 3
    if st.button("Déclarer l'indisponibilité", disabled=hors_ligne or not valide, key="declarer_indispo"):
        reponse = appel(api_client.declarer_indisponibilite, medecin_id, debut, fin, motif.strip())
        if reponse:
            st.session_state["_succes"] = "Indisponibilité enregistrée : les créneaux concernés ne sont plus proposés."
            a_reprogrammer = reponse["rdv_a_reprogrammer"]
            if a_reprogrammer:
                st.session_state["_a_reprogrammer_indispo"] = {
                    "message": f"{len(a_reprogrammer)} rendez-vous déjà pris sur cette période sont à reprogrammer.",
                    "lignes": [
                        {
                            "Identifiant": r["id"],
                            "Date": format_date(r["date_rdv"]),
                            "Heure": format_heure(r["heure_rdv"]),
                            "Patient": r["patient_nom"],
                        }
                        for r in a_reprogrammer
                    ],
                }
            st.rerun()


# --------------------------------------------------------------------------------------
# Rôle Administrateur : comptes, journal, pointages
# --------------------------------------------------------------------------------------


def page_comptes(listes: dict, hors_ligne: bool) -> None:
    st.header("Comptes")
    afficher_message_en_attente()
    cle_a_afficher = st.session_state.pop("_cle_a_afficher", None)
    if cle_a_afficher:
        st.success(cle_a_afficher["message"])
        st.code(cle_a_afficher["cle"], language=None)
        st.warning("Notez cette clé maintenant : elle ne sera plus jamais affichée.")

    colonne_role, colonne_etat, colonne_recherche = st.columns(3)
    role_filtre = colonne_role.selectbox("Rôle", ["Tous"] + ROLES, key="filtre_role")
    etat_filtre = colonne_etat.selectbox("État", ["Tous", "Actifs", "Désactivés"], key="filtre_etat")
    recherche = colonne_recherche.text_input("Recherche (identifiant ou nom)", key="filtre_recherche", max_chars=60)
    donnees = appel(
        api_client.lister_utilisateurs,
        None if role_filtre == "Tous" else role_filtre,
        {"Tous": None, "Actifs": True, "Désactivés": False}[etat_filtre],
        recherche.strip() or None,
    )
    if donnees is None:
        return
    comptes = donnees["elements"]
    st.caption(f"{donnees['total']} compte(s).")
    tableau(
        [
            {
                "Identifiant": c["id"],
                "Rôle": c["role"],
                "Nom": c["nom_affichage"],
                "État": "Actif" if c["actif"] else "Désactivé",
                "Dernière connexion": format_horodatage(c["derniere_connexion"]) or "Jamais",
            }
            for c in comptes
        ]
    )

    st.subheader("Gérer un compte")
    if not comptes:
        st.caption("Aucun compte ne correspond aux filtres.")
    else:
        indexe = {c["id"]: c for c in comptes}
        identifiant = st.selectbox(
            "Compte",
            list(indexe),
            format_func=lambda i: f"{i} - {indexe[i]['nom_affichage']} ({'actif' if indexe[i]['actif'] else 'désactivé'})",
            key="compte_gere",
        )
        compte = indexe[identifiant]
        retirer = False
        if compte["actif"] and compte["role"] == "Médecin":
            retirer = st.checkbox(
                "Le médecin quitte la clinique : ne plus le proposer pour les rendez-vous", key="retirer_du_planning"
            )
        colonne_cle, colonne_action, _ = st.columns([2, 2, 3])
        if colonne_cle.button("Régénérer la clé", disabled=hors_ligne, key="regenerer_cle"):
            reponse = appel(api_client.regenerer_cle, identifiant)
            if reponse:
                st.session_state["_cle_a_afficher"] = {
                    "message": f"Nouvelle clé du compte {identifiant}. L'ancienne ne fonctionne plus.",
                    "cle": reponse["cle"],
                }
                st.rerun()
        if compte["actif"]:
            if colonne_action.button("Désactiver le compte", disabled=hors_ligne, key="desactiver_compte"):
                reponse = appel(api_client.desactiver_utilisateur, identifiant, retirer)
                if reponse:
                    st.session_state["_succes"] = f"Compte {identifiant} désactivé : il ne peut plus se connecter."
                    if reponse["rdv_a_venir"]:
                        st.session_state["_alerte"] = (
                            f"Ce médecin a encore {reponse['rdv_a_venir']} rendez-vous à venir : "
                            "ils ne sont pas reprogrammés automatiquement."
                        )
                    st.rerun()
        elif colonne_action.button("Réactiver le compte", disabled=hors_ligne, key="reactiver_compte"):
            if appel(api_client.reactiver_utilisateur, identifiant):
                st.session_state["_succes"] = f"Compte {identifiant} réactivé."
                st.rerun()

    st.subheader("Créer un compte")
    role_nouveau = st.selectbox("Rôle du nouveau compte", ROLES, key="role_nouveau_compte")
    nom_affichage = nom_medecin = specialite = None
    if role_nouveau == "Médecin":
        colonne_nom, colonne_specialite = st.columns(2)
        nom_medecin = colonne_nom.text_input("Nom du médecin", key="nouveau_nom_medecin", max_chars=120)
        liste_specialites = appel(api_client.lister_specialites, True)
        noms_specialites = [x["nom"] for x in liste_specialites] if liste_specialites else []
        if noms_specialites:
            specialite = colonne_specialite.selectbox("Spécialité", noms_specialites, key="nouvelle_specialite")
            st.caption(
                "La durée de consultation et les horaires par défaut viennent de la spécialité ; "
                "ils se modifient dans « Spécialités et horaires »."
            )
        else:
            st.warning("Aucune spécialité disponible : créez-en une dans « Spécialités et horaires ».")
    else:
        nom_affichage = st.text_input("Nom affiché (facultatif)", key="nouveau_nom_affiche", max_chars=120)
    if st.button(
        "Créer le compte", disabled=hors_ligne or (role_nouveau == "Médecin" and not specialite), key="creer_compte"
    ):
        reponse = appel(api_client.creer_utilisateur, role_nouveau, nom_affichage, nom_medecin, specialite)
        if reponse:
            st.session_state["_cle_a_afficher"] = {
                "message": f"Compte {reponse['utilisateur']['id']} créé ({role_nouveau}).",
                "cle": reponse["cle"],
            }
            st.rerun()


def page_journal() -> None:
    st.header("Journal des actions")
    jour_courant = aujourdhui()
    colonne_periode, colonne_action, colonne_employe = st.columns(3)
    periode = colonne_periode.date_input(
        "Période",
        value=(jour_courant - timedelta(days=7), jour_courant),
        format="DD/MM/YYYY",
        key="periode_journal",
    )
    action = colonne_action.selectbox(
        "Action",
        ["Toutes"] + list(LIBELLES_ACTIONS),
        format_func=lambda a: a if a == "Toutes" else LIBELLES_ACTIONS[a],
        key="action_journal",
    )
    employe = colonne_employe.text_input("Employé (identifiant)", key="employe_journal", max_chars=12)
    if not isinstance(periode, (tuple, list)) or len(periode) != 2:
        st.info("Sélectionnez une date de début et une date de fin.")
        return
    donnees = appel(
        api_client.consulter_journal,
        employe.strip() or None,
        None if action == "Toutes" else action,
        periode[0],
        periode[1],
        200,
    )
    if donnees is None:
        return
    entrees = donnees["elements"]
    suffixe = f" : les {len(entrees)} plus récentes sont affichées" if donnees["total"] > len(entrees) else ""
    st.caption(f"{donnees['total']} entrée(s){suffixe}.")
    tableau(
        [
            {
                "Date et heure": format_horodatage(e["horodatage"]),
                "Employé": e["utilisateur_id"] or "Système",
                "Rôle": e["role"] or "",
                "Action": LIBELLES_ACTIONS.get(e["action"], e["action"]),
                "Cible": e["cible"] or "",
                "Détail": e["detail"] or "",
            }
            for e in entrees
        ]
    )


def page_pointages_admin(hors_ligne: bool) -> None:
    st.header("Pointages")
    afficher_message_en_attente()
    jour_courant = aujourdhui()
    colonne_periode, colonne_employe = st.columns(2)
    periode = colonne_periode.date_input(
        "Période", value=(jour_courant, jour_courant), format="DD/MM/YYYY", key="periode_pointages"
    )
    employe = colonne_employe.text_input("Employé (identifiant)", key="employe_pointages", max_chars=12)
    if not isinstance(periode, (tuple, list)) or len(periode) != 2:
        st.info("Sélectionnez une date de début et une date de fin.")
        return
    donnees = appel(api_client.lister_pointages, periode[0], periode[1], employe.strip() or None)
    if donnees is None:
        return
    pointages = donnees["elements"]
    st.caption(f"{donnees['total']} pointage(s).")

    def duree(p: dict) -> str:
        if p["duree_minutes"] is None:
            return "Départ non pointé"
        return f"{p['duree_minutes'] // 60} h {p['duree_minutes'] % 60:02d}"

    tableau(
        [
            {
                "Employé": p["utilisateur_id"],
                "Nom": p["nom_affichage"],
                "Rôle": p["role"],
                "Jour": format_date(p["jour"]),
                "Arrivée": heure_de_horodatage(p["arrivee"]),
                "Départ": heure_de_horodatage(p["depart"]) or "-",
                "Durée": duree(p),
                "Corrigé": "Oui" if p["corrige"] else "",
            }
            for p in pointages
        ]
    )

    st.subheader("Corriger un pointage")
    if not pointages:
        st.caption("Aucun pointage à corriger sur cette période.")
        return
    indexe = {p["id"]: p for p in pointages}
    pointage_id = st.selectbox(
        "Pointage",
        list(indexe),
        format_func=lambda i: f"{indexe[i]['utilisateur_id']} - {indexe[i]['nom_affichage']} - {format_date(indexe[i]['jour'])}",
        key="pointage_corrige",
    )
    pointage = indexe[pointage_id]
    arrivee_actuelle = datetime.fromisoformat(pointage["arrivee"]).time().replace(second=0, microsecond=0)
    depart_actuel = (
        datetime.fromisoformat(pointage["depart"]).time().replace(second=0, microsecond=0)
        if pointage["depart"]
        else arrivee_actuelle
    )
    champ = st.radio("Heure à corriger", ["Arrivée", "Départ"], horizontal=True, key=f"champ_pointage_{pointage_id}")
    nouvelle_heure = st.time_input(
        f"Nouvelle heure ({champ.lower()})",
        value=arrivee_actuelle if champ == "Arrivée" else depart_actuel,
        step=60,
        key=f"heure_pointage_{pointage_id}_{champ}",
    )
    if st.button("Corriger le pointage", disabled=hors_ligne, key="corriger_pointage"):
        arguments = {"arrivee": nouvelle_heure} if champ == "Arrivée" else {"depart": nouvelle_heure}
        if appel(api_client.corriger_pointage, pointage_id, **arguments):
            st.session_state["_succes"] = (
                f"Pointage de {pointage['utilisateur_id']} corrigé : {champ.lower()} à {nouvelle_heure:%H:%M}. "
                "La correction est tracée dans le journal."
            )
            st.rerun()


# --------------------------------------------------------------------------------------
# Point d'entrée
# --------------------------------------------------------------------------------------


def main() -> None:
    utilisateur = st.session_state.get("utilisateur")
    if not utilisateur or not st.session_state.get("jeton"):
        api_client.definir_jeton(None)
        page_connexion()
        return
    api_client.definir_jeton(st.session_state["jeton"])

    listes, hors_ligne = charger_referentiels(forcer=st.session_state.pop("_actualiser", False))
    if listes is None:
        afficher_logo(320)
        st.subheader(NOM_CLINIQUE)
        st.info(
            "Aucune donnée n'a pu être chargée. Vérifiez que l'API est démarrée "
            "(commande : uvicorn backend.main:app), puis réessayez."
        )
        if st.button("Réessayer", key="reessayer"):
            st.rerun()
        st.stop()

    role = utilisateur["role"]
    with st.sidebar:
        afficher_logo(210)
        st.caption(f"{NOM_CLINIQUE} - Gestion des rendez-vous")
        st.markdown(f"**{utilisateur['nom_affichage']}**")
        st.caption(f"{role} - {utilisateur['id']}")
        page = st.radio("Menu", MENUS[role], key=f"menu_{role}")
        bloc_pointage(hors_ligne)
        bloc_mon_compte(hors_ligne)
        if st.button("Actualiser les listes", key="actualiser"):
            st.session_state["_actualiser"] = True
            st.rerun()
        if st.button("Se déconnecter", key="deconnexion"):
            fermer_session("Vous êtes déconnecté.")
            st.rerun()

    if not listes["medecins"]:
        st.warning(
            "Aucun médecin n'est enregistré : la base ne contient pas encore les données. Importez-les avec "
            "« python -m backend.seed_data », créez les comptes avec « python -m backend.creer_comptes », puis "
            "cliquez sur « Actualiser les listes »."
        )

    if page == "Nouveau rendez-vous":
        page_nouveau_rdv(listes, hors_ligne)
    elif page == "Rendez-vous d'un patient":
        page_rendez_vous_patient(hors_ligne)
    elif page == "Mon planning":
        medecin = next(
            (m for m in listes["medecins"] if m["id"] == utilisateur["medecin_id"]),
            {"id": utilisateur["medecin_id"], "nom": utilisateur["nom_affichage"], "specialite": "spécialité non listée"},
        )
        page_planning_medecin(medecin, hors_ligne)
    elif page == "Plannings des médecins":
        page_plannings_medecins(listes, hors_ligne)
    elif page == "Spécialités et horaires":
        page_specialites_horaires(listes, hors_ligne, role)
    elif page in ("Indisponibilités", "Mes indisponibilités"):
        page_indisponibilites(listes, hors_ligne, utilisateur)
    elif page == "Rendez-vous à relancer":
        page_a_relancer(listes, role)
    elif page == "Tableau de bord":
        page_tableau_de_bord(listes, complet=True)
    elif page == "Synthèse mensuelle":
        page_tableau_de_bord(listes, complet=False)
    elif page == "Fiche patient":
        page_fiche_patient(hors_ligne)
    elif page == "Doublons de patients":
        page_doublons(role, peut_corriger=role in ("Agent d'accueil", "Administrateur"))
    elif page == "Qualité des données":
        page_qualite()
    elif page == "Comptes":
        page_comptes(listes, hors_ligne)
    elif page == "Journal des actions":
        page_journal()
    elif page == "Pointages":
        page_pointages_admin(hors_ligne)


main()
