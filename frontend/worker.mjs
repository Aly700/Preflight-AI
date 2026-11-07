/**
 * Cloudflare Worker entry for the public demo.
 *
 * Static Assets serve the built UI. The Worker runs only for `/api/*`, where it
 * forwards the two chat routes to the upstream API through `forward`, which adds
 * the server-side access key. Both upstream values are Worker secrets
 * (`wrangler secret put`), never files in the assets directory.
 */
import {forward} from './server/forward.mjs';

export const SECURITY_HEADERS = {
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'Content-Security-Policy':
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; " +
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
};

const ROUTES = new Map([
  ['POST /api/chat', '/api/chat'],
  ['GET /api/chat/status', '/api/chat/status'],
]);

/** The upstream path for a request, or null when the method and path are not one of the two chat routes. */
export function route(request) {
  return ROUTES.get(`${request.method} ${new URL(request.url).pathname}`) ?? null;
}

function withSecurityHeaders(response) {
  const headers = new Headers(response.headers);
  for (const [name, value] of Object.entries(SECURITY_HEADERS)) headers.set(name, value);
  return new Response(response.body, {status: response.status, statusText: response.statusText, headers});
}

export default {
  async fetch(request, env) {
    const path = route(request);
    if (path) {
      // Missing secrets become a 503 from `forward`; `?? ''` keeps it from touching Node's process.env.
      return forward(request, path, {baseURL: env.API_BASE_URL ?? '', accessKey: env.PREFLIGHT_ACCESS_KEY ?? ''});
    }
    if (new URL(request.url).pathname.startsWith('/api/')) {
      return Response.json({detail: 'Not found'}, {status: 404, headers: {'Cache-Control': 'no-store'}});
    }
    // Anything else is the single-page app; `_headers` covers assets served without the Worker.
    return withSecurityHeaders(await env.ASSETS.fetch(request));
  },
};
