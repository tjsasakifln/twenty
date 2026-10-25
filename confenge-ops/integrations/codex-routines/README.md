# Codex scheduled routines → Twenty

This integration records the configured Codex schedules in Twenty without
creating another scheduler. Desktop heartbeats and cron tasks remain owned by
Codex; they still depend on the computer and Codex app being available.

Each occurrence has two durable, idempotent events: `START` and `OUTCOME`.
The local Windows client writes SQLite before SSH delivery. The host writes a
second SQLite outbox before using the Twenty API. A run is delivered only after
Twenty read/write reconciliation returns the matching payload hash. Queueing is
never reported as successful execution.

The bounded useful-data contract accepts aggregate numeric metrics, safe
artifact/domain references, and operational Task items created by the routine.
It never copies raw institutional documents, message bodies, credentials,
personal email addresses, or unrestricted record payloads. The Extra tender
owner may explicitly request the existing `extra-sync.sh`; no duplicate Extra
importer is implemented here.

The real catalog is private configuration and must never be committed. Copy
`catalog.example.json` outside the repository, replace it with the private
inventory, and pass that path to both installers. The installed file is named
`catalog.json` inside a restricted directory.

Before each submission, `submit.ps1` rebuilds that private catalog from the
current `~/.codex/automations/*/automation.toml` metadata. It keeps only the
automation ID, display name, kind, schedule, scheduler status, model settings,
and the existing destination policy; prompt text is never stored or sent.
New automations default to the safe `task` destination with Extra sync disabled.
Missing automations are marked `NOT_FOUND` in Twenty on the next confirmed
delivery instead of remaining apparently active.

`cli.py schema` and `cli.py catalog-sync` are dry-run by default. `--apply` is
required for metadata or record writes. Durable installation paths are:

- Windows client: `%USERPROFILE%\.codex\integrations\codex-routines`
- Windows outbox: `%USERPROFILE%\.codex\codex-routines\outbox.sqlite3`
- host client: `/opt/confenge-gestao/integrations/codex-routines`
- host outbox: `/var/lib/confenge-gestao/codex-routines/outbox.sqlite3`

The SSH transport enforces batch mode, connection timeout, and keepalive. Its
host alias is private installation data: set `CODEX_ROUTINES_SSH_HOST`, pass
`-SshHost`, or have the installer create its ignored `transport.json`. Missing
host configuration fails closed before delivery. It
uses only the host-side `/etc/confenge-gestao/admin.json`; credentials are never
copied to Windows or stored in an event.

## Minimal durable event example

Use `contract.build_event`; never hand-write `runKey`, `eventKey`, or
`payloadHash`. START and OUTCOME must reuse the exact same automation ID,
scheduled occurrence, trigger, and optional native run ID. The example is
sanitized: provide values from the current private catalog and occurrence via
environment variables.

After building an event, preserve that exact private JSON until it is `SENT`.
Retries must resend the same bytes/fields; do not recompute `observedAt`,
summary, metrics, or references for the same event key. A different payload
with the same key is intentionally rejected. Never generate a new run key to
escape a delivery failure. Use `CORRECTION` only for a real correction to an
already terminal outcome, with an explicit `correctionReason`.

```python
from pathlib import Path
import json, os, sys

root = Path.home() / ".codex" / "integrations" / "codex-routines"
sys.path.insert(0, str(root))
from contract import build_event, load_catalog

catalog = load_catalog(root / "catalog.json")
identity = {
    "automation_id": os.environ["CODEX_AUTOMATION_ID"],
    "scheduled_for": os.environ["CODEX_SCHEDULED_FOR"],
    "trigger": os.environ.get("CODEX_TRIGGER", "SCHEDULED"),
    "catalog": catalog,
}
started = os.environ["CODEX_STARTED_AT"]
finished = os.environ["CODEX_FINISHED_AT"]
start = build_event(
    **identity, event_type="START", observed_at=started,
    status="RUNNING", startedAt=started,
)
outcome = build_event(
    **identity, event_type="OUTCOME", observed_at=finished,
    status="NO_MATERIAL_CHANGE", coverageStatus="COMPLETE",
    startedAt=started, finishedAt=finished, materialChange=False,
    summary="Cobertura concluída sem mudança material.",
    aggregateMetrics={"sourcesRead": 1, "itemsChanged": 0},
)
assert start["runKey"] == outcome["runKey"]
Path("START.private.json").write_text(json.dumps(start), encoding="utf-8")
Path("OUTCOME.private.json").write_text(json.dumps(outcome), encoding="utf-8")
```

Submit each file through the permanent client:

```powershell
& "$env:USERPROFILE\.codex\integrations\codex-routines\submit.ps1" -InputPath .\START.private.json
& "$env:USERPROFILE\.codex\integrations\codex-routines\submit.ps1" -InputPath .\OUTCOME.private.json
```

Queueing is not confirmation. Query the exact event key in the durable outbox;
only `SENT` means the corresponding event reached Twenty:

```powershell
$python = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
& $python -c "import sys; from pathlib import Path; sys.path.insert(0,str(Path.home()/'.codex'/'integrations'/'codex-routines')); from outbox import Outbox; b=Outbox(Path.home()/'.codex'/'codex-routines'/'outbox.sqlite3'); print(b.event_status(r'<EVENT_KEY>')); b.close()"
```

The encrypted daily backup takes a consistent `sqlite3.backup` snapshot of the
host outbox and runs `integrity_check` on that snapshot. It must not copy the
live `outbox.sqlite3-wal` or `outbox.sqlite3-shm` sidecars. The private catalog
and integration code under `/opt/confenge-gestao/integrations/codex-routines`
are covered separately as configuration files.
