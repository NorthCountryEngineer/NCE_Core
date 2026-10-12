import https from 'node:https';

const HOP_HEADERS = new Set(['connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
  'te', 'trailer', 'transfer-encoding', 'upgrade']);

export function validateConfig(config) {
  for (const key of ['publicOrigin', 'backendOrigin']) {
    const url = new URL(config[key]);
    if (url.protocol !== 'https:' || url.username || url.password || url.pathname !== '/' || url.search || url.hash)
      throw new Error(`${key} must be an HTTPS origin`);
    config[key] = url.origin;
  }
  if (config.publicOrigin === config.backendOrigin) throw new Error('Origin would create a proxy loop');
  if (!/^[a-z]{2}-[a-z]+-\d+$/.test(config.region)) throw new Error('Invalid AWS region');
  if (!/^arn:aws:secretsmanager:[a-z0-9-]+:\d{12}:secret:/.test(config.secretArn))
    throw new Error('Origin secret ARN is required');
  if (config.secretArn.split(':')[3] !== config.region) throw new Error('Secret and proxy AWS regions must match');
  return config;
}

export function createHandler(config, readSecret, transport = https.request) {
  const backend = new URL(config.backendOrigin);
  const publicOrigin = new URL(config.publicOrigin);
  return async (request, response) => {
    // Reject absolute proxy targets. The configured origin, not any incoming
    // URL or Host/forwarded header, selects the upstream and public issuer.
    if (!request.url?.startsWith('/') || request.url.startsWith('//') || /[\r\n]/.test(request.url)) {
      response.writeHead(400, { 'cache-control': 'private, no-store' }); return response.end();
    }
    let secret;
    try { secret = await readSecret(); } catch {
      response.writeHead(503, { 'cache-control': 'private, no-store' }); return response.end();
    }
    const blocked = new Set(HOP_HEADERS);
    for (const name of (request.headers.connection || '').split(',')) blocked.add(name.trim().toLowerCase());
    const headers = {};
    for (const [name, value] of Object.entries(request.headers)) {
      if (!blocked.has(name) && !name.startsWith('x-forwarded-') && !name.startsWith('x-core-')
        && name !== 'forwarded' && name !== 'host' && value !== undefined) headers[name] = value;
    }
    // Set a canonical Host for Django's absolute OAuth URLs and CSRF checks.
    // TLS/SNI still uses backend.hostname, and ALB supplies its own secure proto.
    headers.host = publicOrigin.host;
    headers['x-core-origin-secret'] = secret;
    headers['x-forwarded-proto'] = 'https';
    const upstream = transport({ protocol: 'https:', hostname: backend.hostname,
      port: backend.port || 443, servername: backend.hostname, method: request.method,
      path: request.url, headers, timeout: 60000 }, incoming => {
      const resultHeaders = {};
      for (const [name, value] of Object.entries(incoming.headers)) {
        if (!HOP_HEADERS.has(name) && name !== 'cache-control' && value !== undefined) resultHeaders[name] = value;
      }
      // Preserve Set-Cookie arrays and Origin/CORS responses; never share-cache
      // even anonymous login pages carrying a CSRF/session cookie.
      resultHeaders['cache-control'] = 'private, no-store';
      response.writeHead(incoming.statusCode || 502, resultHeaders);
      incoming.pipe(response);
    });
    upstream.on('timeout', () => upstream.destroy(new Error('Origin timeout')));
    upstream.on('error', () => {
      if (!response.headersSent) response.writeHead(502, { 'cache-control': 'private, no-store' });
      response.end();
    });
    request.on('aborted', () => upstream.destroy());
    request.pipe(upstream);
  };
}
