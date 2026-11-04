# Common contract of the bus implementations

🇬🇧 English · 🇫🇷 [Français](CONTRAT.fr.md)

The bus may have several implementations — Python today, a native Rust
client planned (see [the study](docs/AMELIORATIONS-2026-11-04-client-rust.md))
— writing **the same database** at the same time. This document states
what they must agree on: byte for byte for what is stored, in meaning
for what is returned. The Python implementation is the reference; when
in doubt its behaviour prevails, and the gap is fixed here.

**Change rule.** Any change to a point of this contract changes every
implementation in the same pull request, and passes the conformance
suite (§ 11) on every pair of implementations.

## 1. Choosing the database

- `IAB_DB` names the database; failing that `ORCHESTRATOR_DB` (legacy
  name, still read).
- A value starting with `postgresql://` or `postgres://` is a
  PostgreSQL URL in libpq format; any other value is a SQLite file path.
- Without a variable: `bus.db` of the data directory if it exists, then
  the legacy `~/.local/share/multi-agent-orchestrator/orchestrator.db` if
  it exists, otherwise one database per project,
  `<data>/projects/<project key>.db` (§ 7).
- Data directory: `%LOCALAPPDATA%\inter-agent-bus` on Windows,
  `~/Library/Application Support/inter-agent-bus` on macOS,
  `$XDG_DATA_HOME/inter-agent-bus` (default `~/.local/share`) elsewhere.
- A URL is never displayed whole: only scheme, role, host, port and
  database show (no password, no parameters).

## 2. Schema

- The tables are those of `SCHEMA` in `servers/shared_memory/dialect.py`,
  then the additive migrations of `_migrate` in `store.py`, applied in
  that order at every opening: a column is added only when missing,
  never dropped nor renamed.
- SQLite: ids `INTEGER PRIMARY KEY AUTOINCREMENT`.
- PostgreSQL: ids `BIGSERIAL PRIMARY KEY`; every `TEXT` column carries
  `COLLATE "C"`; foreign keys are `DEFERRABLE INITIALLY DEFERRED`; tables
  live in the current schema (`search_path`). Schema creation and
  migrations run under the writer lock (§ 3).
- A JSON field stored in a text column (`presence.capabilities`) is read
  by parsing, never compared as text: serialization spacing may differ
  between implementations.

## 3. Write transactions

- SQLite: `BEGIN IMMEDIATE`, `WAL` journal mode, 10,000 ms
  `busy_timeout`.
- PostgreSQL: `BEGIN`, then **as first statement**
  `SELECT pg_advisory_xact_lock(6906210)` (`0x696162`, "iab"). A single
  lock, hence a single acquisition order.
- Normal exit: `COMMIT`. Error: `ROLLBACK`, and the original error
  propagates.
- Nothing outside the database while a transaction is open: no network
  call, no file, no process, no wait.
- PostgreSQL: when the transaction changed at least one row,
  `NOTIFY iab_bus` right before `COMMIT`; otherwise no notification.

## 4. Blocking waits

- SQLite: polling at the requested interval.
- PostgreSQL: `LISTEN iab_bus`, then wait for a notification or the end
  of the interval, whichever comes first. A connection that ran `LISTEN`
  is never returned to a connection pool.

## 5. Dates

- ISO 8601 text in UTC, in the format of Python's
  `datetime.isoformat()`: `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`.
- **When the microseconds are zero they are omitted**:
  `YYYY-MM-DDTHH:MM:SS+00:00`. An implementation writes exactly this
  format and reads both forms.
- Deadlines (`lease_deadline`, wake locks) are compared as strings:
  another format (`Z` instead of `+00:00`, milliseconds, local time
  zone) breaks the comparisons.

## 6. Task lifecycle

- States: `queued` → `claimed` → settled (result published); exits
  `dead` (lease expired `max_attempts` times, default 3) and
  `cancelled`.
- Claim: the highest-priority `queued` task, then the oldest
  (`ORDER BY priority DESC, created_at ASC`); expired leases are
  requeued first. `attempts` grows at each claim; the default lease is
  900 s.
