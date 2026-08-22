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

## Composants

- `servers/shared_memory/` — le serveur MCP (`server.py`, wrapper fin) et le
  cœur de stockage (`store.py`, sans dépendance MCP, testable seul). Cycle
  d'une tâche : `queued → claimed (bail) → done` ; un bail expiré re-propose
  la tâche — un agent qui meurt après `claim_task` ne la perd pas.
- `skills/pipeline-router/` — skill de routage : règles universelles (le
  volume aux forfaits, la masse au moins cher au token, le critique au
  meilleur raisonneur, jamais d'auto-revue), roster en entrée.

## Mise en place

```bash
pip install -r requirements.txt               # SDK MCP (le cœur s'en passe)
python3 servers/shared_memory/store_test.py   # tests, dont le partage inter-processus
```

Côté projet consommateur : copier
`skills/pipeline-router/references/roster.example.json` en `roster.json`,
adapter le casting, déclarer `ORCHESTRATOR_ROSTER` (et `ORCHESTRATOR_DB` si
le chemin par défaut ne convient pas).

## Outils MCP

`register_agent` · `push_task` · `claim_task` · `publish_result`
(solde la tâche) · `read_result` · `get_system_state`.
