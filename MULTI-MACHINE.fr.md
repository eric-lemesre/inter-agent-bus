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

## Appairer une machine

Sur la nouvelle machine :

```
$ iab join iab.example.org
Hub: iab.example.org
To pair this machine, on an admin machine run:
    iab approve
and type the code:   WDJB-MJHT
Check words: lampe-orage-violon-tulipe   (valid 10 min, waiting…)
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
partir de la clé de la machine **et** de la CA du moyeu : quiconque
s'interposerait au premier contact les ferait changer. C'est leur
comparaison qui protège l'appairage quand le port d'appairage n'a pas de
certificat public (le client le signale). L'approbation est
volontairement interactive.

**Machines gérées par Ansible** : une invitation à usage unique, liée au
nom, valable 15 minutes, remplace la personne :

```bash
iab invite mimir | ssh mimir 'iab join iab.example.org --name mimir --token -'
```

Le jeton passe par stdin (ou `IAB_INVITE_TOKEN`), jamais en argument ;
il ne voyage pas non plus sur le réseau : la machine prouve qu'elle le
détient par un code d'authentification lié à sa propre demande.

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

## Administrer

| Commande | Effet |
|---|---|
| `iab machines` | machines appairées, rôle, type de clé, dernière visite |
| `iab revoke <machine>` | refusée dès sa requête suivante (la dernière admin ne peut pas l'être) |
| `iab deny <code>` | refuse une demande en attente |
| `iab renew [--force]` | renouvelle le certificat (automatique à moins de 10 jours) |

Chaque appairage et chaque révocation sont journalisés et annoncés sur
le canal (sujet `machines`, auteur `bus`). Une machine restée hors ligne
plus de 30 jours doit être appairée de nouveau.

## Sécurité : ce qui est garanti, ce qui ne l'est pas

- Sans certificat de machine valide et actif, le moyeu ne répond pas ;
  chaque refus de négociation est journalisé avec l'adresse source
  (crochet pour fail2ban ou CrowdSec).
- Les payloads n'apparaissent jamais dans le journal du moyeu ; une
  requête trop grosse est refusée, jamais tronquée.
- Appairage : codes de 8 lettres sans ambiguïté, 10 minutes, usage
  unique ; demandes limitées par adresse et en nombre ; réseaux privés
  seulement par défaut (`pair_allow` dans `hub.json`).
- **Rôle admin** (approuver, inviter, révoquer) : réservé à une machine
  dont la clé est dans une puce TPM (politique du porteur). Tant que la
  preuve TPM n'est pas livrée, seule la machine de `iab hub init` est
  admin.
- **Non pris en charge** : les réseaux qui déchiffrent le TLS. Le client
  le détecte et refuse ; seule une exception du service informatique y
  remédie.
- L'écriture sur le bus reste adjacente à l'exécution de code chez les
  agents : n'appairer que des machines de confiance.
