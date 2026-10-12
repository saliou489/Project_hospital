"""Planning : spécialités (EF-01), plages (EF-02), jours fermés (EF-05), créneaux (EF-10, EF-11), indisponibilités (EF-14).

Droits (cahier des charges, section 4) : l'administrateur paramètre les spécialités ; l'administrateur et le
responsable des opérations (« ajuster les créneaux ») gèrent plages et jours fermés ; un médecin déclare ses propres
indisponibilités ; les agents consultent les créneaux pour prendre les rendez-vous.
"""
import logging
from datetime import date, datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.database import ecrire_avec_reessai, get_db
from backend.exceptions import AccesRefuseError, ConflitError, DonneesInvalidesError, RessourceIntrouvableError
from backend.journal import journaliser
from backend.models import (
    HORIZON_RDV_JOURS,
    Indisponibilite,
    JourFerie,
    Medecin,
    PlageConsultation,
    RendezVous,
    RoleUtilisateur,
    Specialite,
    StatutRdv,
    Utilisateur,
    aujourdhui,
    maintenant,
)
from backend.planning import (
    creneaux_de_plage,
    lister_creneaux,
    premiers_creneaux_libres,
    rdv_a_reprogrammer,
    taux_occupation,
)
from backend.schemas import (
    CreneauLecture,
    ErreurReponse,
    IndisponibiliteCreation,
    IndisponibiliteLecture,
    JourFermeCreation,
    JourFermeLecture,
    OccupationLecture,
    PlageLecture,
    PlagesRemplacement,
    RdvLecture,
    ReponseIndisponibilite,
    ReponseJourFerme,
    ReponseOccupation,
    ReponsePlages,
    SpecialiteCreation,
    SpecialiteLecture,
    SpecialiteMiseAJour,
)
from backend.securite import exiger_roles, utilisateur_courant

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/planning", tags=["Planning"])
router_referentiels = APIRouter(prefix="/referentiels", tags=["Référentiels"])

R = RoleUtilisateur
exiger_admin = exiger_roles(R.ADMINISTRATEUR)
exiger_gestion_planning = exiger_roles(R.ADMINISTRATEUR, R.RESPONSABLE)
exiger_lecture_planning = exiger_roles(R.AGENT, R.MEDECIN, R.RESPONSABLE, R.ADMINISTRATEUR)
exiger_recherche_creneau = exiger_roles(R.AGENT, R.RESPONSABLE, R.ADMINISTRATEUR)
exiger_indisponibilites = exiger_roles(R.MEDECIN, R.RESPONSABLE, R.ADMINISTRATEUR)

REPONSES_ERREURS = {
    400: {"model": ErreurReponse, "description": "Règle métier violée"},
    401: {"model": ErreurReponse, "description": "Non authentifié ou session expirée"},
    403: {"model": ErreurReponse, "description": "Rôle non autorisé"},
    404: {"model": ErreurReponse, "description": "Ressource introuvable"},
    409: {"model": ErreurReponse, "description": "Doublon ou conflit"},
    422: {"model": ErreurReponse, "description": "Données invalides"},
    500: {"model": ErreurReponse, "description": "Erreur interne"},
}


def _medecin_ou_404(db: Session, medecin_id: str) -> Medecin:
    medecin = db.get(Medecin, medecin_id)
    if medecin is None:
        raise RessourceIntrouvableError(f"Médecin {medecin_id} introuvable.", "MEDECIN_INTROUVABLE")
    return medecin


def _verifier_son_planning(utilisateur: Utilisateur, medecin_id: str) -> None:
    """RG-07 : un médecin n'agit que sur son propre planning."""
    if utilisateur.role == R.MEDECIN and utilisateur.medecin_id != medecin_id:
        raise AccesRefuseError("Un médecin ne gère que son propre planning.", "PLANNING_D_UN_AUTRE_MEDECIN")


