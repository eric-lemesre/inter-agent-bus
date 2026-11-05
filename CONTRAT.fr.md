# Contrat commun des implémentations du bus

🇬🇧 [English](CONTRAT.md) · 🇫🇷 Français

Le bus peut avoir plusieurs implémentations — Python, et un client et
un serveur MCP natifs en Rust (voir
[l'étude](docs/AMELIORATIONS-2026-11-04-client-rust.fr.md)) — qui écrivent
**la même base** au même moment. Ce document fixe ce sur quoi elles
doivent s'accorder : à l'octet près pour ce qui est stocké, au sens près
pour ce qui est renvoyé. L'implémentation Python est la référence ; en
cas de doute, son comportement fait foi, et l'écart se corrige ici.

**Règle de changement.** Toute modification d'un point de ce contrat
change toutes les implémentations dans la même demande de fusion, et
passe la suite de conformité (§ 11) sur chaque paire d'implémentations.

## 1. Choix de la base

- `IAB_DB` désigne la base ; à défaut `ORCHESTRATOR_DB` (ancien nom,
  toujours lu).
- Une valeur qui commence par `postgresql://` ou `postgres://` est une
  URL PostgreSQL au format libpq ; toute autre valeur est un chemin de
  fichier SQLite.
- Sans variable : `bus.db` du dossier de données s'il existe, puis
  l'ancienne base `~/.local/share/multi-agent-orchestrator/orchestrator.db`
  si elle existe, sinon une base par projet,
  `<données>/projects/<clé de projet>.db` (§ 7).
- Dossier de données : `%LOCALAPPDATA%\inter-agent-bus` sous Windows,
  `~/Library/Application Support/inter-agent-bus` sous macOS,
  `$XDG_DATA_HOME/inter-agent-bus` (défaut `~/.local/share`) ailleurs.
- Une URL n'est jamais affichée en entier : seuls le schéma, le rôle,
  l'hôte, le port et la base apparaissent (ni mot de passe ni
  paramètres).
- La même URL sert aux deux implémentations, paramètres libpq compris :
  `sslmode`, `sslrootcert`, `sslcert`, `sslkey`, `hostaddr`,
  `connect_timeout`, `options`, `application_name`, `passfile` ; le mot
  de passe vient de l'URL, à défaut de `PGPASSWORD`, à défaut du fichier
  pgpass (`PGPASSFILE`, `~/.pgpass`, `%APPDATA%\postgresql\pgpass.conf`) ;
  les certificats ont par défaut les emplacements de libpq
  (`~/.postgresql/`, `%APPDATA%\postgresql\`).
- Le client natif est plus strict que libpq sur deux points, tous deux
  du côté de la sûreté : en TCP il refuse `sslmode=prefer`, `allow` et
  l'absence de `sslmode` (défaut de libpq, qui retombe en clair) —
  indiquer `verify-full` ; et sans fichier de racines il fait confiance
  au magasin du système (`sslrootcert=system`) là où libpq refuserait de
  vérifier. Il ne gère ni clé chiffrée (`sslpassword`), ni listes de
  révocation (`sslcrl`), ni Kerberos/GSSAPI.

## 2. Schéma

- Les tables sont celles de `SCHEMA` dans `servers/shared_memory/dialect.py`,
  puis les migrations additives de `_migrate` dans `store.py`, appliquées
  dans cet ordre à chaque ouverture : colonne ajoutée seulement si elle
  manque, jamais supprimée ni renommée.
- SQLite : identifiants `INTEGER PRIMARY KEY AUTOINCREMENT`.
- PostgreSQL : identifiants `BIGSERIAL PRIMARY KEY` ; toute colonne
  `TEXT` porte `COLLATE "C"` ; les clés étrangères sont
  `DEFERRABLE INITIALLY DEFERRED` ; les tables vivent dans le schéma
  courant (`search_path`). La création du schéma et les migrations
  s'exécutent sous le verrou d'écriture (§ 3).
- Un champ JSON stocké dans une colonne texte (`presence.capabilities`)
  est lu par analyse, jamais comparé comme texte : les espaces de
  sérialisation peuvent différer d'une implémentation à l'autre.

## 3. Transactions d'écriture

- SQLite : `BEGIN IMMEDIATE`, journal en mode `WAL`, `busy_timeout` de
  10 000 ms.
- PostgreSQL : `BEGIN`, puis **en première instruction**
  `SELECT pg_advisory_xact_lock(6906210)` (`0x696162`, « iab »). Un seul
  verrou, donc un seul ordre de prise.
- Sortie normale : `COMMIT`. Erreur : `ROLLBACK`, et l'erreur d'origine
  est propagée.
- Rien d'extérieur à la base pendant une transaction ouverte : ni appel
  réseau, ni fichier, ni processus, ni attente.
- PostgreSQL : si la transaction a modifié au moins une ligne,
  `NOTIFY iab_bus` juste avant le `COMMIT` ; sinon aucune notification.

## 4. Attentes bloquantes

- SQLite : sondage à l'intervalle demandé.
- PostgreSQL : `LISTEN iab_bus`, puis attente d'une notification ou de
  la fin de l'intervalle, au premier des deux. Une connexion qui a fait
  `LISTEN` n'est jamais remise dans une réserve de connexions.

## 5. Dates

- Texte ISO 8601 en UTC, au format de `datetime.isoformat()` de Python :
  `AAAA-MM-JJTHH:MM:SS.ffffff+00:00`.
- **Quand les microsecondes valent zéro, elles sont omises** :
  `AAAA-MM-JJTHH:MM:SS+00:00`. Une implémentation écrit exactement ce
  format et lit les deux formes.
- Les échéances (`lease_deadline`, verrous de réveil) sont comparées
  comme des chaînes : un autre format (`Z` au lieu de `+00:00`,
  millisecondes, fuseau local) fausse les comparaisons.

## 6. Cycle de vie d'une tâche

- États : `queued` → `claimed` → réglée (résultat publié) ; sorties
  `dead` (bail expiré `max_attempts` fois, défaut 3) et `cancelled`.
- Réclamation : la tâche `queued` de plus haute priorité, puis la plus
  ancienne (`ORDER BY priority DESC, created_at ASC`) ; les baux expirés
  sont remis en file d'abord. `attempts` augmente à chaque réclamation ;
  le bail par défaut est de 900 s.
- Règlement gardé : la publication porte le numéro de tentative reçu à
  la réclamation ; un numéro périmé est refusé.
- Chaque transition s'inscrit dans `events` sous le même nom d'événement
  (`push`, `claim`, `expire`, `dead`, `publish`, `extend`, `cancel`,
  `requeue`, …).
- Livraison au moins une fois : une tâche peut s'exécuter deux fois.

## 7. Identité et clé de projet

- Nom d'agent : `harnais@espace`, chaque composant suivant
  `[a-z0-9][a-z0-9._-]{0,39}`, en minuscules.
- Harnais : `IAB_AGENT_NAME` (ou `ORCHESTRATOR_AGENT_NAME`). S'il
  contient déjà `@`, il donne le nom complet.
- Espace, dans l'ordre : `IAB_WORKSPACE` ; un alias de `workspaces.json`
  (`IAB_WORKSPACES`, sinon le fichier du dossier de données ; préfixe le
  plus long, qui ne franchit jamais la limite d'un dépôt git) ; le nom du
  premier dossier ancêtre qui contient un `.git` (fichier ou dossier) ;
  le nom du dossier de lancement.
- Mise en forme d'un composant : minuscules, toute suite de caractères
  hors `[a-z0-9._-]` remplacée par `-`, ponctuation de tête retirée,
  `-` de fin retirés, 40 caractères au plus, `root` si rien ne reste.
  Un caractère accentué sort donc en `-` (`Dépôt` → `d-p-t`).
- Clé de projet : chemin réel du dossier (liens résolus), mis en
  minuscules sous Windows et macOS, puis
  `<nom du dossier, hors [A-Za-z0-9_-] → '-', 40 caractères>-<12 premiers
  caractères hexadécimaux du SHA-256 du chemin en UTF-8>`.
- **Sous Windows, le chemin réel est sans préfixe `\\?\`** : c'est la
  forme que renvoie `os.path.realpath`. Une implémentation dont la
  fonction de résolution ajoute ce préfixe le retire avant de calculer.

## 8. Ligne de commande

- Mêmes sous-commandes, options et valeurs par défaut que
  `servers/shared_memory/cli.py` (`iab --help`).
- Réponses :
  - une ligne `OK: …` pour une action réussie, code de sortie 0 ;
  - une ligne `ERROR: …` pour un refus ou une erreur, code de sortie
    différent de 0 ;
  - `NO_TASK` quand une réclamation ne trouve rien, code de sortie 0 ;
  - sinon un document JSON, que l'appelant **analyse** (l'espacement et
    l'option `--json`, qui l'indente, peuvent varier).
- Une charge utile, un résultat ou un message absent de la ligne de
  commande, ou donné par `-`, est lu sur l'entrée standard.

## 9. Flux standard et fidélité des données

- Entrée et sorties standard en **UTF-8 sur toutes les plateformes**,
  quelle que soit la page de code de la console.
- **Aucune traduction des fins de ligne**, ni en lecture ni en écriture :
  une charge utile qui contient `\r\n` ou `\r` est stockée, renvoyée et
  transmise telle quelle.
- Une charge utile n'est jamais tronquée ; sa taille n'est limitée que
  par la base.

## 10. Processus lancés (`worker`, `wake`)

- La charge utile ou l'invite passe par l'**entrée standard** du
  processus (ou un fichier temporaire pour `{prompt_file}`), jamais par
  ses arguments ni par un interpréteur de commandes.
- Échanges en octets : la charge utile est encodée en UTF-8, la sortie
  décodée en UTF-8 (un octet invalide devient U+FFFD), sans traduction
  des fins de ligne. `iab worker` publie la sortie standard telle quelle.
- Une sortie vide d'une commande qui a réussi est un échec publié
  `ERROR: …`.

## 11. Suite de conformité

`conformance/run.py` pilote les implémentations par leur ligne de
commande, sans importer `store.py`, et joue chaque scénario sur toutes
les paires d'implémentations partageant une même base :

```bash
python3 conformance/run.py                                   # Python seul
python3 conformance/run.py --impl python='["python3","servers/shared_memory/cli.py"]' \
                           --impl rust='["/chemin/vers/iab"]' --without rust=worker
IAB_TEST_POSTGRES=<URL d'une base de test VIDE> python3 conformance/run.py
```

`--without NOM=SCÉNARIO` saute, en le disant dans le rapport, un
scénario qu'une implémentation n'offre pas encore, sur toutes les paires
où elle figure.

Scénarios : cycle de vie complet, refus, priorités, règlement gardé
après expiration, file des tâches mortes et remise en file,
réclamations concurrentes (une seule gagne), notifications et
diffusions, réveil d'une attente par une écriture de l'autre
implémentation, fidélité d'une charge utile de 200 ko (non-ASCII,
`\r\n`, `\r`), `iab worker`, identité et clé de projet dans un dossier
accentué.

Le banc `scripts/pg_tls.py` monte un serveur PostgreSQL qui n'accepte le
rôle du bus qu'en TLS avec certificat client, et ne donne son URL
(`sslmode=verify-full`) qu'après avoir vérifié que le clair, une
autorité étrangère et un mauvais nom d'hôte sont refusés ; il porte
aussi un rôle authentifié par mot de passe (SCRAM-SHA-256).
`conformance/tls.py` y confronte le client natif : ce qu'il doit
accepter (certificat client, mot de passe par `PGPASSWORD` ou un fichier
pgpass, `verify-ca` sur un nom d'hôte absent du certificat, `require` avec ou
sans fichier de racines, comme libpq) et refuser
(clair, `prefer`, `sslmode` absent, autorité étrangère, mauvais nom
d'hôte, mauvais mot de passe).
L'intégration continue (`.github/workflows/ci.yml`) joue toutes les
suites sous Linux et Windows, Python 3.10 et 3.14, sur SQLite et sur ce
banc, ainsi que la suite sur les quatre paires de l'implémentation
Python et du client natif (`rust/`).

`conformance/mcp_server.py` confronte un serveur MCP à `server.py`, tous
deux pilotés comme processus en stdio par le client du paquet `mcp` :

```bash
python3 conformance/mcp_server.py --impl rust='["/chemin/vers/iab-server"]'
```

Il vérifie l'interface annoncée (§ 12), rejoue une session qui touche
chaque outil — refus et arguments invalides compris — sur une base
neuve par serveur et compare les réponses (dates et base masquées), fait
tourner les deux serveurs sur une même base, chacun réglant ce que
l'autre a commencé, et vérifie qu'un `wait_signal` bloquant ne retient
ni les appels qui le suivent dans la même session ni la poussée qui le
réveille. L'intégration continue le joue sur SQLite et sur le banc TLS.

## 12. Serveur MCP

- Mêmes 23 outils que `servers/shared_memory/server.py` : mêmes noms,
  descriptions, paramètres, types et valeurs par défaut, donc même
  `inputSchema` ; chaque outil déclare le schéma de sortie
  `{"result": string}`.
- Un appel abouti répond le texte de l'opération (`OK: …`, JSON,
  `NO_TASK`, ou un refus `ERROR: …`) à la fois en contenu texte et en
  `structuredContent` `{"result": …}`, avec `isError` faux : un refus du
  bus est une réponse, pas une erreur d'outil.
- L'erreur d'outil (`isError` vrai) est réservée à ce qui empêche
  l'appel : outil inconnu (`Unknown tool: NOM`), arguments invalides
  (première ligne `Error executing tool NOM: N validation error(s) for
  NOMArguments`, puis une entrée par paramètre), panne de la base
  (`Error executing tool NOM: …`).
- Les arguments se lisent comme le mode souple de pydantic : un entier
  accepte `"5"`, `5.0` et un booléen, un nombre accepte un texte
  numérique, un drapeau accepte `true`/`false`, `0`/`1` et les mots
  `yes`, `no`, `on`, `off`, `t`, `f`, `y`, `n` ; un texte n'accepte
  qu'un texte. Les arguments inconnus sont ignorés. Un texte optionnel
  vide, `0` pour `attempt` et `reply_to`, `-1` pour `since_seq` valent
  « non fourni ».
- Identité résolue une fois par processus serveur (§ 7). Quand elle
  nomme un harnais, chaque appel valide enregistre d'abord l'instance à
  sa première apparition (description vide, dossier de lancement) et
  rafraîchit sa présence, de même que le démarrage du serveur ; un échec
  à cet endroit ne fait jamais échouer l'appel. Sans harnais, `whoami`
  répond le `clientInfo` de la poignée de main (`source` =
  `client_info`, `name`, `title`, `version`).
- Un outil bloquant (`wait_signal`, `wait_task`) ne retient pas les
  autres appels de la session.
- Le serveur natif refuse `IAB_HUB` à chaque appel, comme la ligne de
  commande native (§ 1).
