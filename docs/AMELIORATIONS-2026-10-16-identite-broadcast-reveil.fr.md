# Amélioration en attente — identité harnais@espace, diffusion, réveil

🇬🇧 [English](AMELIORATIONS-2026-10-16-identite-broadcast-reveil.md) · 🇫🇷 Français

Notée le 2026-10-16 après un incident d'adressage sur le bus de
production (base héritée `multi-agent-orchestrator/orchestrator.db`,
partagée par tous les projets). Demande du porteur : (1) qu'un agent
s'enregistre sous le nom de son **harnais** (claude, kimi, codewhale,
opencode…) **et** de l'**espace de travail** où il œuvre (homelab,
infra…) ; (2) des demandes en **diffusion** ; (3) que le plugin puisse
**réveiller** un agent sur une demande ou une diffusion. Candidat de
phase 8 de la ROADMAP — découpé en trois lots indépendants, à livrer
dans l'ordre 8a → 8b → 8c (8b et 8c s'appuient sur l'adressage de 8a).

## 1. Constat de terrain (2026-10-16)

Relevé sur le poste, sans rien modifier :

- **Six** serveurs `iab-server` tournaient en même temps. Quatre avec
  `IAB_AGENT_NAME=claude`, lancés depuis quatre répertoires distincts
  (`AEC/LeadSage`, `eric-lemesre/HomeLab`, `IA-conciliateur-justice`,
  `/home/eric`), un `kimi` (dépôt Brother), un sans nom (ce dépôt).
  Tous résolvent la **même** base (règle « base héritée conservée »).
- Dans la base, l'entrée `claude` est **une seule ligne** : décrite comme
  « Architecte-intégrateur (dépôt IA-conciliateur-justice) », TTL 7200 s
  posé par cette session-là, mais rafraîchie par le piggyback de
  **toutes** les sessions Claude. Le statut `alive` de `claude` ne dit
  donc plus rien ; la file `claude` tient une tâche `queued` dont on ne
  sait plus à quelle session elle était destinée.
- La session HomeLab a signé ses annonces et notifications
  `claude-homelab` (3 annonces, 7 notifications) : ce nom n'est **pas
  enregistré**, ses `poll()` ont toujours rendu `[]`. Kimi lui a répondu
  à l'adresse `claude` (deux notifications du 15/10, 08 h 27 et 09 h 14)
  : elles ont été lues **20 heures plus tard**, par hasard, en relevant
  sous l'autre nom. Entre-temps Kimi avait appliqué une modification de
  la chaîne mail que la session HomeLab croyait en attente de feu vert.
- `poll()` avance **un curseur par cible** : avec quatre sessions sous le
  même nom, la première qui relève consomme les notifications des trois
  autres. Ce n'est pas théorique : le curseur `claude` est à 31.
- `announce` et `notify` acceptent n'importe quel `author` : rien ne
  signale qu'un auteur n'est pas enregistré. C'est ce qui a permis au
  fantôme `claude-homelab` d'exister pendant six semaines.
