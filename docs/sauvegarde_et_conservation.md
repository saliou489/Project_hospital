# Sauvegarde, restauration et conservation des données

Ce document répond à ENF-10 (sauvegarde quotidienne, restauration testée) et ENF-08 (durée de conservation documentée).

## 1. Sauvegarde

- **Automatique** : le service `sauvegarde` (docker-compose) fait une sauvegarde au démarrage, puis une par jour à 03:00 (`MEDIRDV_HEURE_SAUVEGARDE`). Les 14 plus récentes sont gardées (`MEDIRDV_NB_SAUVEGARDES`).
- **À la demande** : `docker compose exec api python -m backend.sauvegarde sauvegarder`
- **Voir les sauvegardes** : `docker compose exec api python -m backend.sauvegarde lister`
- **Contrôler une sauvegarde** (empreintes SHA-256 de chaque table, sans rien restaurer) : `docker compose exec api python -m backend.sauvegarde verifier medirdv_AAAAMMJJ_HHMMSS.zip`

Une sauvegarde est un fichier ZIP : un manifeste (date, version du schéma, nombre de lignes et empreinte de chaque table) et une table par fichier. Elle est cohérente (lue en une seule transaction) et n'apparaît sous son nom définitif qu'une fois complète.

**Sortir une sauvegarde de la machine** (indispensable : une sauvegarde sur le même disque ne protège pas d'une panne du disque) :
`docker compose cp sauvegarde:/sauvegardes/medirdv_AAAAMMJJ_HHMMSS.zip .` puis la copier sur un support protégé.

**Attention** : le fichier contient des données personnelles et les empreintes des clés. Ne le publiez jamais (ni GitHub, ni messagerie ouverte), protégez-le comme la base elle-même.

## 2. Restauration

Cas courant : la base est perdue ou corrompue, ou on veut revenir à un état antérieur.

1. Arrêter les services qui écrivent : `docker compose stop interface api analytique sauvegarde`
2. Contrôler le fichier : `docker compose run --rm api python -m backend.sauvegarde verifier medirdv_AAAAMMJJ_HHMMSS.zip`
3. Restaurer :
   - base **vide** (nouvelle installation) : `docker compose run --rm api python -m backend.sauvegarde restaurer medirdv_AAAAMMJJ_HHMMSS.zip`
   - base **non vide** : ajouter `--remplacer`. Cette option EFFACE les données actuelles ; une sauvegarde de sécurité de l'état actuel (`..._avant_restauration.zip`) est faite d'abord et n'est jamais supprimée automatiquement.
4. Redémarrer : `docker compose up -d`

Garanties : la restauration est faite en une seule transaction (en cas d'erreur, la base reste intacte), refusée si le fichier est altéré, si le schéma est d'une autre version, ou si la base n'est pas vide sans `--remplacer`. Les numéros automatiques (journal, historique...) repartent après le plus grand numéro restauré.

## 3. Essai de restauration (à refaire après chaque changement important)

Les tests automatiques (`tests/test_sauvegarde.py`) vérifient, sur PostgreSQL et SQLite : restauration identique (mêmes empreintes de table), refus d'un fichier altéré, refus d'un schéma incompatible, refus d'écraser une base non vide, annulation complète en cas d'échec, restauration d'un moteur vers l'autre.

Essai à la main, sans toucher aux données réelles :

1. `docker compose exec api python -m backend.sauvegarde sauvegarder`
2. `docker compose exec db createdb -U medirdv medirdv_essai`
3. `docker compose run --rm -e DATABASE_URL=postgresql://medirdv:MOT_DE_PASSE@db:5432/medirdv_essai api python -m backend.sauvegarde restaurer medirdv_AAAAMMJJ_HHMMSS.zip`
   (le message final indique le nombre de lignes restaurées : il doit être égal à celui de la sauvegarde)
4. Supprimer la base d'essai : `docker compose exec db dropdb -U medirdv medirdv_essai`

## 4. Conservation des données (loi n° 2008-12)

- **Finalité** : organiser les rendez-vous de la clinique et en suivre l'activité. **Minimisation** : aucune donnée médicale (diagnostic, traitement) n'est stockée ; un patient n'est décrit que par son identité et ses coordonnées de contact. Les journaux applicatifs ne contiennent aucune donnée personnelle en clair.
- **Durées proposées** (à faire valider par la clinique et à reporter dans sa déclaration à la CDP ; réglables dans `.env`) :

| Donnée | Durée | Au terme |
|---|---|---|
| Patient (identité, téléphone, quartier, date de naissance) | 5 ans après le dernier rendez-vous, sans rendez-vous à venir | anonymisation ; les rendez-vous restent, sans lien avec une personne |
| Rendez-vous et historique de leurs modifications | conservés | sans patient identifiable après anonymisation : statistiques historiques |
| Journal des actions | 24 mois | suppression |
| Pointages des employés | 24 mois | suppression |
| Évaluations du score de risque | 12 mois | suppression |
| Sauvegardes | 14 dernières | suppression de la plus ancienne |

- **Application** : `docker compose exec api python -m backend.conservation` simule (rien n'est modifié et le nombre d'éléments concernés est affiché) ; ajouter `--appliquer` pour exécuter. À lancer chaque mois, par exemple depuis le planificateur de la machine ou en recette. L'exécution est inscrite dans le journal.
- **Droit à l'effacement** : `docker compose exec api python -m backend.conservation --patient PAT00001 --appliquer` anonymise un patient à sa demande ; ses rendez-vous passés restent, sans lien avec lui.
- **Limite** : les sauvegardes déjà faites contiennent les données telles qu'elles étaient ; elles disparaissent à leur tour avec la rotation (14 jours).
