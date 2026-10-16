#!/usr/bin/env python3
"""Read-only public transport/auth/cache acceptance check; never logs records or keys."""
import argparse
import ipaddress
import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from twenty_configure import unlist


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--edge-ip', help='An independently resolved public edge address; TLS hostname validation remains enabled.')
    args = parser.parse_args()
    if args.edge_ip:
        ipaddress.ip_address(args.edge_ip)
        original_resolver = socket.getaddrinfo
        def scoped_resolver(host, port, *positional, **keywords):
            return original_resolver(args.edge_ip if host == 'gestao.confenge.com.br' else host, port, *positional, **keywords)
        socket.getaddrinfo = scoped_resolver
    credential = json.loads(Path('/etc/confenge-gestao/integration.json').read_text())
    token = credential['api_key']
    opener = urllib.request.build_opener(NoRedirect)
    path = '/rest/companies?' + urllib.parse.urlencode({'filter': 'id[eq]:00000000-0000-4000-8000-000000000000', 'limit': 1})

    def get(base, authorized):
        headers = {'Accept': 'application/json', 'User-Agent': 'CONFENGE-public-acceptance/1.0'}
        if authorized:
            headers['Authorization'] = 'Bearer ' + token
        request = urllib.request.Request(base + path, headers=headers, method='GET')
        try:
            response = opener.open(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read(512_000)
            try:
                document = json.loads(raw)
            except ValueError:
                document = None
            return {'status': response.code, 'cache_control': response.headers.get('Cache-Control'),
                    'cf_cache_status': response.headers.get('CF-Cache-Status'),
                    'records': len(unlist(document)) if response.code == 200 and isinstance(document, (dict, list)) else None}

    local = get('http://127.0.0.1:18300', True)
    if local['status'] != 200 or local['records'] != 0:
        raise RuntimeError('empty_filter_preflight_failed_no_public_authenticated_request_made')
    public_authorized = get('https://gestao.confenge.com.br', True)
    public_anonymous = get('https://gestao.confenge.com.br', False)
    passed = (public_authorized['status'] == 200 and public_authorized['records'] == 0
              and public_anonymous['status'] in (401, 403)
              and 'no-store' in (public_authorized['cache_control'] or '').lower()
              and public_authorized['cf_cache_status'] not in ('HIT', 'STALE', 'UPDATING'))
    print(json.dumps({'status': 'pass' if passed else 'failed', 'observed_at': datetime.now(timezone.utc).isoformat(),
          'dns_mode': 'explicit_public_edge_with_verified_TLS' if args.edge_ip else 'system_resolver',
          'edge_ip': args.edge_ip,
          'empty_filter_preflight': local, 'public_authorized_empty_query': public_authorized,
          'public_anonymous_same_query': public_anonymous, 'business_records_printed': False,
          'secrets_printed': False}, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
