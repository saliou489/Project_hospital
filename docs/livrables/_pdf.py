"""Briques communes de mise en page des livrables PDF (reportlab, polices standard : pas de symbole hors Latin-1)."""
from pathlib import Path

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (BaseDocTemplate, Frame, Image, KeepTogether, PageBreak, PageTemplate, Paragraph, Spacer,
                                Table, TableStyle)

VERT = colors.HexColor("#106848")
VERT_CLAIR = colors.HexColor("#e6f2ec")
GRIS = colors.HexColor("#555555")
GRIS_CLAIR = colors.HexColor("#f2f2f2")
LOGO = Path(__file__).resolve().parents[2] / "frontend" / "assets" / "logo_baobab.png"

_base = getSampleStyleSheet()
STYLES = {
    "corps": ParagraphStyle("corps", parent=_base["BodyText"], fontName="Helvetica", fontSize=10, leading=14, spaceAfter=6),
    "petit": ParagraphStyle("petit", parent=_base["BodyText"], fontName="Helvetica", fontSize=8, leading=10, textColor=GRIS),
    "cellule": ParagraphStyle("cellule", parent=_base["BodyText"], fontName="Helvetica", fontSize=8, leading=10),
    "cellule_gras": ParagraphStyle("cellule_gras", parent=_base["BodyText"], fontName="Helvetica-Bold", fontSize=8, leading=10),
    "entete": ParagraphStyle("entete", parent=_base["BodyText"], fontName="Helvetica-Bold", fontSize=8, leading=10,
                             textColor=colors.white),
    "h1": ParagraphStyle("h1", parent=_base["Heading1"], fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=VERT,
                         spaceBefore=6, spaceAfter=10, keepWithNext=1),
    "h2": ParagraphStyle("h2", parent=_base["Heading2"], fontName="Helvetica-Bold", fontSize=13, leading=16, textColor=VERT,
                         spaceBefore=10, spaceAfter=6, keepWithNext=1),
    "h3": ParagraphStyle("h3", parent=_base["Heading3"], fontName="Helvetica-Bold", fontSize=10.5, leading=13, spaceBefore=6,
                         spaceAfter=3, keepWithNext=1),
    "puce": ParagraphStyle("puce", parent=_base["BodyText"], fontName="Helvetica", fontSize=10, leading=14, leftIndent=14,
                           bulletIndent=2, spaceAfter=2),
    "titre": ParagraphStyle("titre", parent=_base["Title"], fontName="Helvetica-Bold", fontSize=28, leading=34, textColor=VERT,
                            alignment=TA_CENTER),
    "sous_titre": ParagraphStyle("sous_titre", parent=_base["BodyText"], fontName="Helvetica", fontSize=13, leading=18,
                                 alignment=TA_CENTER, textColor=GRIS),
    "legende": ParagraphStyle("legende", parent=_base["BodyText"], fontName="Helvetica-Oblique", fontSize=8.5, leading=11,
                              textColor=GRIS, alignment=TA_CENTER, spaceAfter=8),
}
LARGEUR = A4[0] - 4 * cm


def p(texte: str, style: str = "corps") -> Paragraph:
    return Paragraph(texte, STYLES[style])


def puces(elements: list[str]) -> list[Paragraph]:
    return [Paragraph(e, STYLES["puce"], bulletText="-") for e in elements]


def image(chemin: Path, largeur: float = LARGEUR, hauteur_max: float = 20 * cm) -> Image:
    """Image proportionnée à la largeur demandée, réduite si elle dépasse la hauteur maximale."""
    with PILImage.open(chemin) as im:
        l, h = im.size
    echelle = min(largeur / l, hauteur_max / h)
    return Image(str(chemin), width=l * echelle, height=h * echelle)


def figure(chemin: Path, legende: str, largeur: float = LARGEUR, hauteur_max: float = 19 * cm) -> KeepTogether:
    cadre = Table([[image(chemin, largeur - 4, hauteur_max)]], colWidths=[largeur])
    cadre.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#bbbbbb")), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                               ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    return KeepTogether([cadre, p(legende, "legende")])


