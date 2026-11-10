// recorrido.mjs — walks «El Atlas Despierto» from start to end with the engine's own quest machine (pure, no browser):
// at each turn it looks for the person who offers the next quest (following their dialogue from the node the engine
// opens with), does each step the way a player would (E on a thing, arrive somewhere, talk and choose the option that
// advances) and fails if the chain gets stuck. Run from the engine checkout:  node <this file> <world folder>
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
const MOTOR = process.env.MOTOR ?? "/var/home/uruk/arkhitektonian-lab/.hermes/profiles/ursa/work/ec-motor-main";
const { cargarAventura } = await import(join(MOTOR, "features/misiones/formato.ts"));
const { aceptar, nodoInicial, nuevoProgreso, registrar, opcionesDe, primerPendiente, disponible, estadoDe } = await import(join(MOTOR, "features/misiones/maquina.ts"));

const A_DIR = join(process.argv[2], "aventura");
const leer = (d) => readdirSync(join(A_DIR, d)).filter((f) => f.endsWith(".json")).map((f) => ({ fichero: `${d}/${f}`, texto: readFileSync(join(A_DIR, d, f), "utf8") }));
const r = cargarAventura({ misiones: leer("misiones"), pnj: leer("pnj"), cosas: leer("cosas"), secuencias: leer("secuencias") });
if (r.issues.length) { console.error(r.issues); process.exit(1); }
const A = r.aventura, p = nuevoProgreso(), log = [];

/** Follow a person's dialogue from where the engine opens it, looking for an option with this action (depth-first). */
function buscar(pnj, quiere) {
  const ini = nodoInicial(pnj, p, A);
  if (!ini) return null;
  const visto = new Set();
  const ir = (id, camino) => {
    if (visto.has(id)) return null; visto.add(id);
    const nodo = pnj.dialogo.nodos[id];
    for (const o of opcionesDe(nodo, p, A)) {
      if (o.accion && quiere(o.accion)) return [...camino, id, o];
      if (o.ir) { const x = ir(o.ir, [...camino, id]); if (x) return x; }
    }
    return null;
  };
  return ir(ini, []);
}
const charlar = (pnj, quiere, que) => {
  const c = buscar(pnj, quiere);
  if (!c) throw new Error(`${pnj.id} no ofrece «${que}» (abre con ${nodoInicial(pnj, p, A)})`);
  registrar(p, A, { tipo: "habla", pnj: pnj.id });
  return c;
};

for (let vuelta = 0; vuelta < 100 && estadoDe(p, "m12-memoria") !== "hecha"; vuelta++) {
  const activa = Object.keys(p.activas)[0];
  if (!activa) {
    const m = [...A.misiones.values()].find((m) => disponible(p, m));
    if (!m) throw new Error("ninguna misión disponible: la cadena se corta");
    const quien = [...A.pnjs.values()].find((n) => buscar(n, (a) => a.tipo === "dar_mision" && a.mision === m.id));
    if (!quien) throw new Error(`nadie ofrece ${m.id}`);
    const c = charlar(quien, (a) => a.tipo === "dar_mision" && a.mision === m.id, `dar ${m.id}`);
    const res = aceptar(p, m, A);
    if (!res.ok) throw new Error(res.motivo);
    log.push(`${m.id}: la da ${quien.id} (${c.filter((x) => typeof x === "string").join("→")})`);
    continue;
  }
  const m = A.misiones.get(activa), act = p.activas[activa], n = primerPendiente(m, act), s = m.pasos[n];
  let hechos;
  if (s.tipo === "usa" || s.tipo === "coge") hechos = registrar(p, A, { tipo: "usa", cosa: s.cosa });
  else if (s.tipo === "llega_a") hechos = registrar(p, A, { tipo: "llega", x: s.x, z: s.z });
  else if (s.tipo === "habla_con") {
    const pnj = A.pnjs.get(s.pnj);
    charlar(pnj, (a) => a.tipo === "avanzar_paso" && a.mision === m.id, `avanzar ${m.id}`);
    hechos = registrar(p, A, { tipo: "avanzar", pnj: s.pnj, mision: m.id });
  } else throw new Error(`paso ${s.tipo} sin simular`);
  if (!hechos.some((h) => h.mision === m.id)) throw new Error(`${m.id}.${s.id} (${s.tipo}) no avanza`);
  log.push(`  ${m.id}.${s.id} ✓${hechos.some((h) => h.completada) ? "  — misión hecha" : ""}`);
}
console.log(log.join("\n"));
if (estadoDe(p, "m12-memoria") !== "hecha") { console.error("NO TERMINA"); process.exit(1); }
// after the walk: what each person says now
for (const n of A.pnjs.values()) console.log(`después: ${n.id} abre con «${nodoInicial(n, p, A)}»`);
console.log(`hechas: ${p.hechas.length}/12 — recorrido completo`);