- Fenced settling: publishing carries the attempt number received at
  claim time; a stale number is refused.
- Every transition is journaled in `events` under the same event name
  (`push`, `claim`, `expire`, `dead`, `publish`, `extend`, `cancel`,
  `requeue`, …).
- At-least-once delivery: a task may run twice.

## 7. Identity and project key

- Agent name: `harness@workspace`, each component matching
  `[a-z0-9][a-z0-9._-]{0,39}`, lower case.
- Harness: `IAB_AGENT_NAME` (or `ORCHESTRATOR_AGENT_NAME`). When it
  already holds `@`, it is the full name.
- Workspace, in order: `IAB_WORKSPACE`; an alias from `workspaces.json`
  (`IAB_WORKSPACES`, else the data directory's file; longest prefix,
  never reaching past a git repository boundary); the name of the first
  ancestor directory holding a `.git` (file or directory); the name of
  the launch directory.
- Component folding: lower case, every run of characters outside
  `[a-z0-9._-]` replaced by `-`, leading punctuation dropped, trailing
  `-` dropped, 40 characters at most, `root` when nothing remains. An
  accented character therefore becomes `-` (`Dépôt` → `d-p-t`).
- Project key: real path of the directory (links resolved), lower-cased
  on Windows and macOS, then
  `<directory name, outside [A-Za-z0-9_-] → '-', 40 characters>-<first
  12 hex characters of the SHA-256 of the path in UTF-8>`.
- **On Windows the real path has no `\\?\` prefix**: that is the form
  `os.path.realpath` returns. An implementation whose resolution adds
  that prefix strips it before computing.

## 8. Command line

- Same subcommands, options and defaults as
  `servers/shared_memory/cli.py` (`iab --help`).
- Answers:
  - one `OK: …` line for a successful action, exit code 0;
  - one `ERROR: …` line for a refusal or an error, non-zero exit code;
  - `NO_TASK` when a claim finds nothing, exit code 0;
  - otherwise a JSON document, which the caller **parses** (spacing and
    the `--json` option, which indents it, may vary).
- A payload, result or message absent from the command line, or given
  as `-`, is read from standard input.

## 9. Standard streams and data fidelity

- Standard input and outputs in **UTF-8 on every platform**, whatever
  the console code page.
- **No line-ending translation**, neither on input nor on output: a
  payload holding `\r\n` or `\r` is stored, returned and passed on as
  is.
- A payload is never truncated; its size is limited by the database
  only.

## 10. Launched processes (`worker`, `wake`)

- The payload or prompt goes through the process's **standard input**
  (or a temporary file for `{prompt_file}`), never through its arguments
  nor through a shell.
- Exchanges in bytes: the payload is encoded in UTF-8, the output
  decoded from UTF-8 (an invalid byte becomes U+FFFD), with no
  line-ending translation. `iab worker` publishes standard output as is.
- Empty output from a command that succeeded is a failure published as
  `ERROR: …`.

## 11. Conformance suite

`conformance/run.py` drives the implementations through their command
line, without importing `store.py`, and plays each scenario on every
pair of implementations sharing one database:

```bash
python3 conformance/run.py                                   # Python only
python3 conformance/run.py --impl python='["python3","servers/shared_memory/cli.py"]' \
                           --impl rust='["/path/to/iab"]'
IAB_TEST_POSTGRES=<URL of an EMPTY test database> python3 conformance/run.py
```

Scenarios: full lifecycle, refusals, priorities, fenced settling after
expiry, dead letters and requeue, concurrent claims (exactly one wins),
notifications and broadcasts, a blocking wait woken by a write of the
other implementation, fidelity of a 200 kB payload (non-ASCII, `\r\n`,
`\r`), `iab worker`, identity and project key in an accented folder.

The `scripts/pg_tls.py` bench starts a PostgreSQL server that accepts
the bus role only over TLS with a client certificate, and hands out its
URL (`sslmode=verify-full`) only after checking that clear text, a
foreign authority and a wrong host name are refused. Continuous
integration (`.github/workflows/ci.yml`) plays every suite on Linux and
Windows, Python 3.10 and 3.14, on SQLite and on that bench.
