"""Création des comptes de connexion, à lancer sur votre poste (opération idempotente).

Usage (depuis la racine du projet) :

    python -m backend.creer_comptes [--responsables 2] [--direction 3] [--administrateurs 2]

Ce que fait la commande :
- un compte par médecin et par agent déjà présents en base, avec leur identifiant (MED016, AGT01…) ;
- des comptes sans fiche : responsables des opérations (OPS01…), direction (DIR01…) et administrateurs
  (ADM01…), dans les effectifs demandés (par défaut ceux du cahier des charges : 2, 3 et 2) ;
- les comptes qui existent déjà sont ignorés : leur clé n'est jamais modifiée ;
- les clés générées sont écrites dans un fichier CSV local (cles_a_distribuer_<date>.csv) à remettre aux
  employés puis à SUPPRIMER. Elles ne sont jamais affichées, et seule leur empreinte est conservée en base.
  Le fichier est ignoré par Git : il ne doit jamais être publié.
"""
import argparse
import csv
import logging
import sys
from pathlib import Path

from sqlalchemy import func, select

from backend.database import RACINE_PROJET, SessionLocale, initialiser_base, transaction_ecriture
from backend.journal import journaliser
from backend.models import PREFIXE_ROLE, Agent, Medecin, RoleUtilisateur, Utilisateur, maintenant
from backend.securite import generer_cle, hacher_cle

logger = logging.getLogger("backend.creer_comptes")


def _nouveau_compte(identifiant: str, role: RoleUtilisateur, nom: str, actif: bool, **liens) -> tuple[Utilisateur, str]:
    """Construit un compte avec une clé neuve ; retourne (compte, clé en clair, à remettre à l'employé)."""
    cle = generer_cle()
    compte = Utilisateur(id=identifiant, role=role, nom_affichage=nom, cle_hash=hacher_cle(cle), actif=actif, **liens)
    return compte, cle


def creer_comptes(db, responsables: int = 2, direction: int = 3, administrateurs: int = 2) -> list[dict]:
    """Crée les comptes manquants dans une seule transaction (tout ou rien).

    Retourne la liste des comptes créés, avec leur clé en clair : cette clé ne doit être ni journalisée ni
    affichée, seulement écrite dans le fichier à distribuer.
    """
    existants = set(db.scalars(select(Utilisateur.id)).all())
    crees: list[dict] = []

    def ajouter(compte: Utilisateur, cle: str) -> None:
        db.add(compte)
        crees.append({"identifiant": compte.id, "role": compte.role.value, "nom": compte.nom_affichage, "cle": cle})

    with transaction_ecriture(db):
        for medecin in db.scalars(select(Medecin).order_by(Medecin.id)).all():
            if medecin.id not in existants:
                ajouter(*_nouveau_compte(medecin.id, RoleUtilisateur.MEDECIN, medecin.nom, medecin.actif, medecin_id=medecin.id))
        for agent in db.scalars(select(Agent).order_by(Agent.id)).all():
            if agent.id not in existants:
                ajouter(*_nouveau_compte(agent.id, RoleUtilisateur.AGENT, f"Agent {agent.id}", agent.actif, agent_id=agent.id))
        for role, effectif in (
            (RoleUtilisateur.RESPONSABLE, responsables),
            (RoleUtilisateur.DIRECTION, direction),
            (RoleUtilisateur.ADMINISTRATEUR, administrateurs),
        ):
            prefixe, largeur = PREFIXE_ROLE[role]
            for numero in range(1, effectif + 1):
                identifiant = f"{prefixe}{numero:0{largeur}d}"
                if identifiant not in existants:
                    ajouter(*_nouveau_compte(identifiant, role, f"{role.value} {identifiant}", True))
        if crees:
            journaliser(db, None, "COMPTES_CREES_EN_MASSE", detail=f"{len(crees)} comptes")
    return crees


def ecrire_fichier_cles(comptes: list[dict], dossier: Path) -> Path:
    """Écrit les clés à distribuer dans un fichier CSV daté, lisible dans Excel."""
    fichier = dossier / f"cles_a_distribuer_{maintenant():%Y%m%d_%H%M%S}.csv"
    with open(fichier, "w", newline="", encoding="utf-8-sig") as sortie:
        ecrivain = csv.writer(sortie)
        ecrivain.writerow(["identifiant", "role", "nom", "cle"])
        for compte in comptes:
            ecrivain.writerow([compte["identifiant"], compte["role"], compte["nom"], compte["cle"]])
    try:
        fichier.chmod(0o600)
    except OSError:  # systèmes de fichiers sans droits Unix (Windows)
        pass
    return fichier


def main(argv: list[str] | None = None) -> int:
    analyseur = argparse.ArgumentParser(description="Crée les comptes de connexion manquants et leurs clés.")
    analyseur.add_argument("--responsables", type=int, default=2, help="responsables des opérations (défaut : 2)")
    analyseur.add_argument("--direction", type=int, default=3, help="comptes Direction (défaut : 3)")
    analyseur.add_argument("--administrateurs", type=int, default=2, help="administrateurs (défaut : 2)")
    analyseur.add_argument("--dossier", type=Path, default=RACINE_PROJET, help="dossier du fichier de clés")
    arguments = analyseur.parse_args(argv)
    if min(arguments.responsables, arguments.direction, arguments.administrateurs) < 0 or arguments.administrateurs < 1:
        print("Les effectifs doivent être positifs, avec au moins 1 administrateur.", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s : %(message)s")
    try:
        initialiser_base()  # ajoute les tables manquantes sans toucher aux données existantes
        with SessionLocale() as db:
            crees = creer_comptes(db, arguments.responsables, arguments.direction, arguments.administrateurs)
            nb_medecins = db.scalar(select(func.count()).select_from(Medecin)) or 0
            nb_agents = db.scalar(select(func.count()).select_from(Agent)) or 0
        if not nb_medecins and not nb_agents:
            logger.warning(
                "Aucun médecin ni agent en base : seuls les comptes sans fiche ont été créés. Importez d'abord les "
                "données avec « python -m backend.seed_data », puis relancez cette commande pour créer les comptes "
                "des médecins et des agents."
            )
        if not crees:
            logger.info("Aucun compte à créer : tous les comptes demandés existent déjà.")
            return 0
        fichier = ecrire_fichier_cles(crees, arguments.dossier)
        par_role: dict[str, int] = {}
        for compte in crees:
            par_role[compte["role"]] = par_role.get(compte["role"], 0) + 1
        logger.info("%d comptes créés : %s", len(crees), ", ".join(f"{n} {r}" for r, n in sorted(par_role.items())))
        logger.info("Clés écrites dans %s", fichier)
        logger.warning("Ce fichier contient des secrets : remettez les clés aux employés, puis supprimez-le. Ne le publiez jamais.")
    except Exception:  # noqa: BLE001 - dernier filet : trace complète dans le journal, code de sortie non nul
        logger.exception("Erreur inattendue ; aucune modification n'a été enregistrée.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
