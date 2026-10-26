// SPDX-FileCopyrightText: 2026 Numen Games S.L.
// SPDX-License-Identifier: MIT
// End-to-end: tonight's copy of a Numinia Engine world can be brought back.
//   1. a small realistic engine world folder (datos/*.json(l) + an uploaded asset, aventura/ from the engine's
//      example adventure, entorno/, and what must never travel: claves.env, sesiones/);
//   2. the REAL nightly.py makes its copy (only `docker run … copia` is swapped for the engine's real `copia`, run
//      with node on the same folder: nightly_native.py);
//   3. the engine's REAL `comprobar` and `importar` take that zip, and the files come back byte for byte (sha256);
//   4. numinia-web's REAL `inspectZip` (the room's «Bring a world back» check) answers ok, engine numinia-engine;
//   5. the contrast: a Hyperfy-shaped zip (what nightly.py used to make for every world) is refused by both.
// Not part of `unittest discover`: needs node >= 24 (type stripping), python3 and read-only checkouts of
// numinia-engine and numinia-web. Nothing in those checkouts is written.
//   ENGINE_DIR=<numinia-engine> WEB_DIR=<numinia-web> PY=python3 MACHINE_DIR=open-worlds/machine node tests/e2e/nightly_restore_e2e.mjs
// One PASS/FAIL line per behaviour; exits 1 when any FAILs.
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { cpSync, mkdirSync, mkdtempSync, openAsBlob, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const { ENGINE_DIR, WEB_DIR, PY = 'python3', MACHINE_DIR = join(here, '..', '..', 'open-worlds', 'machine') } = process.env;
if (!ENGINE_DIR || !WEB_DIR) { console.error('set ENGINE_DIR and WEB_DIR'); process.exit(2); }
const NODE = process.execPath;
const entry = join(ENGINE_DIR, 'packages', 'server', 'src', 'entrada.ts');
const root = mkdtempSync(join(process.env.TMPDIR ?? tmpdir(), 'nightly-restore-'));
let fails = 0;
const check = (name, ok, extra = '') => { console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra ? '  — ' + extra : ''}`); if (!ok) fails++; };
const sha = (b) => createHash('sha256').update(b).digest('hex');
const run = (cmd, args, opts = {}) => spawnSync(cmd, args, { encoding: 'buffer', ...opts });
const text = (b) => (b ? b.toString('utf8').trim() : '');

// 1. the world
const home = join(root, 'srv');
const world = join(home, 'data', 'plaza');
mkdirSync(join(home, 'orders'), { recursive: true });
mkdirSync(join(world, 'datos', 'activos'), { recursive: true });
mkdirSync(join(world, 'entorno'));
mkdirSync(join(world, 'sesiones'));
cpSync(join(ENGINE_DIR, 'aventuras', 'ejemplo'), join(world, 'aventura'), { recursive: true });
const glb = readFileSync(join(ENGINE_DIR, 'activos', 'casa', 'bloque.glb'));
writeFileSync(join(world, 'datos', 'activos', `${sha(glb)}.glb`), glb);
writeFileSync(join(world, 'datos', 'objetos.json'), JSON.stringify({ schema: 'numinia-engine/datos@1', feature: 'objetos', version: 3, state: [['o1', { value: { x: 1 } }]], store: [] }));
writeFileSync(join(world, 'datos', 'objetos.jsonl'), '{"op":"s","key":"o2","value":{"x":2}}\n');
writeFileSync(join(world, 'datos', 'misiones.jsonl'), '');
writeFileSync(join(world, 'entorno', 'entorno.json'), JSON.stringify({ cielo: 'alba' }));
writeFileSync(join(world, 'claves.env'), 'MOTOR_ADMIN=never-in-a-copy-0123456789\n');
writeFileSync(join(world, 'sesiones', '2027-01.jsonl'), '{"quien":"visitor-sentinel"}\n');
const IMAGE = 'ghcr.io/numengames/numinia-engine:sha-0000000000000000000000000000000000000001';
const HYPERFY = 'ghcr.io/numengames/numinia-hyperfy2:sha-0e2045370989d051e73f81c7c9d53121d5c374b3';
const order = (id, image) => ({ id, state: 'running', domain: `${id}.example.org`, image, limits: { memory: '1g', cpus: '1', maxUploadMb: 50 } });
writeFileSync(join(home, 'orders', 'plaza.json'), JSON.stringify(order('plaza', IMAGE)));
const files = (dir, base = dir) => readdirSync(dir, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? files(join(dir, e.name), base) : [relative(base, join(dir, e.name))])).sort();
const travelling = (f) => !f.startsWith('sesiones/') && f !== 'claves.env';
const source = files(world).filter(travelling);
check('the world folder is built', source.length > 10, `${source.length} files that should travel`);

// 2. the real nightly pass
const env = { ...process.env, MACHINE_DIR, ENGINE_DIR, NODE };
const night = run(PY, [join(here, 'nightly_native.py'), home], { env });
const summary = JSON.parse(text(night.stdout) || '{}');
check('nightly.py exits 0 and says copied', night.status === 0 && /^copied: plaza-/.test(summary.worlds?.plaza ?? ''), `${summary.worlds?.plaza ?? text(night.stderr)}`);
const folder = join(home, 'nightly', 'plaza');
const names = readdirSync(folder);
const zip = names.find((n) => n.endsWith('.zip'));
const side = names.find((n) => n.endsWith('.order.json'));
check('one zip and its order file, nothing else', names.length === 2 && zip && side && side === zip.replace(/\.zip$/, '.order.json'), names.join(', '));
check('both are private (0600)', names.every((n) => (statSync(join(folder, n)).mode & 0o777) === 0o600));
const sidecar = JSON.parse(readFileSync(join(folder, side), 'utf8'));
check('the order file has image, domain and limits and no key', sidecar.order.image === IMAGE && sidecar.order.domain === 'plaza.example.org' && sidecar.order.limits.memory === '1g' && !/MOTOR_|ADMIN|SECRET/.test(JSON.stringify(sidecar)));
const zipPath = join(folder, zip);

// 3. the engine's own comprobar / importar
const verdict = run(NODE, [entry, 'comprobar'], { input: readFileSync(zipPath) });
check('engine `comprobar` accepts the nightly zip', verdict.status === 0, text(verdict.stdout) || text(verdict.stderr));
const back = join(root, 'back');
mkdirSync(back);
const imported = run(NODE, [entry, 'importar'], { input: readFileSync(zipPath), env: { ...process.env, MOTOR_MUNDO: back } });
check('engine `importar` unpacks it into an empty folder', imported.status === 0, text(imported.stdout) || text(imported.stderr));
const restored = files(back).filter((f) => f !== 'mundo.json');
check('the same files come back', JSON.stringify(restored) === JSON.stringify(source), `${restored.length} files`);
const differ = source.filter((f) => sha(readFileSync(join(world, f))) !== sha(readFileSync(join(back, f))));
check('every file is byte for byte the same (sha256)', differ.length === 0, differ.join(', ') || `${source.length} files`);
check('no keys and no sessions came back', !restored.some((f) => f.startsWith('sesiones/') || f === 'claves.env'));

// 4. the room's own check (numinia-web's inspectZip)
const { inspectZip } = await import(pathToFileURL(resolve(WEB_DIR, 'apps/store/src/lib/zip-read.ts')).href);
const seen = await inspectZip(await openAsBlob(zipPath));
console.log(`      inspectZip -> ${JSON.stringify(seen)}`);
check("the room's inspectZip: ok, engine numinia-engine, schema @2", seen.ok === true && seen.reading.engine === 'numinia-engine' && seen.reading.schema === 'numinia-engine/mundo@2');

// 5. the contrast: the old shape (a Hyperfy order on the same folder) is not a world to either
writeFileSync(join(home, 'orders', 'plaza.json'), JSON.stringify(order('plaza', HYPERFY)));
mkdirSync(join(home, 'data', 'viejo'));
writeFileSync(join(home, 'orders', 'viejo.json'), JSON.stringify(order('viejo', HYPERFY)));
cpSync(world, join(home, 'data', 'viejo'), { recursive: true });
rmSync(join(home, 'orders', 'plaza.json'));
run(PY, [join(here, 'nightly_native.py'), home], { env });
const oldZip = join(home, 'nightly', 'viejo', readdirSync(join(home, 'nightly', 'viejo')).find((n) => n.endsWith('.zip')));
const oldVerdict = run(NODE, [entry, 'comprobar'], { input: readFileSync(oldZip) });
check('contrast: engine `comprobar` refuses the old Hyperfy-shaped zip', oldVerdict.status !== 0, text(oldVerdict.stderr));
const oldSeen = await inspectZip(await openAsBlob(oldZip));
check("contrast: the room's inspectZip refuses it as well (no world)", oldSeen.ok === false, JSON.stringify(oldSeen));

rmSync(root, { recursive: true, force: true });
console.log(fails ? `\n${fails} FAILED` : '\nall PASS');
process.exit(fails ? 1 : 0);
