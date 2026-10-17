# Amélioration en attente — bus multi-machine sécurisé

🇬🇧 [English](AMELIORATIONS-2026-10-17-multi-machine.md) · 🇫🇷 Français

Notée le 2026-10-17. Demandes du porteur, dans l'ordre où elles ont été
posées :

1. un bus actif **entre plusieurs machines** (poste, mimir, sindri,
   sessions opencode…), « avec la sécurité qui va bien, un peu dans le
   style de Docker Swarm » ;
2. un **moteur de base au choix** : SQLite par défaut, une base libre en
   option (PostgreSQL, MariaDB/MySQL, voire clé-valeur — à challenger) ;
3. un **appairage simple et intuitif mais sûr**, qui ne suppose pas de
   copier-coller entre machines (inspiré de Plex ou Netflix) ;
4. un fonctionnement **qui ne dépende pas de WireGuard** ;
5. la question du **vol de certificat** (lier à l'adresse MAC ?) ;
6. une mise en œuvre **vraiment simple**, installation et configuration
   de WireGuard comprises.

Décisions du porteur prises le 2026-10-17 (§ 13) ; implémentation en
cours, phase 9 de la [ROADMAP](ROADMAP.fr.md).

## 1. Constat

- Le bus est aujourd'hui **mono-machine par construction** :
  invariant 3 (transport stdio, bus SQLite, aucun démon) et une base en
  WAL qui exige un système de fichiers local. Deux machines ne peuvent
  pas partager une base : ni NFS ni SMB.
- Le modèle de confiance est « machine mono-utilisateur » : **qui écrit
  la base pilote les agents**. L'identité `harnais@espace` (phase 8a)
  est déclarative : rien n'empêche un client de se dire `claude@homelab`
  et de réclamer sa file ou de publier à sa place, sinon le fait d'avoir
  accès au fichier.
- Besoins concrets : la session HomeLab du poste doit échanger avec une
  session opencode sur sindri ; des workers sur mimir (audits de
  sécurité en creux, comparatifs) doivent réclamer des tâches poussées
  depuis le poste.

Passer en réseau change le modèle de menace : ce qui était un fichier
`0600` devient un service joignable. L'écriture sur le bus étant
adjacente à l'exécution de code (injection de prompt dans un agent
outillé), l'authentification et l'autorisation sont le cœur de
l'évolution, pas un ajout.

## 2. Ce que la personne fait (objectif de simplicité)

Tout le reste de l'étude sert ce parcours, qui doit rester celui-ci :

| Moment | Geste | Fréquence |
|---|---|---|
| Mise en place | `iab hub init` sur la machine du moyeu (ou le rôle Ansible `iab_hub`) | une fois |
| Nouvelle machine | `iab join` sur elle, puis `iab approve` + 8 caractères tapés et 4 mots comparés sur une machine de confiance | une fois par machine |
| Machine du homelab | rien : le rôle Ansible l'appaire | — |
| Usage | rien : les sessions s'enregistrent seules, les certificats se renouvellent seuls | — |
| Machine perdue | `iab revoke <machine>` | rare |

Aucun fichier, certificat ou jeton à copier d'une machine à l'autre.

## 3. Options d'architecture étudiées

| Option | Principe | Verdict |
|---|---|---|
| Base partagée en NFS/SMB | même fichier SQLite monté partout | **Exclu** : WAL interdit les FS réseau, corruption silencieuse. |
| Réplication SQLite (rqlite, dqlite, Litestream) | consensus Raft ou copie du WAL | **Écarté** : Litestream ne fait que de la sauvegarde ; rqlite et dqlite n'offrent pas les transactions interactives sur lesquelles reposent claim, fencing et verrous de réveil. Un démon de plus. |
| Courtier de messages (NATS JetStream) | files et KV du courtier, mTLS natif | **Écarté pour l'instant** : sécurité mûre, mais la logique d'état quitterait `store.py` (invariant 2). À revoir si plusieurs nœuds actifs deviennent nécessaires. |
| Fédération de bus | une base par machine, transfert vers la machine « propriétaire » | **Plus tard** : cohérence à terme, double autorité, fencing inter-bus à inventer. |
| **Moyeu (hub) + rayons** | **une** base, exposée par un service mince ; les machines y accèdent en réseau authentifié | **Retenu** : la logique reste dans `store.py`. |