def _lecture_specialite(db: Session, specialite: Specialite) -> SpecialiteLecture:
    nombre = db.scalar(
        select(func.count()).select_from(Medecin).where(Medecin.specialite == specialite.nom, Medecin.actif.is_(True))
    )
    return SpecialiteLecture(
        nom=specialite.nom, duree_standard_min=specialite.duree_standard_min, couleur=specialite.couleur,
        actif=specialite.actif, nb_medecins=int(nombre or 0),
    )  # fmt: skip


# --------------------------------------------------------------------------------------
# EF-01 : spécialités
# --------------------------------------------------------------------------------------


@router_referentiels.get("/specialites", response_model=list[SpecialiteLecture], summary="Lister les spécialités", responses=REPONSES_ERREURS)
def lister_specialites(
    actives_seulement: bool = False,
    db: Session = Depends(get_db),
    _utilisateur: Utilisateur = Depends(utilisateur_courant),
) -> list[SpecialiteLecture]:
    requete = select(Specialite).order_by(Specialite.nom)
    if actives_seulement:
        requete = requete.where(Specialite.actif.is_(True))
    return [_lecture_specialite(db, s) for s in db.scalars(requete).all()]


@router_referentiels.post("/specialites", response_model=SpecialiteLecture, status_code=201, summary="Créer une spécialité", responses=REPONSES_ERREURS)
def creer_specialite(
    donnees: SpecialiteCreation,
    db: Session = Depends(get_db),
    administrateur: Utilisateur = Depends(exiger_admin),
) -> SpecialiteLecture:
    nom = " ".join(donnees.nom.split())

    def operation() -> Specialite:
        if any(s.lower() == nom.lower() for s in db.scalars(select(Specialite.nom)).all()):
            raise ConflitError(f"La spécialité « {nom} » existe déjà.", "SPECIALITE_EXISTANTE")
        specialite = Specialite(nom=nom, duree_standard_min=donnees.duree_standard_min, couleur=donnees.couleur, actif=True)
        db.add(specialite)
        journaliser(db, administrateur, "SPECIALITE_CREEE", cible=nom, detail=f"{donnees.duree_standard_min} min")
        db.flush()
        return specialite

    return _lecture_specialite(db, ecrire_avec_reessai(db, operation))


@router_referentiels.patch("/specialites/{nom}", response_model=SpecialiteLecture, summary="Modifier une spécialité", responses=REPONSES_ERREURS)
def modifier_specialite(
    nom: str,
    donnees: SpecialiteMiseAJour,
    db: Session = Depends(get_db),
    administrateur: Utilisateur = Depends(exiger_admin),
) -> SpecialiteLecture:
    """Le nom ne change pas. Un changement de durée s'applique aux nouveaux créneaux ; les rendez-vous déjà pris sont conservés."""

    def operation() -> Specialite:
        specialite = db.get(Specialite, nom)
        if specialite is None:
            raise RessourceIntrouvableError(f"Spécialité « {nom} » introuvable.", "SPECIALITE_INTROUVABLE")
        modifs = []
        if donnees.duree_standard_min is not None and donnees.duree_standard_min != specialite.duree_standard_min:
            specialite.duree_standard_min = donnees.duree_standard_min
            for medecin in db.scalars(select(Medecin).where(Medecin.specialite == nom)):
                medecin.duree_consultation_min = donnees.duree_standard_min
            modifs.append(f"durée {donnees.duree_standard_min} min")
        if donnees.couleur is not None and donnees.couleur != specialite.couleur:
            specialite.couleur = donnees.couleur
            modifs.append("couleur")
        if donnees.actif is not None and donnees.actif != specialite.actif:
            specialite.actif = donnees.actif
            modifs.append("activée" if donnees.actif else "désactivée")
        if not modifs:
            raise DonneesInvalidesError("Aucune modification effective.", "AUCUNE_MODIFICATION")
        journaliser(db, administrateur, "SPECIALITE_MODIFIEE", cible=nom, detail=", ".join(modifs))
        db.flush()
        return specialite

    return _lecture_specialite(db, ecrire_avec_reessai(db, operation))


