# Pending improvement — secure multi-machine bus

🇬🇧 English · 🇫🇷 [Français](AMELIORATIONS-2026-10-17-multi-machine.fr.md)

Noted on 2026-10-17. Owner's requests, in the order they were raised:

1. a bus that works **across several machines** (workstation, mimir,
   sindri, opencode sessions…), "with proper security, somewhat in the
   style of Docker Swarm";
2. a **choice of database engine**: SQLite by default, an open-source
   database as an option (PostgreSQL, MariaDB/MySQL, or even key-value —
   to be challenged);
3. **simple, intuitive yet secure pairing** that does not assume
   copy-paste between machines (inspired by Plex or Netflix);
4. operation that **does not depend on WireGuard**;
5. the question of **certificate theft** (bind to the MAC address?);
6. a **truly simple** rollout, WireGuard installation and configuration
   included.

Owner's decisions taken on 2026-10-17 (§ 13); implementation under way,
phase 9 of the [ROADMAP](ROADMAP.md).

## 1. Findings

- The bus is **single-machine by construction** today: invariant 3
  (stdio transport, SQLite bus, no daemon) and a WAL database that
  requires a local filesystem. Two machines cannot share a database: no
  NFS, no SMB.
- The trust model is "single-user machine": **whoever writes the
  database drives the agents**. The `harness@workspace` identity
  (phase 8a) is declarative: nothing stops a client from calling itself
  `claude@homelab` and claiming its queue or publishing in its place,
  other than access to the file.
- Concrete needs: the workstation's HomeLab session must talk to an
  opencode session on sindri; workers on mimir (off-peak security
  audits, benchmarks) must claim tasks pushed from the workstation.

Going networked changes the threat model: what was a `0600` file becomes
a reachable service. Since writing to the bus is adjacent to code
execution (prompt injection into a tool-wielding agent), authentication
and authorization are the core of this evolution, not an add-on.

## 2. What the person does (simplicity target)

The rest of this study serves this journey, which must stay as is:

| Moment | Action | Frequency |
|---|---|---|
| Setup | `iab hub init` on the hub machine (or the `iab_hub` Ansible role) | once |
| New machine | `iab join` on it, then `iab approve` + 8 typed characters and 4 compared words on a trusted machine | once per machine |
| Homelab machine | nothing: the Ansible role pairs it | — |
| Daily use | nothing: sessions register themselves, certificates renew themselves | — |
| Lost machine | `iab revoke <machine>` | rare |

No file, certificate or token to copy from one machine to another.

## 3. Architecture options considered

| Option | Principle | Verdict |
|---|---|---|
| Shared database over NFS/SMB | same SQLite file mounted everywhere | **Excluded**: WAL forbids network filesystems, silent corruption. |
| SQLite replication (rqlite, dqlite, Litestream) | Raft consensus or WAL shipping | **Rejected**: Litestream is backup only; rqlite and dqlite lack the interactive transactions that claim, fencing and wake locks rely on. One more daemon. |
| Message broker (NATS JetStream) | broker queues and KV, native mTLS | **Rejected for now**: mature security, but the state logic would leave `store.py` (invariant 2). Revisit if several active nodes become necessary. |
| Bus federation | one database per machine, forwarding to the "owning" machine | **Later**: eventual consistency, dual authority, cross-bus fencing to invent. |
| **Hub and spokes** | **one** database, exposed by a thin service; machines reach it over an authenticated network | **Chosen**: the logic stays in `store.py`. |

The hub is **required even with PostgreSQL**: it is what checks which
machine may do what (§ 7). Letting every machine connect to the database
directly would hand it credentials that bypass every rule.

## 4. Chosen architecture: two independent axes

```
              access                          storage
 workstation ─┐                           ┌─ SQLite (default)
 mimir ───────┼─ HTTPS + mTLS ─▶ iab hub ─┤
 sindri ──────┘  (WireGuard, LAN          └─ PostgreSQL (option)
                  or Internet)   store.py
```

- **Storage axis**: `store.py` keeps all the logic; a dialect layer
  picks the engine (§ 5).
