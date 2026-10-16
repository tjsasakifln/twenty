# Architecture

The operations layer runs four containers: Twenty server, Twenty worker,
PostgreSQL 16, and Redis 7. Only the Twenty HTTP port is bound, on loopback.
Nginx terminates TLS for `gestao.confenge.com.br` and forwards WebSocket and HTTP
traffic to that loopback listener.

The Google source job authenticates with a service account limited to
`spreadsheets.readonly`. It fetches a bounded range, validates the expected
shape, and atomically replaces a local snapshot. A separate adapter can project
that snapshot into Twenty using a restricted application key. Source failures
preserve the last valid snapshot and mark freshness explicitly.

Production secrets are host files with restricted permissions. They are absent
from Compose, this repository, logs, and generated status JSON. The public fork
contains no customer records or recovery artifacts.
