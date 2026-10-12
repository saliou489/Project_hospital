# Performance : ENF-01 et ENF-02

## Objectifs (cahier des charges)

- **ENF-01** : réponse de l'API en moins de 500 ms pour 95 % des requêtes, avec 20 utilisateurs simultanés.
- **ENF-02** : chargement du tableau de bord en moins de 3 secondes sur 12 mois de données.

## Comment mesurer

Le script `tests/charge/charge.py` simule des utilisateurs réels (agents, médecins, responsables) contre une API en marche, puis mesure aussi les appels du tableau de bord, seul puis pendant la charge. Il n'est pas lancé par `pytest` : il vise une vraie API.

1. Démarrer l'application avec ses données de démonstration (12 mois) : `docker compose up -d --build`.
2. Récupérer le fichier de clés (`cles_a_distribuer_*.csv`, dans le volume `sortie`) : `docker compose cp initialisation:/sortie/. ./cles_temp` (puis le supprimer après l'essai : il contient des secrets).
3. Lancer : `python tests/charge/charge.py --url http://localhost:8000 --cles cles_temp/cles_a_distribuer_XXXX.csv`

Options : `--utilisateurs 20` (défaut), `--duree 60` (secondes), `--ecritures` (crée aussi des rendez-vous : base jetable uniquement). Code de sortie 0 si les deux objectifs sont atteints.

## Résultats mesurés

Conditions : PostgreSQL 16, environ 7 500 rendez-vous sur 12 mois, un seul processus API (comme dans le conteneur), machine de développement partagée. Lectures seulement.

| Mesure | Résultat | Objectif |
|---|---|---|
| 20 utilisateurs, 60 s (1 826 requêtes, 0 erreur) : 95e centile global | **252 ms** | < 500 ms |
| 40 utilisateurs, 30 s (1 672 requêtes, 0 erreur) : 95e centile global | 358 ms | (marge : le double de la charge demandée) |
| Tableau de bord, chargement initial, API seule | 0,93 s | < 3 s |
| Tableau de bord, chargement initial, pendant la charge à 20 utilisateurs | 1,18 s | < 3 s |
| Tableau de bord, chargement initial, pendant la charge à 40 utilisateurs | 2,24 s | < 3 s |
| Tableau de bord complet affiché dans le navigateur (Streamlit, connexion comprise) | 1,5 s | < 3 s |

Requête la plus lente : le taux d'occupation sur 12 mois (environ 0,7 s), qui parcourt tous les créneaux de l'année ; elle est déjà accélérée par un index en mémoire (4 s avant, 0,4 s à vide) et reste dans l'objectif.

## Limites de la mesure

- Les chiffres dépendent de la machine : refaire la mesure sur la machine de recette avant de conclure.
- Les connexions (hachage Argon2, volontairement lent, environ 175 ms) sont hors objectif : elles sont rares et doivent rester lentes pour protéger les clés.
- Charge de type lecture ; avec `--ecritures`, des créations de rendez-vous sont ajoutées.
- Une seule instance de l'API : si la charge réelle augmente, on peut lancer plusieurs processus (`--workers`) sans changer le code.
