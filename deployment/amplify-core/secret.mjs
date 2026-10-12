import { defaultProvider } from '@aws-sdk/credential-provider-node';
import { SignatureV4 } from '@smithy/signature-v4';
import { HttpRequest } from '@smithy/protocol-http';
import { Sha256 } from '@aws-crypto/sha256-js';

// Use the normal AWS SDK credential chain (Amplify's temporary compute-role
// credentials). No access keys or origin credentials are stored in artifacts.
export function createSecretReader({ region, secretArn, fetchImpl = fetch, credentials = defaultProvider() }) {
  const signer = new SignatureV4({ service: 'secretsmanager', region, credentials, sha256: Sha256 });
  let cached;
  let expires = 0;
  return async function readSecret() {
    if (cached && Date.now() < expires) return cached;
    const hostname = `secretsmanager.${region}.amazonaws.com`;
    const request = await signer.sign(new HttpRequest({ protocol: 'https:', hostname, method: 'POST',
      path: '/', headers: { host: hostname, 'content-type': 'application/x-amz-json-1.1',
        'x-amz-target': 'secretsmanager.GetSecretValue' }, body: JSON.stringify({ SecretId: secretArn }) }));
    const response = await fetchImpl(`https://${hostname}/`, { method: request.method,
      headers: request.headers, body: request.body, signal: AbortSignal.timeout(10000) });
    if (!response.ok) throw new Error('Origin credential unavailable');
    const result = await response.json();
    if (typeof result.SecretString !== 'string' || result.SecretString.length < 32)
      throw new Error('Invalid origin credential');
    cached = result.SecretString;
    expires = Date.now() + 60000;
    return cached;
  };
}
