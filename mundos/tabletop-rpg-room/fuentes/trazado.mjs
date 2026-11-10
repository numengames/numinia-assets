// trazado.mjs — «El Gabinete» laid out as objects of the engine (metres; x east, z south, y up), in the format of
// plantillas/mesa-rol/trazado.mjs (construir.mjs places it):
//   - the building: ONE model (modelos/gabinete.glb, every fixed piece merged where modelos.mjs PIEZAS puts them), fixed;
//   - three engine holes (U12; `marco` = the frame, metres): the map lying on the table (rx −90) and the two
//     consultation plates on their easels. Whoever directs clicks one and chooses a JPG/PNG; the server fits it;
//   - the miniatures (§26, §34): six free objects (not fixed) — two on the map, four on the preparation table. Whoever
//     may build moves them; they are NOT the characters' sheets (§6);
//   - what you collide with: invisible colliders (modelos/choque.glb) with solid boxes: the decagonal wall (each side as
//     short axis-aligned pieces: the sim's solids are AABBs), the table, the tower, the console, the shelves' furniture;
//   - names an agent and the «Gente» list should know (≤ 60 bytes each): the table, the eight seats, the tower, the
//     console, the crystal, the archive, the preparation table, the plates.
// `node trazado.mjs` counts.
import { readFileSync } from "node:fs";
import { SALA, LADOS, PUERTA, MESA, MAPA, ASIENTOS, CRISTAL, TORRE, CONSOLA, PLACAS, ARCHIVO, PREPARACION, GLOBO, VITRINA, MINIATURAS, enPared } from "./medidas.mjs";

const leer = (f) => JSON.parse(readFileSync(new URL(f, import.meta.url), "utf8"));
export const MODELOS = leer("./modelos/modelos.json");
export const ROTULOS = leer("./rotulos/rotulos.json");
const q = (v) => Math.round(v * 1000) / 1000;
const RAD = Math.PI / 180;
const UNIDAD = [-0.5, 0, -0.5, 0.5, 1, 0.5];

const obj = (o) => ({ tipo: "modelo", ext: "glb", y: 0, rx: 0, ry: 0, rz: 0, sx: 1, sy: 1, sz: 1, n: "", fijado: true, oculto: false, caja: null, ...o, x: q(o.x ?? 0), z: q(o.z ?? 0) });
const modelo = (nombre, o = {}) => obj({ m: MODELOS[nombre].sha, modelo: nombre, ...o });
const choque = (x, z, sx, sy, sz, o = {}) => obj({ m: MODELOS.choque.sha, x, z, sx: q(sx), sy: q(sy), sz: q(sz), caja: UNIDAD, modelo: "choque", ...o });
/** The AABB round a rotated rectangle (centre x, z; w along its own x, d along its own z; turned ry). */
function caja(x, z, w, d, ry, alto, o = {}) {
  const c = Math.abs(Math.cos(ry * RAD)), s = Math.abs(Math.sin(ry * RAD));
  return choque(x, z, w * c + d * s, alto, w * s + d * c, o);
}

export const OBJETOS = [];
const pon = (...o) => OBJETOS.push(...o);

// ---- the building ----
pon(modelo("gabinete", { n: "El Gabinete: sala universal de rol" }));

// ---- the holes (the picture plane is 1 m high: scale = the frame's height) ----
pon(obj({ m: ROTULOS["mapa-ruinas"].sha, tipo: "imagen", ext: "png", x: MAPA.x, y: MAPA.y, z: MAPA.z, rx: MAPA.rx, ry: MAPA.ry, sx: MAPA.alto, sy: MAPA.alto, sz: MAPA.alto,
  n: "El mapa de la mesa: quien dirige pone aquí su plano", fijado: false, hueco: "mapa", marco: [MAPA.ancho, MAPA.alto] }));
