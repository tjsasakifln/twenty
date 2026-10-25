# ClickUp → Twenty native importer

This directory contains a generic, idempotent importer for projecting a private
ClickUp snapshot into the existing CONFENGE Twenty workspace. It does not
contain customer records, ClickUp hierarchy names, workspace identifiers, API
keys, or production credentials.

The importer keeps Twenty as the application and authentication boundary. It
creates a small `ClickUp List` custom object, adds source-owned fields to the
native `Task` object, creates one table view per ClickUp list, projects the
named source table/board/calendar views, and creates Twenty navigation folders
and list links. Existing CRM objects and records are never deleted or replaced.

## Safety contract

- Default execution is offline `dry-run`; it performs no HTTP requests.
- Production writes require `--apply`, separate confirmations for the source
  ClickUp and target Twenty workspace IDs, the exact acknowledgement string,
  credentials, and a fresh target-bound signup/security guard report.
- Before its first write, the importer live-checks the target Twenty UUID,
  ACTIVE state, exactly one workspace member, and a disabled public invite.
- Missing source tasks never delete or archive Twenty records.
- Only fields owned by this importer are patched.
- Importer-owned views and navigation rows have deterministic UUIDs. The
  importer never deletes user-created views, filters, navigation entries, or
  records.
- Raw snapshots and credentials remain outside this directory and public Git.
- After cutover, the target-bound marker at
  `/etc/confenge-gestao/clickup-cutover.json` blocks production reimports before
  any network request, even when credentials are moved to another directory.
  An adjacent marker is also checked for other installations. This is an
  operational interlock for the supported host execution path, not a server
  permission boundary against older importer versions or deliberate removal.
  Ongoing Twenty edits must be reconciled deliberately; replaying an old
  snapshot is not a sync. Production credentials remain on the target host.

## Native cutover recovery

The private encrypted production recovery set must include the cutover marker,
the native configuration journal, stage map and application variables beside
the existing administrator credential. The deployed daily backup includes
`clickup-cutover.json`, `clickup-native-cutover-journal.json`,
`clickup-native-stage-map.json` and `clickup-native-application-variables.json`
from `/etc/confenge-gestao/` when present. Restore the marker with the database
before permitting importer execution. Keep the original source export as a
separate private archive; it is evidence for reconciliation, not a live sync.
The deployed backup also preserves `apps/operational-timeline` source and its
lockfile, excluding installed dependencies, SDK output and build caches. The
application's installed metadata and storage remain in the database and storage
recovery set. Keep the task-event gate off until post-restore validation passes.

## Native cutover operation

The Twenty server keeps `LOGIC_FUNCTION_TYPE=DISABLED`; only the worker uses
`LOGIC_FUNCTION_TYPE=LOCAL`. `CODE_INTERPRETER_TYPE` remains `DISABLED` on both
services. The LOCAL driver executes only reviewed application code. It is not a
strong isolation boundary: the child process inherits the worker environment,
internal network and application-storage mount. Do not enable unreviewed or
third-party functions on this worker.

Keep `CUTOVER_ENABLED=false` while applying an application bundle or recreating
the worker. Recreate only the worker; the API, PostgreSQL and Redis do not need a
restart for this setting. Before enabling, run `set_native_cutover_gate.py` with
the target marker and the root-only runtime allowlist. Its fail-closed preflight
checks the exact function inventory and triggers, current builds/checksums,
queued function references, server/worker drivers and disabled code interpreter.
It must be possible to set the gate to `false` even if those runtime checks no
longer pass, so the same helper remains the kill switch.

After every bundle or worker replacement, keep the gate off until the installed
application and variables are verified. Then enable through the guard and run a
tagged disposable QA smoke test. The smoke test must journal IDs before writes,
exercise stage/status round trips, date semantics, assignment, list, subtask and
dependency relations, and remove only its own records. Preserve and reconcile
any concurrent non-QA activity instead of deleting or rewriting it.

Rollback from LOCAL is potentially lossy. First set the gate to `false`, stop
the worker, and capture pending logic-function queue references for explicit
reconciliation. Only then restore `LOGIC_FUNCTION_TYPE=DISABLED` and recreate
the worker. A running Disabled driver may discard queued function work, so never
use that transition as a queue purge and never claim recovery without checking
the affected records.

## Snapshot inputs

`hierarchy.json` accepts the current recursive shape:

```text
hierarchy.root -> workspace -> space -> folder/list -> list
```

Every node must have a unique string `id`; typed nodes use `space`, `folder`,
or `list`. A list directly below the space is supported.

`tasks.json` accepts `workspace_id` and `tasks`. The current source format uses
`status: "found"`, `task_status`, `home_list`, `fields`, `subtask_ids`,
`dependencies`, `related_tasks`, `attachments`, `timing`, `privacy`, and
`states`. Future snapshots can add `parent`, dependency IDs, comments,
assignees, or named custom-field definitions without breaking the importer.

Optional private files may enrich the snapshot:

- `--status-definitions`: `{ "lists": { "<list-id>": [{"id", "status", "color", "orderindex"}] } }`
- `--space-statuses`: inherited statuses plus an explicit `applies_to_lists`
  allowlist; no inheritance is guessed.
- `--field-definitions`: `{ "fields": { "<field-id>": {"name", "type", "options"} } }`
- `--task-links`: one canonical source URL per imported task (exact coverage).
- `--rich-content`: one Quill delta per task; links, headings, lists, and basic
  inline formatting are projected into native rich text.
- `--global-views`: private view names, filters, fields, grouping, calendar,
  sort, and navigation placement.
- `--source-ui-views`: exact source URLs for those named views. Together with
  task and hierarchy IDs, this rewrites only known internal ClickUp links in
  native rich text to their imported Twenty records/views. External links and
  the raw/source URL fields remain unchanged.

## Usage

Offline plan (no credentials and no network):

```powershell
python .\clickup_twenty_importer.py `
  --hierarchy ..\clickup\hierarchy.json `
  --tasks ..\clickup\tasks.json
```

Run tests:

```powershell
python -m unittest discover -s ../../tests -p "test_clickup_twenty_importer.py"
```

Production apply is intentionally explicit and should only run after backup,
health, authentication, and live metadata preflight have passed:

```text
python clickup_twenty_importer.py \
  --hierarchy /private/hierarchy.json \
  --tasks /private/tasks.json \
  --status-definitions /private/observed-statuses.json \
  --space-statuses /private/space-statuses-ui.json \
  --field-definitions /private/custom-fields.json \
  --global-views /private/global-views.json \
  --source-ui-views /private/source-ui-views.json \
  --task-links /private/task-links.json \
  --rich-content /private/task-rich-content.json \
  --apply \
  --credential-file /etc/confenge-gestao/admin.json \
  --signup-guard-report /path/to/fresh-target-guard.json \
  --confirm-workspace-id <clickup-workspace-id> \
  --confirm-target-workspace-id <twenty-workspace-uuid> \
  --ack-production-write APPLY_CLICKUP_TO_TWENTY \
  --dark-theme
```

The production credential file is expected to contain `api_key`, the target
Twenty `workspace_id` UUID, and optionally `base_url`. The guard report must be
bound to the same target UUID and base URL and be no more than 15 minutes old.
