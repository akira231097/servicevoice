// Opt-in standalone process. No provider number/assistant mutations occur here.
import fs from 'node:fs/promises';
import { parseArgs, parseEnv } from 'node:util';
import { createPhoneBridge } from '../integrations/phone-bridge/server.mjs';

const { values } = parseArgs({ options: { 'env-file': { type: 'string' }, 'console-url': { type: 'string', default: 'http://127.0.0.1:8785' } } });
if (!values['env-file']) throw new Error('phone_env_file_required');
const env = parseEnv(await fs.readFile(values['env-file'], 'utf8'));
const bridge = createPhoneBridge({ enabled: env.SERVICEVOICE_PHONE_BRIDGE === 'on',
  webhookSecret: env.VAPI_PHONE_WEBHOOK_SECRET, vapiKey: env.VAPI_API_KEY,
  organizationId: env.VAPI_PHONE_ORG_ID, phoneNumberId: env.VAPI_PHONE_NUMBER_ID,
  sipHost: env.SERVICEVOICE_PHONE_SIP_HOST, sipPort: 5061, consoleUrl: values['console-url'],
  audioSampleRate: 8000, capacity: 1,
}, { onEvent: event => {
  // Events contain only state, aggregate audio geometry and hashed call linkage.
  process.stdout.write(JSON.stringify(event) + '\n');
} });
await bridge.listen();
process.stdout.write(JSON.stringify({ kind: 'phone_bridge_listening', http_host: '127.0.0.1', http_port: 8790,
  media_host: '127.0.0.1', media_port: 9019, saved_phone_routing_modified: false }) + '\n');
let closing = false;
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, async () => {
  if (closing) return; closing = true; await bridge.close();
});