def tableau(entetes: list[str], lignes: list[list], largeurs: list[float], zebre: bool = True) -> Table:
    donnees = [[Paragraph(e, STYLES["entete"]) for e in entetes]]
    for ligne in lignes:
        donnees.append([c if not isinstance(c, str) else Paragraph(c, STYLES["cellule"]) for c in ligne])
    t = Table(donnees, colWidths=largeurs, repeatRows=1)
    style = [("BACKGROUND", (0, 0), (-1, 0), VERT), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c8c8c8")), ("TOPPADDING", (0, 0), (-1, -1), 3),
             ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    if zebre:
        style += [("BACKGROUND", (0, i), (-1, i), GRIS_CLAIR) for i in range(2, len(donnees), 2)]
    t.setStyle(TableStyle(style))
    return t


def encadre(texte: str) -> Table:
    t = Table([[Paragraph(texte, STYLES["corps"])]], colWidths=[LARGEUR])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), VERT_CLAIR), ("LINEBEFORE", (0, 0), (0, -1), 3, VERT),
                           ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 6),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    return t


class Document(BaseDocTemplate):
    """Document A4 avec bandeau et pied de page ; ``titre_court`` apparaît dans le bandeau."""

    def __init__(self, chemin: Path, titre: str, titre_court: str, sujet: str = ""):
        super().__init__(str(chemin), pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2.2 * cm,
                         bottomMargin=2 * cm, title=titre, author="Titulaire du marche MediRDV", subject=sujet)
        self.titre_court = titre_court
        cadre = Frame(self.leftMargin, self.bottomMargin, self.width, self.height, id="normal")
        largeur_p, hauteur_p = landscape(A4)
        cadre_p = Frame(1.5 * cm, 1.8 * cm, largeur_p - 3 * cm, hauteur_p - 3.6 * cm, id="paysage")
        self.addPageTemplates([PageTemplate(id="couverture", frames=[cadre], onPage=lambda c, d: None),
                               PageTemplate(id="page", frames=[cadre], onPage=self._decor),
                               PageTemplate(id="paysage", frames=[cadre_p], onPage=self._decor_paysage, pagesize=landscape(A4))])

    def _decor_paysage(self, canevas, doc) -> None:
        largeur, hauteur = landscape(A4)
        canevas.saveState()
        canevas.setFont("Helvetica", 8)
        canevas.setFillColor(GRIS)
        canevas.drawString(1.5 * cm, hauteur - 1.1 * cm, f"MediRDV - Clinique Les Baobabs - AO-CLB-2026-01 - {self.titre_court}")
        canevas.setStrokeColor(VERT)
        canevas.line(1.5 * cm, hauteur - 1.3 * cm, largeur - 1.5 * cm, hauteur - 1.3 * cm)
        canevas.drawRightString(largeur - 1.5 * cm, 1 * cm, f"Page {doc.page}")
        canevas.restoreState()

    def _decor(self, canevas, doc) -> None:
        canevas.saveState()
        canevas.setFont("Helvetica", 8)
        canevas.setFillColor(GRIS)
        canevas.drawString(2 * cm, A4[1] - 1.3 * cm, f"MediRDV - Clinique Les Baobabs - AO-CLB-2026-01 - {self.titre_court}")
        canevas.setStrokeColor(VERT)
        canevas.setLineWidth(1)
        canevas.line(2 * cm, A4[1] - 1.5 * cm, A4[0] - 2 * cm, A4[1] - 1.5 * cm)
        canevas.drawRightString(A4[0] - 2 * cm, 1.2 * cm, f"Page {doc.page}")
        canevas.restoreState()


def couverture(titre: str, sous_titre: str, mention: str) -> list:
    from reportlab.platypus import NextPageTemplate

    elements = [Spacer(1, 3 * cm)]
    if LOGO.exists():
        elements += [image(LOGO, 9 * cm, 4 * cm), Spacer(1, 2 * cm)]
    elements += [p(titre, "titre"), Spacer(1, 0.5 * cm), p(sous_titre, "sous_titre"), Spacer(1, 4 * cm),
                 p(mention, "sous_titre"), NextPageTemplate("page"), PageBreak()]
    return elements
