# Corrections et améliorations demandées — retour d'usage du 2026-08-25

Dossier rédigé par l'instance Claude qui conduit le jalon J0010 du projet
`IA-conciliateur-justice` (protocole de revue croisée multi-IA, ADR 0012 de ce
projet). Chaque constat vient d'une **friction réellement rencontrée** en
pilotant le bus depuis une session Claude Code non raccordée, avec Kimi et
DeepSeek en workers headless. À traiter par une autre instance ; les
priorités sont ordonnées par impact.

## Contexte d'usage constaté

- Le cœur (`servers/shared_memory/store.py`) est sain : sans dépendance MCP,
  pilotable en Python direct sur la base
  `~/.local/share/multi-agent-orchestrator/orchestrator.db` — c'est ce qui a
  permis de tenir la discipline du bus malgré tout.
- Le serveur MCP fonctionne pour Kimi (`~/.kimi/mcp.json`) et DeepSeek
  (`~/.codewhale/mcp.json`) **en session interactive seulement**.
- La session Claude Code n'avait aucun accès au bus (plugin non installé,
  aucun `mcpServers` projet) ; les workers en mode headless non plus.

## P0 — Installation globale côté Claude Code

**Demande explicite du porteur : l'installation du plugin doit être globale,
pour en bénéficier quelle que soit la session et quel que soit le projet.**

- Fournir la procédure (et si possible un script `install.sh`) qui déclare le
  serveur MCP **au niveau utilisateur** de Claude Code (`~/.claude.json`,
  scope user — pas par projet), avec :
  - `command` : le python du venv du plugin (chemin absolu) ;
  - `args` : `servers/shared_memory/server.py` (chemin absolu) ;
  - `env` : `ORCHESTRATOR_AGENT_NAME=claude`.
- Documenter la vérification (`whoami()` dès l'ouverture d'une session) et le
  fait qu'un serveur ajouté ne se charge qu'à la **prochaine** session.
- Faire lire `ORCHESTRATOR_ROSTER` et `ORCHESTRATOR_DB` depuis l'environnement
  du projet appelant (aujourd'hui les variables ne vivent que dans les
  `.env.sample` ; seuls les défauts implicites fonctionnent).

## P1 — CLI de pilotage (sans MCP)

Un point d'entrée console `orchestrator` (ou `mao`) exposant le cœur :
`push`, `claim`, `publish`, `result`, `state`, `log`. Motif : sans MCP
raccordé, tout pilotage passe par des `python -c` verbeux et fragiles. Le
cœur étant déjà découplé, c'est essentiellement de l'emballage argparse +
`console_scripts`.

## P2 — Mode « worker daemon » headless

Constat bloquant : `kimi -p` **ne charge pas les serveurs MCP** (signalé par
Kimi lui-même) ; `-p` est en outre incompatible avec `--auto` et `--yolo`.
Les workers ne peuvent donc pas faire leur boucle `claim → publish` en mode
non interactif — l'orchestrateur doit passer le payload inline et tenir le
registre à leur place.

- Ajouter `orchestrator worker --agent kimi --cmd 'kimi -p {payload}'`
  (idem deepseek/qwen) : boucle `claim_task` → substitution `{payload}` →
  exécution du CLI → `publish_result` avec la sortie ; en échec, publication
  préfixée `ERROR:` (règle de la skill worker-loop) au lieu de laisser
  expirer le bail.
- Option `--once` (une tâche puis sortie) pour un pilotage pas-à-pas.

## P3 — `claim_task` ciblable et cycle de vie explicite

Constat : `claim_task(agent)` prend la tête de file ; en réclamant pour
`kimi`, l'orchestrateur a récupéré une **ancienne tâche requeuée** (bail
expiré silencieusement) au lieu de la tâche de correction fraîchement
poussée.

- `claim_task(agent, task_id=…)` (claim ciblé) ;
- statuts explicites et visibles : `queued / claimed / expired / done /
  error / cancelled` ;
- commandes `requeue(task_id)` et `cancel(task_id)` ;
- horodatage des transitions, conservé (voir P5).

## P4 — Driver de revue avec contenu embarqué et garde anti-hallucination

Constats sérieux côté DeepSeek CLI (0.8.24) :

- `deepseek review --staged --json` → `{"success": true, "content": ""}` —
  revue silencieusement vide ;
- `deepseek exec` **n'a pas d'accès fichiers** (ni `/tmp`, ni le dépôt) et,
  pire, **hallucine** : sommé de citer la première ligne d'un diff, il a
  inventé `diff --git a/src/App.tsx …` qui n'existe nulle part ;
- seul l'embarquement du **diff intégral dans le prompt** est fiable.

À intégrer au plugin comme driver : `orchestrator review --agent deepseek
--diff <fichier|--staged>` qui (1) génère le diff, (2) l'inline dans le
prompt avec le gabarit de revue (sévérités, verdict), (3) **vérifie que la
sortie cite des lignes réellement présentes dans le diff** (garde
anti-hallucination — rejeter et republier `ERROR:` sinon), (4) publie le
résultat sur le bus.

## P5 — Observabilité

`get_system_state()` est un dump JSON brut. Ajouter `orchestrator log
[task_id]` : historique horodaté des transitions (push/claim/expire/publish),
par tâche et par agent — directement réutilisable dans les fiches de revue
(`docs/revues/`) et les rapports de jalon du projet appelant.

## P6 — Divers

- `README` : documenter le repli `qwen-local` (ollama) comme worker de
  dernier ressort, avec l'exemple de commande.
- `mcp.json` d'exemple : ajouter la variante « scope user » (cf. P0) à côté
  de la variante projet.
- Signaler dans la doc la limite de contexte par agent (le roster la porte
  déjà) et tronquer/refuser proprement un payload qui la dépasse.

## Critère global d'acceptation

Depuis une session Claude Code fraîche, sans configuration projet : pousser
une tâche à Kimi, la voir exécutée par `orchestrator worker` headless,
récupérer le résultat, et lancer une revue DeepSeek fiable sur un diff — le
tout sans un seul `python -c` et avec un historique consultable.
