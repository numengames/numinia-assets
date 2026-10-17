// SPDX-FileCopyrightText: 2026 Numen Games S.L.
// SPDX-License-Identifier: MIT
// End-to-end: a PinWorld signed with viem (throwaway key) -> Caddy (the Caddyfile reconcile.door_site renders)
// -> the real fleetd.py on a unix socket -> one real reconciler pass -> the pin is on the world's order.
// Not part of `unittest discover`: needs node + viem + a caddy binary. Paths come from the environment:
//   VIEM_DIR (…/node_modules/viem/_esm)  CADDY (binary)  PY (python with ecdsa)  MACHINE_DIR (open-worlds/machine)
import { spawn, spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const { VIEM_DIR, CADDY, PY, MACHINE_DIR } = process.env;
const { hashTypedData, recoverTypedDataAddress } = await import(`${VIEM_DIR}/index.js`);
const { generatePrivateKey, privateKeyToAccount } = await import(`${VIEM_DIR}/accounts/index.js`);
const vectors = JSON.parse(readFileSync(join(here, '..', 'fleet_vectors.json'), 'utf8'));

const root = mkdtempSync(join(process.env.TMPDIR ?? tmpdir(), 'pin-'));
const PORT = '18481', url = `http://127.0.0.1:${PORT}`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let fails = 0;
const check = (name, ok, extra = '') => { console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra ? '  — ' + extra : ''}`); if (!ok) fails++; };

const account = privateKeyToAccount(generatePrivateKey());   // throwaway
const stranger = privateKeyToAccount(generatePrivateKey());
mkdirSync(join(root, 'etc'), { recursive: true });
writeFileSync(join(root, 'etc', 'signers'), account.address.toLowerCase() + '\n');

const BUILD_A = 'ghcr.io/numengames/numinia-engine:sha-c57dbb7cc4c51b0834184f1dd47b311e47fde234';
const BUILD_B = 'ghcr.io/numengames/numinia-engine@sha256:' + 'b'.repeat(64);
const orderFile = join(root, 'srv', 'orders', 'plaza.json');
const seed = () => {
  mkdirSync(join(root, 'srv', 'orders'), { recursive: true });
  writeFileSync(orderFile, JSON.stringify({ id: 'plaza', card: 'plaza', state: 'stopped', server: 'test-1', domain: 'plaza.numinia.com',
    image: BUILD_A, limits: { memory: '2g', cpus: '1.5', maxUploadMb: 50 } }));
};
const order = () => JSON.parse(readFileSync(orderFile, 'utf8'));

const sign = async (acct, message) => {
  const types = { PinWorld: vectors.types.PinWorld };
  const typed = { domain: vectors.domain, types, primaryType: 'PinWorld',
    message: Object.fromEntries(types.PinWorld.map(({ name, type }) => [name, type === 'uint256' ? BigInt(message[name]) : message[name]])) };
  const signature = await acct.signTypedData(typed);
  if ((await recoverTypedDataAddress({ ...typed, signature })).toLowerCase() !== acct.address.toLowerCase()) throw new Error('recover');
  return signature;
};
let n = 0;
const pin = async (acct, image, reason) => {
  const message = { machine: 'test-1', world: 'plaza', image, reason, nonce: (++n).toString(16).padStart(32, '0'), expires: Math.floor(Date.now() / 1000) + 600 };
  const r = await fetch(`${url}/fleet/v1/actions`, { method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ type: 'PinWorld', message, signature: await sign(acct, message) }) });
  return { status: r.status, body: await r.json(), message };
};
const get = async (p) => (await fetch(`${url}${p}`)).json();
const reconcile = () => JSON.parse(spawnSync(PY, [join(here, 'take_requests.py')], { env: { ...process.env, MACHINE_DIR, DOOR_ROOT: root }, encoding: 'utf8' }).stdout.trim() || '[]');

let procs = [];
try {
  seed();
  const door = spawn(PY, [join(here, 'run_door.py')], { env: { ...process.env, MACHINE_DIR, DOOR_ROOT: root, DOOR_PORT: PORT }, stdio: ['ignore', 'pipe', 'inherit'] });
  await new Promise((res) => door.stdout.on('data', (d) => String(d).includes('door up') && res()));
  const caddy = spawn(CADDY, ['run', '--config', join(root, 'Caddyfile'), '--adapter', 'caddyfile'], { stdio: 'ignore' });
  procs = [door, caddy];
  for (let i = 0; i < 40; i++) { try { if ((await fetch(`${url}/fleet/v1/machine`)).ok) break; } catch {} await sleep(250); }

  const machine = await get('/fleet/v1/machine');
  check('door advertises listens/pin and a release block', machine.listens?.pin === true && machine.release && machine.release.halted === false && machine.release.target === null, JSON.stringify(machine.release));
  check('old fields still there', ['machine', 'door', 'engines', 'updates', 'imports'].every((k) => k in machine));
  check('world starts without pin', !('pin' in (await get('/fleet/v1/worlds')).worlds[0]));

  let r = await pin(stranger, BUILD_B, 'not an oracle');
  check('stranger refused 403 not-an-oracle-here', r.status === 403 && r.body.error === 'not-an-oracle-here', JSON.stringify(r.body));

  r = await pin(account, BUILD_B, 'client demo until Friday');
  check('signed PinWorld accepted (202) through Caddy', r.status === 202, JSON.stringify(r.body));
  check('request sits in the inbox as PinWorld with the Oracle as signer', readdirSync(join(root, 'srv', 'inbox')).length === 1
    && JSON.parse(readFileSync(join(root, 'srv', 'inbox', readdirSync(join(root, 'srv', 'inbox'))[0]), 'utf8')).signer.toLowerCase() === account.address.toLowerCase());
  const busy = await pin(account, BUILD_A, 'again');
  check('second request while one waits: busy (409)', busy.status === 409 && busy.body.error === 'busy', JSON.stringify(busy.body));

  const taken = reconcile();
  check('reconciler took the PinWorld', taken.length === 1 && taken[0].type === 'PinWorld', JSON.stringify(taken));
  const o = order();
  check('the order holds the pin and the pinned image', o.pin?.image === BUILD_B && o.pin?.reason === 'client demo until Friday' && o.image === BUILD_B, JSON.stringify(o.pin));
  const w = (await get('/fleet/v1/worlds')).worlds[0];
  check('the door shows the pin on /worlds (no signer address)', w.pin?.image === BUILD_B && w.pin?.reason === 'client demo until Friday' && !JSON.stringify(w.pin).includes(account.address.toLowerCase()), JSON.stringify(w.pin));
  const done = await get(`/fleet/v1/requests/${r.message.nonce}`);
  check('request answered done', done.state === 'done', JSON.stringify(done));

  r = await pin(account, '', 'demo over');
  check('release (empty image) accepted', r.status === 202, JSON.stringify(r.body));
  reconcile();
  check('the pin is gone from the order, image kept', !('pin' in order()) && order().image === BUILD_B);
  check('the door no longer shows a pin', !('pin' in (await get('/fleet/v1/worlds')).worlds[0]));

  r = await pin(account, 'docker.io/library/nginx:latest', 'outside the house');
  check('image outside the house registry refused (400)', r.status === 400 && r.body.error === 'invalid', JSON.stringify(r.body));
} catch (e) { console.log('ERROR', e); fails++; }
finally { for (const p of procs) p.kill(); }
console.log(fails ? `${fails} FAILED` : 'ALL PASS');
process.exit(fails ? 1 : 0);
