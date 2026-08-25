# multi-agent-orchestrator

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

Chemin de la base : variable `ORCHESTRATOR_DB`, défaut
`~/.local/share/multi-agent-orchestrator/orchestrator.db`. (`PLUGIN_DATA` ne
convient pas comme bus : la spec le définit *par client*, donc invisible des
autres agents.)

Évolutions prévues et leurs invariants : [`ROADMAP.fr.md`](ROADMAP.fr.md).
Règles de contribution (humain ou agent) : [`AGENTS.md`](AGENTS.md).

## Composants

- `servers/shared_memory/` — le serveur MCP (`server.py`, wrapper fin) et le
  cœur de stockage (`store.py`, sans dépendance MCP, testable seul). Cycle
  d'une tâche : `queued → claimed (bail) → done` ; un bail expiré re-propose
  la tâche — un agent qui meurt après `claim_task` ne la perd pas.
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
python3 -m venv .venv                              # les pythons système sont souvent gérés en externe
.venv/bin/pip install -r requirements.txt          # SDK MCP >= 2.0 (le cœur s'en passe)
.venv/bin/python servers/shared_memory/store_test.py   # tests, dont le partage inter-processus
```

Pointer le `command` des clients vers `.venv/bin/python` (chemin absolu) :
le serveur doit tourner sous un interpréteur qui a le SDK MCP.

Côté projet consommateur : copier
`skills/pipeline-router/references/roster.example.json` en `roster.json`,
adapter le casting, déclarer `ORCHESTRATOR_ROSTER` (et `ORCHESTRATOR_DB` si
le chemin par défaut ne convient pas).

Chaque client d'agent enregistre le serveur MCP (avec un client MCP
générique, utiliser des **chemins absolus** — la convention `cwd` = racine
du plugin n'engage que les clients qui implémentent la spec Agent
Plugins). La session qui orchestre utilise `pipeline-router` ; chaque
session worker utilise `worker-loop` avec une identité donnée par
l'opérateur.

## Outils MCP

`whoami` (identité du client connecté : variable `ORCHESTRATOR_AGENT_NAME`
si le lanceur ou l'enregistrement MCP l'a posée, sinon le clientInfo du
handshake MCP, à confronter aux `client_hints` du roster) ·
`register_agent` · `push_task` · `claim_task` · `publish_result`
(solde la tâche) · `read_result` · `get_system_state`.

Note d'identité : graver `ORCHESTRATOR_AGENT_NAME` dans l'enregistrement
MCP de chaque client (champ `env`) est le moyen fiable de donner son
identité à chaque worker — certains clients n'annoncent qu'un nom de SDK
générique dans le clientInfo.
