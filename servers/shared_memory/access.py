"""Access layer of the inter-agent bus — which backend serves the bus
operations.

The wrappers (server.py, cli.py and the drivers) call the operations
through `bus`, never through store.py directly, so that the same code
runs against two backends:

  - LocalBackend: store.py itself, over the local database — the
    default, unchanged behaviour (stdio, no daemon);
  - RemoteBackend: a client of the hub (`iab hub serve`) — selected by
    IAB_HUB.

The backend is resolved at every call from the environment: a process
never caches a choice its environment no longer makes. Pure helpers that
need no database (identity resolution, name splitting) stay in store.py
and are called there directly.
"""
from __future__ import annotations

try:  # installed package: `iab` maps to this directory (pyproject.toml)
    from . import store
except ImportError:  # bare script usage
    import store

# The store functions a client may call through a backend — the contract
# a hub exposes, one route per operation. Anything else is local.
OPERATIONS = frozenset({
    "register_agent", "rename_agent",
    "push_task", "wait_for_task", "claim_task", "publish_result",
    "cancel_task", "requeue_task", "extend_lease", "read_result",
    "heartbeat", "touch_presence", "touch_identity", "list_presence",
    "announce", "read_channel", "notify", "broadcast", "replies", "poll",
    "pending_signals", "wait_signal",
    "acquire_wake_lock", "renew_wake_lock", "release_wake_lock",
    "record_wake", "wake_count",
    "get_system_state", "get_events", "db_info",
})


def backend():
    """The backend serving this call: store.py (LocalBackend)."""
    return store


class _Bus:
    """`bus.<operation>(…)` forwards to the backend of the moment; an
    unknown name is refused (a local helper reached through `bus` would
    silently bypass a hub)."""

    def __getattr__(self, name: str):
        if name not in OPERATIONS:
            raise AttributeError(f"'{name}' is not a bus operation")
        return getattr(backend(), name)


bus = _Bus()