# --------------------------------------------------------------------------------------
# EF-02 : plages de consultation d'un médecin
# --------------------------------------------------------------------------------------


def rdv_hors_planning(db: Session, medecin: Medecin) -> list[str]:
    """Rendez-vous à venir (Planifié/Confirmé) qui ne correspondent plus à aucun créneau du médecin."""
    plages = db.scalars(select(PlageConsultation).where(PlageConsultation.medecin_id == medecin.id)).all()
    ferme = {j.jour for j in db.scalars(select(JourFerie).where(JourFerie.jour >= aujourdhui()))}
    resultat = []
    for rdv in db.scalars(
        select(RendezVous).where(
            RendezVous.medecin_id == medecin.id, RendezVous.debut > maintenant(),
            RendezVous.statut.in_((StatutRdv.PLANIFIE, StatutRdv.CONFIRME)),
        ).order_by(RendezVous.debut)
    ):  # fmt: skip
        jour = rdv.debut.date()
        pas = int((rdv.fin - rdv.debut).total_seconds() // 60)
        valide = jour not in ferme and any(
            (rdv.debut, rdv.fin) in creneaux_de_plage(jour, p, pas) for p in plages if p.jour_semaine == jour.weekday()
        )
        if not valide:
            resultat.append(rdv.id)
    return resultat


@router_referentiels.get("/medecins/{medecin_id}/plages", response_model=ReponsePlages, summary="Plages de consultation d'un médecin", responses=REPONSES_ERREURS)
def lire_plages(
    medecin_id: str, db: Session = Depends(get_db), _utilisateur: Utilisateur = Depends(utilisateur_courant)
) -> ReponsePlages:
    _medecin_ou_404(db, medecin_id)
    plages = db.scalars(
        select(PlageConsultation).where(PlageConsultation.medecin_id == medecin_id).order_by(PlageConsultation.jour_semaine, PlageConsultation.debut)
    ).all()
    return ReponsePlages(medecin_id=medecin_id, plages=[PlageLecture.model_validate(p) for p in plages])


@router_referentiels.put("/medecins/{medecin_id}/plages", response_model=ReponsePlages, summary="Remplacer les plages d'un médecin", responses=REPONSES_ERREURS)
def remplacer_plages(
    medecin_id: str,
    donnees: PlagesRemplacement,
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(exiger_gestion_planning),
) -> ReponsePlages:
    """Remplace l'ensemble des plages. La réponse liste les rendez-vous à venir qui sortent des nouvelles plages."""
    par_jour: dict[int, list] = {}
    for plage in donnees.plages:
        par_jour.setdefault(plage.jour_semaine, []).append(plage)
    for jour, plages in par_jour.items():
        plages.sort(key=lambda p: p.debut)
        for precedente, suivante in zip(plages, plages[1:]):
            if suivante.debut < precedente.fin:
                raise DonneesInvalidesError("Deux plages d'un même jour se chevauchent.", "PLAGES_CHEVAUCHANTES")

    def operation() -> list[str]:
        medecin = _medecin_ou_404(db, medecin_id)
        for ancienne in db.scalars(select(PlageConsultation).where(PlageConsultation.medecin_id == medecin_id)):
            db.delete(ancienne)
        db.flush()
        for plage in donnees.plages:
            db.add(PlageConsultation(medecin_id=medecin_id, jour_semaine=plage.jour_semaine, debut=plage.debut, fin=plage.fin))
        journaliser(db, utilisateur, "PLAGES_MODIFIEES", cible=medecin_id, detail=f"{len(donnees.plages)} plage(s)")
        db.flush()
        return rdv_hors_planning(db, medecin)

    hors = ecrire_avec_reessai(db, operation)
    return ReponsePlages(
        medecin_id=medecin_id,
        plages=[PlageLecture(jour_semaine=p.jour_semaine, debut=p.debut, fin=p.fin) for p in sorted(donnees.plages, key=lambda p: (p.jour_semaine, p.debut))],
        rdv_hors_planning=hors,
    )


# --------------------------------------------------------------------------------------
# EF-05 : jours fériés et fermetures exceptionnelles
# --------------------------------------------------------------------------------------


@router_referentiels.get("/feries", response_model=list[JourFermeLecture], summary="Lister les jours fériés et fermetures", responses=REPONSES_ERREURS)
def lister_feries(
    annee: Annotated[int | None, Query(ge=2000, le=2100)] = None,
    db: Session = Depends(get_db),
    _utilisateur: Utilisateur = Depends(utilisateur_courant),
) -> list[JourFerie]:
    requete = select(JourFerie).order_by(JourFerie.jour)
    if annee:
        requete = requete.where(JourFerie.jour >= date(annee, 1, 1), JourFerie.jour <= date(annee, 12, 31))
    return list(db.scalars(requete).all())


@router_referentiels.post("/feries", response_model=ReponseJourFerme, status_code=201, summary="Ajouter un jour férié ou une fermeture", responses=REPONSES_ERREURS)
def ajouter_ferie(
    donnees: JourFermeCreation,
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(exiger_gestion_planning),
) -> ReponseJourFerme:
    """La réponse liste les rendez-vous à venir tombant ce jour-là : ils sont à reprogrammer."""

    def operation() -> JourFerie:
        if db.get(JourFerie, donnees.jour) is not None:
            raise ConflitError(f"Le {donnees.jour:%d/%m/%Y} est déjà déclaré fermé.", "JOUR_FERIE_EXISTANT")
        ligne = JourFerie(jour=donnees.jour, libelle=donnees.libelle, type=donnees.type)
        db.add(ligne)
        journaliser(db, utilisateur, "FERIE_AJOUTE", cible=donnees.jour.isoformat(), detail=donnees.libelle)
        db.flush()
        return ligne

    ligne = ecrire_avec_reessai(db, operation)
    impactes = rdv_a_reprogrammer(db, None, datetime.combine(donnees.jour, time.min), datetime.combine(donnees.jour + timedelta(days=1), time.min))
    return ReponseJourFerme(jour_ferme=JourFermeLecture.model_validate(ligne), rdv_a_reprogrammer=[RdvLecture.model_validate(r) for r in impactes])


@router_referentiels.delete("/feries/{jour}", status_code=204, summary="Retirer un jour férié ou une fermeture", responses=REPONSES_ERREURS)
def retirer_ferie(
    jour: date,
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(exiger_gestion_planning),
) -> None:
    def operation() -> None:
        ligne = db.get(JourFerie, jour)
        if ligne is None:
            raise RessourceIntrouvableError(f"Le {jour:%d/%m/%Y} n'est pas déclaré fermé.", "JOUR_FERIE_INTROUVABLE")
        db.delete(ligne)
        journaliser(db, utilisateur, "FERIE_RETIRE", cible=jour.isoformat(), detail=ligne.libelle)

    ecrire_avec_reessai(db, operation)


# --------------------------------------------------------------------------------------
# EF-10 et EF-11 : créneaux
# --------------------------------------------------------------------------------------


def _vers_lecture(db: Session, creneaux, medecins: dict[str, Medecin]) -> list[CreneauLecture]:
    return [
        CreneauLecture(
            medecin_id=c.medecin_id, medecin_nom=medecins[c.medecin_id].nom, specialite=medecins[c.medecin_id].specialite,
            date_rdv=c.debut.date(), heure_rdv=c.debut.time(), heure_fin=c.fin.time(), etat=c.etat, rdv_id=c.rdv_id,
        )
        for c in creneaux
    ]  # fmt: skip


@router.get("/creneaux", response_model=list[CreneauLecture], summary="Créneaux d'un médecin sur une période", responses=REPONSES_ERREURS)
def creneaux_du_medecin(
    medecin_id: str,
    date_debut: date,
    date_fin: date,
    etat: str | None = Query(default=None, pattern="^(libre|occupé|indisponible)$"),
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(exiger_lecture_planning),
) -> list[CreneauLecture]:
    """EF-10 : créneaux générés à partir des plages du médecin et de la durée de sa spécialité."""
    _verifier_son_planning(utilisateur, medecin_id)
    medecin = _medecin_ou_404(db, medecin_id)
    creneaux = lister_creneaux(db, medecin, date_debut, date_fin)
    if etat:
        creneaux = [c for c in creneaux if c.etat == etat]
    return _vers_lecture(db, creneaux, {medecin.id: medecin})


@router.get("/premiers-creneaux", response_model=list[CreneauLecture], summary="Premiers créneaux libres", responses=REPONSES_ERREURS)
def premiers_creneaux(
    specialite: str | None = None,
    medecin_id: str | None = None,
    date_debut: date | None = None,
    date_fin: date | None = None,
    patient_id: str | None = None,
    limite: Annotated[int, Query(ge=1, le=50)] = 5,
    db: Session = Depends(get_db),
    _utilisateur: Utilisateur = Depends(exiger_recherche_creneau),
) -> list[CreneauLecture]:
    """EF-11 : premiers créneaux libres à venir (dans les 90 jours), par spécialité, médecin et période.

    Avec ``patient_id``, les créneaux qui chevaucheraient un rendez-vous du patient sont écartés (RG-02).
    """
    if not specialite and not medecin_id:
        raise DonneesInvalidesError("Indiquez une spécialité ou un médecin.", "CRITERE_MANQUANT")
    if medecin_id:
        _medecin_ou_404(db, medecin_id)
    creneaux = premiers_creneaux_libres(
        db, specialite=specialite, medecin_id=medecin_id, date_debut=date_debut, date_fin=date_fin, patient_id=patient_id, limite=limite
    )
    medecins = {m.id: m for m in db.scalars(select(Medecin).where(Medecin.id.in_({c.medecin_id for c in creneaux})))} if creneaux else {}
    return _vers_lecture(db, creneaux, medecins)


# --------------------------------------------------------------------------------------
# EF-14 : indisponibilités
# --------------------------------------------------------------------------------------


def _lecture_indispo(i: Indisponibilite) -> IndisponibiliteLecture:
    lecture = IndisponibiliteLecture.model_validate(i)
    lecture.medecin_nom = i.medecin.nom
    return lecture


@router.get("/indisponibilites", response_model=list[IndisponibiliteLecture], summary="Lister les indisponibilités", responses=REPONSES_ERREURS)
def lister_indisponibilites(
    medecin_id: str | None = None,
    actives_seulement: bool = True,
    a_venir: bool = True,
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(exiger_lecture_planning),
) -> list[IndisponibiliteLecture]:
    if utilisateur.role == R.MEDECIN:
        medecin_id = utilisateur.medecin_id
    requete = select(Indisponibilite).order_by(Indisponibilite.debut)
    if medecin_id:
        requete = requete.where(Indisponibilite.medecin_id == medecin_id)
    if actives_seulement:
        requete = requete.where(Indisponibilite.active.is_(True))
    if a_venir:
        requete = requete.where(Indisponibilite.fin > maintenant())
    return [_lecture_indispo(i) for i in db.scalars(requete).all()]


@router.post("/indisponibilites", response_model=ReponseIndisponibilite, status_code=201, summary="Déclarer une indisponibilité", responses=REPONSES_ERREURS)
def declarer_indisponibilite(
    donnees: IndisponibiliteCreation,
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(exiger_indisponibilites),
) -> ReponseIndisponibilite:
    """Déclare l'indisponibilité et liste les rendez-vous à venir impactés, à reprogrammer."""
    _verifier_son_planning(utilisateur, donnees.medecin_id)

    def operation() -> Indisponibilite:
        _medecin_ou_404(db, donnees.medecin_id)
        indispo = Indisponibilite(
            medecin_id=donnees.medecin_id, debut=donnees.debut, fin=donnees.fin, motif=donnees.motif,
            active=True, declaree_par=utilisateur.id,
        )  # fmt: skip
        db.add(indispo)
        journaliser(db, utilisateur, "INDISPONIBILITE_DECLAREE", cible=donnees.medecin_id, detail=f"{donnees.debut:%Y-%m-%d %H:%M} -> {donnees.fin:%Y-%m-%d %H:%M}")
        db.flush()
        return indispo

    indispo = ecrire_avec_reessai(db, operation)
    impactes = rdv_a_reprogrammer(db, indispo.medecin_id, indispo.debut, indispo.fin)
    return ReponseIndisponibilite(indisponibilite=_lecture_indispo(indispo), rdv_a_reprogrammer=[RdvLecture.model_validate(r) for r in impactes])


def _indispo_ou_404(db: Session, identifiant: int) -> Indisponibilite:
    indispo = db.get(Indisponibilite, identifiant)
    if indispo is None:
        raise RessourceIntrouvableError(f"Indisponibilité {identifiant} introuvable.", "INDISPONIBILITE_INTROUVABLE")
    return indispo


@router.get("/indisponibilites/{identifiant}/rdv-impactes", response_model=list[RdvLecture], summary="Rendez-vous restant à reprogrammer", responses=REPONSES_ERREURS)
def rdv_impactes(
    identifiant: int, db: Session = Depends(get_db), utilisateur: Utilisateur = Depends(exiger_lecture_planning)
) -> list[RdvLecture]:
    """Calculé à l'instant : un rendez-vous reprogrammé ou annulé disparaît de la liste."""
    indispo = _indispo_ou_404(db, identifiant)
    _verifier_son_planning(utilisateur, indispo.medecin_id)
    if not indispo.active:
        return []
    return [RdvLecture.model_validate(r) for r in rdv_a_reprogrammer(db, indispo.medecin_id, indispo.debut, indispo.fin)]


@router.post("/indisponibilites/{identifiant}/lever", response_model=IndisponibiliteLecture, summary="Lever une indisponibilité", responses=REPONSES_ERREURS)
def lever_indisponibilite(
    identifiant: int,
    db: Session = Depends(get_db),
    utilisateur: Utilisateur = Depends(exiger_indisponibilites),
) -> IndisponibiliteLecture:
    """L'indisponibilité n'est pas supprimée : elle est marquée levée (traçabilité) et les créneaux se rouvrent."""

    def operation() -> Indisponibilite:
        indispo = _indispo_ou_404(db, identifiant)
        _verifier_son_planning(utilisateur, indispo.medecin_id)
        if not indispo.active:
            raise ConflitError("Cette indisponibilité est déjà levée.", "INDISPONIBILITE_DEJA_LEVEE")
        indispo.active = False
        journaliser(db, utilisateur, "INDISPONIBILITE_LEVEE", cible=indispo.medecin_id, detail=f"n° {indispo.id}")
        db.flush()
        return indispo

    return _lecture_indispo(ecrire_avec_reessai(db, operation))


# --------------------------------------------------------------------------------------
# RG-10 : taux d'occupation
# --------------------------------------------------------------------------------------


def construire_occupation(
    db: Session, date_debut: date, date_fin: date, specialite: str | None, medecin_id: str | None
) -> ReponseOccupation:
    lignes = taux_occupation(db, date_debut, date_fin, specialite, medecin_id)
    ouverts = sum(m.creneaux_ouverts for m in lignes)
    occupes = sum(m.creneaux_occupes for m in lignes)
    return ReponseOccupation(
        date_debut=date_debut, date_fin=date_fin, creneaux_ouverts=ouverts, creneaux_occupes=occupes,
        taux_occupation_pct=round(100 * occupes / ouverts, 1) if ouverts else None,
        par_medecin=[
            OccupationLecture(
                medecin_id=m.medecin_id, medecin_nom=m.medecin_nom, specialite=m.specialite,
                creneaux_ouverts=m.creneaux_ouverts, creneaux_occupes=m.creneaux_occupes, taux_occupation_pct=m.taux_pct,
            )
            for m in lignes
        ],
    )  # fmt: skip


__all__ = ["router", "router_referentiels", "construire_occupation", "HORIZON_RDV_JOURS"]