- **Access axis**: an access layer picks `LocalBackend` (current
  behavior, default) or `RemoteBackend` (hub client), based on
  `IAB_HUB`. Unset, nothing changes: invariants 2 and 3 hold in local
  mode; the hub is an **opt-in addition**, the only daemon involved.
- **`iab hub serve`**: a thin wrapper exposing the `store.py` functions
  as JSON over HTTPS (one route per operation, same return strings,
  `ERROR:` included). No state logic in the hub.
- **Agent-side clients unchanged**: local stdio `iab-server`, CLI,
  `iab worker` and `iab supervise` go through `RemoteBackend`. The
  workspace is still derived from the local `cwd`.
- **Blocking waits** (`wait_signal`, `wait_task`): bounded HTTP
  long-poll; with PostgreSQL, `LISTEN/NOTIFY` wakes the hub without
  polling the database.
- **Hub unreachable = loud failure** (invariant 5): `ERROR: hub
  unreachable`, **never** a silent fallback to a local database — that
  would be a partition, worse than an outage (rendezvous rule).

## 5. Storage engine

Measured on the v0.12.0 core: 10 tables, about 140 statements. Dates are
stored as ISO text (no SQLite date functions). SQLite-specific
constructs are few: 10 `BEGIN IMMEDIATE`, 7 `ON CONFLICT`, 1
`INSERT OR`, 3 `AUTOINCREMENT`, 3 `lastrowid`, 6 `PRAGMA` (migrations).
They fit in a home-grown **dialect layer** (placeholders, upsert,
generated ids, locking, schema introspection): no ORM, in line with the
"no new dependencies lightly" rule.

| Engine | Gain | Cost | Verdict |
|---|---|---|---|
| **SQLite** | zero install, zero administration | a single writer (the hub) | **default** |
| **PostgreSQL** | multi-process hub; `FOR UPDATE SKIP LOCKED`, built for queues; `LISTEN/NOTIFY` for wake-ups; proven backup and replication; already run in the homelab | adapt ~30 constructs; tests on two engines; `psycopg` driver in a `[postgres]` extra | **chosen option** |
| MariaDB / MySQL | functionally equivalent | a third dialect, different on the key points (`ON DUPLICATE KEY`, `AUTO_INCREMENT`), a doubled test matrix, nothing PostgreSQL lacks | **rejected**, unless an existing server is to be shared |
| Key-value (Valkey/Redis, etcd) | etcd: built-in Raft and mTLS | no conditional multi-key transactions: claim, lease and fencing rewritten as Lua scripts or etcd transactions, hence outside the core; etcd caps a value at ~1.5 MB (review diffs exceed it) | **rejected** as storage |

The core test suite must run on both engines (PostgreSQL selected by a
test environment variable, skipped when absent).

## 6. Code pairing, Plex- and Netflix-style

Plex (plex.tv/link) and Netflix (code shown by the TV) apply the OAuth
2.0 **device authorization grant** (RFC 8628): the device shows a short
code, the person **reads** it and **types** it on an already signed-in
device. Nothing is copied from one machine to the other.

On the new machine (screen or SSH session):

```
$ iab join
Hub: iab.lemesre.org (certificate verified)
To pair this machine, on a trusted machine type:
    iab approve
then enter the code:   WDJB-MJHT
Check: lamp-storm-violin-tulip              (valid 10 min, waiting…)
```

On a trusted machine (later also a web page behind Authentik, modelled
on plex.tv/link):

```
$ iab approve
Code: WDJB-MJHT
Request from "mimir" — Linux aarch64 — 192.168.20.21 — key: file
Check: lamp-storm-violin-tulip
Are the four words identical to those shown on mimir? [y/N] y
Machine "mimir" paired.
```