Le moyeu est **indispensable même avec PostgreSQL** : c'est lui qui
vérifie quelle machine a le droit de faire quoi (§ 7). Laisser chaque
machine se connecter directement à la base lui donnerait des
identifiants qui contournent toutes les règles.

## 4. Architecture retenue : deux axes indépendants

```
              accès                         stockage
 poste ─┐                                ┌─ SQLite (défaut)
 mimir ─┼── HTTPS + mTLS ──▶ iab hub ────┤
 sindri ┘   (WireGuard, LAN               └─ PostgreSQL (option)
             ou Internet)    store.py
```

- **Axe stockage** : `store.py` garde toute la logique ; une couche de
  dialecte choisit le moteur (§ 5).
- **Axe accès** : une couche d'accès choisit `LocalBackend` (comportement
  actuel, défaut) ou `RemoteBackend` (client du moyeu), selon `IAB_HUB`.
  Absent, rien ne change : invariants 2 et 3 respectés en mode local ; le
  moyeu est un **ajout opt-in**, seul démon du dispositif.
- **`iab hub serve`** : enveloppe mince qui expose les fonctions de
  `store.py` en JSON sur HTTPS (une route par opération, mêmes chaînes de
  retour, `ERROR:` compris). Aucune logique d'état dans le moyeu.
- **Clients inchangés côté agent** : `iab-server` stdio local, CLI,
  `iab worker` et `iab supervise` passent par `RemoteBackend`. L'espace
  de travail reste dérivé du `cwd` local.
- **Attentes bloquantes** (`wait_signal`, `wait_task`) : long-poll HTTP
  borné ; avec PostgreSQL, `LISTEN/NOTIFY` réveille le moyeu sans
  interroger la base en boucle.
- **Moyeu injoignable = échec bruyant** (invariant 5) : `ERROR: hub
  unreachable`, **jamais** de repli silencieux sur une base locale — ce
  serait une partition, pire qu'une panne (règle de rendez-vous).

## 5. Moteur de stockage

Mesure du cœur v0.12.0 : 10 tables, environ 140 requêtes. Les dates sont
stockées en texte ISO (aucune fonction de date SQLite). Les tournures
propres à SQLite sont peu nombreuses : 10 `BEGIN IMMEDIATE`, 7
`ON CONFLICT`, 1 `INSERT OR`, 3 `AUTOINCREMENT`, 3 `lastrowid`, 6
`PRAGMA` (migrations). Elles tiennent dans une **couche de dialecte**
maison (paramètres, upsert, identifiants auto, verrouillage,
introspection du schéma) : pas d'ORM, conformément à la règle « pas de
dépendance à la légère ».

| Moteur | Apport | Coût | Verdict |
|---|---|---|---|
| **SQLite** | zéro installation, zéro administration | un seul écrivain (le moyeu) | **défaut** |
| **PostgreSQL** | moyeu à plusieurs processus ; `FOR UPDATE SKIP LOCKED`, fait pour les files ; `LISTEN/NOTIFY` pour le réveil ; sauvegarde et réplication éprouvées ; déjà exploité dans le homelab | adapter ~30 tournures ; tests sur deux moteurs ; pilote `psycopg` en extra `[postgres]` | **option retenue** |
| MariaDB / MySQL | équivalent fonctionnel | troisième dialecte, différent sur les points clés (`ON DUPLICATE KEY`, `AUTO_INCREMENT`), double matrice de tests, rien de plus que PostgreSQL | **écarté**, sauf serveur existant à mutualiser |
| Clé-valeur (Valkey/Redis, etcd) | etcd : Raft et mTLS intégrés | pas de transactions conditionnelles multi-clés : claim, bail et fencing réécrits en scripts Lua ou transactions etcd, donc hors du cœur ; etcd limite une valeur à ~1,5 Mo (les diffs de revue dépassent) | **écarté** comme stockage |

