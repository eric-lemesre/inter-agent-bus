# Bus multi-machine

🇫🇷 Français · 🇬🇧 [English](MULTI-MACHINE.md)

Par défaut, le bus vit dans une base SQLite locale : tous les agents
d'une machine la partagent, rien ne tourne en fond. Pour faire coopérer
des agents de **plusieurs machines**, un **moyeu** (`iab hub`) expose
cette base sur le réseau ; les machines s'y appairent une fois, puis
leurs clients (serveur MCP, CLI, `iab worker`, `iab supervise`) y passent
d'eux-mêmes dès que `IAB_HUB` est posé. Sans `IAB_HUB`, rien ne change.

Conception et décisions : [`AMELIORATIONS-2026-10-17-multi-machine.fr.md`](AMELIORATIONS-2026-10-17-multi-machine.fr.md).

## Principe

```
 poste ─┐
 mimir ─┼── HTTPS + TLS mutuel ──▶ iab hub ──▶ store.py ──▶ base du bus
 sindri ┘   (port 7443)
```

- **Une machine = un certificat**, émis par la CA privée du moyeu à
  l'appairage. Sa clé est créée sur la machine et n'en sort jamais.
- **Le client ne fait confiance qu'à la CA du bus** pour joindre le
  moyeu, jamais au magasin du système : un proxy d'inspection TLS
  (Zscaler, Netskope…) échoue à la négociation et rien n'est envoyé.
- **Révocation immédiate** : le moyeu revérifie la machine à chaque
  requête.
- **Moyeu injoignable = erreur** (`ERROR: hub unreachable…`), jamais de
  repli sur une base locale.
- **Deux ports** : 7443 pour le bus (TLS mutuel), 7444 pour l'appairage
  (ouvert seulement aux réseaux privés par défaut).

## Installer

```bash
pip install 'inter-agent-bus[hub]'      # ajoute cryptography
```

