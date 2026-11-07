"""Adapted from an independent lifecycle probe: disconnect or transport failure at several points,
then ask whether the NEXT request is admitted (200) or refused (429) before any garbage collection.
Configuration comes from the environment so the same probe runs against the baseline and the rewrite."""
import asyncio, gc, json, os, sys, tempfile
from pathlib import Path
from unittest.mock import patch

temp = tempfile.TemporaryDirectory()
env = {'PREFLIGHT_MCP_URL': 'http://127.0.0.1:8001/mcp'}
for name in ('CHAT_TOKEN_FILE', 'COPILOT_TOKEN_FILE', 'MCP_TOKEN_FILE'):
    p = Path(temp.name) / name; p.write_text('a' * 64); env[name] = str(p)
os.environ.update(env)

import backend.chat as chat  # noqa: E402
from backend.app import create_app  # noqa: E402

def make_app():
    try:
        return create_app(allowed_hosts=['localhost'])
    except TypeError:  # baseline signature
        return create_app()

def scope(version):
    return {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': version}, 'http_version': '1.1', 'method': 'POST',
            'scheme': 'http', 'path': '/api/chat', 'raw_path': b'/api/chat', 'query_string': b'', 'root_path': '',
            'server': ('localhost', 80), 'client': ('127.0.0.1', 1234),
            'headers': [(b'host', b'localhost'), (b'authorization', b'Bearer ' + b'a' * 64), (b'content-type', b'application/json')]}

async def invoke(app, send, version='2.3', disconnected=None):
    delivered = False
    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {'type': 'http.request', 'body': b'{"question":"Summary"}', 'more_body': False}
        if disconnected is None:
            await asyncio.Future()
        else:
            await disconnected.wait()
        return {'type': 'http.disconnect'}
    await app(scope(version), receive, send)

async def next_status(app):
    frames = []
    async def capture(message): frames.append(message)
    async def complete(*_):
        yield {'event': 'done', 'data': {}}
    with patch.object(chat, 'stream_answer', complete):
        await asyncio.wait_for(invoke(app, capture), 1)
    return frames[0]['status']

async def send_failure_case(fail_at, version):
    async def inference(*_):
        for _ in range(3):
            yield {'event': 'delta', 'data': {'text': 'partial'}}
            await asyncio.sleep(0)
        yield {'event': 'done', 'data': {}}
    with patch.object(chat, 'stream_answer', inference):
        app = make_app()
        count = 0
        async def send(message):
            nonlocal count
            number = count; count += 1
            if number == fail_at:
                raise OSError('simulated disconnected transport')
        outcome = 'returned'
        try:
            await asyncio.wait_for(invoke(app, send, version), 1)
        except BaseException as exc:
            outcome = type(exc).__name__
        for _ in range(10): await asyncio.sleep(0)
        status = await next_status(app)
        print(json.dumps({'case': 'send_failure', 'spec': version, 'fail_at': fail_at, 'outcome': outcome, 'next_status_before_gc': status}))

async def disconnect_case(disconnect_at):
    async def inference(*_):
        yield {'event': 'delta', 'data': {'text': 'partial'}}
        await asyncio.Future()
    with patch.object(chat, 'stream_answer', inference):
        app = make_app()
        disconnected = asyncio.Event()
        if disconnect_at == -1: disconnected.set()
        count = 0
        async def send(message):
            nonlocal count
            if count == disconnect_at: disconnected.set()
            count += 1
        outcome = 'returned'
        task = asyncio.create_task(invoke(app, send, '2.3', disconnected))
        done, _ = await asyncio.wait([task], timeout=.1)
        if not done:
            outcome = 'pending'; task.cancel()
        try: await task
        except BaseException as exc: outcome += ':' + type(exc).__name__
        del task
        for _ in range(10): await asyncio.sleep(0)
        status = await next_status(app)
        print(json.dumps({'case': 'http.disconnect', 'disconnect_at': disconnect_at, 'outcome': outcome, 'next_status_before_gc': status}))

async def main():
    gc.disable()  # make "before garbage collection" mean exactly that
    for version in ('2.3', '2.4'):
        for index in range(4):
            await send_failure_case(index, version)
    for index in (-1, 0, 1, 2):
        await disconnect_case(index)

asyncio.run(main())
