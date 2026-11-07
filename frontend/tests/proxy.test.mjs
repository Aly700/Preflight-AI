import test from 'node:test';
import assert from 'node:assert/strict';

async function load() {
  try {
    return await import('../server/forward.mjs');
  } catch {
    assert.fail('API proxy is not implemented');
  }
}

const upstream = {baseURL: 'https://upstream.invalid', accessKey: 'server-key'};
const untouched = () => assert.fail('Must not contact upstream');

/** A POST whose body arrives as a stream with no Content-Length header. */
function streamedPost(chunks, {signal, close = true} = {}) {
  const stream = new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk));
      if (close) controller.close();
    },
  });
  return new Request('https://prototype.invalid/api/chat', {method: 'POST', body: stream, duplex: 'half', signal});
}

test('The proxy returns an explicit setup error without an upstream', async () => {
  const {forward} = await load();
  const r = await forward(new Request('https://prototype.invalid/api/chat/status'), '/api/chat/status', {baseURL: ''});
  assert.equal(r.status, 503);
});

test('Anonymous browser chat uses the server credential and streams the answer without exposing it', async () => {
  const {forward} = await load();
  let captured;
  const accessKey = 'server-secret-for-test';
  const request = new Request('https://prototype.invalid/api/chat', {
    method: 'POST',
    headers: {Cookie: 'unrelated=private', Origin: 'https://prototype.invalid'},
    body: '{"question":"Summary"}',
  });
  const response = await forward(request, '/api/chat', {
    baseURL: 'https://upstream.invalid',
    accessKey,
    fetchImpl: async (url, options) => {
      captured = {url, options};
      return new Response('event: done\ndata: {}\n\n', {headers: {'Content-Type': 'text/event-stream'}});
    },
  });
  const body = await response.text();
  assert.equal(response.status, 200);
  assert.match(body, /event: done/);
  assert.ok(!body.includes(accessKey));
  assert.equal(response.headers.get('Authorization'), null);
  assert.equal(captured.options.headers.Authorization, 'Bearer ' + accessKey);
  assert.equal(captured.options.headers.Cookie, undefined);
  assert.equal(captured.options.headers.Origin, undefined);
  assert.equal(new TextDecoder().decode(captured.options.body), '{"question":"Summary"}');
});

test('Missing server credentials cannot be replaced by a browser-supplied token', async () => {
  const {forward} = await load();
  const request = new Request('https://prototype.invalid/api/chat', {
    method: 'POST', headers: {Authorization: 'Bearer browser-supplied-key'}, body: '{}',
  });
  const response = await forward(request, '/api/chat', {baseURL: 'https://upstream.invalid', accessKey: '', fetchImpl: untouched});
  assert.equal(response.status, 503);
});

test('The proxy ignores browser credentials and rejects cross-origin requests', async () => {
  const {forward} = await load();
  let sent;
  const options = {...upstream, fetchImpl: async (url, request) => {
    sent = request.headers.Authorization;
    return Response.json({configured: true});
  }};
  const status = new Request('https://prototype.invalid/api/chat/status', {headers: {Authorization: 'Bearer browser-key'}});
  const response = await forward(status, '/api/chat/status', options);
  assert.equal(response.status, 200);
  assert.equal(sent, 'Bearer server-key');
  const foreign = new Request('https://prototype.invalid/api/chat', {method: 'POST', headers: {Origin: 'https://untrusted.invalid'}, body: '{}'});
  assert.equal((await forward(foreign, '/api/chat', options)).status, 403);
});

test('An oversized body is refused before upstream is contacted, with or without Content-Length', async () => {
  const {forward, BODY_LIMIT_BYTES} = await load();
  const declared = new Request('https://prototype.invalid/api/chat', {
    method: 'POST', headers: {'Content-Length': String(BODY_LIMIT_BYTES + 1)}, body: 'x'.repeat(BODY_LIMIT_BYTES + 1),
  });
  assert.equal((await forward(declared, '/api/chat', {...upstream, fetchImpl: untouched})).status, 413);
  let pulled = 0;
  const chunk = 'x'.repeat(4096);
  const endless = new ReadableStream({pull(controller) { pulled += 1; controller.enqueue(new TextEncoder().encode(chunk)); }});
  const streamed = new Request('https://prototype.invalid/api/chat', {method: 'POST', body: endless, duplex: 'half'});
  assert.equal(streamed.headers.get('content-length'), null);
  assert.equal((await forward(streamed, '/api/chat', {...upstream, fetchImpl: untouched})).status, 413);
  assert.ok(pulled * chunk.length < BODY_LIMIT_BYTES * 2, `read ${pulled * chunk.length} bytes before refusing`);
});

test('A streamed body within the limit is forwarded intact', async () => {
  const {forward} = await load();
  let received;
  const response = await forward(streamedPost(['{"question":', '"Summary"}']), '/api/chat', {...upstream,
    fetchImpl: async (url, options) => { received = new TextDecoder().decode(options.body); return Response.json({ok: true}); }});
  assert.equal(response.status, 200);
  assert.equal(received, '{"question":"Summary"}');
});