- Cause racine documentée dès la phase 2 (« Honnêteté sur l'identité »)
  : `iab install --scope user` cuit **une** identité pour **toutes** les
  sessions du client. Le remède de l'époque (une base par projet) a été
  neutralisé par la règle de conservation de la base héritée — voulue,
  car le porteur veut bien un bus **inter-projets** (Kimi sur Brother
  parle à Claude sur HomeLab). Le problème n'est donc pas la base
  partagée : c'est l'identité à un seul composant.

## 2. Lot 8a — identité `harnais@espace`

### 2.1 Modèle

Un agent est une **instance** : un harnais (le client/CLI qui le porte)
posé sur un espace de travail (le dépôt ou dossier où il travaille).

```
<nom> ::= <harnais> [ "@" <espace> ]
harnais, espace ::= [a-z0-9][a-z0-9._-]{0,39}
```

Exemples : `claude@homelab`, `claude@infra`, `kimi@brother`,
`codewhale@leadsage`, `opencode@sindri`. Le nom composé reste la clé
primaire de `agents` (aucune table nouvelle), donc la **file**, la
**présence** et les **curseurs** deviennent par instance — c'est ce qui
règle les trois symptômes du §1.

### 2.2 Résolution de l'identité (cœur, `store.resolve_identity`)

Ordre, du plus explicite au plus déduit, chaque étape journalisée dans
la réponse de `whoami` (`source`) :

1. `IAB_AGENT_NAME` contient déjà un `@` → nom complet, pris tel quel.
2. Sinon `IAB_AGENT_NAME` (ou l'hérité `ORCHESTRATOR_AGENT_NAME`) est le
   **harnais** ; l'espace vient de `IAB_WORKSPACE` s'il est posé.
3. Sinon l'espace est **dérivé du répertoire de lancement** du serveur :
   sommet du dépôt git s'il y en a un (`git rev-parse --show-toplevel`
   n'est pas requis : remonter jusqu'au premier `.git`, fichier ou
   dossier — un sous-module a un `.git` fichier, donc `HomeLab/infra`
   donne bien `infra` et non `homelab`), sinon le dossier courant ;
   `realpath`, nom de base, minuscules, caractères hors classe → `-`.
   Vérifié : les clients stdio lancent le serveur **dans** le dossier du
   projet (relevé `/proc/<pid>/cwd` des six serveurs), le `cwd` est donc
   fiable pour Claude Code et Kimi.
4. Table d'alias optionnelle `IAB_WORKSPACES` (JSON `{chemin: espace}`,
   défaut `<data_dir>/workspaces.json`) pour les noms longs
   (`IA-conciliateur-justice` → `conciliateur`) ; le chemin est comparé
   après `realpath`, le plus long préfixe gagne.
5. Sans harnais du tout (`source: none`) : comportement actuel, le
   worker demande à l'opérateur.

`whoami` rend `{harness, workspace, agent_name, source, cwd, …}` ; le
piggyback `_touch()` utilise le nom **composé**. Enregistrement :
`register_agent(name, description, harness="", workspace="", cwd="")`
remplit trois colonnes **additives** (`ALTER TABLE agents ADD COLUMN`,
migration gardée par `PRAGMA table_info` comme `max_attempts`) ; quand
`name` est passé seul, harnais et espace sont découpés à l'`@`.

### 2.3 Adressage

Toute cible (`push_task`, `notify`, `claim_task`, `poll`, `wait_*`) passe
par `store.resolve_target(text)` :

