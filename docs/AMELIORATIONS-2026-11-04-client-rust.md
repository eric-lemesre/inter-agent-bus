# Pending improvement — native Rust client (Windows first)

🇬🇧 English · 🇫🇷 [Français](AMELIORATIONS-2026-11-04-client-rust.fr.md)

Noted on 2026-11-04. Requests from the owner:

1. a **Windows executable** of the bus, written in Rust, for a managed
   corporate workstation;
2. a bus shared through **a remote PostgreSQL database over TLS** (a
   company-internal server): two machines communicate by pointing
   `IAB_DB` at the same database, without a hub;
3. later, **`.deb` and `.rpm`** packages;
4. **two maintained implementations**: Python stays, Rust is added.

Study only: nothing is implemented. Pending decisions are in § 11.

## 1. Findings

- The Python core (`store.py`, `dialect.py`) already supports
  PostgreSQL: `IAB_DB=postgresql://…` selects the engine and the URL goes
  to libpq unchanged, so `sslmode=verify-full`, `sslrootcert`, `sslcert`,
  `sslkey` work as is. A bus shared between two machines is therefore
  possible **today, in Python**.
- What Python requires on the target workstation: an interpreter, a
  virtual environment (64 MB), access to PyPI or an internal package
  repository, and `psycopg[binary]`. On a managed workstation each of
  these may be refused or delayed.
- Performance: not decisive. `iab signals` takes 70 ms (50 ms of it
  interpreter start-up), an MCP server uses 14 to 26 MB. Rust's gain is
  **distribution** (one file, no dependency), not speed.
- The bus has never run on Windows: no continuous integration, no test
  on that platform, whatever the implementation.

## 2. Scope of the Rust client

**In scope** (about 3,500 lines of Python to carry over):

- the core: queues, leases, settling fenced by the attempt number,
  journal, presence, channel, notifications, broadcasts, wake locks —
  the equivalent of `store.py`;
- both engines: SQLite (local file) and PostgreSQL (TLS) — the
  equivalent of `dialect.py`;
- the `iab` command line, same subcommands and same outputs;
- the stdio MCP server, same tools and parameters as `server.py`: an
  MCP client switches implementation by changing only the executable
  path;
- `iab worker`, `iab wake`, `iab supervise` (process launching).

**Out of scope** (Python only): the hub (`hub.py`), pairing, the PKI,
TPM attestation, `iab review`, `iab import`. A bus shared through
PostgreSQL needs none of them. The Rust client will not read `IAB_HUB`
at first: with that variable set it refuses to start rather than
silently ignore the hub.

## 3. Two implementations: the common contract

Two implementations writing the same database must agree byte for byte
on what is stored, and in meaning on what is returned. The contract
becomes a document of the repository (`CONTRAT.md` / `CONTRAT.fr.md`),
and any change to one of the points below changes **both
implementations in the same pull request**.

| Contract point | Current value (Python) | Pitfall for Rust |
|---|---|---|
| Schema and additive migrations | `SCHEMA` and `_migrate` in `dialect.py`/`store.py` | apply the same migrations, in the same order, idempotently |
| Dates | ISO 8601 text, `datetime.isoformat()` in UTC | Python **omits the microseconds when they are 0** (`…:59+00:00` vs `…:59.828491+00:00`); lease comparisons are string comparisons: reproduce that exact format and read both forms |
| PostgreSQL collation | `TEXT COLLATE "C"` | required by the text date comparisons |
| Writer lock | SQLite: `BEGIN IMMEDIATE`; PostgreSQL: `pg_advisory_xact_lock(0x696162)` as first statement | same key, same place; no wait outside the database while the transaction is open |
| Wake-up | `NOTIFY iab_bus` at COMMIT when a row changed; `LISTEN iab_bus` on a connection never returned to the pool | same channel, same "only when modified" rule |
| Identity | `harness@workspace`, workspace derived from the enclosing git repository, `workspaces.json` aliases, `IAB_WORKSPACE` | same slug algorithm; on Windows `std::fs::canonicalize` returns a `\\?\C:\…` path Python never produces: use `dunce::canonicalize`, otherwise the project key (SHA-256 of the path) differs and the two clients open two different databases |
| Project key | `realpath`, lower-cased on Windows and macOS, `slug-<12 hex>` | same, tested on accented paths and links |
| Tool results | JSON, `ERROR:` prefix for refusals | Python serializes with `", "` and `": "`, serde_json without spaces: compare **parsed JSON**, never text; same key order (ordered structs or `preserve_order`) |
| Lifecycle | queued → claimed → done; dead, cancelled; `attempts`, `max_attempts`, settling refused to a stale attempt | identical behaviour, cross-tested (§ 7) |