for (const [k, p] of PLACAS.entries()) {
  const ad = enPared(p.angulo, Math.hypot(p.x, p.z) - 0.03, 0); // a hair in front of the frame (towards the table)
  pon(obj({ m: ROTULOS[k ? "placa-inscripcion" : "placa-manual"].sha, tipo: "imagen", ext: "png", x: ad.x, y: p.y, z: ad.z, ry: p.ry, sx: p.alto, sy: p.alto, sz: p.alto,
    n: p.nombre, fijado: false, hueco: k ? "placa-este" : "placa-oeste", marco: [p.ancho, p.alto] }));
}

// ---- the miniatures (free: whoever may build moves them) ----
for (const m of MINIATURAS) {
  let x, z, y;
  if (m.en === "mapa") { x = m.x; z = m.z; y = MESA.colocar + 0.004; }
  else { const R = PREPARACION.ry * RAD; x = PREPARACION.x + m.t * Math.cos(R); z = PREPARACION.z - m.t * Math.sin(R); y = PREPARACION.alto + (m.t > 0 ? 0.006 : 0); }
  pon(modelo(`mini-${m.id}`, { x, z, y: q(y), ry: m.en === "mapa" ? 180 : PREPARACION.ry, n: m.nombre, fijado: false }));
}

// ---- what you bump into (invisible) ----
// the wall: each side cut in five short pieces (tight AABBs); the door's side leaves its opening
{
  const r = SALA.apotema + SALA.grosor / 2, L = 2 * SALA.medioLadoFuera, partes = 5;
  for (const { angulo, puerta } of LADOS) {
    for (let k = 0; k < partes; k++) {
      const t0 = -L / 2 + (L * k) / partes, t1 = t0 + L / partes, tc = (t0 + t1) / 2;
      if (puerta && Math.abs(tc) < PUERTA.ancho / 2 + 0.2) continue;
      const p = enPared(angulo, r, tc);
      pon(caja(p.x, p.z, L / partes + 0.1, SALA.grosor, 90 - angulo, SALA.alto));
    }
  }
  pon(choque(PUERTA.x, PUERTA.z, PUERTA.ancho, SALA.alto - PUERTA.alto, SALA.grosor, { y: PUERTA.alto }));       // lintel
  pon(choque(PUERTA.x, PUERTA.z, PUERTA.ancho, PUERTA.alto, SALA.grosor, { caja: null, n: "La puerta del Gabinete" }));
}
// the ceiling and the lantern (invisible, solid): the camera stops under them instead of rising through the roof
{
  const R = SALA.apotema + SALA.grosor, h = SALA.techo, y = SALA.alto, L = SALA.linterna, a = L.radio;
  pon(choque(0, -(a + R) / 2, 2 * R, h, R - a, { y }), choque(0, (a + R) / 2, 2 * R, h, R - a, { y }));   // north and south of the hole
  pon(choque(-(a + R) / 2, 0, R - a, h, 2 * a, { y }), choque((a + R) / 2, 0, R - a, h, 2 * a, { y }));   // west and east of it
  const alto = L.y1 - y;                                                                              // the lantern's glass drum
  pon(choque(0, -a, 2 * a, alto, 0.2, { y }), choque(0, a, 2 * a, alto, 0.2, { y }), choque(-a, 0, 0.2, alto, 2 * a, { y }), choque(a, 0, 0.2, alto, 2 * a, { y }));
  pon(choque(0, 0, 2 * a, 0.3, 2 * a, { y: L.y1 }));                                                  // and its cap
}
// the shelves (0.42 deep): a strip along each wall but the door's, so nobody walks into the books
for (const { angulo, puerta } of LADOS) {
  if (puerta) continue;
  for (const t of [-2.1, 0, 2.1]) { const p = enPared(angulo, SALA.apotema - 0.21, t); pon(caja(p.x, p.z, 2.1, 0.42, 90 - angulo, 2.8)); }
}
// the table: a round top as three boxes
{
  const r = MESA.radio + 0.03, h = MESA.alto + 0.02;
  pon(choque(MESA.x, MESA.z, 2 * r * 0.92, h, 2 * r * 0.4, { n: "La mesa" }));
  pon(choque(MESA.x, MESA.z, 2 * r * 0.4, h, 2 * r * 0.92));
  pon(choque(MESA.x, MESA.z, 2 * r * 0.72, h, 2 * r * 0.72));
}
pon(caja(TORRE.plinto.x, TORRE.plinto.z, TORRE.plinto.ancho, TORRE.plinto.fondo, TORRE.ry, TORRE.alto, { n: "Torre de dados (estación de dados)" }));
pon(caja(CONSOLA.x, CONSOLA.z, CONSOLA.ancho, CONSOLA.fondo, CONSOLA.ry, CONSOLA.alto + 0.2, { n: "Consola de quien dirige" }));
for (const f of CONSOLA.fanales) pon(choque(f.x, f.z, 0.36, 2, 0.36));
for (const p of PLACAS) pon(caja(p.x, p.z, 1.3, 0.7, p.ry, 2.1));
pon(caja(ARCHIVO.x, ARCHIVO.z, ARCHIVO.ancho, ARCHIVO.fondo, ARCHIVO.ry, ARCHIVO.alto, { n: "Archivo de la campaña" }));
pon(caja(PREPARACION.x, PREPARACION.z, PREPARACION.ancho, PREPARACION.fondo, PREPARACION.ry, PREPARACION.alto, { n: "Mesa de preparación" }));
pon(choque(GLOBO.x, GLOBO.z, 1.1, 2.1, 1.1, { n: "Globo celeste" }));
pon(caja(VITRINA.x, VITRINA.z, VITRINA.ancho, VITRINA.fondo, VITRINA.ry, VITRINA.alto, { n: "Vitrina de la esfera armilar" }));
pon(choque(CRISTAL.x, CRISTAL.z, 0.9, 0.9, 0.9, { y: CRISTAL.y - 0.45, caja: null, n: "El cristal narrativo" }));
// the seats: not solid (the engine's seat piece sits you there), named
for (const a of ASIENTOS) pon(choque(a.x, a.z, 0.6, a.dj ? 1.5 : 1.0, 0.6, { caja: null, ry: a.ry, n: a.etiqueta }));