Une machine admin a aussi besoin de sa puce TPM : voir
[Clés TPM](#clés-tpm-machines-admin).

## Créer le moyeu (une fois)

Sur la machine du moyeu (de préférence une petite VM dédiée, sans
agents) :

```bash
iab hub init --name iab.example.org --name 192.168.20.30
iab hub serve
```

`init` crée la CA du bus, les certificats, la base, et appaire la machine
locale comme **première machine admin**. Le premier `--name` est
l'adresse que les clients utiliseront (`IAB_HUB=https://iab.example.org:7443`).
Le moyeu renouvelle seul son certificat de bus.

Exemple d'unité systemd :

```ini
[Unit]
Description=inter-agent-bus hub
After=network-online.target

[Service]
ExecStart=/opt/iab/bin/iab hub serve
User=iab
Restart=on-failure
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/home/iab/.local/share/inter-agent-bus

[Install]
WantedBy=multi-user.target
```

## Base PostgreSQL (option)

SQLite convient au moyeu tant qu'il reste le seul écrivain. Pour un
moyeu sur une base PostgreSQL existante (sauvegarde `pg_dump`,
supervision déjà en place) :

```bash
pip install 'inter-agent-bus[hub,postgres]'
iab hub init --name iab.example.org --db 'postgresql://iab@/iab?host=/run/postgresql'
```

Le schéma se crée seul. Préférez une authentification sans mot de passe
dans l'URL (socket Unix et `peer`, ou `PGPASSFILE`) ; `hub.json` reste de
toute façon lisible par le seul compte du moyeu, et `whoami` masque le
mot de passe. Le comportement est identique à SQLite (mêmes tests) ; les
attentes bloquantes se réveillent dès le COMMIT d'une écriture
(LISTEN/NOTIFY) au lieu d'attendre leur prochain sondage.

## Appairer une machine

Sur la nouvelle machine :

```
$ iab join iab.example.org
Hub: iab.example.org
To pair this machine, on an admin machine run:
    iab approve
and type the code:   WDJB-MJHT   (valid 10 min, waiting…)
Check words: lampe-orage-violon-tulipe
```

Sur une machine admin :

```
$ iab approve
Code: wdjb mjht
Request from 'mimir' — Linux aarch64 — 192.168.20.21 — key: file
Check words: lampe-orage-violon-tulipe
Are the four words identical to those shown on mimir? Pair it as 'mimir' (member) [y/N] y
Machine 'mimir' paired (certificate until 2026-11-16).
```

La nouvelle machine termine seule. Les **quatre mots** sont calculés à
partir de la clé de la machine, de la CA du moyeu et d'un aléa de chaque
côté, selon un schéma « engagement puis révélation » (celui de la
comparaison numérique Bluetooth) : la machine s'engage sur sa demande
avant de connaître l'aléa du moyeu, et ne la révèle qu'ensuite. Personne
ne peut donc choisir une clé qui accorderait les mots des deux côtés :
un intermédiaire au premier contact a une chance sur quatre milliards,
et une seule tentative par appairage. C'est cette comparaison qui
protège l'appairage quand le port d'appairage n'a pas de certificat
public (le client le signale). L'approbation est volontairement
interactive, et rien de ce qu'envoie l'autre côté n'est affiché sans
être débarrassé des caractères de contrôle.

**Machines gérées par Ansible** : une invitation à usage unique, liée au
nom, valable 15 minutes, remplace la personne :

```bash
iab invite mimir | ssh mimir 'iab join iab.example.org --name mimir --token -'
```

Le jeton passe par stdin (ou `IAB_INVITE_TOKEN`), jamais en argument ;
il ne voyage pas non plus sur le réseau : la machine prouve qu'elle le
détient par un code d'authentification lié à sa propre demande.

## Clés TPM (machines admin)

Une machine admin (approuver, inviter, révoquer, rattacher, trust-ek)
doit garder sa clé **dans sa puce TPM 2.0**, et le prouver : le moyeu ne
croit jamais une machine sur parole (politique du porteur). La clé est
générée par la puce et ne peut pas en sortir ; ce que la machine garde
sur disque est la clé enveloppée par sa puce, inutilisable ailleurs —
copier le disque ou une sauvegarde ne donne rien. Toute machine peut
avoir une clé TPM, admin ou non.

Prérequis (Linux) :

```bash
sudo apt install tpm2-openssl python3-tpm2-pytss
sudo usermod -aG tss $USER        # puis ouvrir une nouvelle session
```

`tpm2-openssl` sert à tout processus qui parle au moyeu avec cette clé
(serveurs MCP, workers, superviseur), tpm2-pytss seulement à
`iab join`. Avec le paquet Debian, créer le venv avec
`--system-site-packages` ; sinon `apt install libtss2-dev`, puis
`pip install 'inter-agent-bus[hub,tpm]'`.

Appairer avec `--tpm`, par code ou par invitation :

```
$ iab invite yggdrasil --role admin | ssh yggdrasil 'iab join iab.example.org --tpm --token -'
TPM endorsement key: sha256 e6a081ffe30f57007a1071a150e97e4afd748aeb5565c6692e8a58cbaf649acf
  certified by STM TPM EK Intermediate CA 06
Paired as 'yggdrasil'. Certificate valid until 2026-11-17, renewed automatically.
```

La machine envoie les parties publiques de sa clé et de la clé
d'endossement (EK) de sa puce, avec le certificat d'EK que son fabricant
a inscrit dans la puce. Le moyeu vérifie que la clé a été générée dans
la puce et ne peut jamais en sortir (attributs fixedTPM, fixedParent,
sensitiveDataOrigin) et que le certificat d'EK remonte à une autorité
d'un fabricant ; il chiffre ensuite un secret que seule cette puce peut
ouvrir, et seulement pour cette clé (activation de justificatif :
TPM2_MakeCredential / ActivateCredential). Une clé logicielle déguisée
avec les bons attributs n'obtient rien.

- **Autorités des fabricants** : les racines livrées avec le moyeu
  viennent du TrustedTpm.cab de Microsoft (AMD, Infineon, Intel,
  Nationz, Nuvoton, Qualcomm, STMicroelectronics… ;
  `scripts/tpm_roots.py` les régénère). La machine récupère les
  intermédiaires à l'adresse inscrite dans son certificat (accès
  Internet au moment de l'appairage). Une autorité manquante, PEM ou
  DER, se dépose dans `<répertoire du moyeu>/tpm-ca/`, lu au démarrage
  du moyeu. Les certificats d'EK sont vérifiés par leur signature, pas
  par leur date : les TPM survivent à leurs certificats.
- **TPM virtuel** (une VM), ou certificat illisible par le moyeu (les
  plus anciens de Nuvoton ne sont pas du DER valide, `cryptography` les
  refuse) : pas de preuve du fabricant. `iab join` affiche l'empreinte
  de l'EK ; un admin lui fait confiance **une fois**, après l'avoir lue
  sur la machine elle-même (sa console, pas un message relayé par
  elle) :

  ```bash
  iab trust-ek e6a081ffe30f5700…49acf --note "sindri, vTPM sur odin"
  iab trust-ek                          # liste
  iab trust-ek --remove <empreinte>     # plus de nouvel appairage ; les machines appairées restent
  ```

  Un TPM virtuel protège autant que son hôte.
- Ce que la preuve garantit : la clé ne peut pas être copiée. Pas qui a
  le droit d'appairer : une approbation par code ou une invitation en
  décide toujours.
- La machine de `iab hub init` reste admin avec une clé fichier : elle
  vit sur le moyeu, à côté de la clé de l'autorité, qu'un TPM ne
  protégerait pas mieux.

`iab approve` montre comment la clé a été prouvée, `iab machines` le
conserve (`attestation`).

## Utiliser

Poser `IAB_HUB` dans l'environnement des clients, par exemple dans la
déclaration MCP :

```json
{"type": "stdio", "command": "iab-server",
 "env": {"IAB_AGENT_NAME": "claude", "IAB_HUB": "https://iab.example.org:7443"}}
```

`iab ping` vérifie l'appairage ; `whoami` indique `"db_source": "hub"`.
L'espace de travail de chaque session reste dérivé de son répertoire
local.

## Reprendre un bus existant

Une machine qui avait déjà un bus local garde ses agents, files,
résultats, canal et journal : arrêter ses agents (le fichier est alors
complet, rien ne reste dans son `-wal`), copier le fichier (voir `iab
whoami` sur elle) vers le moyeu, puis sur le moyeu :

```bash
iab hub import mimir-bus.db --bind mimir --dry-run   # vérifie et compte
iab hub import mimir-bus.db --bind mimir
```

Tout passe en une transaction, après les entrées propres au moyeu (les
numéros du canal et des notifications sont renumérotés, les curseurs de
lecture suivent) ; les verrous de réveil restent derrière ; les entrées
de journal importées portent `import:<machine>`, quoi que dise le
fichier. `--bind` rattache les instances importées à la machine d'où
elles viennent, comme au premier usage : elles travaillent depuis elle,
mais ne comptent dans la politique qu'une fois qu'un admin s'en est
porté garant par `iab bind` ; un nom qu'une autre machine détient fait
refuser l'import. Sans `--bind`, elles n'appartiennent à aucune machine
et sont refusées tant qu'un admin ne les a pas rattachées une à une. Un nom ou
un identifiant de tâche présent des deux côtés fait refuser tout
l'import, sans rien écrire : le renommer d'abord sur la source
(`IAB_DB=mimir-bus.db iab rename …`). Le fichier est traité comme non
fiable : une ligne qui concerne autre chose que ses propres agents et
tâches (du travail pour une instance du moyeu, un résultat pour une
tâche du moyeu, un message à une instance qu'il n'apporte pas, ou signé
par elle) fait aussi refuser tout l'import, et ce qu'il apporte obéit à
`accepts_from` comme si la machine de `--bind` l'avait envoyé en direct
(sans `--bind`, aucune règle n'est satisfaite). Relancer les agents avec
`IAB_HUB` posé : ce qui s'écrirait encore dans le fichier local ne
serait pas repris.

## Réveil sur plusieurs machines

Le bloc `launch` du roster gagne un champ `host` : la machine qui lance
l'agent (son CLI et son `cwd` y vivent). Lancer `iab supervise` sur
**chaque** machine : il ne réveille que les entrées dont `host` est son
nom d'appairage. Donnez un `host` à chaque entrée : le moyeu ne laisse
prendre le verrou de réveil d'une instance qu'à la machine à laquelle
elle est rattachée, si bien qu'une entrée sans `host` est réveillée par
sa machine et refusée (une ligne `ERROR` à chaque tour) sur les autres. Une session interactive ouverte se réveille par
`iab watch --on any` en tâche de fond, en long-poll vers le moyeu.

## Administrer

| Commande | Effet |
|---|---|
| `iab machines` | machines appairées, rôle, type de clé, dernière visite |
| `iab revoke <machine>` | refusée dès sa requête suivante (la dernière admin ne peut pas l'être) |
| `iab deny <code>` | refuse une demande en attente |
| `iab bind <instance> <machine>` | rattache une instance à une machine (déménagement, ou pour qu'elle compte dans la politique) |
| `iab trust-ek [<empreinte>] [--note …] [--remove]` | fait confiance à la clé d'endossement d'un TPM par son empreinte (TPM virtuel) ; sans argument, les liste |
| `iab renew [--force]` | renouvelle le certificat (automatique à moins de 10 jours) |

Chaque appairage et chaque révocation sont journalisés et annoncés sur
le canal (sujet `machines`, auteur `bus`). Une machine restée hors ligne
plus de 30 jours doit être appairée de nouveau.

## Autorisation

Chaque requête vient d'une machine authentifiée, ce qui rend trois
règles applicables par le moyeu :

1. **Rattachement** : une instance `harnais@espace` **nouvelle**
   appartient à la machine qui la crée ; aucune autre ne peut ensuite
   réclamer sa file, publier, battre son cœur, lire sa boîte ou prendre
   son verrou de réveil. Une instance déjà existante que nul ne possède
   (base importée, enregistrement antérieur) est refusée tant qu'un
   admin ne l'a pas rattachée : `iab bind <instance> <machine>`, qui
   sert aussi à la déménager. Une opération refusée ne laisse aucun
   rattachement derrière elle. `iab machines` liste les instances de
   chaque machine.
2. **Opérations d'orchestrateur** : annuler, re-proposer, renommer,
   publier en force, diffuser vers un motif (`*`, `harnais@*`…).
   Réservées aux principaux de `policy.orchestrators` — par défaut les
   machines admin.
3. **`accepts_from`** : qui peut pousser du travail ou envoyer un
   message (notification, chaque destinataire d'une diffusion) à qui —
   la défense principale contre une injection de prompt passant des
   agents d'une machine à ceux d'une autre.

La politique se règle dans `hub.json`, jamais dans le code :

```json
"policy": {
  "orchestrators": ["role:admin", "claude@homelab"],
  "accepts_from": {"kimi@*": ["claude@*", "machine:yggdrasil"]}
},
"rate": {"per_second": 50, "burst": 200}
```

Principaux d'une requête : `machine:<nom>`, `role:<rôle>` (prouvés par le
certificat), et l'instance que le client déclare ou au nom de laquelle
il agit, **seulement si un admin l'a rattachée** à cette même machine
(`iab bind`) : un rattachement au premier usage prouve l'antériorité,
pas la confiance. Préférez `machine:<nom>` dans la politique.

Précisions :

- un motif `kimi@*` ne couvre pas un nom nu hérité `kimi` : renommez-le
  (`iab rename kimi kimi@<espace>`) — s'il n'appartient encore à aucune
  machine, un admin le rattache d'abord (`iab bind kimi <machine>`) ;
- le rattachement d'un admin suit un renommage fait par une machine
  admin, pas celui d'une machine membre ;
- une adresse ambiguë ou inconnue est refusée par le moyeu avant
  d'atteindre le cœur ;
- une politique mal formée (une chaîne au lieu d'une liste) empêche le
  moyeu de démarrer, plutôt que de s'appliquer de travers. Les lectures (état, résultats, journal, présence)
restent ouvertes à toute machine appairée. Le journal nomme la machine
de chaque événement (`iab log`).

## Sécurité : ce qui est garanti, ce qui ne l'est pas

- Sans certificat de machine valide et actif, le moyeu ne répond pas ;
  chaque refus de négociation est journalisé avec l'adresse source
  (crochet pour fail2ban ou CrowdSec).
- Les payloads n'apparaissent jamais dans le journal du moyeu ; une
  requête trop grosse est refusée, jamais tronquée.
- Appairage : codes de 8 lettres sans ambiguïté, 10 minutes, usage
  unique ; demandes limitées par adresse (`pair_rate`) et en nombre ;
  réseaux privés seulement par défaut (`pair_allow` dans `hub.json`),
  filtrés avant toute négociation TLS. L'adresse du moyeu enregistrée
  par la machine est liée aux mots et à la preuve d'invitation.
- **Le port d'appairage (7444) ne doit jamais passer par un relais TCP**
  (nginx stream, tunnel) : le moyeu y verrait l'adresse privée du relais
  et l'appairage s'ouvrirait à tout Internet. Liez-le à l'interface du
  réseau local ou du tunnel (`iab hub serve --pair-listen
  192.168.20.30:7444`). `100.64.0.0/10` (tailnet) est admis par défaut :
  retirez-le de `pair_allow` si le moyeu est lui-même derrière le CGNAT
  d'un opérateur.
- Plafonds : connexions ouvertes (256, dont 32 par adresse), négociation
  TLS bornée à 10 s, débit par machine (`rate`), réponses lues bornées
  côté client. Avec PostgreSQL, chaque attente bloquante tient une
  connexion : prévoir `max_connections` au-delà du nombre d'attentes
  simultanées.
- Le certificat du port du bus est vérifié deux fois par jour et
  renouvelé à chaud ; deux réponses de renouvellement perdues de suite
  ne verrouillent pas une machine.
- **Rôle admin** (approuver, inviter, révoquer, rattacher, trust-ek) :
  réservé à une machine dont la clé est attestée dans une puce TPM
  (politique du porteur, voir [Clés TPM](#clés-tpm-machines-admin)), et
  à la machine de `iab hub init`. Une invitation admin présentée avec
  une clé fichier est refusée et reste valable.
- **Non pris en charge** : les réseaux qui déchiffrent le TLS. Le client
  le détecte et refuse ; seule une exception du service informatique y
  remédie.
- L'écriture sur le bus reste adjacente à l'exécution de code chez les
  agents : n'appairer que des machines de confiance.
