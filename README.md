# BAOBAB — Clinique Les Baobabs (MédiRDV)

Application de gestion des rendez-vous (AO-CLB-2026-01). Données entièrement fictives.
Pile : FastAPI, SQLAlchemy 2, Pydantic v2, Alembic, PostgreSQL 16, Streamlit.

## Démarrage avec Docker (une seule commande)

```powershell
copy .env.example .env      # puis renseigner POSTGRES_PASSWORD et MEDIRDV_SECRET_JWT dans .env
docker compose up -d --build
```

- Interface : http://localhost:8501 — API : http://localhost:8000/docs
- Au premier démarrage, le service `initialisation` crée le schéma (Alembic), importe médecins et agents, génère
  12 mois d'historique et 3 mois à venir, puis crée les comptes. Les clés de connexion sont écrites dans le volume `sortie` :

```powershell
docker compose exec api sh -c "cat /sortie/cles_a_distribuer_*.csv"
```

Ce fichier contient des secrets : ne jamais le publier ; le supprimer après distribution.
Pour repartir de zéro (efface TOUTES les données) : `docker compose down -v`.

## Démarrage sans Docker (SQLite, développement)

```powershell
pip install -r requirements.txt
python -m backend.initialiser
uvicorn backend.main:app --port 8000
streamlit run frontend/app.py
```

## Tests

```powershell
pytest --cov=backend                       # SQLite temporaire
$env:TEST_DATABASE_URL="postgresql://utilisateur:mdp@localhost:5432/base_de_test"; pytest   # PostgreSQL (base vidée !)
```

## Migrations

Le schéma est géré par Alembic (`alembic/versions`). Il est appliqué automatiquement au démarrage.
Nouvelle migration après modification de `backend/models.py` : `alembic revision --autogenerate -m "description"`.

## Planning (phase 2)