La suite de tests du cœur doit tourner sur les deux moteurs (PostgreSQL
désigné par une variable d'environnement de test, ignoré s'il est
absent).

## 6. Appairage par code, à la manière de Plex et Netflix

Plex (plex.tv/link) et Netflix (code affiché par la télévision)
appliquent le **flux d'autorisation d'appareil** d'OAuth 2.0 (RFC 8628) :
l'appareil affiche un code court, la personne le **lit** et le **tape**
sur un appareil déjà connecté. Rien n'est copié d'une machine à l'autre.

Sur la nouvelle machine (écran ou session SSH) :

```
$ iab join
Moyeu : iab.lemesre.org (certificat vérifié)
Pour appairer cette machine, sur une machine de confiance tapez :
    iab approve
puis saisissez le code :   WDJB-MJHT
Contrôle : lampe-orage-violon-tulipe        (valable 10 min, en attente…)
```

Sur une machine de confiance (plus tard aussi une page web derrière
Authentik, sur le modèle de plex.tv/link) :

```
$ iab approve
Code : WDJB-MJHT
Demande de « mimir » — Linux aarch64 — 192.168.20.21 — clé : fichier
Contrôle : lampe-orage-violon-tulipe
Les quatre mots sont-ils identiques à ceux affichés sur mimir ? [o/N] o
Machine « mimir » appairée.
```

La nouvelle machine termine seule (« Appairée. Certificat valable
30 jours, renouvellement automatique. ») ; une notification part vers
le porteur.

| Risque | Parade |
|---|---|
| Code deviné | 8 caractères d'un alphabet sans ambiguïté (~25 milliards de combinaisons), 10 min, usage unique, tentatives limitées |
| Code « piégé » (un tiers lance `join` et fait approuver son code) | l'écran d'approbation montre nom, système, adresse source, type de clé et les 4 mots ; refus par défaut |
| Faux moyeu, interception | certificat serveur public vérifié (§ 7) ; les 4 mots sont dérivés de la clé reçue par le moyeu : une interposition les rend différents |
| Clé privée volée en transit | générée sur la machine, jamais transmise : seule une CSR voyage |
| Approbation par n'importe qui | seules les machines de rôle admin approuvent (ou la page web derrière Authentik) |
| Appairage à l'insu du porteur | notification et événement au journal à chaque appairage ; `iab machines`, `iab revoke` |

Cas particuliers :

- **Première machine** : `iab hub init` en fait la première machine
  admin.
- **Machines du homelab** : le rôle Ansible enchaîne une invitation
  interne (jeton à usage unique, 15 min, lié au nom) sur le moyeu et
  `iab join --token` sur la cible ; aucun geste humain.
- **Adresse du moyeu** : `iab join` sans argument la trouve par un nom
  DNS fixe fourni par l'inventaire ; `iab join <hôte>` sinon.
- **Machine hors ligne plus de 30 jours** : certificat expiré, appairage
  à refaire — compromis assumé entre simplicité et sécurité.

## 7. Sécurité, indépendante du réseau

Le bus doit fonctionner par WireGuard, sur le LAN **et** par Internet.
WireGuard devient une protection de plus, jamais une condition. Ce qu'il
apportait est obtenu autrement :

