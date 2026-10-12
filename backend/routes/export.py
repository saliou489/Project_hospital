"""Export CSV et PDF des indicateurs (EF-26).

Le CSV et le PDF sont écrits à partir de la même structure de données (``Rapport``) : ils ne peuvent pas se contredire.
Les chiffres sont ceux des endpoints du tableau de bord (mêmes fonctions), donc ceux affichés à l'écran.
Réservé au responsable des opérations et à l'administrateur (cahier des charges, section 4) ; chaque export est journalisé.
"""
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.journal import journaliser
from backend.models import RoleUtilisateur, Utilisateur, maintenant
from backend.routes import analyse, stats
from backend.routes.stats import REPONSES_ERREURS
from backend.securite import exiger_roles

router = APIRouter(prefix="/export", tags=["Export"])

exiger_export = exiger_roles(RoleUtilisateur.RESPONSABLE, RoleUtilisateur.ADMINISTRATEUR)

TYPES = {"csv": "text/csv; charset=utf-8", "pdf": "application/pdf"}


@dataclass
class Section:
    titre: str
    colonnes: list[str]
    lignes: list[list]
    note: str | None = None


@dataclass
class Rapport:
    titre: str
    perimetre: list[tuple[str, str]]
    sections: list[Section] = field(default_factory=list)


def _nombre(valeur, decimales: int = 1) -> str:
    """Nombre au format français (virgule décimale) ; ``None`` signifie « non calculable »."""
    if valeur is None:
        return "non calculable"
    if isinstance(valeur, int):
        return str(valeur)
    return f"{valeur:.{decimales}f}".replace(".", ",")


def _jour(valeur: date | None, defaut: str) -> str:
    return valeur.strftime("%d/%m/%Y") if valeur else defaut


def construire_rapport(
    db: Session, utilisateur: Utilisateur, date_debut: date | None, date_fin: date | None,
    specialite: str | None, medecin_id: str | None,
) -> Rapport:
    """Rassemble les indicateurs de la période ; les filtres sont validés comme à l'écran (période, médecin existant)."""
    args = dict(date_debut=date_debut, date_fin=date_fin, db=db, utilisateur=utilisateur)
    kpi = stats.indicateurs_cles(specialite=specialite, medecin_id=medecin_id, **args)
    par_specialite = stats.absence_par_specialite(medecin_id=medecin_id, **args)
    mensuelle = stats.absence_mensuelle(specialite=specialite, medecin_id=medecin_id, **args)
    delai = analyse.delai_attente(specialite=specialite, medecin_id=medecin_id, **args)
    annulation = analyse.taux_annulation(specialite=specialite, medecin_id=medecin_id, **args)

    rapport = Rapport(
        titre="Indicateurs de la clinique",
        perimetre=[
            ("Période", f"du {_jour(date_debut, 'début de l’historique')} au {_jour(date_fin, 'dernier rendez-vous')}"),
            ("Spécialité", specialite or "Toutes"),
            ("Médecin", medecin_id or "Tous"),
            ("Exporté le", maintenant().strftime("%d/%m/%Y à %H:%M")),
            ("Exporté par", utilisateur.id),
        ],
    )
    r = kpi.repartition_statuts
    rapport.sections.append(Section(
        "Indicateurs clés", ["Indicateur", "Valeur"],
        [["Rendez-vous", kpi.total_rdv], ["Honorés", kpi.honores], ["Absents", kpi.absents],
         ["Annulés", r["Annulé"]], ["Planifiés", r["Planifié"]], ["Confirmés", r["Confirmé"]],
         ["Taux d'absence (%)", _nombre(kpi.taux_absence_pct)]],
        "Taux d'absence = absents / (honorés + absents). Les rendez-vous planifiés, confirmés et annulés sont exclus du calcul.",
    ))
    if not medecin_id and not specialite:
        rapport.sections.append(Section(
            "Taux d'absence par spécialité", ["Spécialité", "Rendez-vous", "Honorés", "Absents", "Taux d'absence (%)"],
            [[s.specialite, s.total_rdv, s.honores, s.absents, _nombre(s.taux_absence_pct)] for s in par_specialite],
        ))
    rapport.sections.append(Section(
        "Évolution mensuelle", ["Mois", "Rendez-vous", "Honorés", "Absents", "Taux d'absence (%)"],
        [[m.periode, m.total_rdv, m.honores, m.absents, _nombre(m.taux_absence_pct)] for m in mensuelle],
    ))
    rapport.sections.append(Section(
        "Délai d'attente (prise au rendez-vous)", ["Spécialité", "Rendez-vous", "Médiane (jours)", "Moyenne (jours)", "90e percentile (jours)"],
        [[d.specialite, d.nombre_rdv, _nombre(d.mediane_jours), _nombre(d.moyenne_jours), _nombre(d.p90_jours)] for d in delai.par_specialite]
        + [["Ensemble", delai.nombre_rdv, _nombre(delai.mediane_jours), "", ""]],
        "Les rendez-vous annulés sont exclus.",
    ))
    rapport.sections.append(Section(
        "Annulations par motif", ["Motif", "Annulés", "Part des rendez-vous créés (%)"],
        [[m.motif, m.nombre, _nombre(m.part_pct)] for m in annulation.par_motif]
        + [["Taux d'annulation global (%)", annulation.annules, _nombre(annulation.taux_annulation_pct)]],
    ))
    return rapport


# --------------------------------------------------------------------------------------------------
# CSV : séparateur « ; », virgule décimale et BOM UTF-8, pour une ouverture directe dans Excel en français
# --------------------------------------------------------------------------------------------------

