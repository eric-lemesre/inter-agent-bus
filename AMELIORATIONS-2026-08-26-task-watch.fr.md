# Amélioration en attente — veille d'arrivée de tâches pour les workers

🇬🇧 [English](AMELIORATIONS-2026-08-26-task-watch.md) · 🇫🇷 Français

Notée le 2026-08-26, après un contournement de terrain dans une session
worker Kimi CLI (jalon J0011 d'`IA-conciliateur-justice`, protocole trois
mains). À intégrer à la ROADMAP comme candidat de phase 8 — décision laissée
au mainteneur.

## Constat

Une session worker interactive ne découvre les tâches de sa file que lorsqu'un
humain (ou l'orchestrateur par un autre canal) la prévient, ou lorsqu'elle
interroge manuellement. Le 2026-08-26, le worker Kimi a dû être réveillé par
l'utilisateur (« tu as une tâche qui t'attends »), puis a installé un
**watcher bash lié à la session** qui interroge directement la base SQLite du
bus (`SELECT … FROM tasks WHERE agent=? AND status='queued'`) pour être
réveillé à la prochaine arrivée. Ça fonctionne, mais ça viole les invariants
du plugin :

- ça **court-circuite la couche MCP/store** en lisant directement le fichier
  de base — logique dupliquée hors de `store.py` ;
- ça **meurt avec la session** — aucune persistance, aucune supervision ;
- c'est un **script shell** — non multi-plateforme (invariant 4), et chaque
  runtime d'agent finit par le réinventer.

## Briques existantes

- Une table `notifications` et des signaux dirigés existent déjà
  (`store.notify` / `store.poll_notifications`, outils MCP `notify` / `poll`,
  CLI `iab notify` / `iab poll`).
- Mais `push_task` **n'émet aucune notification** vers l'agent cible, et rien
  ne permet à un worker de **se bloquer jusqu'à l'arrivée d'une tâche** — pas
  d'entrée de type `wait` ni dans le store, ni dans le CLI, ni dans le
  serveur MCP.

## Proposition

Du mécanisme, jamais de distribution (invariant 1). Le cœur d'abord
(invariant 2) : tout ce qui suit atterrit dans `store.py` avec des tests dans
`store_test.py` exécutables sans le SDK MCP, puis des enveloppes minces.

1. **Notification au push.** `push_task` insère une notification dirigée vers
   l'agent cible (« tâche `<id>` en file, priorité N »). Drapeau de retrait
   `notify=False` pour les pousseurs à haute fréquence. La discipline
   at-least-once s'applique déjà : la notification est un indice pour
   `claim_task`, pas un canal de livraison.
2. **Attente bloquante dans le cœur.** `store.wait_for_task(agent, timeout_s,
   interval_s) -> list[str]` : interroge la file de l'agent et renvoie les
   identifiants des tâches en file dès que l'une apparaît, ou une liste vide
   au timeout. Boucle `time.sleep` en pur Python — aucune primitive
   spécifique à l'OS.
3. **Enveloppe CLI.** `iab watch <agent> [--timeout N] [--interval N]` —
   affiche les identifiants en file sur stdout et sort 0 dès qu'une tâche
   apparaît, sort 0 sans sortie au timeout. C'est la primitive qu'un
   superviseur (unité systemd utilisateur, cron, ou tâche de fond d'une
   session d'agent) peut exécuter pour réveiller un worker sans script
   maison.
4. **Outil MCP optionnel** `wait_task(agent, timeout_seconds)` — même appel
   cœur, pour les runtimes capables d'émettre un appel d'outil en arrière-
   plan. Documenté comme bloquant : il retient l'appel jusqu'au retour.

Le skill worker-loop documente alors le motif : *watch → claim → execute →
publish → watch à nouveau*, et chaque worker obtient la persistance
gratuitement via le superviseur qui enveloppe `iab watch`.

## Critères d'acceptation

- `store_test.py` : `push_task` rend la notification visible via
  `poll_notifications` pour la cible (et pas pour les autres) ;
  `wait_for_task` renvoie l'identifiant quand un autre thread/processus
  pousse pendant l'attente, et renvoie vide après le timeout sinon.
- `iab watch` se comporte à l'identique sous Linux, macOS et Windows
  (pur Python, `pathlib`, aucun shell).
- README (EN/FR) et `skills/worker-loop/` mis à jour pour enseigner le
  motif ; l'avertissement at-least-once demeure (un worker réveillé peut
  trouver une file vide — expiration de bail, annulation — et doit le
  tolérer).
