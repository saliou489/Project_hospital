"""Génère les livrables PDF L1, L7 et L9 dans docs/livrables/pdf.

    python docs/livrables/captures.py cles.csv        # (facultatif) régénère les captures
    python docs/livrables/generer_livrables.py 221 88  # nombre de tests, couverture en %
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import conception, guide, recette  # noqa: E402

SORTIE = Path(__file__).parent / "pdf"


def main(nb_tests: int, couverture_pct: int) -> None:
    SORTIE.mkdir(exist_ok=True)
    conception.construire(SORTIE / "L1_dossier_de_conception.pdf", nb_tests, couverture_pct)
    guide.construire(SORTIE / "L7_guide_utilisateur.pdf")
    recette.construire(SORTIE / "L9_cahier_de_recette_et_PV.pdf", f"{nb_tests} tests réussis, couverture {couverture_pct} %")


if __name__ == "__main__":
    main(int(sys.argv[1]), int(sys.argv[2]))
