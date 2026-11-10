// construir.mjs — puts the role-playing room v2 «El Gabinete» (gabinete-rol) on a running engine by the normal protocol, as a page would
// (copied from plantillas/galeria/construir.mjs; changes: the engine is ec-motor-main, the world folder
// comes from MUNDO_DIR, and `mover` also carries rx/rz — the map lies flat on the table):
//   1. copies the models and placeholder pictures into the world's datos/activos/<sha256>.<ext> (what an upload
//      leaves there, H9; the server looks a file up when it is asked for it);
//   2. per object: colocar → mover (scale) → hueco (U12: the placeholders) → ajustar (solid box) → fijar, waiting for
//      the server's answer;
//   3. the braziers' fire (elementos: poner).
// Usage: MUNDO_DIR=<world folder> node construir.mjs [ws://127.0.0.1:8881]   — writes construido.json (which object id is each placeholder).
import { copyFileSync, existsSync, mkdirSync, writeFileSync } from "node:fs";
import { webSocketClient } from "../../ec-motor-main/packages/net/src/index.ts";
import { ClientSession } from "../../ec-motor-main/packages/client/src/index.ts";
import funciones from "../../ec-motor-main/features/cliente.ts";
import { OBJETOS, ELEMENTOS, FICHEROS } from "./trazado.mjs";

const url = process.argv[2] ?? "ws://127.0.0.1:8881";
const MUNDO = process.env.MUNDO_DIR ? process.env.MUNDO_DIR.replace(/\/?$/, "/") : new URL("./mundo/", import.meta.url).pathname;
const ACTIVOS = `${MUNDO}datos/activos/`;
mkdirSync(ACTIVOS, { recursive: true });
let copiados = 0;
for (const f of FICHEROS) {
  const destino = `${ACTIVOS}${f.sha}.${f.ext}`;
  if (!existsSync(destino)) { copyFileSync(new URL(`./${f.desde}`, import.meta.url), destino); copiados++; }
}
console.log(`ficheros: ${FICHEROS.length} (${copiados} copiados ahora a ${ACTIVOS})`);

const t = webSocketClient(url);
await t.opened;
const s = new ClientSession(t, { features: funciones, view: { label() {}, bounce() {}, place() {}, here: () => ({ x: 0, y: 0, z: 0 }) } });
const esperas = { objetos: [], elementos: [] };
let permiso = null;
s.features.tap((feature, event, msg) => {
  if (feature === "objetos" && event === "permiso") permiso = msg.construir === true;
  if ((feature === "objetos" || feature === "elementos") && (event === "hecho" || event === "rechazado")) {
    const w = esperas[feature].shift();
    if (w) w(event === "hecho" ? { ok: true, id: String(msg.id) } : { ok: false, motivo: String(msg.motivo) });
  }
});
const tick = setInterval(() => s.tick(0, 0), 50);
const espera = (ms) => new Promise((r) => setTimeout(r, ms));
for (let i = 0; i < 100 && !(s.entity >= 0 && s.features.catalog); i++) await espera(100);
for (let i = 0; i < 30 && permiso === null; i++) await espera(100);
console.log(`entidad ${s.entity}, catálogo ${s.features.catalog ? "sí" : "no"}, permiso de construir: ${permiso}`);
if (!permiso) { clearInterval(tick); t.close(); process.exit(2); }

const pide = (feature, intent, msg) => new Promise((resolve) => {
  esperas[feature].push(resolve);
  const r = s.features.send(feature, intent, msg);
  if (!r.ok) { esperas[feature].pop(); resolve({ ok: false, motivo: r.why }); }
});
const hechos = [], fallos = [];
for (const o of OBJETOS) {
  const c = await pide("objetos", "colocar", { modelo: o.m, x: o.x, y: o.y, z: o.z, ry: o.ry, nombre: o.n, decorado: !o.n, tipo: o.tipo, ext: o.ext });
  if (!c.ok) { fallos.push({ o: o.n || o.modelo || o.m, en: "colocar", motivo: c.motivo }); continue; }
  const id = c.id;
  await espera(220);
  if (o.sx !== 1 || o.sy !== 1 || o.sz !== 1 || o.rx || o.rz) {
    const m = await pide("objetos", "mover", { id, x: o.x, y: o.y, z: o.z, rx: o.rx ?? 0, ry: o.ry, rz: o.rz ?? 0, sx: o.sx, sy: o.sy, sz: o.sz });
    if (!m.ok) fallos.push({ o: o.n || o.modelo, en: "mover", motivo: m.motivo });
    await espera(120);
  }
  if (o.marco) { // U12: a hole — click it (or E near it) and choose a picture: the server fits it into this frame
    const h = await pide("objetos", "hueco", { id, ancho: o.marco[0], alto: o.marco[1] });
    if (!h.ok) fallos.push({ o: o.n, en: "hueco", motivo: h.motivo });
    await espera(150);
  }
  if (o.caja || o.oculto) {
    const [bx0, by0, bz0, bx1, by1, bz1] = o.caja ?? [0, 0, 0, 0, 0, 0];
    const a = await pide("objetos", "ajustar", { id, oculto: o.oculto, volumen: 1, solido: !!o.caja, bx0, by0, bz0, bx1, by1, bz1, alcance: "burbuja", radio: 8 });
    if (!a.ok) fallos.push({ o: o.n || o.modelo, en: "ajustar", motivo: a.motivo });
    await espera(220);
  }
  if (o.fijado) {
    const f = await pide("objetos", "fijar", { id, fijado: true });
    if (!f.ok) fallos.push({ o: o.n || o.modelo, en: "fijar", motivo: f.motivo });
    await espera(220);
  }
  hechos.push({ id, nombre: o.n, modelo: o.modelo ?? "rótulo", hueco: o.hueco, x: o.x, y: o.y, z: o.z });
}
const elementos = [];
for (const e of ELEMENTOS) {
  const r = await pide("elementos", "poner", e);
  if (r.ok) elementos.push({ id: r.id, nombre: e.nombre }); else fallos.push({ o: e.nombre, en: "poner", motivo: r.motivo });
  await espera(220);
}
clearInterval(tick);
console.log(`colocados ${hechos.length} de ${OBJETOS.length} objetos · elementos ${elementos.length} de ${ELEMENTOS.length} · fallos ${fallos.length}`);
for (const f of fallos.slice(0, 12)) console.log(`  ${f.en} ${f.o}: ${f.motivo}`);
writeFileSync(new URL("./construido.json", import.meta.url), JSON.stringify({ url, cuando: new Date().toISOString(), hechos, elementos, fallos }, null, 1) + "\n");
t.close();
process.exit(fallos.length ? 1 : 0);
