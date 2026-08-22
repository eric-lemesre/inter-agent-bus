---
name: pipeline-router
description: Décomposer une demande en sous-tâches et les router vers les agents du roster déclaré par le projet, selon des règles coût/capacité universelles — à utiliser pour orchestrer plusieurs agents IA via le bus partagé (push_task/claim_task/publish_result).
---

# Pipeline Router

## Rôle

Analyser une demande ou un ticket, la subdiviser en sous-tâches, et assigner
chaque sous-tâche à l'agent **du roster du projet** le mieux placé. Cette
skill ne connaît **aucun agent par avance** : le casting est une politique du
projet consommateur, jamais du plugin.

## Le roster

Le projet déclare ses agents dans un fichier de roster (chemin dans la
variable `ORCHESTRATOR_ROSTER`, sinon `roster.json` à la racine du projet).
Chaque entrée décrit : `name`, `provider`, `cost_model`
(`flat` = forfait, `credits` = au token, `local` = gratuit), `budget_cap`
éventuel, `specialties` (mots-clés de capacités), `context_window`, `notes`.
Un exemple complet : [`references/roster.example.json`](references/roster.example.json).

Avant tout routage : lire le roster, puis `register_agent(name, description)`
pour chaque agent afin de créer les files.

## Règles de routage universelles

1. **Le volume va aux forfaits** (`cost_model: flat`) : leur coût marginal est
   nul — génération de masse, refactoring large, suites de tests.
2. **Le traitement de masse économe va au moins cher au token**
   (`credits` à bas prix, ou `local`) : parsing, formatage, triage, mocks.
3. **Le critique va au meilleur raisonneur** : architecture, code de
   sécurité, revue finale — indépendamment du coût.
4. **Jamais d'auto-revue** : l'agent qui relit une production est toujours
   différent de celui qui l'a écrite.
5. **Respecter les plafonds** : un agent à `budget_cap` proche de l'épuisement
   bascule ses tâches vers l'agent de repli déclaré (`fallback` du roster).
6. **Les très longs contextes vont aux grandes fenêtres** (`context_window`) :
   ingestion de dépôts entiers, journaux volumineux, spécifications longues.

## Workflow d'exécution

1. Lire le roster ; `register_agent(...)` pour chaque agent.
2. Décomposer la demande en sous-tâches à **contrat fermé** (entrées, sorties,
   critères d'acceptation).
3. Pour chaque sous-tâche : choisir l'agent par les règles ci-dessus, puis
   `push_task(target_agent=..., task_id=..., payload=<contrat>, priority=...)`.
4. Chaque agent travaille en boucle : `claim_task(agent_name=...)` →
   exécution → `publish_result(...)` (le résultat solde la tâche ; un bail
   expiré la re-propose automatiquement).
5. Les dépendances se résolvent par `read_result(task_id)` — une sous-tâche
   aval référence dans son payload les `task_id` amont dont elle a besoin.
6. Superviser avec `get_system_state()` ; router les revues croisées en
   appliquant la règle 4.

## Aide au routage hors session

[`scripts/router.py`](scripts/router.py) propose un routage heuristique à
partir du roster : `python3 scripts/router.py <roster.json> "<description>"`.
C'est un dépannage pour scripts et hooks — en session, c'est l'agent
orchestrateur qui route, avec les règles ci-dessus.