for (const o of OBJETOS) if (Buffer.byteLength(o.n, "utf8") > 60) throw new Error(`nombre de más de 60 bytes: «${o.n}»`);

/** The engine's elements: the fire in the console's two lanterns (each fire brings its own warm light). */
export const ELEMENTOS = CONSOLA.fanales.map((f) => ({ tipo: "fuego", x: f.x, y: f.y, z: f.z, ancho: 1, largo: 1, alto: 1, tamano: 0.16, giro: 0, color: 0xefa517, intensidad: 0.8, nombre: f.nombre }));

const usados = new Set(OBJETOS.map((o) => o.m));
export const FICHEROS = [
  ...Object.values(MODELOS).filter((m) => usados.has(m.sha)).map((m) => ({ sha: m.sha, ext: "glb", desde: m.fichero })),
  ...Object.values(ROTULOS).map((r) => ({ sha: r.sha, ext: "png", desde: r.fichero })),
];

if (process.argv[1] && import.meta.url.endsWith(process.argv[1].split("/").pop())) {
  const n = (f) => OBJETOS.filter(f).length;
  console.log(`objetos ${OBJETOS.length}: a la vista ${n((o) => o.modelo !== "choque")} (edificio 1, huecos ${n((o) => o.tipo === "imagen")}, miniaturas ${n((o) => String(o.modelo).startsWith("mini-"))}), choques invisibles ${n((o) => o.modelo === "choque")}`);
  console.log(`sólidos ${n((o) => o.caja)} · fijados ${n((o) => o.fijado)} · libres ${n((o) => !o.fijado)} · con nombre ${n((o) => o.n)} · elementos ${ELEMENTOS.length} · ficheros ${FICHEROS.length}`);
}
