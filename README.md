# multi-agent-orchestrator

Plugin **[Agent Plugins](https://agent-plugins.org/) v1.0.0** : bus de
coordination entre agents IA hétérogènes (Claude Code, Kimi, DeepSeek,
locaux…). Le plugin fournit le **mécanisme** — files de tâches avec bail
(claim/ack), dépôt de résultats, état observable — et **jamais le casting** :
les agents et leurs rôles sont déclarés par le projet consommateur dans son
roster.

## Architecture

- `servers/shared_memory/` — serveur MCP stdio. **Chaque client d'agents
  lance sa propre instance** : l'état vit dans une base **SQLite commune**
  (`store.py`, mode WAL), pas en mémoire du processus. Chemin :
  `ORCHESTRATOR_DB`, sinon `~/.local/share/multi-agent-orchestrator/orchestrator.db`.
  (`PLUGIN_DATA` ne convient pas : géré *par client*, donc invisible des
  autres agents.)
- `skills/pipeline-router/` — skill de routage : règles universelles
  (le volume aux forfaits, la masse au moins cher, le critique au meilleur
  raisonneur, jamais d'auto-revue), roster en entrée.
- Cycle d'une tâche : `queued → claimed (bail) → done`. Un bail expiré
  re-propose la tâche : un agent qui meurt après `claim_task` ne la perd pas.

## Mise en place

```bash
pip install -r requirements.txt          # SDK MCP (le cœur s'en passe)
python3 servers/shared_memory/store_test.py   # tests, dont le partage inter-processus
```

Côté projet consommateur : copier
`skills/pipeline-router/references/roster.example.json` en `roster.json`,
adapter le casting, déclarer `ORCHESTRATOR_ROSTER` (et `ORCHESTRATOR_DB` si
le chemin par défaut ne convient pas).

## Outils MCP

`register_agent` · `push_task` · `claim_task` · `publish_result`
(solde la tâche) · `read_result` · `get_system_state`.
