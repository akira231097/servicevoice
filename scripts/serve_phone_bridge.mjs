// Opt-in standalone process. No provider number/assistant mutations occur here.
import fs from 'node:fs/promises';
import { parseArgs, parseEnv } from 'node:util';
import { createPhoneBridge } from '../integrations/phone-bridge/server.mjs';
if (process.platform !== 'linux') throw new Error('phone_bridge_requires_reviewed_linux_host');

const { values } = parseArgs({ options: { 'env-file': { type: 'string' }, 'http-socket': { type: 'string' }, 'ownership-file': { type: 'string' }, 'console-url': { type: 'string', default: 'http://127.0.0.1:8785' } } });
if (!values['env-file']) throw new Error('phone_env_file_required');
if (!values['ownership-file']) throw new Error('phone_ownership_file_required');
const env = parseEnv(await fs.readFile(values['env-file'], 'utf8'));
if (env.SERVICEVOICE_PHONE_BRIDGE_ADMISSION && !['on', 'off'].includes(env.SERVICEVOICE_PHONE_BRIDGE_ADMISSION)) throw new Error('phone_admission_flag');
if (env.SERVICEVOICE_PHONE_BRIDGE_ADMISSION === 'on'
  && (!env.SERVICEVOICE_PHONE_BRIDGE_ADMISSION_URL || !env.SERVICEVOICE_PHONE_BRIDGE_CONTROL_SECRET)) throw new Error('phone_admission_configuration');
const bridge = createPhoneBridge({ enabled: env.SERVICEVOICE_PHONE_BRIDGE === 'on',
  webhookSecret: env.VAPI_PHONE_WEBHOOK_SECRET, vapiKey: env.VAPI_API_KEY,
  organizationId: env.VAPI_PHONE_ORG_ID, phoneNumberId: env.VAPI_PHONE_NUMBER_ID,
  assistantId: env.VAPI_BACKCHANNEL_ASSISTANT_ID, ownershipFile: values['ownership-file'],
  sipHost: env.SERVICEVOICE_PHONE_SIP_HOST, sipPort: 5061, consoleUrl: values['console-url'],
  audioSampleRate: 8000, capacity: 1,
  httpSocketPath: values['http-socket'],
  ...(env.SERVICEVOICE_PHONE_BRIDGE_ADMISSION === 'on' ? {
    admissionUrl: env.SERVICEVOICE_PHONE_BRIDGE_ADMISSION_URL,
    admissionSecret: env.SERVICEVOICE_PHONE_BRIDGE_CONTROL_SECRET,
  } : {}),
}, { onEvent: event => {
  // Events contain only state, aggregate audio geometry and hashed call linkage.
  process.stdout.write(JSON.stringify(event) + '\n');
} });
await bridge.listen();
process.stdout.write(JSON.stringify({ kind: 'phone_bridge_listening', http_transport: values['http-socket'] ? 'unix' : 'loopback_tcp',
  ...(values['http-socket'] ? {} : { http_host: '127.0.0.1', http_port: 8790 }),
  media_host: '127.0.0.1', media_port: 9019, saved_phone_routing_modified: false }) + '\n');
let closing = false;
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, async () => {
  if (closing) return; closing = true;
  process.stdout.write(JSON.stringify({ kind: 'phone_bridge_closed', ...await bridge.close() }) + '\n');
});
