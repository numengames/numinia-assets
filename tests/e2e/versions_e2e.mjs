// SPDX-FileCopyrightText: 2026 Numen Games S.L.
// SPDX-License-Identifier: MIT
// End-to-end: the five version requests, signed with viem (throwaway key) -> the real fleetd.py on a unix
// socket -> real reconciler runs (reconcile.run_once) on a temp fleet home -> what /fleet/v1/worlds shows.
// A world `plaza` (Hyperfy, real SQLite database, assets, storage.json) goes through: duplicate, a busy clash,
// publish (the switching phase and the swap), download, import into a slot, delete.
// What is faked: Docker only (a container set that `docker compose up` makes equal to the compose file, and
// `container gone?` answered from it; see versions_world.py). Caddy is not in this loop: the script talks to
// the door's socket itself. Not part of `unittest discover`: needs node + viem and a python with ecdsa.
// Paths come from the environment:  VIEM_DIR (…/node_modules/viem/_esm)  PY (python with ecdsa)  MACHINE_DIR (open-worlds/machine)
//   VIEM_DIR=<numinia-web>/node_modules/viem/_esm PY=python3 MACHINE_DIR=open-worlds/machine node tests/e2e/versions_e2e.mjs
// One PASS/FAIL line per behaviour; exits 1 when any FAILs.
import { spawn, spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { request } from 'node:http';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const { VIEM_DIR, PY, MACHINE_DIR } = process.env;
const { recoverTypedDataAddress } = await import(`${VIEM_DIR}/index.js`);
const { generatePrivateKey, privateKeyToAccount } = await import(`${VIEM_DIR}/accounts/index.js`);
const vectors = JSON.parse(readFileSync(join(here, '..', 'fleet_vectors.json'), 'utf8'));

const root = mkdtempSync(join(process.env.TMPDIR ?? tmpdir(), 'versions-'));
const sock = join(root, 'fleetd.sock');
const env = { ...process.env, MACHINE_DIR, DOOR_ROOT: root, DOOR_PORT: '0' };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let fails = 0;
const check = (name, ok, extra = '') => { console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra ? '  — ' + extra : ''}`); if (!ok) fails++; };
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const oracle = privateKeyToAccount(generatePrivateKey());   // throwaway, never written anywhere
const stranger = privateKeyToAccount(generatePrivateKey());

// --- the door, over its socket
const call = (method, path, body = null, headers = {}) => new Promise((resolve, reject) => {
  const data = body === null ? null : Buffer.isBuffer(body) ? body : Buffer.from(body);
  const r = request({ socketPath: sock, method, path, headers: { ...(data ? { 'content-length': data.length } : {}), ...headers } }, (res) => {
    const chunks = [];
    res.on('data', (c) => chunks.push(c));
    res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, raw: Buffer.concat(chunks) }));
  });
  r.on('error', reject);
  if (data) r.write(data);
  r.end();
});
const json = (res) => JSON.parse(res.raw.toString('utf8'));
const get = async (p) => json(await call('GET', `/fleet/v1${p}`));

// --- signed requests
let n = 0;
const nonce = () => (++n).toString(16).padStart(32, '0');
const sign = async (acct, type, message) => {
  const types = { [type]: vectors.types[type] };
  const typed = { domain: vectors.domain, types, primaryType: type,
    message: Object.fromEntries(types[type].map(({ name, type: t }) => [name, t === 'uint256' ? BigInt(message[name]) : message[name]])) };
  const signature = await acct.signTypedData(typed);
  if ((await recoverTypedDataAddress({ ...typed, signature })).toLowerCase() !== acct.address.toLowerCase()) throw new Error('recover');
  return signature;
};
const send = async (type, fields, acct = oracle) => {
  const message = { machine: 'test-1', ...fields, nonce: nonce(), expires: Math.floor(Date.now() / 1000) + 600 };
  const res = await call('POST', '/fleet/v1/actions', JSON.stringify({ type, message, signature: await sign(acct, type, message) }),
    { 'content-type': 'application/json' });
  return { status: res.status, body: json(res), message };
};
const version = (type, v) => send(type, { world: 'plaza', version: v });

// --- the machine's side (python): one real run of the reconciler, the fixture world, a look at the disk
const py = (...args) => {
  const r = spawnSync(PY, [join(here, 'versions_world.py'), ...args], { env, encoding: 'utf8' });
  if (r.status !== 0) throw new Error(`versions_world.py ${args.join(' ')} failed:\n${r.stderr}`);
  return JSON.parse(r.stdout.trim().split('\n').pop());
};
const run = () => py('run');
const world = async () => (await get('/worlds')).worlds.find((w) => w.id === 'plaza');
const ids = (w) => w.versions.map((v) => v.id);
const answer = (nonceValue) => get(`/requests/${nonceValue}`);

let door;
try {
  door = spawn(PY, [join(here, 'run_door.py')], { env, stdio: ['ignore', 'pipe', 'ignore'] });
  await new Promise((res) => door.stdout.on('data', (d) => String(d).includes('door up') && res()));
  writeFileSync(join(root, 'etc', 'signers'), oracle.address.toLowerCase() + '\n');
  py('seed');
  const first = run();
  check('the fake Docker brought the seeded world up', same(first.containers, ['plaza']), JSON.stringify(first));

  // --- what the door says before any version operation
  const machine = await get('/machine');
  check('the door advertises versions.max = 2', machine.versions?.max === 2, JSON.stringify(machine.versions));
  let w = await world();
  check('a world without a versions file shows one implicit version v1, published, origin created',
    w.versions.length === 1 && w.versions[0].id === 'v1' && w.versions[0].published === true && w.versions[0].origin === 'created' && w.versions[0].bytes === null,
    JSON.stringify(w.versions));
  check('the world is running and has no version coming', w.phase === 'running' && !('versionComing' in w));

  // a stranger is refused before anything is written
  let r = await send('DuplicateVersion', { world: 'plaza', version: 'v1' }, stranger);
  check('DuplicateVersion signed by a stranger: 403 not-an-oracle-here', r.status === 403 && r.body.error === 'not-an-oracle-here', JSON.stringify(r.body));

  // --- 1. DuplicateVersion: the published, running Hyperfy world is copied into v2
  const before = py('look');
  r = await version('DuplicateVersion', 'v1');
  check('DuplicateVersion v1 signed by an Oracle: 202', r.status === 202 && r.body.request === r.message.nonce, JSON.stringify(r.body));
  w = await world();
  check('while it waits the world shows versionComing duplicate/waiting and keeps its phase',
    same(w.versionComing, { kind: 'duplicate', state: 'waiting' }) && w.phase === 'running', JSON.stringify(w.versionComing));
  let ran = run();
  let done = await answer(r.message.nonce);
  check('the reconciler took it: request done', done.state === 'done', JSON.stringify(done));
  check('the published world was given its save pause first (sleep faked)', ran.slept.length > 0 && ran.slept.every((s) => s > 0), JSON.stringify(ran.slept));
  w = await world();
  check('/fleet/v1/worlds shows two versions: v1 published, v2 copied from v1',
    same(ids(w), ['v1', 'v2']) && w.versions[0].published === true && w.versions[1].published === false && w.versions[1].origin === 'copy:v1'
    && w.versions[1].bytes > 0 && !('versionComing' in w), JSON.stringify(w.versions));
  let disk = py('look');
  check('v2 holds its own copy of the data (storage.json, db.sqlite), the big assets hardlinked, data/plaza untouched',
    same(disk.versions, ['v2']) && disk.versionStorage.v2 === '{"from": "created"}' && disk.assetLinked.v2 === true
    && same(disk.data, before.data) && disk.inoData === before.inoData, JSON.stringify({ versions: disk.versions, linked: disk.assetLinked }));
  check('the world kept running, with the same order and keys', same(ran.containers, ['plaza']) && same(disk.order, before.order) && disk.env, JSON.stringify(ran.containers));

  r = await version('DuplicateVersion', 'v2');
  check('a third version: the door accepts (202), the reconciler decides', r.status === 202, JSON.stringify(r.body));
  run();
  done = await answer(r.message.nonce);
  check('  … answer failed / full', done.state === 'failed' && done.rule === 'full' && same(ids(await world()), ['v1', 'v2']), JSON.stringify(done));

  // --- 2. PublishVersion v2: busy clashes, the switching phase, the swap
  r = await version('PublishVersion', 'v2');
  check('PublishVersion v2 signed by an Oracle: 202, waiting in the inbox', r.status === 202, JSON.stringify(r.body));
  const publish = r.message.nonce;
  const stop = await send('StopWorld', { world: 'plaza' });
  check('StopWorld while a PublishVersion waits: 409 busy', stop.status === 409 && stop.body.error === 'busy', JSON.stringify(stop.body));
  const delWaiting = await version('DeleteVersion', 'v2');
  check('DeleteVersion while a PublishVersion waits: 409 busy', delWaiting.status === 409 && delWaiting.body.error === 'busy', JSON.stringify(delWaiting.body));
  const second = await version('PublishVersion', 'v2');
  check('a second PublishVersion while one waits: 409 busy (the first wins)', second.status === 409 && second.body.error === 'busy', JSON.stringify(second.body));
  const mid = py('look');
  py('hold', 'on');   // Docker cannot take the container down yet
  ran = run();
  w = await world();
  disk = py('look');
  done = await answer(publish);
  check('the run wrote the journal and answered working; the world shows phase switching',
    w.phase === 'switching' && done.state === 'working' && same(Object.keys(disk.journal), ['plaza']) && disk.journal.plaza.to === 'v2' && disk.journal.plaza.from === 'v1',
    JSON.stringify({ phase: w.phase, answer: done.state, journal: disk.journal }));
  check('with the container still there, nothing moved (data/plaza is still the old folder)', disk.inoData === mid.inoData && same(disk.versions, ['v2']) && ran.containers.includes('plaza'));
  const stopSwitching = await send('StopWorld', { world: 'plaza' });
  check('StopWorld while the journal names the world: 409 busy', stopSwitching.status === 409 && stopSwitching.body.error === 'busy', JSON.stringify(stopSwitching.body));
  const publishAgain = await version('PublishVersion', 'v1');
  check('PublishVersion while the journal names the world: 409 busy', publishAgain.status === 409 && publishAgain.body.error === 'busy', JSON.stringify(publishAgain.body));

  py('hold', 'off');   // Docker lets the container go: the next run swaps the folders
  ran = run();
  disk = py('look');
  done = await answer(publish);
  w = await world();
  check('the next run (container gone) finished the switch: request done, journal empty', done.state === 'done' && same(disk.journal, {}), JSON.stringify(done));
  check('published flipped: v2 is published, v1 is not', same(ids(w), ['v1', 'v2']) && w.versions.find((v) => v.id === 'v2').published === true && w.versions.find((v) => v.id === 'v1').published === false, JSON.stringify(w.versions));
  check('the folders swapped by inode: data/plaza is the old v2, versions/plaza/v1 the old data/plaza',
    disk.inoData === mid.inoVersions.v2 && disk.inoVersions.v1 === mid.inoData && same(disk.versions, ['v1']), JSON.stringify({ now: disk.inoData, v2was: mid.inoVersions.v2 }));
  check('the world came back by itself, same address and keys, phase running',
    w.phase === 'running' && same(ran.containers, ['plaza']) && ran.ups >= 1 && disk.order.domain === 'plaza.numinia.com' && disk.env && same(disk.order, before.order), JSON.stringify({ phase: w.phase, containers: ran.containers }));

  // --- 3. DownloadVersion of the unpublished one (v1 now)
  r = await version('DownloadVersion', 'v1');
  check('DownloadVersion v1: 202 with a copy id', r.status === 202 && /^[0-9a-f]{32}$/.test(r.body.copy ?? ''), JSON.stringify(r.body));
  run();
  const copy = await get(`/copies/${r.body.copy}`);
  check('the copy is ready and its file name has -v1-', copy.state === 'ready' && /^plaza-v1-.+\.zip$/.test(copy.filename ?? ''), JSON.stringify(copy));
  const file = await call('GET', `/fleet/v1/copies/${r.body.copy}/file`);
  check('the file is served as an attachment named -v1-, a zip', file.status === 200 && /attachment; filename="plaza-v1-/.test(file.headers['content-disposition'] ?? '') && file.raw.subarray(0, 2).toString() === 'PK', `${file.status} ${file.headers['content-disposition']} ${file.raw.length} bytes`);

  // --- 4. ImportVersion: a real small Hyperfy zip through the real PUT slot, replacing the unpublished v1
  const zipPath = join(root, 'upload.zip');
  const zip = py('zip', zipPath);
  r = await send('ImportVersion', { world: 'plaza', zipBytes: zip.bytes, zipSha256: zip.sha256, replace: 'v1' });
  check('ImportVersion (replace v1): 202 with an upload slot', r.status === 202 && r.body.upload === `uploads/${r.message.nonce}`, JSON.stringify(r.body));
  const importNonce = r.message.nonce;
  w = await world();
  check('the slot shows versionComing import/uploading, the world still running and still two versions',
    w.versionComing?.kind === 'import' && w.versionComing?.state === 'uploading' && w.versionComing?.of === zip.bytes && w.phase === 'running' && same(ids(w), ['v1', 'v2']), JSON.stringify(w.versionComing));
  const wrong = await call('PUT', `/fleet/v1/uploads/${importNonce}`, Buffer.from('not the zip'));
  check('a body of the wrong size is refused (400 wrong-size)', wrong.status === 400 && json(wrong).error === 'wrong-size', `${wrong.status} ${wrong.raw.toString().trim()}`);
  const put = await call('PUT', `/fleet/v1/uploads/${importNonce}`, readFileSync(zipPath));
  check('PUT of the zip into the slot: 202', put.status === 202, `${put.status} ${put.raw.toString().trim()}`);
  w = await world();
  check('the zip is in: versionComing import/waiting', w.versionComing?.kind === 'import' && w.versionComing?.state === 'waiting', JSON.stringify(w.versionComing));
  ran = run();
  done = await answer(importNonce);
  w = await world();
  disk = py('look');
  check('the reconciler unpacked it: request done', done.state === 'done', JSON.stringify(done));
  check('versions v2 (published) and v3 (origin zip) — v1 was replaced',
    same(ids(w), ['v2', 'v3']) && w.versions[0].published === true && w.versions[1].origin === 'zip' && w.versions[1].published === false && w.versions[1].bytes > 0, JSON.stringify(w.versions));
  check('v3 holds the uploaded zip\'s data; data/plaza is untouched; the next version number was not reused',
    same(disk.versions, ['v3']) && disk.versionStorage.v3 === '{"from": "zip"}' && disk.inoData === mid.inoVersions.v2 && disk.meta.next === 4, JSON.stringify({ versions: disk.versions, next: disk.meta?.next }));
  check('the live world was never touched: still running, no versionComing, nothing left in uploads/staging',
    w.phase === 'running' && same(ran.containers, ['plaza']) && !('versionComing' in w) && same(disk.uploads, []) && same(disk.staging, []), JSON.stringify({ uploads: disk.uploads, staging: disk.staging }));

  // --- 5. DeleteVersion of the unpublished one
  r = await version('DeleteVersion', 'v3');
  check('DeleteVersion v3: 202', r.status === 202, JSON.stringify(r.body));
  run();
  done = await answer(r.message.nonce);
  w = await world();
  disk = py('look');
  check('v3 is gone: request done, back to one version (v2, published), its folder removed',
    done.state === 'done' && same(ids(w), ['v2']) && w.versions[0].published === true && same(disk.versions, []),
    JSON.stringify({ answer: done.state, versions: w.versions, folders: disk.versions }));
  r = await version('DeleteVersion', 'v2');
  run();
  done = await answer(r.message.nonce);
  check('DeleteVersion of the only (published) version is refused by the reconciler', done.state === 'failed' && ['published', 'only-version'].includes(done.rule) && same(ids(await world()), ['v2']), JSON.stringify(done));
} catch (e) { console.log('ERROR', e); fails++; }
finally { door?.kill(); rmSync(root, { recursive: true, force: true }); }
console.log(fails ? `${fails} FAILED` : 'ALL PASS');
process.exit(fails ? 1 : 0);
