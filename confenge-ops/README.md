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
  navigation. See its README for input contracts and guarded production use.
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

The unit-test command includes the ClickUp importer's offline contract suite.

The dedicated Nginx root location requests an HTML document from Twenty even
when a client sends `Accept: */*`. API and asset routes retain their existing
content negotiation.

The complete operational dossier, evidence, backup material, and business data
remain in the private delivery package and production recovery store.
