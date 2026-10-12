import test from 'node:test';
import assert from 'node:assert/strict';
import { PassThrough } from 'node:stream';
import { EventEmitter } from 'node:events';
import { createHandler, validateConfig } from './proxy.mjs';
import { createSecretReader } from './secret.mjs';

const config = { publicOrigin: 'https://main.example.amplifyapp.com', backendOrigin: 'https://core-origin.axolotl.news',
  region: 'us-east-1', secretArn: 'arn:aws:secretsmanager:us-east-1:123456789012:secret:origin-test' };

test('configuration requires TLS and forbids credentials, URL paths and loops', () => {
  assert.equal(validateConfig({ ...config }).publicOrigin, config.publicOrigin);
  for (const backendOrigin of ['http://origin.test', 'https://user:pass@origin.test', 'https://origin.test/oauth/', config.publicOrigin])
    assert.throws(() => validateConfig({ ...config, backendOrigin }));
});

test('POST body, cookies, CSRF Origin and OPTIONS survive; forwarding/secret spoofing does not', async () => {
  for (const method of ['POST', 'OPTIONS']) {
    const request = new PassThrough();
    request.method = method;
    request.url = '/oauth/token/?x=1';
    request.headers = { host: 'untrusted.example', origin: 'https://axolotl.news', cookie: 'sessionid=abc; csrftoken=xyz',
      'content-type': 'application/x-www-form-urlencoded', authorization: 'Basic test',
      'x-forwarded-host': 'evil.example', 'x-forwarded-proto': 'http', forwarded: 'host=evil.example',
      'x-core-origin-secret': 'attacker', connection: 'x-injected', 'x-injected': 'evil' };
    const response = new PassThrough();
    let returned;
    response.writeHead = (status, headers) => { returned = { status, headers }; response.headersSent = true; };
    let outgoing;
    let body = '';
    const transport = (options, receive) => {
      outgoing = options;
      const upstream = new PassThrough();
      upstream.on('data', chunk => { body += chunk; });
      upstream.on('finish', () => {
        const incoming = new PassThrough(); incoming.statusCode = 200;
        incoming.headers = { 'set-cookie': ['sessionid=next; Secure; HttpOnly', 'csrftoken=next; Secure'],
          'access-control-allow-origin': 'https://axolotl.news', 'cache-control': 'public, max-age=999' };
        receive(incoming); incoming.end('ok');
      });
      return upstream;
    };
    const completed = new Promise(resolve => response.on('finish', resolve));
    await createHandler(config, async () => 's'.repeat(64), transport)(request, response);
    request.end('grant_type=authorization_code&code=sensitive');
    await completed;
    assert.equal(outgoing.hostname, 'core-origin.axolotl.news');
    assert.equal(outgoing.servername, 'core-origin.axolotl.news');
    assert.equal(outgoing.path, '/oauth/token/?x=1');
    assert.equal(outgoing.method, method);
    assert.equal(outgoing.headers.host, 'main.example.amplifyapp.com');
    assert.equal(outgoing.headers.cookie, request.headers.cookie);
    assert.equal(outgoing.headers.origin, 'https://axolotl.news');
    assert.equal(outgoing.headers.authorization, 'Basic test');
    assert.equal(outgoing.headers['x-core-origin-secret'], 's'.repeat(64));
    assert.equal(outgoing.headers['x-forwarded-proto'], 'https');
    assert.equal(outgoing.headers['x-forwarded-host'], undefined);
    assert.equal(outgoing.headers.forwarded, undefined);
    assert.equal(outgoing.headers['x-injected'], undefined);
    assert.equal(body, 'grant_type=authorization_code&code=sensitive');
    assert.equal(returned.headers['cache-control'], 'private, no-store');
    assert.equal(returned.headers['set-cookie'].length, 2);
    assert.equal(returned.headers['access-control-allow-origin'], 'https://axolotl.news');
  }
});

test('absolute URLs are rejected before secret retrieval or transport', async () => {
  const request = new EventEmitter(); request.url = 'https://evil.test/'; request.headers = {};
  let status;
  await createHandler(config, () => assert.fail('secret should not be fetched'), () => assert.fail('must not connect'))(request,
    { writeHead: value => { status = value; }, end() {} });
  assert.equal(status, 400);
});

test('Secrets Manager request is role-signed and caches only in memory', async () => {
  let count = 0;
  const read = createSecretReader({ ...config, credentials: async () => ({ accessKeyId: 'LOCALTEST', secretAccessKey: 'local-only', sessionToken: 'temporary-test' }),
    fetchImpl: async (url, options) => {
      count++; assert.equal(url, 'https://secretsmanager.us-east-1.amazonaws.com/');
      assert.match(options.headers.authorization, /^AWS4-HMAC-SHA256 /);
      assert.equal(options.headers['x-amz-security-token'], 'temporary-test');
      assert.equal(JSON.parse(options.body).SecretId, config.secretArn);
      return { ok: true, json: async () => ({ SecretString: 's'.repeat(64) }) };
    } });
  assert.equal(await read(), 's'.repeat(64));
  assert.equal(await read(), 's'.repeat(64));
  assert.equal(count, 1);
});