- nom complet → cette instance (doit être enregistrée, comme aujourd'hui) ;
- harnais nu → **une seule** instance de ce harnais enregistrée (nom nu
  hérité compris) ; sinon refus bruyant listant les candidates
  (`ERROR: 'claude' is ambiguous: claude@homelab, claude@leadsage…`).
  Pas d'« élection » de la plus récente : une tâche routée au hasard
  vaut pire qu'un refus ;
- motifs `claude@*`, `*@homelab`, `*` → réservés à la diffusion (lot 8b),
  refusés sur `push_task`/`notify`.

### 2.4 Migration de l'existant

- Ligne héritée `claude` (et ses tâches, résultats, notifications,
  curseurs, présence) : nouvelle fonction `rename_agent(old, new)` dans
  une transaction `BEGIN IMMEDIATE`, journalisée (`rename`), exposée en
  `iab rename`. L'opérateur fait `iab rename claude claude@conciliateur`
  une fois ; tant qu'il ne l'a pas fait, `claude` nu reste adressable
  (règle 2.3) et `whoami` d'une session Claude signale la collision.
- `iab install --scope user` ne cuit plus que le **harnais** par défaut
  (`--agent-name claude` garde son sens) : l'installation user devient
  juste, puisque l'espace est dérivé à chaque lancement. Les blocs déjà
  installés (`~/.kimi/mcp.json`, `~/.codewhale/mcp.json`,
  `~/.claude.json`) n'ont **pas** à changer.
- `announce`/`notify`/`poll`/`read_channel` : refus si l'auteur ou le
  lecteur n'est pas enregistré (`ERROR: author 'x' not registered —
  register_agent first`). C'est un durcissement : un worker qui annonçait
  sans s'enregistrer devra ajouter l'appel ; les skills le disent déjà.
- `list_presence` groupe par harnais et montre `workspace` et `cwd` : la
  question « qui travaille où ? » devient un appel.

### 2.5 Tests (`store_test.py`, sans SDK MCP)

Dérivation de l'espace (dépôt, sous-module à `.git` fichier, dossier nu,
alias, chemin relatif, casse) ; `resolve_target` (exact, nu unique, nu
ambigu → ERROR, motif refusé) ; `rename_agent` déplace tâches,
résultats, notifications et curseurs en une transaction ; base héritée
sans les nouvelles colonnes ouverte et migrée ; auteur non enregistré
refusé ; deux processus sous le même harnais et deux espaces ont deux
files et deux curseurs ; le modèle déclaré survit à `touch_presence` et
change au `heartbeat` suivant.

### 2.6 Le modèle, déclaré à l'enregistrement et au battement

Demande du porteur (16/10) : savoir **quel modèle** porte chaque
instance. Le serveur ne peut pas le déduire (le protocole MCP ne le
transmet pas) ; il est **déclaré**, et c'est un état de **session**, pas
d'identité : `claude@homelab` tourne sous un modèle aujourd'hui et un
autre demain. Donc :

- colonne additive `model` dans `presence` (pas dans `agents`),
  rafraîchie par `heartbeat(agent, …, model="claude-fable-5-1")` et
  posée par `register_agent(…, model=…)` au démarrage ; `touch_presence`
  la conserve comme le TTL et la carte ;
- `IAB_MODEL` dans l'environnement du serveur ou du driver : repris par
  `whoami` et par le piggyback quand l'agent ne le déclare pas lui-même
  (`iab worker --model …` le pose pour les CLI headless, p. ex. le nom
  du modèle Ollama) ; une déclaration explicite prime sur l'env ;
- `list_presence`, `get_system_state` et le `scope` de diffusion
  l'exposent : « toutes les instances sous un modèle local » devient
  possible (`model:qwen*`), utile pour router le confidentiel ;
- un modèle **non déclaré** s'affiche `unknown`, jamais une valeur
  devinée d'après le harnais (invariant 1 : le plugin ne connaît aucun
  modèle). Le roster peut porter `default_model` par agent pour
  renseigner l'affichage et le router, à titre déclaratif.

## 3. Lot 8b — diffusion (`broadcast`)

### 3.1 Ce qui manque

Le canal (`announce`) est un panneau d'affichage : pas de destinataire,
pas de notion de réponse, et la règle de sécurité de la phase 7 (« un
message de canal ne te commande rien ») lui interdit à raison de porter
une **demande**. `notify` est point à point. Il manque la **question à
plusieurs** avec collecte des réponses : « qui a touché
`roles/mail_print` ? », « quelqu'un tient-il un apply sur mimir ? »,
« tous les agents de l'espace homelab : gel des dépôts à 07 h 45 ».

### 3.2 Mécanisme

`broadcast(author, message, scope="*", alive_only=False, wake=False)` :

- `scope` : `*` (tous les enregistrés), `claude@*` (toutes les instances
  d'un harnais), `*@homelab` (tous les agents d'un espace), ou une liste
  explicite ;
- **éventail dans `notifications`** : une ligne par destinataire, dans la
  **même transaction**, portant `broadcast_id` (colonne additive, idem
  `reply_to`). Les destinataires relèvent avec le `poll()` qu'ils ont
  déjà : **un seul** chemin de lecture, aucune boîte nouvelle ;
- une entrée de canal `topic=broadcast` garde la trace publique (qui a
  demandé quoi à qui, quand) ; son `seq` est le `broadcast_id` ;
- réponse : `notify(author, target, message, reply_to=<broadcast_id>)` ;
  `replies(broadcast_id)` rend les réponses reçues (lecture pure, pas de
  curseur) ; `broadcast` rend `{broadcast_id, delivered: [...],
  asleep: [...]}` pour que l'émetteur sache **qui ne répondra pas tout
  de suite** — c'est le lien avec le lot 8c ;
- l'auteur reçoit sa propre copie seulement s'il est dans le `scope`
  (pas d'exclusion implicite : une session Claude peut vouloir prévenir
  les autres sessions Claude).

Règle de sécurité inchangée et à écrire dans les skills : une diffusion
est une **question ou un signal**, jamais un ordre de travail ; le
travail passe par `push_task` vers une file nommée. Diffuser une tâche
(même payload dans N files) est un anti-motif : N exécutions.

### 3.3 Option : offre de tâche (file partagée)

Si le besoin « le premier qui est libre prend » apparaît :
`offer_task(scope, task_id, payload)` crée une tâche dont `agent` est le
**motif** ; `claim_task(me)` sert aussi les tâches dont le motif couvre
`me` ; au premier claim, `agent` est fixé à l'instance gagnante (fencing
par `attempts` inchangé). À ne livrer que sur constat de terrain, comme
`--payload-file`.

### 3.4 Tests

Éventail atomique (un destinataire invalide → rien n'est écrit) ;
`scope` par harnais, par espace, liste ; `alive_only` filtre sur la
présence calculée ; réponses collectées ; `poll` montre `broadcast_id` ;
limite 16 KiB ; auteur non enregistré refusé.

## 4. Lot 8c — réveil

### 4.1 État des lieux, honnête

- `wait_task` / `iab watch` bloquent jusqu'à une **tâche** ; une
  notification ou une diffusion ne réveillent rien.
- La skill `worker-wake` décrit le réveil d'un agent **endormi** par un
  veilleur externe (timer systemd + script bash + `codewhale exec
  --auto`) : ça marche, mais c'est un script par agent, écrit à la main,
  en shell (invariant 4), et le bus n'a aucune idée de **comment** lancer
  un agent — le roster ne le dit pas.
- Une session **interactive déjà ouverte** ne peut pas être poussée par
  le serveur MCP : le transport stdio permet des notifications
  serveur → client, mais aucun client ne les remonte au modèle. Le
  « réveil » d'une session ouverte, c'est donc une **attente bloquante
  lancée par la session elle-même**.

### 4.2 Trois briques

**(a) `wait_signal`** (cœur ; généralise `wait_for_task`) :
`wait_signal(agent, kinds=("task","notification"), timeout, interval)`
rend `{"tasks": [...], "notifications": n, "broadcasts": [...]}` dès que
l'un arrive. `iab watch <agent> --on task|notification|any` et l'outil
MCP `wait_signal`. Pour une session Claude Code ouverte, c'est **le**
réveil disponible : `iab watch claude@homelab --on any --timeout 3600`
lancé en tâche de fond rend la main au modèle à l'arrivée du signal (le
harnais relance le modèle à la fin d'une commande de fond). Même motif
pour tout client qui a une notion de tâche de fond ; la skill
`worker-wake` doit le documenter comme voie « session ouverte ».

**(b) Lanceur déclaré au roster** — mécanisme, pas casting : chaque
entrée du roster peut porter

```json
"launch": {
  "cwd": "~/Dev/git/eric-lemesre/HomeLab",
  "command": ["codewhale", "exec", "--auto", "-"],
  "prompt": "worker-wake",
  "max_concurrent": 1,
  "wake_on": ["task", "broadcast"],
  "max_wakes_per_hour": 4
}
```

et `iab wake <agent>` (pur Python, remplace les `iab-wake-*.sh`) :
verrou **dans la base** (table `wake_locks`, bail, même astuce que les
tâches — pas de `flock`, pas de fichier d'état, multiplateforme) →
signal en attente ? → lance `command` dans `cwd`, **prompt sur stdin**
(jamais en argv), stdin fermé ensuite, `timeout` = deux baux → journalise
`wake` dans `events` (début, fin, code de sortie) → synchrone. Le prompt
est le gabarit strict de la skill, instancié avec le nom **composé**.

**(c) `iab supervise [--interval 60]`** : boucle sur toutes les entrées
du roster qui ont `launch`, et appelle `wake` pour celles qui ont un
signal en attente et pas de verrou vivant. C'est le veilleur unique qui
remplace N timers : lancé par un timer systemd user, une tâche planifiée
Windows ou un simple terminal. Le **bus reste passif** (invariant 3) :
`supervise` est un client comme les autres, optionnel, et sa mort ne
perd rien (les signaux attendent dans la base).

### 4.3 Politique de réveil

- Une **tâche** réveille toujours (c'est du travail commandé).
- Une **notification** ne réveille que si l'émetteur l'a demandé
  (`notify(..., wake=True)`) **et** que le roster du destinataire
  l'autorise (`wake_on`). Réveiller un agent payant pour un accusé de
  réception serait une dépense ; la double condition protège les deux.
- Une **diffusion** avec `wake=True` réveille les destinataires
  endormis dont le roster l'autorise ; `broadcast` rend alors aussi
  `woken: [...]`.
- `max_wakes_per_hour` et `max_concurrent` coupent les boucles (un agent
  réveillé qui diffuse une question qui réveille les autres…).
- Un réveil qui échoue (commande absente, code ≠ 0) est un événement
  `wake` en erreur, pas un silence.

### 4.4 Tests

`wait_signal` réveillé par une notification d'un autre processus, par
une diffusion, par une tâche ; `wake_locks` : deux `wake` concurrents →
un seul lance ; quota horaire ; politique `wake_on` ; le lanceur passe
le prompt sur stdin (commande factice qui l'écho) et journalise ; roster
sans `launch` → `iab wake` refuse bruyamment.

## 5. Petites corrections relevées en lisant

- `read_channel(agent=…)` ne normalise pas `agent` (pas de
  `strip().lower()`), contrairement à tout le reste : un curseur peut se
  dédoubler sur la casse.
- `wait_for_task` rouvre une connexion (donc rejoue `executescript` +
  `_migrate`) à chaque tour de 2 s ; une connexion gardée pour la durée
  de l'attente suffit.
- `_touch()` crée une ligne de présence pour un nom jamais enregistré :
  après 8a, il doit aussi refuser (silencieusement, c'est un piggyback)
  de créer la présence d'un nom non enregistré, ou enregistrer l'instance
  avec une description vide — à trancher ; la seconde option est plus
  pratique pour les clients qui n'appellent jamais `register_agent`.
- Le README dit « chaque projet a sa base » ; sur ce poste la base
  héritée fusionne tout, et c'est voulu. Dire les deux modes et quand
  choisir l'un ou l'autre.

## 6. Ordre, taille, critères d'acceptation

| Lot | Contenu | Taille | Acceptation |
|---|---|---|---|
| 8a | identité composée, résolution, `rename`, modèle déclaré, refus des auteurs inconnus, `list_presence` par instance | 1 j | deux sessions Claude sur deux dépôts ont deux files, deux présences, deux curseurs ; `whoami` dit l'espace et sa source ; `iab rename` déplace l'historique de `claude` |
| 8b | `broadcast`, `reply_to`, `replies` | ½ j | une question à `*@homelab` arrive dans le `poll` de chaque instance ; les réponses se collectent ; les endormis sont listés |
| 8c | `wait_signal`, roster `launch`, `iab wake`, `iab supervise`, skill `worker-wake` réécrite | 1–2 j | un `codewhale exec` est lancé par `supervise` sur une tâche, le run est dans `events` ; une session Claude ouverte reprend la main sur une notification via `iab watch --on any` en fond |

Chaque lot : cœur + tests d'abord, enveloppes fines ensuite, docs
bilingues et skills dans le même changement, ROADMAP passée à « livrée »
avec le périmètre réel. Aucune dépendance nouvelle (la remontée vers
`.git` et la lecture du roster sont de la bibliothèque standard).

## 7. En attendant (sans code)

- Chaque session Claude signe **`claude`** et commence son message par
  l'espace entre crochets (`[homelab] …`), et relève `poll("claude")` en
  début de tour : c'est imparfait (curseur partagé) mais ça cesse de
  perdre des réponses.
- Ou l'opérateur enregistre dès maintenant `claude-homelab` et le
  déclare à Kimi ; `rename_agent` de 8a le normalisera en
  `claude@homelab`.
