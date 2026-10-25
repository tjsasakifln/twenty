# CONFENGE operations layer

This directory contains the public, sanitized operations layer for the CONFENGE
management center. It is based on upstream Twenty commit
`7e1431c84adbb264db98ab8b8d008a8401b6d4d6` (v2.45.0 deployment line).

The implementation intentionally uses configuration and external adapters; it
does not patch Twenty core. The deployed application is pinned by image digest,
binds its application port to loopback, and is exposed through a dedicated TLS
reverse proxy. Google Sheets access uses the read-only scope and writes an
atomic local snapshot before the separate Twenty synchronization stage.

## Public contents

- `deploy/`: pinned Compose topology and a dedicated Nginx virtual host.
- `config/workspace-model.json`: custom object and field schema. It contains no
  records or tenant identifiers.
- `integrations/`: read-only Google Sheets reader and integrity probe, freshness
  calculation and public access probe.
- `integrations/clickup/`: an offline-first native Twenty importer for private
  ClickUp snapshots, including typed fields, task relationships, views and
  navigation, plus a recoverable native-workflow configurator and disposable
  production smoke check. See its README for guarded production use.
- `apps/operational-timeline/`: native agenda and timeline pages, and gated
  task-event rules for stages, completion, dates and new-task defaults.
- `integrations/operational-overview/`: additive operational dashboard widgets
  and goals linked to tasks; preserves existing dashboards and business records.
- `integrations/codex-routines/`: scheduled-run registry, confirmed result delivery
  and durable outboxes. The actual routine catalog remains private. Codex keeps
  ownership of scheduling; local routines require the desktop app and computer.
- `scripts/infra/`: initialization, deployment, health, and transactional
  public-origin deployment helpers.
- `tests/`: offline unit tests. They use fixtures and fake transports only.

## Configuration

Copy `.env.example` on the target host and generate all replacement values
there. Supply `EXTRA_SPREADSHEET_ID` through the service environment and keep the
Google service-account JSON outside the repository with read-only Sheets scope.
The published source never contains those values.

Run the offline checks from this directory:

```bash
python -m unittest discover -s tests -p 'test_*.py'
bash scripts/infra/test-deploy-public-origin-mocks.sh
```

The unit-test command includes the import, native configuration, overview and
routine-delivery contract suites. The native application's README documents its
separate TypeScript tests, build, metadata planning and rollout procedure.

The dedicated Nginx root location requests an HTML document from Twenty even
when a client sends `Accept: */*`. API and asset routes retain their existing
content negotiation.

The complete operational dossier, evidence, backup material, and business data
remain in the private delivery package and production recovery store.