## 4. Proposed architecture

A Cargo workspace in the same repository (`rust/`), so both
implementations share conformance tests, contract and version.

```
rust/
  Cargo.toml            workspace, shared lints
  crates/
    iab-core/           state logic (equivalent of store.py), no MCP, no network
    iab-db/             Dialect trait + SQLite (rusqlite) + PostgreSQL
    iab-cli/            `iab` executable (clap)
    iab-mcp/            `iab-server` executable (rmcp, stdio)
```

Choices and reasons:

- **Synchronous core**, like `store.py`: each operation opens a short
  transaction and waits on nothing outside the database. The MCP server
  (asynchronous, tokio) calls the core through `spawn_blocking`. No
  logic in the wrappers, same rule as in Python.
- **`Dialect` trait** mirroring the `dialect.py` interface exactly:
  `connect`, `create_schema`, `write` (lock taken as first statement,
  COMMIT on normal exit, ROLLBACK on error), `pause`, `columns`,
  `insert_id`. The core's SQL stays portable with `?`; the PostgreSQL
  dialect translates it to `$1…$n` (same translation as `_translate`,
  same tests on literals containing `?` and `%`).
- **SQLite**: `rusqlite` with the `bundled` feature (SQLite compiled
  into the executable), WAL, 10 s `busy_timeout`, `BEGIN IMMEDIATE`. The
  file format is shared: a Python client and a Rust client use the same
  local database.
- **PostgreSQL**: `postgres` (synchronous wrapper of `tokio-postgres`
  0.7.18) with TLS through **rustls** and `rustls-platform-verifier`. On
  Windows, verification goes through the system certificate store: the
  company's internal authority is trusted without dropping a file. All
  Rust, hence an executable without DLLs.
- **TLS parameters of the URL**: `tokio-postgres` only knows
  `sslmode=disable|prefer|require` and `sslnegotiation=postgres|direct`
  (direct: PostgreSQL ≥ 17, ALPN `postgresql`). It knows neither
  `verify-ca`, `verify-full`, nor `sslrootcert`, `sslcert`, `sslkey`.
  `iab-db` therefore reads those parameters itself, strips them from the
  URL and builds the rustls connector accordingly:
  - `verify-full` → `require` + chain **and** host name verification;
  - `verify-ca` → `require` + chain verification only;
  - `sslrootcert` → roots taken from that file instead of the system
    store;
  - `sslcert`/`sslkey` → client certificate;
  - `prefer`, libpq's default, is **refused** for a remote database: it
    accepts a clear-text connection when the server offers no TLS.

  libpq's default locations are honoured (`%APPDATA%\postgresql\root.crt`,
  `postgresql.crt`, `postgresql.key`; `~/.postgresql/` elsewhere), as is
  `PGPASSFILE` / `pgpass.conf`: one configuration serves both
  implementations.
- **MCP**: `rmcp` 3.8, the official Rust SDK, stdio transport, tools
  declared with `#[tool]`. The 23 tools of `server.py` with the same
  names, parameters and defaults.
- **Command line**: `clap`, same subcommands as `cli.py` within the
  scope of § 2. A payload is never passed through argv to a launched
  process (`worker`, `wake`): stdin or a temporary file, as in Python.

## 5. Code rules adopted

Checked against the `rust-best-practices` guide and the two PostgreSQL
guides:

- errors: `thiserror` in `iab-core` and `iab-db` (one enum per crate),
  `anyhow` only in the two executables; no `unwrap`/`expect` outside
  tests; a business refusal stays a returned `ERROR: …` value, never a
  panic;
- workspace lints: `cargo clippy --all-targets --all-features --locked
  -- -D warnings`, a justified `#[expect(…)]` rather than `#[allow(…)]`,
  `missing_docs` on the library crates;
- static dispatch (generics) for the dialect inside the core;
  `dyn Dialect` only where the URL selects the engine;
- bound parameters everywhere, never a value interpolated into SQL. Only
  the table and column names of migrations are interpolated, and they
  come from code constants;
- short write transactions: the lock is the first statement, no network,
  file or process call while it is open, a single lock hence a single
  order; a connection that ran `LISTEN` is never returned to the pool;
- the schema stays Python's, including where PostgreSQL would offer
  better (`TIMESTAMPTZ` instead of text dates, `IDENTITY` columns
  instead of `BIGSERIAL`, a composite index `notifications(target, seq)`
  for `poll`). These improvements remain possible later, **in both
  implementations at once**, through an additive migration;
- tests named after behaviour (`claim_should_refuse_stale_attempt`), one
  behaviour per test.

## 6. Distribution

