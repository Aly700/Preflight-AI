import assert from 'node:assert/strict';
import {mkdir, mkdtemp, rm, writeFile} from 'node:fs/promises';
import http from 'node:http';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {after, before, test} from 'node:test';

import {createHandler, serve} from '../server/local.mjs';

const KEY = 'k'.repeat(64);
let parent;
let dist;

before(async () => {
  parent = await mkdtemp(path.join(tmpdir(), 'preflight-web-'));
  dist = path.join(parent, 'dist');
  await mkdir(path.join(dist, 'assets'), {recursive: true});
  await writeFile(path.join(dist, 'index.html'), '<!doctype html><title>ui</title>');
  await writeFile(path.join(dist, 'assets', 'app.js'), 'console.log(1)');
  await writeFile(path.join(dist, 'icon.svg'), '<svg/>');
  await writeFile(path.join(parent, 'secret.txt'), 'outside the static root');
});
after(() => rm(parent, {recursive: true, force: true}));

const handler = (overrides = {}) =>
  createHandler({staticDir: dist, baseURL: 'http://preflight-oss-api:8000', accessKey: KEY, ...overrides});
const get = (pathname, init) => handler(init?.handler).then((h) => h(new Request('http://web.local' + pathname, init?.request)));

test('serves index with security headers and no-store, assets immutable', async () => {
  const handle = createHandler({staticDir: dist, baseURL: 'http://preflight-oss-api:8000', accessKey: KEY});
  const index = await handle(new Request('http://web.local/'));
  assert.equal(index.status, 200);
  assert.equal(index.headers.get('Content-Type'), 'text/html; charset=utf-8');
  assert.equal(index.headers.get('Cache-Control'), 'no-store');
  assert.match(index.headers.get('Content-Security-Policy'), /frame-ancestors 'none'/);
  assert.equal(index.headers.get('X-Content-Type-Options'), 'nosniff');
  assert.equal(await index.text(), '<!doctype html><title>ui</title>');
  const asset = await handle(new Request('http://web.local/assets/app.js'));
  assert.equal(asset.status, 200);
  assert.equal(asset.headers.get('Content-Type'), 'text/javascript');
  assert.equal(asset.headers.get('Cache-Control'), 'public, max-age=31536000, immutable');
  const icon = await handle(new Request('http://web.local/icon.svg'));
  assert.equal(icon.headers.get('Content-Type'), 'image/svg+xml');
});

test('unknown client route falls back to index.html; unknown /api path is 404 JSON', async () => {
  const handle = createHandler({staticDir: dist, baseURL: 'http://preflight-oss-api:8000', accessKey: KEY});
  const page = await handle(new Request('http://web.local/some/client/route'));
  assert.equal(page.status, 200);
  assert.equal(page.headers.get('Content-Type'), 'text/html; charset=utf-8');
  const api = await handle(new Request('http://web.local/api/aircraft'));
  assert.equal(api.status, 404);
  assert.deepEqual(await api.json(), {detail: 'Not found'});
  assert.equal(api.headers.get('Cache-Control'), 'no-store');
  const missing = await handle(new Request('http://web.local/assets/missing.js'));
  assert.equal(missing.status, 404);
});

test('static path traversal is refused', async () => {
  const handle = createHandler({staticDir: dist, baseURL: 'http://preflight-oss-api:8000', accessKey: KEY});
  for (const target of ['/%2e%2e/secret.txt', '/..%2fsecret.txt', '/assets/%2e%2e/%2e%2e/secret.txt', '/%00/secret.txt', '/%zz']) {
    const response = await handle(new Request('http://web.local' + target));
    assert.equal(response.status, 404, target);
    assert.ok(!(await response.text()).includes('outside the static root'), target);
  }
});

test('chat routes are forwarded over plain http with the server-side key', async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({url, init});
    return new Response('event: done\ndata: {}\n\n', {status: 200, headers: {'Content-Type': 'text/event-stream'}});
  };
  const handle = createHandler({staticDir: dist, baseURL: 'http://preflight-oss-api:8000', accessKey: KEY, fetchImpl});
  const response = await handle(
    new Request('http://web.local/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', Origin: 'http://web.local'},
      body: JSON.stringify({question: 'Which aircraft has open issues?', history: []}),
    })
  );
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('Content-Type'), 'text/event-stream');
  assert.match(await response.text(), /event: done/);
  assert.equal(calls[0].url, 'http://preflight-oss-api:8000/api/chat');
  assert.equal(calls[0].init.headers.Authorization, 'Bearer ' + KEY);
  const status = await handle(new Request('http://web.local/api/chat/status'));
  assert.equal(status.status, 200);
  assert.equal(calls[1].url, 'http://preflight-oss-api:8000/api/chat/status');
});

test('client disconnect cancels the upstream stream', async () => {
  let cancelled = false;
  let firstChunkSent;
  const sent = new Promise((resolve) => (firstChunkSent = resolve));
  const fetchImpl = async () =>
    new Response(
      new ReadableStream({
        start(controller) {
          controller.enqueue(new TextEncoder().encode('event: status\ndata: {}\n\n'));
          firstChunkSent();
        },
        cancel() {
          cancelled = true;
        },
      }),
      {status: 200, headers: {'Content-Type': 'text/event-stream'}}
    );
  const server = serve({port: 0, host: '127.0.0.1', staticDir: dist, baseURL: 'http://preflight-oss-api:8000', accessKey: KEY, fetchImpl});
  await new Promise((resolve) => server.once('listening', resolve));
  const {port} = server.address();
  await new Promise((resolve, reject) => {
    const request = http.request({host: '127.0.0.1', port, method: 'POST', path: '/api/chat', headers: {'Content-Type': 'application/json'}}, (response) => {
      assert.equal(response.statusCode, 200);
      response.once('data', () => {
        request.destroy();
        resolve();
      });
    });
    request.once('error', reject);
    request.end(JSON.stringify({question: 'x', history: []}));
  });
  await sent;
  const deadline = Date.now() + 2000;
  while (!cancelled && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 20));
  server.close();
  assert.equal(cancelled, true);
});

test('backpressure does not accumulate close listeners on the response', async () => {
  const {EventEmitter} = await import('node:events');
  const {sendResponse} = await import('../server/local.mjs');
  const res = new EventEmitter();
  Object.assign(res, {
    destroyed: false,
    writableFinished: false,
    writeHead() {},
    write() {
      setImmediate(() => res.emit('drain'));
      return false; // every write stalls, then drains
    },
    end() {
      res.writableFinished = true;
    },
    destroy() {
      res.destroyed = true;
    },
  });
  const body = new ReadableStream({
    start(controller) {
      for (let i = 0; i < 30; i++) controller.enqueue(new Uint8Array(8));
      controller.close();
    },
  });
  await sendResponse(res, new Response(body, {status: 200}), new AbortController().signal);
  assert.equal(res.writableFinished, true);
  assert.ok(res.listenerCount('close') <= 1, `close listeners left: ${res.listenerCount('close')}`);
  assert.equal(res.listenerCount('drain'), 0);
});
