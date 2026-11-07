#!/usr/bin/env python3
"""Create the local stack's secret files once, without printing a value.

Generated values are random. The two model credentials are created as empty
placeholders: an empty file means "not configured" to the services, and the
developer replaces it with their own key. Existing files are never overwritten.

Privacy on the host comes from the 0700 directory; the files themselves are
0644 because Compose bind-mounts each file into a container that runs as an
unprivileged user, and on a Linux daemon the host mode is what that user sees.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parents[1]
# Name -> bytes of randomness; token_hex doubles the length, so 32 bytes is the 64-character MCP rule.
GENERATED = {'postgres-password': 32, 'api-password': 24, 'mcp-password': 24, 'chat-token': 32, 'mcp-token': 32}
PLACEHOLDERS = ('copilot-token', 'model-api-key')


FILE_MODE = 0o644
DIRECTORY_MODE = 0o700


def write_new(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        handle.write(content)


def ensure_secrets(directory: Path) -> dict[str, str]:
    """Create missing files, normalise modes, and return each name's status: created, kept, placeholder or present."""
    directory.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
    directory.chmod(DIRECTORY_MODE)
    status: dict[str, str] = {}
    for name, length in GENERATED.items():
        path = directory / name
        if path.exists():
            status[name] = 'kept'
        else:
            write_new(path, secrets.token_hex(length) + '\n')
            status[name] = 'created'
    for name in PLACEHOLDERS:
        path = directory / name
        if path.exists():
            status[name] = 'present' if path.read_text(encoding='utf-8').strip() else 'placeholder'
        else:
            write_new(path, '')
            status[name] = 'placeholder'
    for name in (*GENERATED, *PLACEHOLDERS):
        (directory / name).chmod(FILE_MODE)
    return status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT / '.secrets')
    args = parser.parse_args()
    print(json.dumps(ensure_secrets(args.directory), sort_keys=True))


if __name__ == '__main__':
    main()
