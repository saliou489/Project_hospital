"""L7 : guide utilisateur par rôle (PDF illustré)."""
from pathlib import Path

from reportlab.lib.units import cm
from reportlab.platypus import PageBreak

from _pdf import Document, couverture, encadre, figure, p, puces, tableau

CAPT = Path(__file__).parent / "captures"


def f(nom: str, legende: str, hauteur_max: float = 17 * cm):
    return figure(CAPT / f"{nom}.png", legende, hauteur_max=hauteur_max)


def construire(sortie: Path) -> None:
    doc = Document(sortie, "Guide utilisateur - MediRDV", "Guide utilisateur (L7)", "Guide par rôle")
    el = couverture("Guide utilisateur", "Application MédiRDV - Clinique Les Baobabs",
                    "Appel d'offres AO-CLB-2026-01 - Livrable L7<br/>Un chapitre par rôle : agent d'accueil, médecin, "
                    "responsable des opérations, direction, administrateur")

    # ------------------------------------------------------------------ Démarrer
    el += [p("Avant de commencer", "h1"),
           p("MédiRDV est l'application de gestion des rendez-vous de la clinique. Elle s'utilise dans un navigateur "
             "(ordinateur, tablette ou téléphone). Chaque employé dispose d'un compte personnel ; ce qu'il voit dépend de "
             "son rôle."),
           tableau(["Rôle", "Ce que vous faites", "Menus disponibles"],
                   [["Agent d'accueil", "Prendre, confirmer, reporter et annuler des rendez-vous ; gérer les fiches patients.",
                     "Nouveau rendez-vous, Rendez-vous d'un patient, Rendez-vous à relancer, Fiche patient, Doublons de patients"],
                    ["Médecin", "Consulter votre planning, clôturer vos rendez-vous, déclarer vos indisponibilités.",
                     "Mon planning, Mes indisponibilités"],
                    ["Responsable des opérations", "Suivre l'activité, ajuster horaires et spécialités, exporter les indicateurs.",
                     "Tableau de bord, Rendez-vous à relancer, Plannings des médecins, Spécialités et horaires, "
                     "Indisponibilités, Doublons de patients, Qualité des données"],
                    ["Direction", "Consulter la synthèse mensuelle (lecture seule).", "Synthèse mensuelle"],
                    ["Administrateur", "Gérer les comptes, consulter le journal et les pointages ; accès à tout le reste.",
                     "Comptes, Journal des actions, Pointages et les menus du responsable, plus Fiche patient"]],
                   [3.5 * cm, 6 * cm, 7.5 * cm]),
           p("Se connecter", "h2"),
           ] + puces([
               "Ouvrez l'adresse de l'application que vous a donnée l'administrateur.",
               "Choisissez votre <b>rôle</b>, saisissez votre <b>identifiant</b> (par exemple AGT01) et votre <b>clé personnelle</b>.",
               "Cliquez sur « Se connecter » (ou appuyez sur Entrée).",
           ]) + [
           f("connexion", "Écran de connexion."),
           encadre("<b>Votre clé est personnelle.</b> Ne la communiquez à personne. Après 5 essais erronés, le compte est "
                   "verrouillé pendant 5 minutes. Votre session reste ouverte 8 heures, puis il faut vous reconnecter."),
           p("Changer sa clé", "h3"),
           p("Dans le menu de gauche, ouvrez « Mon compte », saisissez la clé actuelle puis la nouvelle (10 caractères au "
             "minimum) et cliquez sur « Changer ma clé ». Les autres sessions ouvertes avec l'ancienne clé sont fermées."),
           p("Pointer son arrivée et son départ", "h3"),
           p("Le bloc « Pointage » du menu de gauche permet d'enregistrer votre arrivée (« Pointer mon arrivée ») et votre "
             "départ (« Pointer mon départ »). En cas d'oubli, l'administrateur peut corriger le pointage."),
           p("Se déconnecter", "h3"),
           p("Cliquez sur « Se déconnecter » en bas du menu de gauche, surtout sur un poste partagé."),
           p("Sur tablette et téléphone", "h3"),
           p("L'affichage s'adapte à la taille de l'écran. Sur téléphone, le menu est replié : touchez la flèche en haut à "
             "gauche de l'écran pour l'ouvrir."),
           f("telephone_agent", "Affichage sur téléphone : le menu se replie derrière la flèche en haut à gauche.", 11 * cm),
           PageBreak()]

    # ------------------------------------------------------------------ Agent
    el += [p("Chapitre 1 - Agent d'accueil", "h1"),
           p("Prendre un rendez-vous", "h2"),
           p("Menu « Nouveau rendez-vous ». Comptez quatre clics pour un rendez-vous standard : le champ de recherche, la "
             "spécialité (deux clics) puis le bouton d'enregistrement.")] + puces([
               "<b>1. Patient.</b> Saisissez au moins 2 caractères du nom, du prénom, du téléphone ou de l'identifiant. Le "
               "premier résultat est sélectionné ; choisissez-en un autre dans la liste si besoin. Si le patient n'existe "
               "pas, ouvrez « Créer une fiche patient ».",
               "<b>2. Rendez-vous.</b> Choisissez la spécialité. Par défaut, les 8 premiers créneaux libres sont proposés ; "
               "vous pouvez aussi « Choisir un jour ». Le médecin est attribué selon la disponibilité.",
               "Indiquez si vous le souhaitez le canal (téléphone, guichet, recommandation d'un médecin), puis cliquez sur "
               "« Enregistrer le rendez-vous ».",
           ]) + [
           f("agent_nouveau_rdv", "Nouveau rendez-vous : patient, spécialité, créneaux disponibles.", 15 * cm),
           encadre("<b>Messages que vous pouvez rencontrer.</b> « Conflit d'agenda » : le médecin a déjà un rendez-vous à "
                   "cette heure (quelqu'un vient de prendre le créneau), choisissez-en un autre. « Conflit patient » : le "
                   "patient a déjà un rendez-vous qui se chevauche. Aucun rendez-vous dans le passé ni à plus de 90 jours. Si le patient a cumulé 3 absences en 6 mois, un "
                   "avertissement s'affiche : il n'empêche pas l'enregistrement."),
           p("Créer une fiche patient", "h3"),
           p("Renseignez nom, prénom, date de naissance, téléphone et sexe. Si une fiche identique existe déjà (mêmes nom, "
             "prénom et date de naissance), l'application vous le signale avant de créer un doublon."),
           PageBreak(),
           p("Confirmer, reporter ou annuler", "h2"),
           p("Menu « Rendez-vous d'un patient » : recherchez le patient, puis sous « Modifier un rendez-vous » choisissez le "
             "rendez-vous et l'action."),
           ] + puces([
               "<b>Confirmer</b> : le patient a confirmé sa venue lors de l'appel de rappel.",
               "<b>Reporter</b> : choisissez un nouveau créneau libre ; l'ancien est libéré.",
               "<b>Annuler</b> : le motif est obligatoire (patient, médecin, clinique, autre). Le créneau est libéré "
               "immédiatement.",
           ]) + [
           f("agent_rdv_patient", "Rendez-vous d'un patient : liste et modification.", 12 * cm),
           p("L'historique de chaque modification (qui, quand, avant et après) est consultable sous « Historique des "
             "modifications d'un rendez-vous ». Aucun rendez-vous n'est jamais supprimé."),
           p("Rendez-vous à relancer", "h2"),
           p("Cette liste classe les rendez-vous à venir selon leur risque d'absence. Appelez en priorité les patients du "
             "haut de la liste pour confirmer leur venue. Les facteurs qui expliquent chaque score sont indiqués."),
           f("agent_a_relancer", "Liste des rendez-vous à relancer.", 11 * cm),
           p("Fiche patient et doublons", "h2"),
           p("« Fiche patient » permet de rechercher et corriger les coordonnées. « Doublons de patients » liste les fiches "
             "qui semblent concerner la même personne ; la fusion n'est pas automatique : signalez-les à l'administrateur."),
           PageBreak()]

    # ------------------------------------------------------------------ Médecin
    el += [p("Chapitre 2 - Médecin", "h1"),
           p("Consulter son planning", "h2"),
           p("Menu « Mon planning ». Choisissez la vue :")] + puces([
               "<b>Jour</b> : les rendez-vous d'une journée.",
               "<b>Semaine</b> : une grille avec une ligne par heure et une colonne par jour.",
               "<b>Liste</b> : tous les rendez-vous d'une période de votre choix.",
           ]) + [
           f("medecin_planning", "Mon planning, vue Semaine.", 9 * cm),
           p("Clôturer un rendez-vous", "h3"),
           p("En bas de page, choisissez le rendez-vous du jour (ou antérieur) puis cliquez sur « Marquer honoré » ou "
             "« Marquer absent ». Un rendez-vous non marqué passe automatiquement en « Absent » une heure après son début. "
             "Seul le médecin concerné, un responsable ou un administrateur peut clôturer un rendez-vous."),
           p("Déclarer une indisponibilité", "h2"),
           p("Menu « Mes indisponibilités » : indiquez la date de début, la date de fin et le motif, puis cliquez sur "
             "« Déclarer l'indisponibilité ». Plus aucun créneau n'est proposé sur la période. Les rendez-vous déjà pris sont "
             "listés : l'accueil les reprogrammera. « Lever cette indisponibilité » annule la déclaration."),
           f("medecin_indisponibilites", "Mes indisponibilités.", 12 * cm),
           PageBreak()]

    # ------------------------------------------------------------------ Responsable
    el += [p("Chapitre 3 - Responsable des opérations", "h1"),
           p("Tableau de bord", "h2"),
           p("En haut, les indicateurs du jour : rendez-vous prévus, à venir, honorés, absents et taux d'occupation. En "
             "dessous, choisissez une analyse :")] + puces([
               "<b>Absences</b> : taux d'absence par spécialité et évolution mensuelle.",
               "<b>Demande</b> : carte de chaleur des rendez-vous par jour de la semaine et par heure ; repérez les pics du "
               "lundi et du samedi matin.",
               "<b>Annulations</b> : taux d'annulation et répartition par motif.",
               "<b>Prévision</b> : volume hebdomadaire attendu sur 4 semaines, avec l'erreur de la méthode retenue.",
               "<b>Risque d'absence</b> : qualité du modèle de score et seuil de relance.",
           ]) + [
           f("resp_vue_absences", "Tableau de bord, vue Absences.", 17 * cm),
           p("Filtres et export", "h3"),
           p("Les filtres de période, de spécialité et de médecin s'appliquent à tous les graphiques. « Exporter ces "
             "indicateurs » prépare un fichier CSV (ouvrable dans Excel) ou PDF : choisissez le format, cliquez sur "
             "« Préparer le fichier », puis sur le bouton de téléchargement. Les taux d'absence excluent les annulations."),
           f("resp_vue_demande", "Vue Demande : carte de chaleur.", 17 * cm),
           f("resp_vue_prevision", "Vue Prévision.", 17 * cm),
           f("resp_vue_risque", "Vue Risque d'absence.", 17 * cm),
           encadre("<b>Lire les prévisions et le score avec prudence.</b> Ce sont des aides à la décision calculées sur "
                   "l'historique. La page « Limites de cette analyse » indique la qualité réelle du modèle : un score "
                   "modéré ne désigne pas avec certitude les absents, il aide à choisir qui appeler en priorité."),
           PageBreak(),
           p("Plannings des médecins, horaires et spécialités", "h2"),
           p("« Plannings des médecins » : filtrez par spécialité puis choisissez un médecin ; mêmes vues Jour, Semaine et "
             "Liste que pour le médecin."),
           f("resp_plannings", "Plannings des médecins.", 10 * cm),
           p("« Spécialités et horaires » : créez ou modifiez une spécialité (durée de consultation de 15, 20 ou 30 minutes, "
             "couleur), modifiez les jours et plages horaires d'un médecin (« Enregistrer les horaires »), ajoutez ou retirez "
             "un jour férié ou une fermeture exceptionnelle. Si une modification touche des rendez-vous existants, ils sont "
             "listés pour être reprogrammés."),
           f("resp_specialites", "Spécialités et horaires.", 10 * cm),
           p("Indisponibilités, doublons et qualité des données", "h3"),
           p("Vous pouvez déclarer les indisponibilités de n'importe quel médecin (même fonctionnement que pour le médecin). "
             "« Qualité des données » résume les contrôles automatiques sur les données (valeurs manquantes, incohérences) ; "
             "« Doublons de patients » liste les fiches probablement en double."),
           f("resp_qualite", "Qualité des données.", 9 * cm),
           PageBreak()]

    # ------------------------------------------------------------------ Direction
    el += [p("Chapitre 4 - Direction", "h1"),
           p("« Synthèse mensuelle » est l'unique menu : les indicateurs du jour et les analyses du tableau de bord, en lecture "
             "seule, sur la période de votre choix. Vous ne pouvez rien modifier."),
           f("direction_synthese", "Synthèse mensuelle.", 17 * cm),
           PageBreak()]

    # ------------------------------------------------------------------ Admin
    el += [p("Chapitre 5 - Administrateur", "h1"),
           p("Comptes", "h2"),
           p("Menu « Comptes ». Pour créer un compte, indiquez le rôle et le nom puis validez ; la <b>clé personnelle est "
             "affichée une seule fois</b> : transmettez-la à l'employé en main propre. Sous « Gérer un compte », vous pouvez "
             "désactiver ou réactiver un compte, et générer une nouvelle clé en cas d'oubli."),
           f("admin_comptes", "Gestion des comptes.", 15 * cm),
           encadre("<b>Fichiers de clés.</b> Quand les comptes sont créés en masse, un fichier « cles_a_distribuer » est "
                   "produit. Il contient les clés en clair : distribuez-les puis <b>supprimez ce fichier</b>. Ne le "
                   "publiez, ni ne l'envoyez par messagerie."),
           p("Journal des actions", "h2"),
           p("Qui a créé, modifié ou annulé quoi, et quand. Filtrez par période, type d'action ou employé (identifiant). Le journal ne "
             "contient aucun nom de patient, uniquement des identifiants."),
           f("admin_journal", "Journal des actions.", 9 * cm),
           p("Pointages", "h2"),
           p("Consultez les arrivées et départs du personnel et corrigez un pointage oublié (« Corriger le pointage »). La "
             "correction est tracée."),
           f("admin_pointages", "Pointages.", 7 * cm),
           p("Sauvegarde et conservation", "h2"),
           p("Une sauvegarde est faite chaque nuit et conservée 14 jours ; les procédures de vérification, de restauration "
             "et de conservation des données (anonymisation après 5 ans) sont décrites dans le document « Sauvegarde et "
             "conservation » du dépôt. Les sauvegardes contiennent des données personnelles : gardez-les sur un support protégé."),
           PageBreak(),
           p("En cas de problème", "h1"),
           tableau(["Message ou situation", "Que faire"],
                   [["« Session expirée »", "Reconnectez-vous : votre session dure 8 heures."],
                    ["« Compte verrouillé »", "Attendez 5 minutes, ou demandez à l'administrateur de générer une nouvelle clé."],
                    ["« Conflit d'agenda » ou « Conflit patient »", "Le créneau vient d'être pris, ou le patient a déjà un rendez-vous à cette heure : choisissez un autre créneau."],
                    ["« Votre rôle ne permet pas cette action »", "Demandez à un responsable ou à l'administrateur."],
                    ["« Service momentanément indisponible »", "Le serveur est injoignable : réessayez dans quelques instants et prévenez l'administrateur si cela persiste. Les listes déjà chargées restent affichées avec leur heure de mise à jour."],
                    ["Listes qui ne se mettent pas à jour", "Cliquez sur « Actualiser les listes » dans le menu de gauche."]],
                   [6 * cm, 11 * cm]),
           p("Statuts d'un rendez-vous", "h2"),
           tableau(["Statut", "Signification"],
                   [["Planifié", "Créé par l'accueil."], ["Confirmé", "Le patient a confirmé sa venue."],
                    ["Honoré", "Le patient est venu (marqué par le médecin ou un responsable)."],
                    ["Absent", "Le patient n'est pas venu (marqué, ou automatique une heure après le début)."],
                    ["Annulé", "Annulé avec motif depuis Planifié ou Confirmé ; le créneau est libre."]],
                   [4 * cm, 13 * cm])]
    doc.build(el)
