"""L9 : cahier de recette et modèle de procès-verbal (PDF)."""
from pathlib import Path

from reportlab.lib.units import cm
from reportlab.platypus import NextPageTemplate, PageBreak, Spacer

from _pdf import Document, couverture, p, puces, tableau
from contenu_recette import CAS


def construire(sortie: Path, suite_tests: str) -> None:
    doc = Document(sortie, "Cahier de recette et proces-verbal - MediRDV", "Cahier de recette (L9)", "Recette")
    el = couverture("Cahier de recette\net procès-verbal", "Application MédiRDV - Clinique Les Baobabs",
                    "Appel d'offres AO-CLB-2026-01 - Livrable L9<br/>Document à compléter et à signer par la clinique")

    el += [p("1. Objet et déroulement", "h1"),
           p("Ce document est le cahier de recette de l'application MédiRDV. La clinique le déroule sur l'environnement de "
             "recette, installé à partir du seul README (cahier des charges, section 12.3). Chaque cas de test précise "
             "l'exigence vérifiée, le rôle à utiliser, les étapes et le résultat attendu. La clinique renseigne la colonne "
             "« Résultat » et, le cas échéant, le numéro d'anomalie."),
           p("<b>Couverture.</b> Le cahier compte %d cas, dont %d sur des exigences P1 : référentiels, planning et rendez-vous "
             "(règles RG-01 à RG-11), tableau de bord, authentification et comptes, affichage tablette et téléphone, "
             "déploiement. Les exigences P2 les plus visibles (prévision, score de risque, export, sauvegarde) sont "
             "également couvertes." % (len(CAS), sum(1 for c in CAS if c[2] == "P1"))),
           p("2. Classement des anomalies", "h2"),
           tableau(["Classe", "Définition", "Conséquence"],
                   [["Bloquante", "Une exigence P1 n'est pas remplie.", "Recette refusée tant qu'elle subsiste."],
                    ["Majeure", "Une exigence P2 n'est pas remplie, ou contournement lourd.", "Recette refusée tant qu'elle subsiste."],
                    ["Mineure", "Gêne sans impact fonctionnel.", "Corrigée pendant la garantie de 3 mois."]],
                   [3 * cm, 8 * cm, 6 * cm]),
           Spacer(1, 6),
           p("La recette est prononcée si aucune anomalie bloquante ni majeure ne subsiste. Le titulaire dispose de 5 jours "
             "ouvrés pour corriger les anomalies relevées à chaque cycle, dans la limite de deux cycles."),
           p("3. Préparation de l'environnement", "h2")]
    el += puces([
        "Installer l'application avec la procédure du README (une seule commande Docker Compose).",
        "Charger les données de démonstration et créer les comptes de test ; relever les clés distribuées, puis supprimer "
        "le fichier de clés une fois distribuées (il ne doit jamais être publié).",
        "Disposer d'au moins un compte par rôle : agent d'accueil, médecin, responsable des opérations, direction, administrateur.",
        "Prévoir une tablette (ou une fenêtre de navigateur réduite) et un téléphone pour le cas CR-43.",
    ])
    el += [p("4. Preuves complémentaires fournies par le titulaire", "h2"),
           p("En complément de la recette manuelle, la suite de tests automatisés (livrable L5) vérifie les mêmes règles à "
             "chaque modification du code. Résultat du dernier passage : <b>%s</b>. La colonne « Test automatique » du "
             "cahier indique le fichier de test correspondant. Ces tests ne remplacent pas la vérification par la clinique." % suite_tests),
           NextPageTemplate("paysage"), PageBreak(),
           p("5. Cas de test", "h1")]

    largeurs = [1.4 * cm, 2.2 * cm, 3.0 * cm, 7.6 * cm, 6.0 * cm, 2.6 * cm, 1.9 * cm, 1.9 * cm]
    lignes = []
    for ident, exig, prio, role, objet, etapes, attendu, auto in CAS:
        lignes.append([ident, f"{exig}<br/>({prio})", role, f"<b>{objet}.</b> {etapes}", attendu, auto, "", ""])
    el.append(tableau(["N°", "Exigence", "Rôle", "Objet et étapes", "Résultat attendu", "Test automatique", "Résultat (OK/KO)",
                       "Anomalie n°"], lignes, largeurs))

    el += [NextPageTemplate("page"), PageBreak(), p("6. Registre des anomalies", "h1"),
           p("À remplir par la clinique pour chaque cas non conforme.")]
    el.append(tableau(["Anomalie n°", "Cas", "Description", "Classe (bloquante, majeure, mineure)", "Corrigée le"],
                      [["<br/><br/>", "", "", "", ""] for _ in range(8)], [2.3 * cm, 1.7 * cm, 7 * cm, 4 * cm, 2 * cm], zebre=False))
    el += [Spacer(1, 12), p("7. Procès-verbal de recette", "h1"),
           p("Je soussigné(e), représentant la Clinique Les Baobabs, atteste avoir déroulé le cahier de recette de "
             "l'application MédiRDV (référence AO-CLB-2026-01) du ........................ au ........................ "),
           tableau(["Synthèse", "Nombre"],
                   [["Cas déroulés", ""], ["Cas conformes", ""], ["Anomalies bloquantes restantes", ""],
                    ["Anomalies majeures restantes", ""], ["Anomalies mineures restantes (à corriger sous garantie)", ""]],
                   [11 * cm, 4 * cm], zebre=False),
           Spacer(1, 8),
           p("Décision :   [   ] Recette prononcée      [   ] Recette prononcée avec réserves mineures      "
             "[   ] Recette non prononcée (nouveau cycle)"),
           p("Réserves éventuelles : ................................................................................................"
             "................................................................................................................"),
           Spacer(1, 10),
           tableau(["Pour la Clinique Les Baobabs", "Pour le titulaire"],
                   [["Nom, fonction :<br/><br/>Date :<br/><br/>Signature :<br/><br/><br/>", "Nom, fonction :<br/><br/>Date :<br/><br/>Signature :<br/><br/><br/>"]],
                   [8.5 * cm, 8.5 * cm], zebre=False)]
    doc.build(el)