- **Spécialités** (durée 15, 20 ou 30 min, couleur) : gérées par l'administrateur. La durée d'une consultation vient de la spécialité.
- **Horaires** : chaque médecin a des plages hebdomadaires (par défaut lundi-vendredi, 08:00-12:00 et 14:00-17:00). Les créneaux sont calculés à partir des plages ; le dernier créneau doit tenir dans la plage.
- **Jours fériés** : les jours fériés légaux sont générés automatiquement ; les fêtes dépendant de la lune (Korité, Tabaski...) et les fermetures exceptionnelles sont ajoutées par le responsable ou l'administrateur.
- **Indisponibilités** : un médecin déclare ses absences ; les créneaux concernés ne sont plus proposés et les rendez-vous déjà pris sont listés pour être reprogrammés (rien n'est déplacé automatiquement).
- **Prise de rendez-vous** : l'agent choisit un créneau libre (premiers créneaux libres ou jour précis) ; le serveur refuse tout rendez-vous hors plage, hors grille, un jour fermé ou pendant une indisponibilité (RG-03).
- **Taux d'occupation** (RG-10) : créneaux honorés ou absents / créneaux ouverts, affiché au tableau de bord.
- **Historique** (RG-11) : chaque modification d'un rendez-vous est tracée.
- **Absence automatique** (EF-17) : un rendez-vous planifié ou confirmé passe à « Absent » une heure après son début (tâche de fond de l'API ; seuls les 7 derniers jours sont traités).
- La migration `0002_planning` est appliquée automatiquement au démarrage de l'API ; les données existantes sont conservées.

## Analyse et prévision (phase 3)

- **Tableau de bord** : indicateurs du jour, carte de chaleur jour × heure, délai d'attente médian, annulations (par motif, mois, spécialité), évolution mensuelle, avec filtres période / spécialité / médecin. La direction ne voit que des agrégats.
- **Prévision** (EF-24) : volume hebdomadaire par spécialité sur 4 semaines, trois méthodes comparées (moyenne mobile, Holt, régression) sur 8 semaines tenues à l'écart (MAE, MAPE), intervalle d'environ 90 %.
- **Score de risque d'absence** (EF-25) : régression logistique et forêt aléatoire, variables autorisées uniquement, validation dans le temps. Le seuil de relance vient d'un calcul coût / bénéfice dont les hypothèses se règlent dans `.env` (`MEDIRDV_COUT_RELANCE`, `MEDIRDV_COUT_CRENEAU`, `MEDIRDV_EFFICACITE_RELANCE`). La page « Rendez-vous à relancer » (agents, responsable, administrateur) liste les rendez-vous à rappeler ; le score ne bloque jamais une prise de rendez-vous et n'est jamais visible du patient.
- **Module analytique** : le service `analytique` calcule les scores au démarrage puis chaque nuit à 02:00 (`MEDIRDV_HEURE_NOCTURNE`) et les écrit dans `scores_risque` / `evaluations_risque` ; l'API les lit. Calcul immédiat : `python -m backend.analytique.nocturne --une-fois` (ou bouton « Recalculer maintenant »).
- **Rapport d'analyse** reproductible : `notebooks/analyse_risque_absence.ipynb` (exploration, choix de variables, résultats chiffrés, limites). Pour le rejouer : `pip install jupyter matplotlib`, puis `jupyter nbconvert --to notebook --execute --inplace notebooks/analyse_risque_absence.ipynb`.
- La migration `0003_risque_absence` est appliquée automatiquement ; les données existantes sont conservées.

## Export et sécurité (phase 4)

- **Export CSV et PDF** (EF-26) : dans le tableau de bord (responsable, administrateur), « Exporter ces indicateurs ». Le fichier reprend les filtres de l'écran et les mêmes chiffres ; le CSV s'ouvre directement dans Excel en français (séparateur « ; », virgule décimale). Chaque export est inscrit dans le journal. Les cellules de texte commençant par `=`, `+`, `-` ou `@` sont neutralisées contre l'injection de formules.
- **En-têtes de sécurité HTTP** (ENF-06) sur toutes les réponses de l'API : `nosniff`, `X-Frame-Options: DENY`, politique de contenu `default-src 'none'`, `no-store`, `Referrer-Policy`, `Permissions-Policy`. Injections SQL : requêtes paramétrées partout ; des tests envoient des chaînes hostiles à la recherche, à la connexion et aux filtres. XSS : l'API ne renvoie que du JSON, et l'interface Streamlit n'exécute pas le HTML des données.
- **HTTPS** (ENF-07), optionnel : `docker compose --profile https up -d` démarre un proxy Caddy ; l'interface est alors sur `https://localhost:8443`. Avec `MEDIRDV_DOMAINE=votre-domaine` dans `.env`, le certificat public est obtenu automatiquement. En production, ne publier que ce service.
- **Sauvegarde et restauration** (ENF-10) : le service `sauvegarde` fait une sauvegarde par jour (14 conservées) ; `python -m backend.sauvegarde sauvegarder | lister | verifier | restaurer`. La restauration est testée automatiquement, y compris d'un moteur de base à l'autre. Procédure complète : `docs/sauvegarde_et_conservation.md`.
- **Conservation des données** (ENF-08) : `python -m backend.conservation` (simulation) puis `--appliquer` : anonymisation des patients inactifs depuis 5 ans, purge du journal et des pointages. Durées proposées et à valider, réglables dans `.env`.
- **Performance** (ENF-01, ENF-02) : `python tests/charge/charge.py --url http://localhost:8000 --cles <fichier de clés>` mesure 20 utilisateurs simultanés et le tableau de bord. Résultats mesurés (95e centile 252 ms, tableau de bord 1,2 s) et mode d'emploi : `docs/performance.md`.

## Livrables du marché (phase 5)

Les documents demandés par le cahier des charges (section 12.1) sont dans `livrables/` (PDF) ; leurs sources et le générateur sont dans `docs/livrables/` :

| Code | Document | Emplacement |
|---|---|---|
| L1 | Dossier de conception | `livrables/L1_dossier_de_conception.pdf` |
| L4 | Notebooks d'analyse (score d'absence, prévision) | `notebooks/` et `livrables/L4_*.pdf` |
| L5 | Rapport de couverture de tests (HTML) | `docs/livrables/L5_couverture_html/index.html` |
| L7 | Guide utilisateur par rôle | `livrables/L7_guide_utilisateur.pdf` |
| L9 | Cahier de recette et modèle de procès-verbal (à signer par la clinique) | `livrables/L9_cahier_de_recette_et_PV.pdf` |

Les PDF L1, L7 et L9 sont générés par du code et se régénèrent après toute modification de l'application :

```
python docs/livrables/captures.py chemin/cles_a_distribuer.csv   # captures d'écran (API et interface lancées sur une base de démonstration)
python docs/livrables/generer_livrables.py 221 88                # nombre de tests, couverture en % ; PDF écrits dans docs/livrables/pdf/ (à copier dans livrables/)
```

Rapport de couverture : `python -m pytest --cov=backend --cov-report=html:docs/livrables/L5_couverture_html`.