- **Windows**: target `x86_64-pc-windows-msvc`, built on a GitHub
  Actions Windows runner (native, simpler than cross-compiling), C
  runtime linked statically (`+crt-static`). Result: `iab.exe` and
  `iab-server.exe`, no DLLs. One `.zip` per version, with SHA-256 sums.
- **Signing**: a managed workstation may block an unsigned `.exe`
  (AppLocker, antivirus). Either a code-signing certificate
  (Authenticode) or an exception rule set by the company's IT. To be
  decided (§ 11).
- **Linux**: target `x86_64-unknown-linux-musl`, a static executable
  that also runs on an old distribution (RHEL 8, glibc 2.28).
  `cargo-deb` and `cargo-generate-rpm` for packages, later.
- **Version**: one number for both implementations; a `vX.Y.Z` tag
  publishes the Python package and the Rust executables.

## 7. Tests and conformance

- Unit tests in each crate.
- **A shared conformance suite**, written in Python (it drives
  processes, it does not import `store.py`): each scenario runs with the
  Python/Python, Rust/Rust, **Python/Rust and Rust/Python combinations on
  the same database**. Examples:
  - Python pushes a task, Rust claims and publishes it, Python reads the
    result;
  - concurrent claims mixed across implementations: exactly one wins;
  - a lease expired on the Rust side, taken over on the Python side: the
    stale settle is refused;
  - a blocking wait on the Rust side woken by a Python write
    (PostgreSQL: `LISTEN`/`NOTIFY`);
  - identical identity and project key for the same folder, accented
    paths included;
  - `store_test.py` stays the behavioural reference: its scenarios are
    carried over one by one into the conformance suite.
- **Continuous integration**: Linux and Windows matrix, Python 3.10 and
  3.14, SQLite and PostgreSQL over TLS with `verify-full` and a client
  certificate (test authority generated during the run).

## 8. Steps

| Step | Content | Usable at the end |
|---|---|---|
| R0 | Contract (`CONTRAT.md`), conformance suite in Python, Linux and Windows CI on the Python implementation | proof that Python works on Windows and on PostgreSQL over TLS; useful even without Rust |
| R1 | `iab-core` + SQLite + `iab` (register, push, claim, publish, poll, notify, signals, state) | local bus on Windows in Rust |
| R2 | PostgreSQL over TLS (rustls, libpq parameters, `pgpass`), waits through `LISTEN`/`NOTIFY` | bus shared between two machines |
| R3 | `iab-server`: the 23 MCP tools | MCP agents on Windows on the Rust executable |
| R4 | `worker`, `wake`, `supervise` on Windows | launching asleep agents |
| R5 | Signing, published archives; `.deb`/`.rpm` when needed | distribution |

Each step passes the full conformance suite on both implementations
before merging.

## 9. Roadmap changes

- Principle 4 ("tooling is pure Python") becomes: "Two implementations,
  Python (reference, complete) and Rust (native client); their agreement
  is guaranteed by the contract and the conformance suite."
- Principle 2 ("all state logic lands in `store.py`") now applies to
  `store.py` **and** `iab-core`, in the same pull request.
- New "native client" phase after phase 9.

## 10. Risks

| Risk | Effect | Mitigation |
|---|---|---|
| Kerberos/SSPI authentication imposed by the company | Rust drivers (`tokio-postgres`, `sqlx`) do not support it | ask for the chosen method first; otherwise a libpq-linked build (`libpq.dll` shipped alongside) or Python on that workstation |
| Transaction-mode pooler (PgBouncer) | no more `LISTEN`/`NOTIFY`: waits only wake at the next poll | direct connection or session-mode pooler |
| Unsigned `.exe` blocked | the executable does not start | signing certificate or IT exception |
| The two implementations diverge | corruption or inconsistency of the shared bus | contract, mandatory cross conformance suite, both changed in the same merge |
| Double maintenance cost | every core change is written twice | Rust scope limited to the client; the hub stays in Python |
| Company data in the database | tasks and results stored in clear | the company validates this use of the database; nothing secret in payloads (rule already in force) |
| `tokio-postgres` behaviour on an unknown URL parameter | to be checked: probably refused at parse time | `iab-db` strips libpq's TLS parameters itself before passing the URL on |

## 11. Decisions expected from the owner

1. **Authentication method** imposed by the company's database
   administrators (SCRAM password, client certificate, Kerberos/SSPI).
   It decides the driver (§ 4, § 10).
2. **Signing** of the executable: code certificate (which one, paid by
   whom) or IT exception.
3. **PostgreSQL server version and settings**: version, pooler,
   dedicated schema, tables created by the bus itself or by the
   administrators (SQL script provided).
4. **MCP client used on the Windows workstation** (it sets the
   configuration to document).
5. **Order**: start with R0, which also serves the Python implementation.
