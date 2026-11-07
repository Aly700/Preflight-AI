/**
 * A narrow server-side streaming proxy for the public chat path.
 *
 * The browser never sees the upstream access key: this code adds it, forwards
 * only the chat route it is given, bounds the request body while it arrives,
 * and passes the upstream SSE or JSON stream back unchanged. Upstream failures
 * are reported with fixed messages so hostnames, keys and stack traces never
 * reach the client; the cause is logged on the server only.
 */
export const BODY_LIMIT_BYTES = 32768;
export const UPSTREAM_TIMEOUT_MS = 125000;

const fail = (status, detail) => Response.json({detail}, {status, headers: {'Cache-Control': 'no-store'}});

/** Resolve or reject with `promise`, but reject as soon as `signal` aborts. */
function abortable(promise, signal) {
  if (!signal) return promise;
  if (signal.aborted) {
    promise.catch(() => {}); // the operation has started: its later rejection must never go unobserved
    return Promise.reject(signal.reason ?? new DOMException('Aborted', 'AbortError'));
  }
  return new Promise((resolve, reject) => {
    const onAbort = () => reject(signal.reason ?? new DOMException('Aborted', 'AbortError'));
    signal.addEventListener('abort', onAbort, {once: true});
    promise.then(resolve, reject).finally(() => signal.removeEventListener('abort', onAbort));
  });
}

/** Start cancelling a stream without waiting for its cancel hook; a hook that never settles must not delay a response. */
function discard(cancellable) {
  try {
    cancellable?.cancel().catch(() => {});
  } catch {
    // Cancelling an already-released or errored stream is not an error for the caller.
  }
}

/**
 * Read a request body of at most `limit` bytes. Returns `null` the moment the
 * limit is exceeded, without buffering the rest, and cancels the reader when
 * `signal` aborts or reading ends for any reason.
 */
export async function readBounded(request, limit, signal) {
  const declared = request.headers.get('content-length');
  if (declared !== null && Number(declared) > limit) return null;
  if (!request.body) return new Uint8Array(0);
  const reader = request.body.getReader();
  const chunks = [];
  let total = 0;
  try {
    for (;;) {
      if (signal?.aborted) throw signal.reason ?? new DOMException('Aborted', 'AbortError');
      const {value, done} = await abortable(reader.read(), signal);
      if (done) break;
      total += value.byteLength;
      if (total > limit) return null;
      chunks.push(value);
    }
  } finally {
    discard(reader);
  }
  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return body;
}

export async function forward(request, path, {
  baseURL = process.env.API_BASE_URL,
  accessKey = process.env.PREFLIGHT_ACCESS_KEY,
  fetchImpl = fetch,
  timeoutMs = UPSTREAM_TIMEOUT_MS,
  allowHttp = false,
} = {}) {
  if (!baseURL) return fail(503, 'The prototype API connection is not configured');
  if (!accessKey?.trim()) return fail(503, 'The prototype server credential is not configured');
  const origin = request.headers.get('origin');
  if (origin && origin !== new URL(request.url).origin) return fail(403, 'Origin not allowed');
  let target;
  try {
    target = new URL(path, baseURL);
  } catch {
    return fail(503, 'The prototype API connection is invalid');
  }
  // Plain http is for a private Compose network only; the hosted Worker never enables it.
  if (target.protocol !== 'https:' && !(allowHttp && target.protocol === 'http:')) {
    return fail(503, 'The prototype API connection is invalid');
  }
  // One deadline covers reading the body and the upstream call; the client going away ends both.
  const signal = AbortSignal.any([request.signal, AbortSignal.timeout(timeoutMs)]);
  try {
    let body;
    if (request.method === 'POST') {
      body = await readBounded(request, BODY_LIMIT_BYTES, signal);
      if (body === null) return fail(413, 'Request is too large');
    }
    // The public path uses a deployment secret, never a browser-supplied key; no browser header is forwarded.
    const response = await fetchImpl(target.toString(), {
      method: request.method,
      body,
      headers: {'Content-Type': 'application/json', Authorization: `Bearer ${accessKey.trim()}`},
      signal,
      // Workers reject `redirect: 'error'`; 'manual' hands back the 3xx, which is refused below so the
      // key is never replayed to a redirect target.
      redirect: 'manual',
    });
    const contentType = response.headers.get('content-type') || '';
    const redirected = response.status >= 300 && response.status < 400;
    if (redirected || (!contentType.includes('application/json') && !contentType.includes('text/event-stream'))) {
      discard(response.body);
      console.error('chat proxy: upstream response refused', {status: response.status, contentType});
      return fail(502, 'The prototype API connection is unavailable');
    }
    return new Response(response.body, {
      status: response.status,
      headers: {'Content-Type': contentType, 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'},
    });
  } catch (error) {
    // Server-side only: the client still gets the fixed message, operators get the cause in the service log.
    console.error('chat proxy: upstream request failed', {name: error?.name, message: error?.message});
    return fail(502, 'The prototype API connection is unavailable');
  }
}