| Rôle tenu par WireGuard | Remplacement |
|---|---|
| authentifier le moyeu au premier contact | **certificat serveur public** (Let's Encrypt, `iab.lemesre.org`) vérifié comme tout site HTTPS ; la CA interne du bus ne signe que les certificats de machines |
| filtrer qui peut parler au bus | **mTLS obligatoire** sur toute l'API : sans certificat de machine valide, refus dès la négociation TLS |
| limiter qui peut demander un appairage | page d'appairage publique par nature (comme plex.tv/link), bornée : débit par IP, demandes en attente plafonnées, expiration 10 min, fail2ban |

**Joignabilité depuis Internet** : le moyeu reste dans le homelab, joint
par l'edge existant de lemesre.org, sans déchiffrement intermédiaire :

```
Internet ──443──▶ lemesre.org (nginx stream, aiguillage SNI, sans déchiffrer)
                        │  tunnel WireGuard existant (edge ⇄ heimdall)
                        ▼
                     iab hub   (TLS de bout en bout jusqu'au moyeu)
```

Le port 443 passe la plupart des réseaux ; lemesre.org ne voit jamais le
contenu et ne valide pas les certificats de machines (c'est le moyeu).

**Points d'écoute et politique** (proposition de départ) :

| Origine | Bus (mTLS) | Demande d'appairage | Approbation |
|---|---|---|---|
| WireGuard | oui | oui | admin |
| LAN | oui | oui | admin |
| Internet | oui | **non par défaut**, option | admin |

**Autorisation liée à l'identité prouvée** (le vrai gain sur le mode
local) :

- chaque instance `harnais@espace` est **rattachée à une machine** à son
  premier enregistrement ; le moyeu refuse qu'une autre machine réclame
  sa file, publie, batte le cœur ou relève sa boîte en son nom ;
- les opérations d'orchestrateur (`force` au publish, `cancel`,
  `requeue`, `rename_agent`, `broadcast *`) exigent un rôle déclaré au
  roster ;
- liste blanche d'émetteurs par file au roster (`accepts_from`) : qui
  peut pousser du travail à qui — défense principale contre l'injection
  de prompt d'une machine à l'autre.

