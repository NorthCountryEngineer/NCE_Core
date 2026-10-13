import { mkdir, copyFile, writeFile } from 'node:fs/promises';
const keys = ['VITE_CORE_ISSUER', 'VITE_CORE_BROWSER_CLIENT_ID', 'VITE_APPLICATION_ORIGIN', 'VITE_API_URL'];
const configuration = Object.fromEntries(keys.map(key => [key, process.env[key] || '']));
for (const key of ['VITE_CORE_ISSUER', 'VITE_APPLICATION_ORIGIN', 'VITE_API_URL']) {
  const value = configuration[key];
  if (value && new URL(value).protocol !== 'https:') throw new Error('Public endpoints must use HTTPS.');
}
await mkdir('dist/oauth/callback', { recursive: true });
for (const file of ['index.html', 'app.js', 'style.css']) await copyFile(file, `dist/${file}`);
await copyFile('index.html', 'dist/oauth/callback/index.html');
await writeFile('dist/config.js', `export default ${JSON.stringify(configuration)};\n`);
