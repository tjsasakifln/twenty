# Twenty native operational overview

This directory contains a generic, idempotent installer for a native Twenty
operational overview. It reuses an existing dashboard, preserves all existing
tabs and widgets, and adds a separate `Operação` tab with:

- an explicit-focus task table based on `Foco atual`;
- an overdue-work task table covering both date-only and date-time deadlines;
- a stacked task count by `Frente` and native task status;
- an empty native `Meta` object related to tasks, plus its table widget;
- an optional routine-health table when the canonical `codexRoutine` object is
  already installed by the scheduled-routines integration.

It does not create goal records, target values, task records, AI credentials,
or a separate portal. Existing CRM objects and dashboard content are not
deleted or overwritten.

## Safety contract

- The default mode is a live, read-only plan. GraphQL reads use HTTP POST but
  are accounted as reads.
- Credentials can be sent only to the fixed canonical HTTPS origin or to an
  HTTP loopback address; the CLI has no hostname-override flag.
- Apply requires an exact acknowledgement and an exact target workspace UUID.
- The target workspace must be active and keep public invitations disabled.
- Owned object and field metadata carries the marker
  `Managed by confenge-operational-overview/v1`.
- Owned views and their filter, group, and sort children use deterministic
  UUIDs. Unexpected children abort before view writes; known children are
  reconciled to their complete owned configuration.
- The task-to-goal relation is accepted only when its target, direction,
  inverse field, and cross-referenced IDs form the exact expected graph.
- Widget creation converges by reserved title and exact native configuration.
  Manual widget positions are preserved after creation; configuration
  collisions abort instead of being overwritten.
- Missing `codexRoutine` metadata is a non-fatal skip, never a reason to create
  a competing routines model.
- AI inspection reads only `/client-config`; it never calls a model or sends
  business data. A visible AI button does not mean a provider is configured.

## Read-only plan

Run on the server so the credential never leaves its root-readable host file:

```text
python operational_overview.py \
  --credential-file /etc/confenge-gestao/admin.json \
  --dashboard-id <existing-dashboard-uuid>
```

Optional private source snapshots can add evidence to the plan without being
copied into the repository:

```text
  --source-tasks /private/tasks.json \
  --source-fields /private/custom-fields.json
```

## Guarded apply

Run only after reviewing the emitted plan and taking the normal production
backup:

```text
python operational_overview.py \
  --credential-file /etc/confenge-gestao/admin.json \
  --dashboard-id <existing-dashboard-uuid> \
  --apply \
  --confirm-workspace-id <target-workspace-uuid> \
  --ack APPLY_OPERATIONAL_OVERVIEW
```

The apply result reports metadata writes and explicitly confirms zero goal
records created and zero task records updated.
