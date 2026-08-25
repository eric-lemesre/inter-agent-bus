# inter-agent-bus

🇫🇷 Français · 🇬🇧 [English](README.md)

Plugin **[Agent Plugins](https://agent-plugins.org/) v1.0.0** : bus de
coordination entre agents IA hétérogènes (Claude Code, Kimi, DeepSeek,
modèles locaux…). Le plugin fournit le **mécanisme** — files de tâches avec
bail (claim/ack), dépôt de résultats partagé, état observable — et **jamais
le casting** : les agents et leurs rôles sont déclarés par le projet
consommateur dans son roster.

## Transport et état partagé — deux couches, un bus

Ce sont des couches différentes, et les deux sont vraies en même temps :

- **stdio est le transport** : chaque client d'agents lance **sa propre
  instance** du serveur MCP (c'est le fonctionnement des serveurs stdio —
  un sous-processus par client). Rien n'est partagé à cette couche.
- **SQLite est le bus** : toutes les instances lisent et écrivent la **même
  base** (mode WAL, transactions immédiates). C'est *là* que le partage a
  lieu.

L'alternative sans base serait un unique serveur `streamable-http` qui
*serait* la mémoire — mais il faudrait démarrer, superviser et sécuriser ce
démon. Pour un poste local multi-CLI, stdio + bus SQLite l'emporte.

Chemin de la base — ordre de résolution : (1) la variable `IAB_DB`,
qui fait autorité — la poser par projet au moindre doute (l'héritée
`ORCHESTRATOR_DB` reste honorée) ; (2) une base globale ou antérieure
au renommage existante est conservée (migration) ; (3) sinon **une
base par projet**, dérivée du répertoire de lancement (normalisé par
realpath) sous le répertoire de données de la plateforme — une
installation scope user ne doit pas fusionner tous les projets dans un
seul bus. Tous les participants d'un projet doivent résoudre le même
chemin : `whoami()` renvoie le chemin résolu, sa source et la clé de
projet — un désaccord de rendez-vous se diagnostique en un appel.
(`PLUGIN_DATA` ne convient pas comme bus : la spec le définit *par
client*, donc invisible des autres agents.)

Évolutions prévues et leurs invariants : [`ROADMAP.fr.md`](ROADMAP.fr.md).
Règles de contribution (humain ou agent) : [`AGENTS.md`](AGENTS.md).

## Composants

- `servers/shared_memory/` — le serveur MCP (`server.py`, wrapper fin) et le
  cœur de stockage (`store.py`, sans dépendance MCP, testable seul). Cycle
  d'une tâche : `queued → claimed (bail) → done`, avec deux sorties
  terminales : `dead` (mise en dead-letter après `max_attempts` baux
  expirés — `requeue_task` la ranime) et `cancelled`. Un bail expiré
  re-propose la tâche — un agent qui meurt après `claim_task` ne la perd
  pas ; et le solde est clôturé par le jeton de tentative du claim — un
  worker périmé ne peut pas écraser le résultat de celui qui a repris la
  tâche.
- `skills/pipeline-router/` — skill de routage (côté orchestrateur) :
  règles universelles (le volume aux forfaits, la masse au moins cher au
  token, le critique au meilleur raisonneur, jamais d'auto-revue), roster
  en entrée.
- `skills/worker-loop/` — skill worker (côté consommateur) :
  enregistrement sous l'identité donnée par l'opérateur, puis boucle
  `claim_task` → exécution → `publish_result` ; discipline d'échec
  (résultats `ERROR:` plutôt que baux expirés en silence, refus
  d'auto-revue, passage de main à l'épuisement du budget).

## Mise en place

```bash
python3 -m venv .venv                                  # py -m venv .venv sous Windows
.venv/bin/pip install -r requirements.txt -e .         # SDK MCP >= 2.0 + la CLI `iab`
.venv/bin/python servers/shared_memory/store_test.py   # tests, dont le partage inter-processus
```

Le point d'entrée console `iab` reflète les outils MCP (`iab register /
push / claim / publish / cancel / requeue / extend / result / state /
log / whoami`) : le bus se pilote sans MCP et sans `python -c`. Un payload donné comme `-` (ou
omis) est lu sur stdin — ne jamais construire une ligne de commande
shell autour d'un payload. `iab log [task_id]` restitue le journal des
transitions (push/claim/expire/publish) ; `iab whoami` affiche
l'identité issue de l'environnement et le chemin de base résolu.

`iab install --scope user` déclare le serveur MCP au niveau
utilisateur de Claude Code (via `claude mcp add-json`), avec
l'interpréteur du venv et le chemin du serveur en absolu et
`IAB_AGENT_NAME=claude` gravé dans `env` (`--agent-name` pour changer,
`--print` pour émettre la variante scope user du `mcp.json` projet de
ce dépôt sans l'appliquer, `--scope project|local` pour des portées
plus étroites). Un serveur ajouté ne se
charge qu'à la *prochaine* session — rouvrir, puis vérifier avec
`whoami()`. Réserve d'une installation scope user : toutes les
sessions de ce client partagent l'identité gravée, et chaque projet
reçoit sa propre base de bus sauf si `IAB_DB` en décide autrement.

Pointer le `command` des clients vers `.venv/bin/python` (chemin absolu) :
le serveur doit tourner sous un interpréteur qui a le SDK MCP.

Côté projet consommateur : copier
`skills/pipeline-router/references/roster.example.json` en `roster.json`,
adapter le casting, déclarer `IAB_ROSTER` (et `IAB_DB` si le chemin par
défaut ne convient pas). Les anciens noms `ORCHESTRATOR_*` restent
honorés.

Chaque client d'agent enregistre le serveur MCP (avec un client MCP
générique, utiliser des **chemins absolus** — la convention `cwd` = racine
du plugin n'engage que les clients qui implémentent la spec Agent
Plugins). La session qui orchestre utilise `pipeline-router` ; chaque
session worker utilise `worker-loop` avec une identité donnée par
l'opérateur.

## Workers headless

Certains CLI d'agents ne chargent aucun serveur MCP en mode non
interactif (la défaillance de terrain derrière la demande de worker
daemon) : ils ne peuvent pas tenir leur propre boucle claim/publish.
`iab worker` la tient pour eux :

```bash
iab worker --agent kimi --once -- kimi --exec -    # à adapter aux options du CLI
```

Claim sous bail → exécution de la commande avec le payload **sur
stdin** (jamais sur la ligne de commande) → publication de stdout avec
le jeton de tentative du claim. Code de sortie non nul, sortie vide ou
`--task-timeout` produisent un résultat `ERROR:` plutôt qu'un bail qui
expire en silence ; pendant l'exécution, le bail est renouvelé à
chaque battement — un travail long n'est pas re-proposé en plein vol.
`--once` traite une seule tâche (sortie 0 propre, 1 sur ERROR) ; sinon
la boucle interroge avec un recul jusqu'à 60 s. La livraison est
at-least-once : les commandes worker doivent être idempotentes.

Le repli local de dernier ressort du roster d'exemple se lance de la
même façon — ollama lit le prompt sur stdin :

```bash
iab worker --agent qwen-local -- ollama run qwen3-coder:30b
```

## Revues gardées

Né du terrain : un CLI de revue a renvoyé un succès silencieusement
*vide* ; un autre, sans accès fichiers, a *halluciné* le diff qu'on lui
demandait de citer. `iab review` ferme les deux brèches :

```bash
iab review --agent deepseek --staged         -- <commande de revue>
iab review --agent deepseek --diff work.diff -- <commande de revue>
```

Le diff intégral est embarqué dans le prompt (le seul transport
fiable) ; le relecteur doit répondre par un unique objet JSON (verdict,
constats avec sévérités) reprenant un nonce propre à l'exécution (test
de vie) ; et chaque constat doit pointer un fichier et une ligne
réellement couverts par les hunks du diff. Un prompt qui dépasse le
`context_window` de l'agent au roster est refusé, jamais tronqué. Le
verdict vérifié — ou le rejet `ERROR:`, sortie brute jointe — est
publié sur le bus sous `--task-id`. La garde ne filtre que les
défaillances *mécaniques* ; elle ne juge pas le fond : maintenir la
revue croisée par un autre agent (règle du routeur).

## Outils MCP

`whoami` (identité du client connecté — variable `IAB_AGENT_NAME` si le
lanceur ou l'enregistrement MCP l'a posée, sinon le clientInfo du
handshake MCP, à confronter aux `client_hints` du roster — plus le
chemin résolu de la base du bus) ·
`register_agent` · `push_task` · `claim_task` (tête de sa file, ou une
tâche précise via `task_id`) · `publish_result` (solde la tâche ;
passer le jeton `attempt` du claim — clôture de bail) · `cancel_task` ·
`requeue_task` · `extend_lease` · `read_result` · `get_system_state` ·
`get_events` (journal des transitions, filtrable par tâche et/ou
agent).

Note d'identité : graver `IAB_AGENT_NAME` dans l'enregistrement MCP de
chaque client (champ `env`) est le moyen fiable de donner son identité à
chaque worker — certains clients n'annoncent qu'un nom de SDK générique
dans le clientInfo.

## Sécurité / modèle de confiance

Le bus suppose une machine mono-utilisateur. Tout ce qui peut écrire la
base peut piloter des agents autonomes et outillés : l'accès en
écriture est adjacent à l'injection de prompt — donc adjacent à
l'exécution de code. Le fichier de base est forcé en permissions
propriétaire-seul (0600) sous POSIX à chaque connexion ; sous Windows
il hérite des ACL du profil utilisateur — garder le répertoire de
données privé. Le worker headless tient les payloads hors de la ligne
de commande (stdin uniquement) et hors de ses journaux (stderr montre
les identifiants de tâche, jamais le contenu). SQLite en mode WAL exige
un système de fichiers local : ne pas placer le bus sur un partage
NFS/SMB.