test('A client that aborts while its body is still arriving releases the reader', async () => {
  const {forward} = await load();
  const controller = new AbortController();
  let cancelled = false;
  const stalled = new ReadableStream({
    start(c) { c.enqueue(new TextEncoder().encode('{"question":')); },
    cancel() { cancelled = true; },
  });
  const request = new Request('https://prototype.invalid/api/chat', {method: 'POST', body: stalled, duplex: 'half', signal: controller.signal});
  const pending = forward(request, '/api/chat', {...upstream, fetchImpl: untouched});
  setTimeout(() => controller.abort(), 20);
  const response = await pending;
  assert.equal(response.status, 502);
  assert.equal(cancelled, true);
});

test('Only an HTTPS upstream is accepted', async () => {
  const {forward} = await load();
  const request = new Request('https://prototype.invalid/api/chat/status');
  assert.equal((await forward(request, '/api/chat/status', {baseURL: 'http://upstream.invalid', accessKey: 'k', fetchImpl: untouched})).status, 503);
  assert.equal((await forward(request, '/api/chat/status', {baseURL: 'not a url', accessKey: 'k', fetchImpl: untouched})).status, 503);
});

test('Upstream failures and unexpected content types become a fixed 502 and the upstream body is cancelled', async () => {
  const {forward} = await load();
  const request = () => new Request('https://prototype.invalid/api/chat/status');
  const thrown = await forward(request(), '/api/chat/status', {...upstream, fetchImpl: async () => { throw new Error('ECONNREFUSED 10.0.0.9:8000 key=server-key'); }});
  assert.equal(thrown.status, 502);
  const text = await thrown.text();
  assert.ok(!text.includes('10.0.0.9') && !text.includes('server-key'));
  let cancelled = false;
  const html = new ReadableStream({start(c) { c.enqueue(new TextEncoder().encode('<html>')); }, cancel() { cancelled = true; }});
  const rejected = await forward(request(), '/api/chat/status', {...upstream, fetchImpl: async () => new Response(html, {headers: {'Content-Type': 'text/html'}})});
  assert.equal(rejected.status, 502);
  assert.equal(cancelled, true);
});

test('An already-aborted request with an errored body is reported without an unhandled rejection', async () => {
  const {forward} = await load();
  const unhandled = [];
  const observe = (reason) => unhandled.push(reason);
  process.on('unhandledRejection', observe);
  try {
    const controller = new AbortController();
    controller.abort();
    const errored = new ReadableStream({pull(c) { c.error(new Error('upload failed')); }});
    const request = new Request('https://prototype.invalid/api/chat', {method: 'POST', body: errored, duplex: 'half', signal: controller.signal});
    const response = await forward(request, '/api/chat', {...upstream, fetchImpl: untouched});
    assert.equal(response.status, 502);
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.deepEqual(unhandled, []);
  } finally {
    process.off('unhandledRejection', observe);
  }
});

test('A cancel hook that never settles does not delay the 413 or the 502', async () => {
  const {forward, BODY_LIMIT_BYTES} = await load();
  const never = () => new Promise(() => {});
  const timeout = (ms) => new Promise((_, reject) => setTimeout(() => reject(new Error('response delayed')), ms));
  const oversized = new ReadableStream({
    pull(c) { c.enqueue(new Uint8Array(BODY_LIMIT_BYTES + 1)); },
    cancel: never,
  });
  const upload = new Request('https://prototype.invalid/api/chat', {method: 'POST', body: oversized, duplex: 'half'});
  const refused = await Promise.race([forward(upload, '/api/chat', {...upstream, fetchImpl: untouched}), timeout(500)]);
  assert.equal(refused.status, 413);
  const html = new ReadableStream({start(c) { c.enqueue(new TextEncoder().encode('<html>')); }, cancel: never});
  const rejected = await Promise.race([
    forward(new Request('https://prototype.invalid/api/chat/status'), '/api/chat/status',
      {...upstream, fetchImpl: async () => new Response(html, {headers: {'Content-Type': 'text/html'}})}),
    timeout(500),
  ]);
  assert.equal(rejected.status, 502);
});

test('An upstream redirect is refused with the fixed 502 and never followed, so the key cannot be replayed', async () => {
  const {forward} = await load();
  let captured;
  let cancelled = false;
  const redirectBody = new ReadableStream({cancel() { cancelled = true; }});
  const response = await forward(new Request('https://prototype.invalid/api/chat/status'), '/api/chat/status', {
    ...upstream,
    fetchImpl: async (url, options) => {
      captured = options;
      return new Response(redirectBody, {status: 302, headers: {Location: 'https://elsewhere.invalid/', 'Content-Type': 'application/json'}});
    },
  });
  assert.equal(captured.redirect, 'manual');
  assert.equal(response.status, 502);
  assert.deepEqual(await response.json(), {detail: 'The prototype API connection is unavailable'});
  assert.equal(cancelled, true);
});

test('A plain http upstream is refused by default and allowed only with allowHttp', async () => {
  const {forward} = await load();
  const request = () => new Request('http://web.local/api/chat/status');
  const target = {baseURL: 'http://preflight-oss-api:8000', accessKey: 'k' + 'k'.repeat(63)};
  const refused = await forward(request(), '/api/chat/status', {...target, fetchImpl: untouched});
  assert.equal(refused.status, 503);
  let captured;
  const allowed = await forward(request(), '/api/chat/status', {
    ...target,
    allowHttp: true,
    fetchImpl: async (url) => {
      captured = url;
      return Response.json({configured: true});
    },
  });
  assert.equal(allowed.status, 200);
  assert.equal(captured, 'http://preflight-oss-api:8000/api/chat/status');
});
