# Amélioration en attente — client natif en Rust (Windows d'abord)

🇬🇧 [English](AMELIORATIONS-2026-11-04-client-rust.md) · 🇫🇷 Français

Notée le 2026-11-04. Demandes du porteur :

1. un **exécutable Windows** du bus, écrit en Rust, pour un poste
   d'entreprise géré ;
2. un bus partagé par **une base PostgreSQL distante, en TLS** (serveur
   interne de l'entreprise) : deux machines communiquent en pointant
   `IAB_DB` sur la même base, sans moyeu ;
3. plus tard, des paquets **`.deb` et `.rpm`** ;
4. **deux implémentations maintenues** : Python reste, Rust s'ajoute.

Étude seulement : rien n'est implémenté. Les décisions à prendre sont
au § 11.

## 1. Constat

- Le cœur Python (`store.py`, `dialect.py`) gère déjà PostgreSQL :
  `IAB_DB=postgresql://…` choisit le moteur, l'URL va telle quelle à
  libpq, donc `sslmode=verify-full`, `sslrootcert`, `sslcert`, `sslkey`
  fonctionnent sans modification. Un bus partagé entre deux machines est
  donc possible **dès aujourd'hui en Python**.
- Ce que Python impose sur le poste visé : un interpréteur, un
  environnement virtuel (64 Mo), l'accès à PyPI ou un dépôt de paquets
  interne, et `psycopg[binary]`. Sur un poste géré, chacun de ces points
  peut être refusé ou retardé.
- Performances : non déterminantes. `iab signals` prend 70 ms (dont
  50 ms de démarrage de l'interpréteur), un serveur MCP occupe de 14 à
  26 Mo. Le gain de Rust est la **distribution** (un fichier, pas de
  dépendance), pas la vitesse.
- Le bus n'a jamais tourné sous Windows : aucune intégration continue,
  aucun test sur cette plateforme, quelle que soit l'implémentation.

## 2. Périmètre du client Rust

**Dans le périmètre** (environ 3 500 lignes Python à transposer) :

- le cœur : files, baux, clôture gardée par le numéro de tentative,
  journal, présence, canal, notifications, diffusions, verrous de
  réveil — l'équivalent de `store.py` ;
- les deux moteurs : SQLite (fichier local) et PostgreSQL (TLS) —
  l'équivalent de `dialect.py` ;
- la ligne de commande `iab`, mêmes sous-commandes et mêmes sorties ;
- le serveur MCP sur stdio, mêmes outils et mêmes paramètres que
  `server.py` : un client MCP passe d'une implémentation à l'autre en
  changeant seulement le chemin de l'exécutable ;
- `iab worker`, `iab wake`, `iab supervise` (lancement de processus).

**Hors périmètre** (restent en Python seul) : le moyeu (`hub.py`),
l'appairage, la PKI, l'attestation TPM, `iab review`, `iab import`. Un
bus partagé par PostgreSQL n'en a pas besoin. Le client Rust ne lira pas
`IAB_HUB` dans un premier temps : avec cette variable posée, il refuse
de démarrer plutôt que d'ignorer le moyeu en silence.

## 3. Deux implémentations : le contrat commun

Deux implémentations qui écrivent la même base doivent s'accorder à
l'octet près sur ce qui est stocké, et au sens près sur ce qui est
renvoyé. Le contrat devient un document du dépôt (`CONTRAT.md` /
`CONTRAT.fr.md`) et tout changement de l'un des points ci-dessous
modifie **les deux implémentations dans la même demande de fusion**.

| Point du contrat | Valeur actuelle (Python) | Piège pour Rust |
|---|---|---|
| Schéma et migrations additives | `SCHEMA` et `_migrate` de `dialect.py`/`store.py` | appliquer les mêmes migrations, dans le même ordre, idempotentes |
| Dates | texte ISO 8601, `datetime.isoformat()` en UTC | Python **omet les microsecondes quand elles valent 0** (`…:59+00:00` contre `…:59.828491+00:00`) ; les comparaisons de baux sont des comparaisons de chaînes : reproduire ce format exact et lire les deux formes |
| Collation PostgreSQL | `TEXT COLLATE "C"` | indispensable aux comparaisons de dates en texte |
| Verrou d'écriture | SQLite : `BEGIN IMMEDIATE` ; PostgreSQL : `pg_advisory_xact_lock(0x696162)` en première instruction | même clé, même place ; aucune attente hors base pendant la transaction |
| Réveil | `NOTIFY iab_bus` au COMMIT si une ligne a changé ; `LISTEN iab_bus` sur une connexion jamais remise au pool | même canal, même règle « seulement si modifié » |
| Identité | `harnais@espace`, espace dérivé du dépôt git englobant, alias de `workspaces.json`, `IAB_WORKSPACE` | même algorithme de « slug » ; sous Windows, `std::fs::canonicalize` renvoie un chemin `\\?\C:\…` que Python ne produit pas : utiliser `dunce::canonicalize`, sinon la clé de projet (empreinte SHA-256 du chemin) diffère et les deux clients ouvrent deux bases différentes |
| Clé de projet | `realpath`, minuscules sous Windows et macOS, `slug-<12 hex>` | idem, testée sur des chemins accentués et des liens |
| Résultats des outils | JSON, préfixe `ERROR:` pour les refus | Python sérialise avec `", "` et `": "`, serde_json sans espaces : comparer le **JSON analysé**, jamais le texte ; même ordre des clés (structures ordonnées ou `preserve_order`) |
| Cycle de vie | queued → claimed → done ; dead, cancelled ; `attempts`, `max_attempts`, clôture refusée à un numéro de tentative périmé | comportement identique, testé croisé (§ 7) |

## 4. Architecture proposée

Un espace de travail Cargo dans le même dépôt (`rust/`), pour que les
deux implémentations partagent tests de conformité, contrat et version.

```
rust/
  Cargo.toml            espace de travail, lints communs
  crates/
    iab-core/           logique d'état (équivalent de store.py), sans MCP, sans réseau
    iab-db/             trait Dialect + SQLite (rusqlite) + PostgreSQL
    iab-cli/            exécutable `iab` (clap)
    iab-mcp/            exécutable `iab-server` (rmcp, stdio)
```

Choix et raisons :

- **Cœur synchrone**, comme `store.py` : chaque opération ouvre une
  transaction courte, sans rien attendre hors de la base. Le serveur MCP
  (asynchrone, tokio) appelle le cœur par `spawn_blocking`. Pas de
  logique dans les enveloppes, même règle qu'en Python.
- **Trait `Dialect`** reprenant exactement l'interface de `dialect.py` :
  `connect`, `create_schema`, `write` (verrou pris en première
  instruction, COMMIT en sortie normale, ROLLBACK sur erreur), `pause`,
  `columns`, `insert_id`. Le SQL du cœur reste portable avec `?` ; le
  dialecte PostgreSQL le traduit en `$1…$n` (même traduction que
  `_translate`, mêmes tests sur les littéraux contenant `?` et `%`).
- **SQLite** : `rusqlite` avec la fonctionnalité `bundled` (SQLite
  compilé dans l'exécutable), WAL, `busy_timeout` à 10 s, `BEGIN
  IMMEDIATE`. Le format de fichier est commun : un client Python et un
  client Rust partagent la même base locale.
- **PostgreSQL** : `postgres` (enveloppe synchrone de `tokio-postgres`,
  0.7.18) avec TLS par **rustls** et `rustls-platform-verifier`. Sous
  Windows, la vérification passe par le magasin de certificats du
  système : l'autorité interne de l'entreprise est reconnue sans fichier
  à déposer. Tout en Rust, donc un exécutable sans DLL.
- **Paramètres TLS de l'URL** : `tokio-postgres` ne connaît que
  `sslmode=disable|prefer|require` et `sslnegotiation=postgres|direct`
  (direct : PostgreSQL ≥ 17, ALPN `postgresql`). Il ne connaît ni
  `verify-ca`, ni `verify-full`, ni `sslrootcert`, `sslcert`, `sslkey`.
  `iab-db` lit donc ces paramètres lui-même, les retire de l'URL et
  construit le connecteur rustls en conséquence :
  - `verify-full` → `require` + vérification de la chaîne **et** du nom ;
  - `verify-ca` → `require` + vérification de la chaîne seule ;
  - `sslrootcert` → racines tirées de ce fichier au lieu du magasin du
    système ;
  - `sslcert`/`sslkey` → certificat client ;
  - `prefer`, valeur par défaut de libpq, est **refusé** pour une base
    distante : il accepte une connexion en clair si le serveur ne
    propose pas TLS.

  Les emplacements par défaut de libpq sont repris
  (`%APPDATA%\postgresql\root.crt`, `postgresql.crt`, `postgresql.key` ;
  `~/.postgresql/` ailleurs), ainsi que `PGPASSFILE` / `pgpass.conf` :
  la même configuration sert aux deux implémentations.
- **MCP** : `rmcp` 3.8, le SDK officiel en Rust, transport stdio, outils
  déclarés par `#[tool]`. Les 23 outils de `server.py` avec les mêmes
  noms, paramètres et valeurs par défaut.
- **Ligne de commande** : `clap`, mêmes sous-commandes que `cli.py` dans
  le périmètre du § 2. Une charge utile n'est jamais passée par argv à
  un processus lancé (`worker`, `wake`) : entrée standard ou fichier
  temporaire, comme en Python.

## 5. Règles de code retenues

Confrontées aux règles du guide `rust-best-practices` et aux deux guides
PostgreSQL :

- erreurs : `thiserror` dans `iab-core` et `iab-db` (une énumération par
  crate), `anyhow` seulement dans les deux exécutables ; aucun
  `unwrap`/`expect` hors des tests ; un refus métier reste une valeur
  renvoyée `ERROR: …`, jamais une panique ;
- lints de l'espace de travail : `cargo clippy --all-targets
  --all-features --locked -- -D warnings`, `#[expect(…)]` justifié plutôt
  que `#[allow(…)]`, `missing_docs` sur les crates de bibliothèque ;
- appels statiques (génériques) pour le dialecte dans le cœur ;
  `dyn Dialect` seulement au point où l'URL choisit le moteur ;
- paramètres liés partout, jamais de valeur interpolée dans le SQL. Seuls
  les noms de table et de colonne des migrations sont interpolés, et ils
  viennent de constantes du code ;
- transactions d'écriture courtes : le verrou est la première
  instruction, aucun appel réseau, fichier ou processus tant qu'elle est
  ouverte, un seul verrou donc un seul ordre ; une connexion qui a fait
  `LISTEN` n'est jamais remise au pool ;
- le schéma reste celui de Python, y compris là où PostgreSQL offrirait
  mieux (`TIMESTAMPTZ` au lieu de dates en texte, colonnes `IDENTITY` au
  lieu de `BIGSERIAL`, index composé `notifications(target, seq)` pour
  `poll`). Ces améliorations sont possibles plus tard, **dans les deux
  implémentations à la fois**, par migration additive ;
- tests nommés par comportement (`claim_should_refuse_stale_attempt`), un
  comportement par test.

## 6. Distribution

- **Windows** : cible `x86_64-pc-windows-msvc`, construite sur un
  exécuteur GitHub Actions Windows (natif, plus simple qu'une
  compilation croisée), bibliothèque C liée statiquement (`+crt-static`).
  Résultat : `iab.exe` et `iab-server.exe`, sans DLL. Une archive `.zip`
  par version, avec empreintes SHA-256.
- **Signature** : un poste géré peut bloquer un `.exe` non signé
  (AppLocker, antivirus). Il faut soit un certificat de signature de
  code (Authenticode), soit une règle d'exception posée par le service
  informatique de l'entreprise. À trancher (§ 11).
- **Linux** : cible `x86_64-unknown-linux-musl`, exécutable statique qui
  tourne aussi sur une distribution ancienne (RHEL 8, glibc 2.28).
  `cargo-deb` et `cargo-generate-rpm` pour les paquets, plus tard.
- **Version** : un seul numéro pour les deux implémentations ; une
  étiquette `vX.Y.Z` publie le paquet Python et les exécutables Rust.

## 7. Tests et conformité

- Les tests unitaires de chaque crate.
- **Une suite de conformité commune**, écrite en Python (elle pilote des
  processus, elle n'importe pas `store.py`) : chaque scénario s'exécute
  avec les combinaisons Python/Python, Rust/Rust, **Python/Rust et
  Rust/Python sur la même base**. Exemples :
  - Python dépose une tâche, Rust la réclame et publie, Python lit le
    résultat ;
  - réclamations concurrentes, mélangées entre les deux
    implémentations : une seule gagne ;
  - un bail expiré côté Rust, repris côté Python : la clôture périmée est
    refusée ;
  - une attente bloquante côté Rust réveillée par une écriture Python
    (PostgreSQL : `LISTEN`/`NOTIFY`) ;
  - identité et clé de projet identiques pour un même dossier, chemins
    accentués compris ;
  - `store_test.py` reste la référence de comportement : ses scénarios
    sont repris un à un dans la suite de conformité.
- **Intégration continue** : matrice Linux et Windows, Python 3.10 et
  3.14, SQLite et PostgreSQL en TLS avec `verify-full` et un certificat
  client (autorité de test générée pendant l'exécution).

## 8. Étapes

| Étape | Contenu | Utilisable à la fin |
|---|---|---|
| R0 | Contrat (`CONTRAT.md`), suite de conformité en Python, intégration continue Linux et Windows sur l'implémentation Python | preuve que Python marche sous Windows et sur PostgreSQL en TLS ; utile même sans Rust |
| R1 | `iab-core` + SQLite + `iab` (register, push, claim, publish, poll, notify, signals, state) | bus local sous Windows en Rust |
| R2 | PostgreSQL en TLS (rustls, paramètres libpq, `pgpass`), attentes par `LISTEN`/`NOTIFY` | bus partagé entre deux machines |
| R3 | `iab-server` : les 23 outils MCP | agents MCP sous Windows sur l'exécutable Rust |
| R4 | `worker`, `wake`, `supervise` sous Windows | lancement des agents endormis |
| R5 | Signature, archives publiées ; `.deb`/`.rpm` quand le besoin viendra | distribution |

Chaque étape passe la suite de conformité complète sur les deux
implémentations avant fusion.

## 9. Modifications de la feuille de route

- Principe 4 (« L'outillage est en pur Python ») devient : « Deux
  implémentations, Python (référence, complète) et Rust (client natif) ;
  leur accord est garanti par le contrat et la suite de conformité. »
- Principe 2 (« toute logique d'état atterrit dans `store.py` ») vaut
  désormais pour `store.py` **et** `iab-core`, dans la même demande de
  fusion.
- Nouvelle phase « client natif » après la phase 9.

## 10. Risques

| Risque | Effet | Parade |
|---|---|---|
| Authentification Kerberos/SSPI imposée par l'entreprise | les pilotes Rust (`tokio-postgres`, `sqlx`) ne la gèrent pas | demander d'abord la méthode retenue ; à défaut, version liée à libpq (`libpq.dll` livrée à côté) ou Python sur ce poste |
| Pooler en mode transaction (PgBouncer) | plus de `LISTEN`/`NOTIFY` : les attentes ne se réveillent qu'au sondage | connexion directe ou pooler en mode session |
| `.exe` non signé bloqué | l'exécutable ne démarre pas | certificat de signature ou exception du service informatique |
| Divergence des deux implémentations | corruption ou incohérence du bus partagé | contrat, suite de conformité croisée obligatoire, modification des deux dans la même fusion |
| Coût de maintenance double | chaque évolution du cœur s'écrit deux fois | périmètre Rust limité au client ; le moyeu reste en Python |
| Données de l'entreprise dans la base | tâches et résultats stockés en clair | validation par l'entreprise de l'usage de cette base ; rien de secret dans les charges utiles (règle déjà en vigueur) |
| Comportement de `tokio-postgres` sur un paramètre inconnu de l'URL | à vérifier : refus probable à l'analyse | `iab-db` retire lui-même les paramètres TLS de libpq avant de transmettre l'URL |

## 11. Décisions attendues du porteur

1. **Méthode d'authentification** imposée par les administrateurs de la
   base de l'entreprise (mot de passe SCRAM, certificat client,
   Kerberos/SSPI). Elle décide du pilote (§ 4, § 10).
2. **Signature** de l'exécutable : certificat de code (lequel, payé par
   qui) ou exception du service informatique.
3. **Version et réglages du serveur PostgreSQL** : version, pooler,
   schéma dédié, création des tables par le bus lui-même ou par les
   administrateurs (script SQL fourni).
4. **Client MCP utilisé sur le poste Windows** (il fixe la configuration
   à documenter).
5. **Ordre** : commencer par R0, qui sert aussi la version Python.
