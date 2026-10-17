# Multi-machine bus

🇬🇧 English · 🇫🇷 [Français](MULTI-MACHINE.fr.md)

By default the bus lives in a local SQLite database: every agent of a
machine shares it, nothing runs in the background. To make agents of
**several machines** cooperate, a **hub** (`iab hub`) exposes that
database over the network; machines pair with it once, then their
clients (MCP server, CLI, `iab worker`, `iab supervise`) go through it by
themselves as soon as `IAB_HUB` is set. Without `IAB_HUB`, nothing
changes.

Design and decisions: [`AMELIORATIONS-2026-10-17-multi-machine.md`](AMELIORATIONS-2026-10-17-multi-machine.md).

## Principle

```
 desk   ─┐
 mimir  ─┼── HTTPS + mutual TLS ──▶ iab hub ──▶ store.py ──▶ bus database
 sindri ─┘   (port 7443)
```

- **One machine = one certificate**, issued by the hub's private CA at
  pairing. Its key is created on the machine and never leaves it.
- **The client trusts the bus CA only** to reach the hub, never the
  system store: a TLS-inspecting proxy (Zscaler, Netskope…) fails the
  handshake and nothing is sent.
- **Immediate revocation**: the hub re-checks the machine at every
  request.
- **Unreachable hub = error** (`ERROR: hub unreachable…`), never a
  fallback on a local database.
- **Two ports**: 7443 for the bus (mutual TLS), 7444 for pairing (open
  to private networks only by default).

## Install

```bash
pip install 'inter-agent-bus[hub]'      # adds cryptography
```

## Create the hub (once)

On the hub machine (preferably a small dedicated VM, without agents):

```bash
iab hub init --name iab.example.org --name 192.168.20.30
iab hub serve
```

`init` creates the bus CA, the certificates, the database, and pairs the
local machine as the **first admin machine**. The first `--name` is the
address clients will use (`IAB_HUB=https://iab.example.org:7443`). The
hub renews its bus certificate by itself.

Example systemd unit:

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

## Pair a machine

On the new machine:

```
$ iab join iab.example.org
Hub: iab.example.org
To pair this machine, on an admin machine run:
    iab approve
and type the code:   WDJB-MJHT   (valid 10 min, waiting…)
Check words: lampe-orage-violon-tulipe
```

On an admin machine:

```
$ iab approve
Code: wdjb mjht
Request from 'mimir' — Linux aarch64 — 192.168.20.21 — key: file
Check words: lampe-orage-violon-tulipe
Are the four words identical to those shown on mimir? Pair it as 'mimir' (member) [y/N] y
Machine 'mimir' paired (certificate until 2026-11-16).
```

The new machine finishes by itself. The **four words** are computed from
the machine's key, the hub's CA and a nonce from each side, in a
"commit, then reveal" scheme (that of Bluetooth numeric comparison): the
machine commits to its request before it knows the hub's nonce, and
reveals it only afterwards. Nobody can therefore choose a key that makes
both sides' words agree: a party in the middle of the first contact has
one chance in four billion, and a single try per pairing. Comparing them
is what protects the pairing when the pairing port has no public
certificate (the client says so). Approval is interactive on purpose,
and nothing the other side sends is displayed before control characters
are stripped.

**Machines managed by Ansible**: a single-use invitation, bound to the
name, valid 15 minutes, replaces the person:

```bash
iab invite mimir | ssh mimir 'iab join iab.example.org --name mimir --token -'
```

The token goes through stdin (or `IAB_INVITE_TOKEN`), never as an
argument; it does not travel on the network either: the machine proves
it holds it with an authentication code bound to its own request.

## Use

Set `IAB_HUB` in the clients' environment, for instance in the MCP
declaration:

```json
{"type": "stdio", "command": "iab-server",
 "env": {"IAB_AGENT_NAME": "claude", "IAB_HUB": "https://iab.example.org:7443"}}
```

`iab ping` checks the pairing; `whoami` shows `"db_source": "hub"`. Each
session's workspace is still derived from its local directory.

## Administer

| Command | Effect |
|---|---|
| `iab machines` | paired machines, role, key kind, last seen |
| `iab revoke <machine>` | refused from its next request (the last admin cannot be) |
| `iab deny <code>` | refuses a pending request |
| `iab bind <instance> <machine>` | binds an instance to a machine (it moved, or so that it counts in the policy) |
| `iab renew [--force]` | renews the certificate (automatic with less than 10 days left) |

Every pairing and revocation is journaled and announced on the channel
(topic `machines`, author `bus`). A machine offline for more than 30
days must pair again.

## Authorization

Every request comes from an authenticated machine, which lets the hub
enforce three rules:

1. **Binding**: a **new** `harness@workspace` instance belongs to the
   machine that creates it; no other machine may then claim its queue,
   publish, beat its heart, read its mailbox or take its wake lock. An
   already existing instance nobody owns (imported database, earlier
   registration) is refused until an admin binds it:
   `iab bind <instance> <machine>`, which also moves it. A refused
   operation leaves no binding behind. `iab machines` lists each
   machine's instances.
2. **Orchestrator operations**: cancel, requeue, rename, forced publish,
   broadcast to a pattern (`*`, `harness@*`…). Reserved to the
   principals of `policy.orchestrators` — the admin machines by default.
3. **`accepts_from`**: who may push work or send a message (a
   notification, every recipient of a broadcast) to whom — the main
   defence against a prompt injection travelling from one machine's
   agents to another's.

The policy is set in `hub.json`, never in code:

```json
"policy": {
  "orchestrators": ["role:admin", "claude@homelab"],
  "accepts_from": {"kimi@*": ["claude@*", "machine:yggdrasil"]}
},
"rate": {"per_second": 50, "burst": 200}
```

Principals of a request: `machine:<name>`, `role:<role>` (proven by the
certificate), and the instance the client declares or acts as, **only
when an admin bound it** to that same machine (`iab bind`): a binding
made on first use proves precedence, not trust. Prefer `machine:<name>`
in the policy. Reads (state, results, journal, presence) stay open
to every paired machine. The journal names the machine of every event
(`iab log`).

## Security: what is guaranteed, what is not

- Without a valid, active machine certificate, the hub does not answer;
  every refused handshake is logged with its source address (a hook for
  fail2ban or CrowdSec).
- Payloads never appear in the hub's log; an oversized request is
  refused, never truncated.
- Pairing: 8-letter unambiguous codes, 10 minutes, single use; requests
  limited per address and in number; private networks only by default
  (`pair_allow` in `hub.json`).
- **Admin role** (approve, invite, revoke): reserved to a machine whose
  key lives in a TPM chip (owner's policy). Until the TPM proof ships,
  only the machine of `iab hub init` is admin.
- **Not supported**: networks that decrypt TLS. The client detects it
  and refuses; only an exception from the IT department fixes it.
- Writing to the bus remains adjacent to code execution at the agents:
  pair trusted machines only.
