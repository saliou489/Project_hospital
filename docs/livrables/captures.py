"""Génère les captures d'écran de l'interface utilisées dans les guides (L1, L7).

Prérequis : l'API (port 8765) et l'interface (port 8598) tournent sur une base de démonstration, et le fichier de clés
``cles_a_distribuer_*.csv`` correspondant est passé en argument. Les captures ne contiennent que des données synthétiques.

    python docs/livrables/captures.py chemin/vers/cles_a_distribuer.csv
"""
import csv
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8598"
SORTIE = Path(__file__).parent / "captures"
LIBELLE_ROLE = {"AGT": "Agent d'accueil", "MED": "Médecin", "OPS": "Responsable des opérations",
                "DIR": "Direction", "ADM": "Administrateur"}
# (identifiant, [(menu, nom de fichier, action facultative)])
PARCOURS = {
    "AGT01": [("Nouveau rendez-vous", "agent_nouveau_rdv", "recherche"), ("Rendez-vous d'un patient", "agent_rdv_patient", "recherche_patient"),
              ("Rendez-vous à relancer", "agent_a_relancer", None), ("Fiche patient", "agent_fiche_patient", None),
              ("Doublons de patients", "agent_doublons", None)],
    "MED001": [("Mon planning", "medecin_planning", "semaine"), ("Mes indisponibilités", "medecin_indisponibilites", None)],
    "OPS01": [("Tableau de bord", "resp_tableau_de_bord", None), ("Plannings des médecins", "resp_plannings", "semaine"),
              ("Spécialités et horaires", "resp_specialites", None), ("Indisponibilités", "resp_indisponibilites", None),
              ("Qualité des données", "resp_qualite", None)],
    "DIR01": [("Synthèse mensuelle", "direction_synthese", None)],
    "ADM01": [("Comptes", "admin_comptes", None), ("Journal des actions", "admin_journal", None), ("Pointages", "admin_pointages", None)],
}
VUES = [("Demande", "resp_vue_demande"), ("Annulations", "resp_vue_annulations"), ("Prévision", "resp_vue_prevision"),
        ("Risque d'absence", "resp_vue_risque")]


def recadrer(chemin: Path) -> None:
    """Retire le vide sous le contenu (la fenêtre de capture est plus haute que la plupart des pages)."""
    from PIL import Image, ImageChops

    image = Image.open(chemin).convert("RGB")
    if image.width < 800:
        return
    zone = image.crop((340, 0, image.width, image.height))  # on ignore le menu latéral, gris sur toute la hauteur
    fond = Image.new("RGB", zone.size, zone.getpixel((zone.width - 1, zone.height - 1)))
    boite = ImageChops.difference(zone, fond).getbbox()
    bas = min(image.height, (boite[3] if boite else image.height) + 40)
    image.crop((0, 0, image.width, max(bas, 500))).save(chemin)


def lire_cles(chemin: str) -> dict[str, str]:
    with open(chemin, encoding="utf-8-sig", newline="") as f:
        return {ligne["identifiant"]: ligne["cle"] for ligne in csv.DictReader(f)}


def connecter(pg, ident: str, cle: str, photo: bool = True) -> None:
    pg.goto(URL)
    pg.wait_for_selector("text=Se connecter", timeout=30000)
    if photo and ident[:3] == "AGT":
        pg.screenshot(path=str(SORTIE / "connexion.png"))
    pg.get_by_label("Rôle").click()
    pg.get_by_role("option", name=LIBELLE_ROLE[ident[:3]], exact=True).click()
    pg.get_by_label("Identifiant").fill(ident)
    pg.get_by_label("Clé personnelle").fill(cle)
    pg.get_by_role("button", name="Se connecter").click()
    pg.wait_for_timeout(3500)


def main(chemin_cles: str) -> None:
    cles = lire_cles(chemin_cles)
    SORTIE.mkdir(exist_ok=True)
    with sync_playwright() as p:
        navigateur = p.chromium.launch()
        for ident, pages in PARCOURS.items():
            pg = navigateur.new_page(viewport={"width": 1280, "height": 2100 if ident in ("OPS01", "DIR01") else 1250})
            connecter(pg, ident, cles[ident])
            for menu, fichier, action in pages:
                pg.locator("[data-testid=stSidebar]").get_by_text(menu, exact=True).click()
                pg.wait_for_timeout(2500)
                if action == "semaine":
                    pg.locator("[data-testid=stMain]").get_by_text("Semaine", exact=True).first.click()
                    pg.wait_for_timeout(2500)
                if action in ("recherche", "recherche_patient"):
                    pg.locator("[data-testid=stMain] input[type=text]").first.fill("Dia")
                    pg.keyboard.press("Enter")
                    pg.wait_for_timeout(2000)
                pg.screenshot(path=str(SORTIE / f"{fichier}.png"), full_page=True)
                if fichier == "resp_tableau_de_bord":
                    pg.screenshot(path=str(SORTIE / "resp_vue_absences.png"), full_page=True)
                    for vue, nom in VUES:
                        pg.get_by_text(vue, exact=True).first.click()
                        pg.wait_for_timeout(3500)
                        pg.screenshot(path=str(SORTIE / f"{nom}.png"), full_page=True)
            pg.close()
        # Vue téléphone, pour le guide (ENF-11)
        pg = navigateur.new_page(viewport={"width": 390, "height": 844})
        connecter(pg, "AGT01", cles["AGT01"], photo=False)
        pg.screenshot(path=str(SORTIE / "telephone_agent.png"))
        navigateur.close()
    for fichier in SORTIE.glob("*.png"):
        recadrer(fichier)


if __name__ == "__main__":
    main(sys.argv[1])