_DEBUT_DANGEREUX = re.compile(r"^[=+\-@\t\r]")
_NOMBRE_FR = re.compile(r"^-?\d+(,\d+)?$")


def _cellule_csv(valeur) -> str | int:
    """Neutralise l'injection de formules : une cellule de texte commençant par = + - @ est préfixée d'une apostrophe."""
    if isinstance(valeur, str) and _DEBUT_DANGEREUX.match(valeur) and not _NOMBRE_FR.match(valeur):
        return "'" + valeur
    return valeur


def en_csv(rapport: Rapport) -> bytes:
    sortie = io.StringIO()
    ecrivain = csv.writer(sortie, delimiter=";", lineterminator="\r\n")
    ecrivain.writerow([_cellule_csv(f"MédiRDV : {rapport.titre}")])
    for cle, valeur in rapport.perimetre:
        ecrivain.writerow([cle, _cellule_csv(valeur)])
    for section in rapport.sections:
        ecrivain.writerow([])
        ecrivain.writerow([_cellule_csv(section.titre)])
        ecrivain.writerow(section.colonnes)
        for ligne in section.lignes:
            ecrivain.writerow([_cellule_csv(c) for c in ligne])
        if section.note:
            ecrivain.writerow([_cellule_csv(section.note)])
    return b"\xef\xbb\xbf" + sortie.getvalue().encode("utf-8")


# --------------------------------------------------------------------------------------------------
# PDF (ReportLab)
# --------------------------------------------------------------------------------------------------

def en_pdf(rapport: Rapport) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    vert = colors.HexColor("#1b6b4a")
    styles = getSampleStyleSheet()
    titre = ParagraphStyle("t", parent=styles["Title"], textColor=vert, fontSize=18, alignment=0)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], textColor=vert, fontSize=12, spaceBefore=14, spaceAfter=4)
    normal = ParagraphStyle("n", parent=styles["Normal"], fontSize=9, leading=12)
    note = ParagraphStyle("note", parent=normal, textColor=colors.HexColor("#555555"), fontSize=8, spaceBefore=3)

    def pied(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#555555"))
        canvas.drawString(2 * cm, 1.2 * cm, "Clinique Les Baobabs - MédiRDV : document interne, données de rendez-vous sans information médicale")
        canvas.drawRightString(A4[0] - 2 * cm, 1.2 * cm, f"Page {doc.page}")
        canvas.restoreState()

    tampon = io.BytesIO()
    doc = SimpleDocTemplate(
        tampon, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=1.8 * cm, bottomMargin=2 * cm,
        title=f"MédiRDV - {rapport.titre}", author="MédiRDV",
    )
    elements = [Paragraph(escape(f"MédiRDV : {rapport.titre}"), titre)]
    for cle, valeur in rapport.perimetre:
        elements.append(Paragraph(f"<b>{escape(cle)}</b> : {escape(str(valeur))}", normal))
    largeur_utile = A4[0] - 4 * cm
    for section in rapport.sections:
        elements.append(Paragraph(escape(section.titre), h2))
        entete = ParagraphStyle("e", parent=normal, textColor=colors.white, fontName="Helvetica-Bold", fontSize=8.5, leading=10)
        entete_droite = ParagraphStyle("ed", parent=entete, alignment=2)
        # En-têtes en paragraphes : ils passent à la ligne au lieu de déborder sur la colonne voisine.
        en_tete = [Paragraph(escape(c), entete if i == 0 else entete_droite) for i, c in enumerate(section.colonnes)]
        donnees = [en_tete] + [[str(c) for c in ligne] for ligne in section.lignes]
        if len(donnees) == 1:
            elements.append(Paragraph("Aucune donnée sur cette période.", normal))
            continue
        premiere = 4.6 * cm if len(section.colonnes) > 2 else largeur_utile * 0.6
        autres = (largeur_utile - premiere) / (len(section.colonnes) - 1)
        tableau = Table(donnees, colWidths=[premiere] + [autres] * (len(section.colonnes) - 1), repeatRows=1)
        tableau.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), vert), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f3")]),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c7d0cb")),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        elements.append(tableau)
        if section.note:
            elements.append(Paragraph(escape(section.note), note))
        elements.append(Spacer(1, 4))
    doc.build(elements, onFirstPage=pied, onLaterPages=pied)
    return tampon.getvalue()


@router.get(
    "/indicateurs",
    summary="Exporter les indicateurs de la période en CSV ou en PDF (EF-26)",
    responses={**REPONSES_ERREURS, 200: {"content": {"text/csv": {}, "application/pdf": {}}, "description": "Fichier à télécharger"}},
    response_class=Response,
)
def exporter_indicateurs(
    db: Annotated[Session, Depends(get_db)],
    utilisateur: Annotated[Utilisateur, Depends(exiger_export)],
    format: Annotated[str, Query(pattern="^(csv|pdf)$", description="csv ou pdf")] = "csv",
    date_debut: date | None = None,
    date_fin: date | None = None,
    specialite: str | None = None,
    medecin_id: str | None = None,
) -> Response:
    rapport = construire_rapport(db, utilisateur, date_debut, date_fin, specialite, medecin_id)
    contenu = en_csv(rapport) if format == "csv" else en_pdf(rapport)
    journaliser(
        db, utilisateur, "EXPORT_INDICATEURS", cible=format.upper(),
        detail=f"du {date_debut or '-'} au {date_fin or '-'} ; spécialité {specialite or 'toutes'} ; médecin {medecin_id or 'tous'}",
    )
    db.commit()
    nom = f"indicateurs_medirdv_{maintenant():%Y%m%d_%H%M}.{format}"
    return Response(content=contenu, media_type=TYPES[format], headers={"Content-Disposition": f'attachment; filename="{nom}"'})
