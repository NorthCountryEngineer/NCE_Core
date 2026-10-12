import http from 'node:http';
import { readFileSync } from 'node:fs';
import { createHandler, validateConfig } from './proxy.mjs';
import { createSecretReader } from './secret.mjs';

const config = validateConfig(JSON.parse(readFileSync(new URL('./config.json', import.meta.url), 'utf8')));
http.createServer(createHandler(config, createSecretReader(config))).listen(3000, '0.0.0.0');