The new machine finishes on its own ("Paired. Certificate valid
30 days, automatic renewal."); a notification goes to the owner.

| Risk | Countermeasure |
|---|---|
| Guessed code | 8 characters from an unambiguous alphabet (~25 billion combinations), 10 min, single use, limited attempts |
| "Planted" code (a third party runs `join` and gets their code approved) | the approval screen shows name, OS, source address, key type and the 4 words; deny by default |
| Fake hub, interception | public server certificate checked (§ 7); the 4 words derive from the key the hub received: interposition makes them differ |
| Private key stolen in transit | generated on the machine, never sent: only a CSR travels |
| Approval by anyone | only admin-role machines approve (or the web page behind Authentik) |
| Pairing behind the owner's back | notification and journal event on every pairing; `iab machines`, `iab revoke` |

Special cases:

- **First machine**: `iab hub init` makes it the first admin machine.
- **Homelab machines**: the Ansible role chains an internal invitation
  (single-use token, 15 min, bound to the name) on the hub and
  `iab join --token` on the target; no human action.
- **Hub address**: `iab join` without an argument finds it through a
  fixed DNS name provided by the inventory; `iab join <host>` otherwise.
- **Machine offline for more than 30 days**: certificate expired,
  pairing to redo — an accepted trade-off between simplicity and
  security.

## 7. Security, independent of the network

The bus must work over WireGuard, on the LAN **and** over the Internet.
WireGuard becomes one more layer, never a precondition. What it provided
is obtained otherwise:

| Role WireGuard played | Replacement |
|---|---|
| authenticate the hub on first contact | **public server certificate** (Let's Encrypt, `iab.lemesre.org`) checked like any HTTPS site; the bus's internal CA only signs machine certificates |
| filter who can talk to the bus | **mandatory mTLS** on the whole API: no valid machine certificate, refused during the TLS handshake |
| limit who can request pairing | pairing page public by nature (like plex.tv/link), bounded: per-IP rate, capped pending requests, 10-min expiry, fail2ban |

**Implementation adjustment (9b)**: the certificate of the **bus
listener** is issued by the bus CA itself, and the client trusts that CA
only (received at pairing), never the system store. An inspecting proxy
cannot present a certificate chaining to it: the handshake fails before
anything is sent, with no ad hoc mechanism. The public certificate
(Let's Encrypt) now serves only the **pairing listener**, the one first
contact where the machine does not know the bus CA yet. The hub renews
its bus certificate by itself.

**Reachability from the Internet**: the hub stays in the homelab,
reached through the existing lemesre.org edge, with no decryption on the
way:

```
Internet ──443──▶ lemesre.org (nginx stream, SNI routing, no decryption)
                        │  existing WireGuard tunnel (edge ⇄ heimdall)
                        ▼
                     iab hub   (end-to-end TLS to the hub)
```

Port 443 passes most networks; lemesre.org never sees the content and
does not validate machine certificates (the hub does).

**Listeners and policy** (initial proposal):

| Origin | Bus (mTLS) | Pairing request | Approval |
|---|---|---|---|
| WireGuard | yes | yes | admin |
| LAN | yes | yes | admin |
| Internet | yes | **no by default**, optional | admin |

**Authorization bound to the proven identity** (the real gain over local
mode):

- each `harness@workspace` instance is **bound to a machine** on its
  first registration; the hub refuses another machine claiming its
  queue, publishing, heartbeating or polling its inbox on its behalf;
- orchestrator operations (`force` on publish, `cancel`, `requeue`,
  `rename_agent`, `broadcast *`) require a role declared in the roster;
- per-queue sender allow list in the roster (`accepts_from`): who may
  push work to whom — the main defense against cross-machine prompt
  injection.

**Key theft: the TPM chip rather than the MAC address.** Binding the
certificate to the MAC address does not protect: the hub cannot see it
past the first router, so the machine would have to *declare* it, and a
thief declares the same one (it is a string, settable in one command);
it also changes by itself (randomized Wi-Fi MAC, docking station,
recreated VM). Any self-declared "hardware fingerprint" has the same
flaw. The countermeasure is to make the key **non-copyable**: generated
**inside the TPM 2.0 chip**, which signs without ever releasing it
(Secure Enclave on macOS, platform provider on Windows; YubiKey PIV as
an option). Copying the disk or a backup then yields nothing. At pairing
time, TPM attestation proves the key was born in the chip; the hub
applies a per-machine policy (`key: hardware | file`) and refuses a
pairing that does not meet it. Without a chip: `0600` key file,
short-lived certificates, revocation, notification.

Fleet survey on 2026-10-17 (over SSH):

| TPM 2.0 | Machines |
|---|---|
| ✅ present | yggdrasil (workstation, Dell Precision 7750), odin, hugin, tyr, lemesre-org |
| ⚠️ TPM 1.2 only | munin (file key) |
| ❌ absent | mimir (DGX Spark), asgard (Synology), talaxie (VPS), mail-gfrexit, mail-rlc |
| ? not checked | sindri (VM on odin: virtual TPM possible, its protection depends on odin) |

**Corporate networks with TLS inspection** (Zscaler, Netskope…): the
proxy decrypts traffic, mTLS cannot pass and the bus content would be
visible to the employer. **Not supported**: the client detects a server
certificate other than the expected one and refuses loudly ("TLS
inspection detected, nothing sent"). The only legitimate path is an
exception granted by the IT department. The owner's work ThinkPad
(Zscaler) is out of scope.

**Other measures**: maximum sizes enforced by the hub (refuse, never
truncate), per-machine rate limiting, journal enriched with the calling
machine, payloads kept out of HTTP logs, no secret in payloads; hub
database `0600`, consistent nightly backup (SQLite backup API or
`pg_dump`), bounded journal retention. The channel and broadcasts remain
**data, never orders**.

## 8. WireGuard: making the network layer simple too

The bus no longer depends on it, but the homelab relies on WireGuard
(D9, opencode facade, roaming access). Current state: keys generated and
exchanged by hand, two nested tunnels on the laptop (edge then
`wg-homelab`, MTU 1340), peers declared one by one in
`wireguard_edge_peers` and in OPNsense — about two evenings per rollout
(`infra/docs/designs/acces-nomade-identite.md`).

| Path | Principle | Simplicity | Verdict |
|---|---|---|---|
| Status quo | keys and peers by hand | low | to move past |
| `wireguard_peer` Ansible role | the key is generated **on** the peer, the public key comes back as an Ansible fact, both sides are rendered; QR code (`qrencode`) for the phone | good for managed machines, average for roaming devices | **to do in any case** |
| **Headscale** (self-hosted Tailscale) on lemesre.org | `tailscale up --login-server …` shows a link; approval through Authentik SSO — **exactly the Plex model**; per-device key, tag-based ACLs, internal DNS, traversal of filtered networks through an HTTPS relay | very good, phone and laptops included | **recommended** once the fleet exceeds a few devices — the bus makes it so |

The roaming-access design already kept Headscale "in reserve" should the
fleet grow. The bus makes it grow. The two pairings stay **independent**
(defense in depth): network and bus each have their own identity, and
compromising one does not open the other. Decision to be recorded in the
HomeLab repository.

## 9. Multi-machine wake-up

- The roster's `launch` block gains a `host` field; `iab supervise` runs
  **on each machine** and only wakes the instances of its own machine
  (certificate ↔ `host`). The wake lock, already in the database, stays
  the arbiter.
- An open session wakes itself with `iab watch --on any` in the
  background, now long-polling the hub.

## 10. Dependencies and platforms

- Client: standard library (`http.client`, `ssl`, `HTTPS_PROXY` through
  CONNECT tunnelling); keys and CSR through `cryptography` in the `[hub]`
  extra — preferred over the `openssl` command line, absent from Windows
  by default and version-dependent; TPM through `tpm2-pytss` (Linux),
  the OS API elsewhere, in a `[tpm]` extra.
- Hub: `[hub]` extra; `[postgres]` for `psycopg`; the MCP SDK from
  `[server]` provides streamable HTTP for the remote variant.
- The core stays dependency-free. Linux, macOS and Windows stay
  first-class on the client side; the hub targets Linux (systemd) first.

## 11. Homelab deployment (proposal)

- **Hub machine**: a **small dedicated VM** (or an always-on host that
  runs no agents), preferably with a TPM (virtual on odin). Not sindri:
  the hub holds the certificate signing key, which must not sit next to
  autonomous agents; not mimir: frequent reboots and audit agents.
- `iab_hub` Ansible role in `infra`: package pinned to a tag, confined
  systemd unit, CA and key in the vault, backup to asgard, SNI routing on
  lemesre.org.
- Managed machines: paired by the role; `IAB_HUB` in the clients'
  environment; the `poste_llm` role only adds `IAB_HUB` to its
  `poste_opencode_mcp` key.
- Migration: `iab hub import <local database>` takes over the agents,
  queues, results and journal of an existing database.

## 12. Proposed breakdown (phase 9)

- **9a** — access and dialect layers, `LocalBackend` + SQLite, no
  behavior change; core tests unchanged and green.
- **9b** — hub: `iab hub init/serve`, mTLS, public server certificate,
  `RemoteBackend`, long-poll, TLS-inspection detection; `smoke.py`
  extended to a hub on loopback.
- **9c** — pairing: `iab join`, `iab approve`, codes and check words,
  internal invitation for Ansible, renewal, `iab machines`,
  `iab revoke`, notifications.
- **9d** — authorization: instance ↔ machine binding, orchestrator
  roles, `accepts_from`, limits.
- **9e** — TPM keys and attestation, per-machine policy.
- **9f** — PostgreSQL dialect, `LISTEN/NOTIFY`, tests on two engines.
- **9g** — `host` in `launch`, per-machine supervisor.
- **9h** (optional) — MCP streamable HTTP for sandboxed harnesses.

## 13. Decisions expected from the owner, with their impact

**Decided on 2026-10-17**: single hub (1); `cryptography` (3); remote
MCP variant deferred (4); **one single database** (5) — the "one
database per trust domain" advice is not retained, `accepts_from`
remains the main defence between agents; pairing from the Internet
closed at first (6); TPM mandatory for admin machines (7); Headscale
approved, carried by the HomeLab repository (8). Still open: the hub
machine (2), to settle on the HomeLab side with the `iab_hub` role.

1. **Single hub or high availability?** Single: if its machine goes
   down, cross-machine coordination stops (explicit error), standalone
   agents carry on; a few minutes of downtime on reboot, restore from
   the nightly backup at worst. High availability: three always-on
   machines (Raft quorum) or PostgreSQL replication with failover
   (Patroni), heavy to run. **Advice: single**, agents are not a
   critical service.
2. **Hub machine?** sindri: nothing to create, but the signing key sits
   next to autonomous agents. mimir: PostgreSQL already there, but
   reboots and audit agents. Dedicated VM: clean isolation, one more VM
   described in Ansible. **Advice: dedicated VM.**
3. **`cryptography` or `openssl`?** Only affects the pairing client (an
   extra), not the core. `cryptography`: ready packages for every
   platform, testable calls. `openssl`: absent from Windows by default,
   version-dependent options. **Advice: `cryptography`.**
4. **Remote MCP variant (9h)?** Without it, `iab` is installed on every
   agent machine, including inside the opencode service, which must then
   be able to read the machine certificate. With it, no installation,
   but a per-session token in the opencode configuration (comparable
   exposure) and a workspace carried by the token for lack of `cwd`.
   **Advice: later**, every machine is managed by Ansible.
5. **One database per project or a single one?** Single: everyone can
   talk, but an injection in one project reaches the agents of the
   others. Per project: isolation, but cross-project exchanges, which
   are genuinely useful (HomeLab ⇄ Conciliateur for inferd), are lost.
   **Advice: one database per trust domain** on the same hub
   ("personal": HomeLab, infra, AI tooling; one domain per external
   context: justice, associations), with `accepts_from` bounding sends
   within a domain.
6. **Pairing from the Internet?** Closed: the only unauthenticated
   public endpoint stays shut, an already paired machine still works
   everywhere. Open: pair while travelling, at the price of a public
   page to watch. **Advice: closed at first.**
7. **Where is a hardware key required?** **Advice: TPM mandatory for
   admin machines** (those that approve), file key tolerated elsewhere.
8. **Headscale for the homelab?** Decision for the HomeLab repository
   (§ 8). **Advice: yes**, before or together with the multi-machine bus.