**Vol de clé : la puce TPM plutôt que l'adresse MAC.** Lier le
certificat à l'adresse MAC ne protège pas : le moyeu ne la voit pas
au-delà du premier routeur, la machine devrait donc la *déclarer*, et un
voleur déclare la même (c'est une chaîne, réglable en une commande) ;
elle change en outre d'elle-même (MAC aléatoire en Wi-Fi, station
d'accueil, VM recréée). Toute « empreinte matérielle » auto-déclarée a le
même défaut. La parade est de rendre la clé **non copiable** : générée
**dans la puce TPM 2.0**, qui signe sans jamais la livrer (Secure
Enclave sous macOS, fournisseur de plateforme sous Windows ; YubiKey PIV
en option). Copier le disque ou une sauvegarde ne donne alors rien. À
l'appairage, l'attestation TPM prouve que la clé est née dans la puce ;
le moyeu applique une politique par machine (`cle: materielle |
fichier`) et refuse un appairage qui ne la respecte pas. Sans puce : clé
en fichier `0600`, certificats courts, révocation, notification.

Relevé du parc le 2026-10-17 (par SSH) :

| TPM 2.0 | Machines |
|---|---|
| ✅ présent | yggdrasil (poste, Dell Precision 7750), odin, hugin, tyr, lemesre-org |
| ⚠️ TPM 1.2 seulement | munin (clé en fichier) |
| ❌ absent | mimir (DGX Spark), asgard (Synology), talaxie (VPS), mail-gfrexit, mail-rlc |
| ? non vérifié | sindri (VM sur odin : TPM virtuel possible, sa protection dépend d'odin) |

**Réseaux d'entreprise à inspection TLS** (Zscaler, Netskope…) : le
proxy déchiffre le trafic, le mTLS ne peut pas passer et le contenu du
bus serait visible de l'employeur. **Non pris en charge** : le client
détecte un certificat serveur qui n'est pas celui attendu et refuse
bruyamment (« inspection TLS détectée, rien n'est envoyé »). Seule voie
légitime : une exception accordée par le service informatique. Le
ThinkPad professionnel du porteur (Zscaler) est hors périmètre.

**Autres mesures** : tailles maximales vérifiées par le moyeu (refus,
jamais troncature), débit limité par machine, journal enrichi de la
machine appelante, payloads hors des journaux HTTP, aucun secret en
payload ; base du moyeu en `0600`, sauvegarde nocturne cohérente
(API de sauvegarde SQLite ou `pg_dump`), conservation du journal bornée.
Le canal et les diffusions restent **des données, jamais des ordres**.

## 8. WireGuard : rendre la couche réseau simple aussi

Le bus n'en dépend plus, mais le homelab s'appuie sur WireGuard (D9,
façade opencode, accès nomade). État actuel : clés générées et
échangées à la main, deux tunnels imbriqués sur le portable (edge puis
`wg-homelab`, MTU 1340), pairs déclarés un par un dans
`wireguard_edge_peers` et dans OPNsense — environ deux soirées par mise
en service (`infra/docs/designs/acces-nomade-identite.md`).

| Piste | Principe | Simplicité | Verdict |
|---|---|---|---|
| Statu quo | clés et pairs à la main | faible | à dépasser |
| Rôle Ansible `wireguard_peer` | la clé est générée **sur** le pair, la clé publique remonte en fait Ansible, les deux côtés sont rendus ; QR code (`qrencode`) pour le téléphone | bonne pour les machines gérées, moyenne pour les appareils nomades | **à faire dans tous les cas** |
| **Headscale** (Tailscale auto-hébergé) sur lemesre.org | `tailscale up --login-server …` affiche un lien ; approbation par SSO Authentik — **exactement le modèle Plex** ; clé par appareil, ACL par étiquette, DNS interne, traversée des réseaux filtrants par relais HTTPS | très bonne, y compris téléphone et portables | **recommandé** dès que le parc dépasse quelques appareils — c'est le cas avec le bus |

La conception nomade gardait déjà Headscale « en réserve » si le parc
grandissait. Le bus le fait grandir. Les deux appairages restent
**indépendants** (défense en profondeur) : le réseau et le bus ont
chacun leur identité, et la compromission de l'un n'ouvre pas l'autre.
Décision à porter dans le dépôt HomeLab.

## 9. Réveil multi-machine

- Le bloc `launch` du roster gagne un champ `host` ; `iab supervise`
  tourne **sur chaque machine** et ne réveille que les instances de sa
  machine (certificat ↔ `host`). Le verrou de réveil, déjà en base, reste
  l'arbitre.
- Une session ouverte se réveille par `iab watch --on any` en tâche de
  fond, désormais en long-poll vers le moyeu.

## 10. Dépendances et plateformes

- Client : bibliothèque standard (`http.client`, `ssl`, proxy
  `HTTPS_PROXY` par tunnel CONNECT) ; clés et CSR par `cryptography` dans
  l'extra `[hub]` — préférée à `openssl` en ligne de commande, absent de
  Windows par défaut et variable selon les versions ; TPM par
  `tpm2-pytss` (Linux), API du système ailleurs, dans un extra `[tpm]`.
- Moyeu : extra `[hub]` ; `[postgres]` pour `psycopg` ; le SDK MCP de
  `[server]` fournit le streamable HTTP de la variante distante.
- Le cœur reste sans dépendance. Linux, macOS, Windows restent de
  premier rang côté client ; le moyeu vise d'abord Linux (systemd).

## 11. Déploiement homelab (proposition)

- **Machine du moyeu** : une **petite VM dédiée** (ou un hôte toujours
  allumé qui n'exécute pas d'agents), de préférence avec TPM (virtuel sur
  odin). Pas sindri : le moyeu porte la clé de signature des certificats,
  qui ne doit pas côtoyer des agents autonomes ; pas mimir : redémarrages
  fréquents et agents d'audit.
- Rôle Ansible `iab_hub` dans `infra` : paquet épinglé sur un tag, unité
  systemd confinée, CA et clé dans le vault, sauvegarde vers asgard,
  aiguillage SNI sur lemesre.org.
- Machines gérées : appairage par le rôle ; `IAB_HUB` dans
  l'environnement des clients ; le rôle `poste_llm` n'ajoute que
  `IAB_HUB` à sa clé `poste_opencode_mcp`.
- Migration : `iab hub import <base locale>` reprend agents, files,
  résultats et journal d'une base existante.

## 12. Découpage proposé (phase 9)

- **9a** — couches d'accès et de dialecte, `LocalBackend` + SQLite, sans
  changement de comportement ; tests du cœur inchangés et verts.
- **9b** — moyeu : `iab hub init/serve`, mTLS, certificat serveur public,
  `RemoteBackend`, long-poll, détection d'inspection TLS ; `smoke.py`
  étendu à un moyeu en boucle locale.
- **9c** — appairage : `iab join`, `iab approve`, codes et mots de
  contrôle, invitation interne pour Ansible, renouvellement, `iab
  machines`, `iab revoke`, notifications.
- **9d** — autorisation : rattachement instance ↔ machine, rôles
  d'orchestrateur, `accepts_from`, limites.
- **9e** — clés TPM et attestation, politique par machine.
- **9f** — dialecte PostgreSQL, `LISTEN/NOTIFY`, tests sur deux moteurs.
- **9g** — `host` dans `launch`, superviseur par machine.
- **9h** (optionnel) — MCP streamable HTTP pour les harnais confinés.

## 13. Décisions attendues du porteur, avec leur impact

**Tranché le 2026-10-17** : moyeu unique (1) ; `cryptography` (3) ;
variante MCP distante reportée (4) ; **une seule base** (5) — l'avis
« une base par domaine de confiance » n'est pas retenu, `accepts_from`
reste la défense principale entre agents ; appairage depuis Internet
fermé au départ (6) ; TPM obligatoire pour les machines admin (7) ;
Headscale validé, porté par le dépôt HomeLab (8). Reste ouvert : la
machine du moyeu (2), à trancher côté HomeLab avec le rôle `iab_hub`.

1. **Moyeu unique ou haute disponibilité ?** Unique : si sa machine
   tombe, la coordination entre machines s'arrête (erreur explicite), les
   agents isolés continuent ; quelques minutes d'arrêt au redémarrage,
   restauration depuis la sauvegarde nocturne au pire. Haute
   disponibilité : trois machines toujours allumées (quorum Raft) ou une
   réplication PostgreSQL avec bascule (Patroni), lourde à exploiter.
   **Avis : unique**, les agents ne sont pas un service critique.
2. **Machine du moyeu ?** sindri : rien à créer, mais la clé de
   signature côtoie des agents autonomes. mimir : PostgreSQL déjà là,
   mais redémarrages et agents d'audit. VM dédiée : isolation nette, une
   VM de plus décrite en Ansible. **Avis : VM dédiée.**
3. **`cryptography` ou `openssl` ?** Ne touche que le client d'appairage
   (extra), pas le cœur. `cryptography` : paquets prêts pour toutes les
   plateformes, appels testables. `openssl` : absent de Windows par
   défaut, options variables. **Avis : `cryptography`.**
4. **Variante MCP distante (9h) ?** Sans elle, `iab` s'installe sur
   chaque machine d'agents, y compris dans le service opencode, qui doit
   alors pouvoir lire le certificat de la machine. Avec elle, aucune
   installation, mais un jeton par session dans la configuration opencode
   (exposition comparable) et un espace de travail porté par le jeton
   faute de `cwd`. **Avis : plus tard**, toutes les machines sont gérées
   par Ansible.
5. **Une base par projet ou une seule ?** Une seule : tout le monde se
   parle, mais une injection dans un projet atteint les agents des
   autres. Une par projet : isolement, mais les échanges entre projets,
   réellement utiles (HomeLab ⇄ Conciliateur pour inferd), tombent.
   **Avis : une base par domaine de confiance** sur le même moyeu
   (« personnel » : HomeLab, infra, outils IA ; un domaine par contexte
   extérieur : justice, associations), `accepts_from` bornant les envois
   à l'intérieur d'un domaine.
6. **Appairage depuis Internet ?** Fermé : le seul point public sans
   authentification reste clos, une machine déjà appairée fonctionne
   quand même partout. Ouvert : appairer en déplacement, au prix d'une
   page publique à surveiller. **Avis : fermé au départ.**
7. **Clé matérielle exigée où ?** **Avis : TPM obligatoire pour les
   machines admin** (celles qui approuvent), fichier toléré ailleurs.
8. **Headscale pour le homelab ?** Décision à porter dans le dépôt
   HomeLab (§ 8). **Avis : oui**, avant ou avec le bus multi-machine.
