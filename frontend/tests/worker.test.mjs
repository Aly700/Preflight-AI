import assert from 'node:assert/strict';
import {test} from 'node:test';

import worker, {route, SECURITY_HEADERS} from '../worker.mjs';

const env = () => ({
  API_BASE_URL: 'https://api.example.test',
  PREFLIGHT_ACCESS_KEY: 'server-side-key',
  ASSETS: {fetch: async () => new Response('<!doctype html><title>ui</title>', {status: 200, headers: {'Content-Type': 'text/html'}})},
});

/** Run `fn` with the global fetch replaced by a recorder that answers with `reply`. */
async function withUpstream(reply, fn) {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push({url, init, body: init.body ? new TextDecoder().decode(init.body) : undefined});
    return reply();
  };
  try {
    return await fn(calls);
  } finally {
    globalThis.fetch = original;
  }
}

const sse = () => new Response('event: done\ndata: {}\n\n', {status: 200, headers: {'Content-Type': 'text/event-stream'}});

test('route maps only the two chat routes, by method and path', () => {
  assert.equal(route(new Request('https://demo.test/api/chat', {method: 'POST'})), '/api/chat');
  assert.equal(route(new Request('https://demo.test/api/chat/status')), '/api/chat/status');
  assert.equal(route(new Request('https://demo.test/api/chat')), null);
  assert.equal(route(new Request('https://demo.test/api/chat/status', {method: 'POST'})), null);
  assert.equal(route(new Request('https://demo.test/api/aircraft')), null);
  assert.equal(route(new Request('https://demo.test/')), null);
});

test('POST /api/chat is forwarded with the server-side key and the body, and streams back', async () => {
  await withUpstream(sse, async (calls) => {
    const request = new Request('https://demo.test/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', Origin: 'https://demo.test'},
      body: JSON.stringify({question: 'Which aircraft has open issues?'}),
    });
    const response = await worker.fetch(request, env());
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('content-type'), 'text/event-stream');
    assert.equal(response.headers.get('cache-control'), 'no-store');
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, 'https://api.example.test/api/chat');
    assert.equal(calls[0].init.headers.Authorization, 'Bearer server-side-key');
    assert.equal(calls[0].body, JSON.stringify({question: 'Which aircraft has open issues?'}));
    assert.equal(await response.text(), 'event: done\ndata: {}\n\n');
  });
});

test('GET /api/chat/status is forwarded and returned as JSON', async () => {
  const reply = () => Response.json({configured: true});
  await withUpstream(reply, async (calls) => {
    const response = await worker.fetch(new Request('https://demo.test/api/chat/status'), env());
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), {configured: true});
    assert.equal(calls[0].url, 'https://api.example.test/api/chat/status');
  });
});

test('other /api paths are 404 and never reach the upstream', async () => {
  await withUpstream(sse, async (calls) => {
    for (const [method, path] of [['GET', '/api/aircraft'], ['GET', '/api/chat'], ['POST', '/api/chat/status'], ['DELETE', '/api/chat']]) {
      const response = await worker.fetch(new Request(`https://demo.test${path}`, {method}), env());
      assert.equal(response.status, 404, `${method} ${path}`);
      assert.deepEqual(await response.json(), {detail: 'Not found'});
    }
    assert.equal(calls.length, 0);
  });
});

test('missing secrets answer 503 without calling the upstream', async () => {
  await withUpstream(sse, async (calls) => {
    const bare = {ASSETS: env().ASSETS};
    const response = await worker.fetch(new Request('https://demo.test/api/chat/status'), bare);
    assert.equal(response.status, 503);
    assert.equal(calls.length, 0);
  });
});

test('the single-page app is served through Static Assets with security headers', async () => {
  const response = await worker.fetch(new Request('https://demo.test/some/client/route'), env());
  assert.equal(response.status, 200);
  assert.match(await response.text(), /<title>ui<\/title>/);
  for (const [name, value] of Object.entries(SECURITY_HEADERS)) assert.equal(response.headers.get(name), value);
});
