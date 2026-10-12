import { cpSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { validateConfig } from './proxy.mjs';

const config = validateConfig({ publicOrigin: process.env.CORE_PUBLIC_ORIGIN,
  backendOrigin: process.env.CORE_BACKEND_ORIGIN, region: process.env.CORE_AWS_REGION,
  secretArn: process.env.CORE_ORIGIN_SECRET_ARN });
const out = '.amplify-hosting';
rmSync(out, { recursive: true, force: true });
mkdirSync(`${out}/compute/default`, { recursive: true });
mkdirSync(`${out}/static`, { recursive: true });
for (const file of ['server.mjs', 'proxy.mjs', 'secret.mjs', 'package.json'])
  cpSync(file, `${out}/compute/default/${file}`);
cpSync('node_modules', `${out}/compute/default/node_modules`, { recursive: true });
writeFileSync(`${out}/compute/default/config.json`, JSON.stringify(config));
writeFileSync(`${out}/deploy-manifest.json`, JSON.stringify({ version: 1,
  routes: [{ path: '/*', target: { kind: 'Compute', src: 'default' } }],
  computeResources: [{ name: 'default', runtime: 'nodejs22.x', entrypoint: 'server.mjs' }],
  framework: { name: 'NCE Core proxy', version: '1.0.0' } }, null, 2));
console.log('Built Amplify SSR artifact. Config contains public origins/ARN only.');
