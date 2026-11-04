# Amélioration en attente — mode `--payload-file` pour les drivers

Notée le 2026-08-25, à l'issue de la livraison des phases 1–6 de la
trame. À intégrer à la ROADMAP **au premier CLI réel qui l'exige** —
pas avant : c'est une surface supplémentaire (nettoyage, sémantique
Windows) qui ne se justifie que sur constat de terrain.

## Constat

`iab worker` et `iab review` passent le payload aux commandes
exclusivement **sur stdin**. C'est le bon défaut (rien à nettoyer,
rien ne touche le disque hors du bus, comportement uniforme sur les
trois plateformes, pas de deadlock grâce à `communicate()`), mais il
existe des CLI d'agents qu'il ne couvre pas :

1. ceux qui n'acceptent un prompt que par **chemin de fichier**
   (`--file prompt.md`), sans convention `-` ;
2. ceux qui **changent de comportement quand stdin est un pipe**
   (détection du mode non interactif, capacités dégradées — la famille
   de problèmes `kimi -p` incompatible `--auto`) ;
3. le besoin futur d'**entrées multiples** (diff + référentiel de
   règles en documents séparés) — stdin est un flux unique.

## Proposition

Option opt-in `--payload-file` sur `iab worker` et `iab review` :

- écrire le payload via `tempfile.mkstemp` (donc `0600`) ;
- substituer un marqueur `{payload_file}` dans l'argv de la commande —
  passer un **chemin** généré par nous en argv est sain ; l'interdit
  de la règle 3 d'AGENTS.md porte sur l'interpolation du *contenu*,
  et il reste entier ;
- supprimer le fichier dans un `finally`, y compris sur échec ;
- Windows : `delete=False`, fermer le descripteur avant de lancer la
  commande (l'enfant ne peut pas lire un fichier encore ouvert par le
  parent), suppression manuelle.

Stdin reste le défaut ; le mode fichier n'est jamais implicite.

## Critère d'acceptation

Un CLI sans support stdin exécute une tâche de bout en bout via
`iab worker --payload-file` ; le fichier temporaire n'existe plus
après l'exécution (succès, échec et timeout) ; les tests couvrent la
substitution du marqueur et le nettoyage sur les trois issues.
