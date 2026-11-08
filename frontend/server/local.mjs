/**
 * Local web entry point for the Compose stack.
 *
 * Serves the built UI from a static directory and forwards the two chat routes
 * through the same `route` and `forward` the Cloudflare Worker uses, so the local
 * and hosted paths share one proxy contract. Plain http to the API is allowed
 * here because the upstream is a private Compose service, never the internet.
 */
import {createReadStream, readFileSync} from 'node:fs';
import {stat} from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import {Readable} from 'node:stream';
import {pathToFileURL} from 'node:url';

import {route, SECURITY_HEADERS} from '../worker.mjs';
import {forward} from './forward.mjs';

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript',
  '.css': 'text/css',
  '.svg': 'image/svg+xml',
  '.json': 'application/json',
  '.txt': 'text/plain; charset=utf-8',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
};
const IMMUTABLE = 'public, max-age=31536000, immutable';

const notFound = () =>
  Response.json({detail: 'Not found'}, {status: 404, headers: {'Cache-Control': 'no-store', ...SECURITY_HEADERS}});

/** The decoded URL path, or null for malformed encoding, an embedded NUL or a parent segment. */
function decodePath(pathname) {
  try {
    const decoded = decodeURIComponent(pathname);
    return decoded.includes('\0') || decoded.split('/').includes('..') ? null : decoded;
  } catch {
    return null;
  }
}

/** The absolute file for a decoded path, or null when it escapes the root or is not a regular file. */
async function resolveFile(root, decoded) {
  const absolute = path.resolve(root, '.' + path.posix.normalize('/' + decoded));
  if (absolute !== root && !absolute.startsWith(root + path.sep)) return null;
  try {
    return (await stat(absolute)).isFile() ? absolute : null;
  } catch {
    return null;
  }
}

function fileResponse(root, absolute, {head = false} = {}) {
  const relative = path.relative(root, absolute);
  const headers = new Headers(SECURITY_HEADERS);
  headers.set('Content-Type', TYPES[path.extname(absolute)] ?? 'application/octet-stream');
  if (relative === 'index.html') headers.set('Cache-Control', 'no-store');
  else if (relative.startsWith('assets' + path.sep)) headers.set('Cache-Control', IMMUTABLE);
  const body = head ? null : Readable.toWeb(createReadStream(absolute));
  return new Response(body, {status: 200, headers});
}

/** A fetch-style handler: chat routes are forwarded, other /api paths are 404, everything else is the static app. */
export function createHandler({staticDir, baseURL, accessKey, fetchImpl = fetch}) {
  const root = path.resolve(staticDir);
  return async function handle(request) {
    const target = route(request);
    if (target) return forward(request, target, {baseURL, accessKey, fetchImpl, allowHttp: true});
    const {pathname} = new URL(request.url);
    if (pathname.startsWith('/api/')) return notFound();
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return new Response(null, {status: 405, headers: {Allow: 'GET, HEAD', ...SECURITY_HEADERS}});
    }
    const head = request.method === 'HEAD';
    const decoded = decodePath(pathname);
    if (decoded === null || decoded === '/_headers') return notFound();
    const file = await resolveFile(root, decoded);
    if (file) return fileResponse(root, file, {head});
    // Single-page application: extension-less paths are client routes.
    if (!path.posix.extname(decoded)) {
      const index = await resolveFile(root, '/index.html');
      if (index) return fileResponse(root, index, {head});
    }
    return notFound();
  };
}

/** Build a fetch Request from an incoming Node request; `signal` aborts upstream work when the client goes away. */
export function toRequest(req, origin, signal) {
  const headers = new Headers();
  for (const [name, value] of Object.entries(req.headers)) {
    if (typeof value === 'string') headers.set(name, value);
    else if (Array.isArray(value)) headers.set(name, value.join(', '));
  }
  const withBody = req.method !== 'GET' && req.method !== 'HEAD';
  return new Request(new URL(req.url, origin), {
    method: req.method,
    headers,
    body: withBody ? Readable.toWeb(req) : null,
    duplex: withBody ? 'half' : undefined,
    signal,
  });
}

/** Write a fetch Response to a Node response chunk by chunk; `signal` cancels the body when the client disconnects. */
export async function sendResponse(res, response, signal) {
  res.writeHead(response.status, Object.fromEntries(response.headers));
  if (!response.body) {
    res.end();
    return;
  }
  const reader = response.body.getReader();
  const onAbort = () => reader.cancel().catch(() => {});
  signal?.addEventListener('abort', onAbort, {once: true});
  let closed = false;
  const onClose = () => {
    closed = true;
  };
  res.once('close', onClose);
  // Wait for the socket to drain, or stop waiting when it closes; both listeners go away on either event.
  const drained = () =>
    new Promise((resolve) => {
      if (closed) return resolve();
      const settle = () => {
        res.off('drain', settle);
        res.off('close', settle);
        resolve();
      };
      res.once('drain', settle);
      res.once('close', settle);
    });
  try {
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      if (!res.write(value)) await drained();
      if (res.destroyed || closed) break;
    }
    res.end();
  } catch {
    res.destroy();
  } finally {
    res.off('close', onClose);
    signal?.removeEventListener('abort', onAbort);
  }
}

export function serve({port, host, staticDir, baseURL, accessKey, fetchImpl}) {
  const handle = createHandler({staticDir, baseURL, accessKey, fetchImpl});
  const server = http.createServer(async (req, res) => {
    const controller = new AbortController();
    res.once('close', () => {
      if (!res.writableFinished) controller.abort();
    });
    try {
      const origin = `http://${req.headers.host ?? `${host}:${port}`}`;
      // Check the raw path before the URL constructor removes dot segments.
      const response = decodePath(req.url.split('?')[0]) === null
        ? notFound()
        : await handle(toRequest(req, origin, controller.signal));
      await sendResponse(res, response, controller.signal);
    } catch {
      if (!res.headersSent) res.writeHead(500, {'Content-Type': 'application/json', ...SECURITY_HEADERS});
      res.end(JSON.stringify({detail: 'Internal error'}));
    }
  });
  server.listen(port, host);
  return server;
}

function main() {
  const port = Number(process.env.PORT ?? 8080);
  const host = process.env.HOST ?? '0.0.0.0';
  const staticDir = process.env.STATIC_DIR ?? './dist';
  const baseURL = process.env.API_BASE_URL;
  const keyFile = process.env.PREFLIGHT_ACCESS_KEY_FILE;
  if (!baseURL || !keyFile) {
    console.error('API_BASE_URL and PREFLIGHT_ACCESS_KEY_FILE are required');
    process.exit(1);
  }
  const accessKey = readFileSync(keyFile, 'utf8').trim();
  if (!accessKey) {
    console.error('PREFLIGHT_ACCESS_KEY_FILE names an empty file');
    process.exit(1);
  }
  const server = serve({port, host, staticDir, baseURL, accessKey});
  server.once('listening', () => console.log(`listening on http://${host}:${port}`));
  const stop = () => server.close(() => process.exit(0));
  process.once('SIGTERM', stop);
  process.once('SIGINT', stop);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
