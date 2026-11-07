"""Authenticated container health check; prints no credentials or response body."""

import json
import urllib.request

from mcp_service.auth import load_token


def main() -> None:
    request = urllib.request.Request(
        'http://localhost:8001/health', headers={'Authorization': 'Bearer ' + load_token()}
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            healthy = response.status == 200 and json.load(response) == {'status': 'ok', 'database': 'ok'}
    except Exception:
        raise SystemExit(1) from None
    raise SystemExit(0 if healthy else 1)


if __name__ == '__main__':
    main()
