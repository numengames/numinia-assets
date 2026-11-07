// modelos.mjs — writes the low-poly models of «El Gabinete» into modelos/*.glb and their list into modelos/modelos.json.
// Flat colour from the house palette only (STD-023: the 16 colours, no new hex), faceted on purpose, own work CC0.
// Every fixed piece is merged into gabinete.glb, the only building model the world places (one draw call per material,
// ≤ 12). The pieces stay in modelos/ as a kit; the miniatures are separate models (they move).
//
// Art direction (spec III.A): a steampunk cabinet of wonders. Palette mapping, since the house palette is closed:
//   walnut / mahogany → Bronce #3A332D · ebony → Bronce sombra #292420 · brass → Ámbar (metallic) · aged bronze →
//   Ámbar tostado (metallic) · leather → Grana honda · parchment / ivory → Arena · petrol green felt → Turquesa honda ·
//   dark stone → Ceniza · glass → Verdemar (transparent) · amber light → Ámbar (emissive) · the «live part» (what you
//   use) → Turquesa (emissive, house convention). The muted violet of §11.D has no house colour: not used.
// Run: node modelos.mjs
import { mkdirSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { Modelo } from "./glb.mjs";
import { SALA, LADOS, PUERTA, MESA, MAPA, ASIENTOS, CRISTAL, TORRE, CONSOLA, PLACAS, ARCHIVO, PREPARACION, GLOBO, VITRINA, RELOJ, ROSA, MINIATURAS, enPared } from "./medidas.mjs";

const DIR = new URL("./modelos/", import.meta.url);
mkdirSync(DIR, { recursive: true });
const RAD = Math.PI / 180;

/** The cabinet's materials (a model only writes the ones it uses). */
function casa(nombre) {
  return new Modelo(nombre)
    .material("madera", "bronce", { rugoso: 0.8 })
    .material("ebano", "broncesombra", { rugoso: 0.75 })
    .material("laton", "ambar", { metal: 0.55, rugoso: 0.38 })
    .material("bronce", "ambartostado", { metal: 0.45, rugoso: 0.45 })
    .material("cuero", "granahonda", { rugoso: 0.7 })
    .material("pergamino", "arena", { rugoso: 0.9 })
    .material("piedra", "ceniza", { rugoso: 0.92 })
    .material("pano", "turquesahonda", { rugoso: 0.95 })
    .material("cristal", "verdemar", { alfa: 0.26, rugoso: 0.08 })
    .material("luz", "ambar", { rugoso: 0.4, brillo: 1.0 })
    .material("senal", "turquesa", { rugoso: 0.5, brillo: 0.7 });
}

const hechos = {}, piezas = {};
function guardar(m, notas = "") {
  piezas[m.nombre] = m;
  const g = m.glb({ generador: "plantillas/gabinete-rol glb.mjs" });
  writeFileSync(new URL(`${m.nombre}.glb`, DIR), g.bytes);
  hechos[m.nombre] = { fichero: `modelos/${m.nombre}.glb`, sha: g.sha, bytes: g.bytes.length, tris: g.tris, materiales: g.materiales, min: g.min.map((v) => Math.round(v * 1000) / 1000), max: g.max.map((v) => Math.round(v * 1000) / 1000), vueltas: m.vueltas(), notas };
}
/** A box along the wall at `angulo`: t metres along it, its centre at radius r, from y0, largo × alto × hondo. */
const tangente = (m, mat, angulo, r, t, y0, largo, alto, hondo) => { const p = enPared(angulo, r, t); return m.caja(mat, p.x, y0, p.z, largo, alto, hondo, 90 - angulo); };
/** A point in a piece's own frame (x right, z front) turned by ry and moved to (x0, z0). */
const giro = (x0, z0, ry) => { const R = ry * RAD, c = Math.cos(R), s = Math.sin(R); return (px, py, pz) => [x0 + px * c + pz * s, py, z0 - px * s + pz * c]; };
/** A tiny seeded random (the same books every time: deterministic bytes). */
const azar = (semilla) => { let s = semilla >>> 0; return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; }; };

// ---- the floor: dark stone decagon, a petrol-green rug under the table and seats, the brass compass rose, a runner ----
{
  const m = casa("suelo");
  m.tronco("piedra", SALA.lados, 0, 0, 0, 0.02, SALA.radioVertice, SALA.radioVertice, 0);
  m.tronco("pano", 24, 0, 0, 0.02, 0.026, ROSA.r0, ROSA.r0, 0);                       // the rug
  m.anillo("laton", 24, 0, 0, 0.02, 0.03, ROSA.r1, ROSA.r0, 0);                         // its brass ring
  m.anillo("laton", 40, 0, 0, 0.02, 0.026, 8.7, 8.62, 0);                               // a thin ring further out
  for (let k = 0; k < 8; k++) {                                                         // eight points between the chairs
    const a = (22.5 + 45 * k) * RAD, w = 0.17, mat = k % 2 ? "ebano" : "laton";
    const pts = [[ROSA.punta * Math.cos(a), ROSA.punta * Math.sin(a)], [ROSA.r1 * Math.cos(a - w), ROSA.r1 * Math.sin(a - w)], [ROSA.r1 * Math.cos(a + w), ROSA.r1 * Math.sin(a + w)]];
    const cx = (pts[0][0] + pts[1][0] + pts[2][0]) / 3, cz = (pts[0][1] + pts[1][1] + pts[2][1]) / 3;
    pts.sort((p, q) => Math.atan2(p[1] - cz, p[0] - cx) - Math.atan2(q[1] - cz, q[0] - cx));
    m.losa(mat, pts, 0.02, 0.028);
  }
  m.caja("cuero", 0, 0.02, (ROSA.punta + SALA.apotema) / 2 + 0.2, 1.4, 0.006, SALA.apotema - ROSA.punta - 0.4);   // the runner from the door
  m.caja("laton", PUERTA.x, 0.02, PUERTA.z, PUERTA.ancho, 0.03, SALA.grosor);          // threshold
  guardar(m, "suelo de piedra oscura, alfombra verde petróleo, rosa de los vientos de latón y alfombra de entrada");
}

// ---- the walls: ten sides of walnut on a stone plinth, a clerestory window on each, the door on the south side ----
{
  const m = casa("muro"), r = SALA.apotema + SALA.grosor / 2, L = 2 * SALA.medioLadoFuera, g = SALA.grosor, V = SALA.ventana;
  for (const { angulo, puerta } of LADOS) {
    tangente(m, "piedra", angulo, r - 0.04, 0, 0, L, 0.28, g + 0.08);                   // the plinth (zócalo)
    if (puerta) {
      const lado = (L - PUERTA.ancho) / 2;
      for (const s of [-1, 1]) tangente(m, "madera", angulo, r, s * (PUERTA.ancho / 2 + lado / 2), 0.28, lado, PUERTA.alto - 0.28, g);
      tangente(m, "madera", angulo, r, 0, PUERTA.alto, L, SALA.alto - PUERTA.alto, g);
      // the brass frame of the door, inside, and a lintel moulding
      const pi = (t, y) => { const p = enPared(angulo, SALA.apotema - 0.03, t); return [p.x, y, p.z]; };
      m.barra("laton", pi(-PUERTA.ancho / 2, 0.02), pi(-PUERTA.ancho / 2, PUERTA.alto), 0.09).barra("laton", pi(PUERTA.ancho / 2, 0.02), pi(PUERTA.ancho / 2, PUERTA.alto), 0.09);
      m.barra("laton", pi(-PUERTA.ancho / 2 - 0.1, PUERTA.alto + 0.05), pi(PUERTA.ancho / 2 + 0.1, PUERTA.alto + 0.05), 0.12);
      continue;
    }
    const lado = (L - V.ancho) / 2;
    tangente(m, "madera", angulo, r, 0, 0.28, L, V.y0 - 0.28, g);                       // under the window
    for (const s of [-1, 1]) tangente(m, "madera", angulo, r, s * (V.ancho / 2 + lado / 2), V.y0, lado, V.y1 - V.y0, g);
    tangente(m, "madera", angulo, r, 0, V.y1, L, SALA.alto - V.y1, g);                  // over it
    tangente(m, "cristal", angulo, r, 0, V.y0, V.ancho, V.y1 - V.y0, 0.04);             // the pane
    const pi = (t, y) => { const p = enPared(angulo, SALA.apotema - 0.02, t); return [p.x, y, p.z]; };
    m.barra("laton", pi(-V.ancho / 2, V.y0), pi(V.ancho / 2, V.y0), 0.06).barra("laton", pi(-V.ancho / 2, V.y1), pi(V.ancho / 2, V.y1), 0.06);
    for (let k = 0; k <= 4; k++) { const t = -V.ancho / 2 + (V.ancho * k) / 4; m.barra("laton", pi(t, V.y0), pi(t, V.y1), k % 4 ? 0.035 : 0.06); }
    m.barra("laton", pi(-V.ancho / 2, (V.y0 + V.y1) / 2), pi(V.ancho / 2, (V.y0 + V.y1) / 2), 0.025);   // a transom: geometric glazing
  }
  guardar(m, "muro decagonal de nogal con zócalo de piedra, ventanal alto en cada lado y la puerta al sur");
}

// ---- the bookshelves on the nine walls (not the door's), with the library ladder on the west one ----
{
  const m = casa("biblioteca"), ancho = 6.2, hondo = 0.42, alto = 2.75, r = SALA.apotema - hondo / 2, baldas = [0.1, 0.62, 1.14, 1.66, 2.18];
  const lomos = ["cuero", "pano", "pergamino", "bronce", "ebano", "cuero", "madera"];
  for (const { angulo, puerta } of LADOS) {
    if (puerta) continue;
    const rnd = azar(angulo * 7 + 3);
    for (let k = 0; k <= 4; k++) tangente(m, "ebano", angulo, r, -ancho / 2 + (ancho * k) / 4, 0, 0.06, alto, hondo); // uprights
    for (const y of [...baldas, alto - 0.04]) tangente(m, "ebano", angulo, r, 0, y - 0.04, ancho, 0.04, hondo);       // shelves
    tangente(m, "laton", angulo, r - 0.02, 0, alto, ancho + 0.1, 0.06, hondo + 0.06);                                  // brass crown
    for (let b = 0; b < 4; b++) for (const y of baldas) {
      const t0 = -ancho / 2 + (ancho * b) / 4 + 0.05, t1 = t0 + ancho / 4 - 0.1;
      let t = t0;
      while (t < t1 - 0.12) {
        const w = Math.min(0.18 + rnd() * 0.32, t1 - t), h = 0.28 + rnd() * 0.16;
        if (rnd() < 0.13) { t += w * 0.6; continue; }                                   // a gap
        tangente(m, lomos[Math.floor(rnd() * lomos.length)], angulo, r + 0.03, t + w / 2, y, w - 0.01, Math.min(h, 0.46), hondo - 0.1);
        t += w;
      }
    }
  }
  // the ladder on the west wall (180°), leaning on the shelves
  const pie = (t, d, y) => { const p = enPared(180, SALA.apotema - d, t); return [p.x, y, p.z]; };
  for (const s of [-0.24, 0.24]) m.barra("bronce", pie(1.9 + s, 1.05, 0.02), pie(1.9 + s, 0.45, 2.85), 0.05);
  for (let k = 1; k <= 8; k++) { const f = k / 9; m.barra("laton", pie(1.66, 1.05 - 0.6 * f, 0.02 + 2.83 * f), pie(2.14, 1.05 - 0.6 * f, 0.02 + 2.83 * f), 0.035); }
  m.barra("laton", pie(-2.9, 0.3, 2.9), pie(2.9, 0.3, 2.9), 0.04);                       // its rail
  guardar(m, "estanterías de ébano con libros en nueve muros y la escalerilla de la biblioteca");
}

// ---- the ceiling at 4 m: walnut coffers with ebony beams, a brass ring round the lantern, the glass lantern and its dome ----
{
  const m = casa("techo"), L = SALA.linterna, rV = SALA.radioVertice;
  m.anillo("madera", SALA.lados, 0, 0, SALA.alto, SALA.alto + SALA.techo, rV, L.radio, 0, true);
  for (let k = 0; k < SALA.lados; k++) {                                                 // radial beams, on the vertices
    const a = 36 * k * RAD, ri = L.radio, ro = SALA.apotema / Math.cos(18 * RAD) - 0.1;
    m.barra("ebano", [ri * Math.cos(a), SALA.alto - 0.1, ri * Math.sin(a)], [ro * Math.cos(a), SALA.alto - 0.1, ro * Math.sin(a)], 0.2);
  }
  m.anillo("ebano", SALA.lados, 0, 0, SALA.alto - 0.18, SALA.alto, 7.35, 7.1, 0, true);
  m.anillo("laton", SALA.lados, 0, 0, SALA.alto - 0.2, SALA.alto + 0.02, L.radio + 0.14, L.radio - 0.04, 0, true);
  m.tronco("cristal", SALA.lados, 0, 0, SALA.alto + SALA.techo, L.y1, L.radio, L.radio, 0, false);
  for (let k = 0; k < SALA.lados; k++) { const a = 36 * k * RAD; m.barra("laton", [L.radio * Math.cos(a), SALA.alto + SALA.techo, L.radio * Math.sin(a)], [L.radio * Math.cos(a), L.y1, L.radio * Math.sin(a)], 0.07); }
  m.anillo("laton", SALA.lados, 0, 0, L.y1, L.y1 + 0.1, L.radio + 0.15, L.radio - 0.1, 0, true);
  m.troncoDentro("madera", SALA.lados, 0, 0, L.y1 + 0.1, L.cupula, L.radio - 0.1, L.oculo, 0);
  m.tronco("piedra", SALA.lados, 0, 0, L.y1 + 0.1, L.cupula + 0.05, L.radio + 0.15, L.oculo + 0.05, 0, false);
  for (let k = 0; k < SALA.lados; k++) {                                                  // brass ribs under the dome
    const a = (36 * k + 18) * RAD, f = 0.96;
    m.barra("laton", [(L.radio - 0.12) * f * Math.cos(a), L.y1 + 0.1, (L.radio - 0.12) * f * Math.sin(a)], [(L.oculo + 0.02) * Math.cos(a), L.cupula - 0.04, (L.oculo + 0.02) * Math.sin(a)], 0.05);
  }
  m.tronco("laton", SALA.lados, 0, 0, L.cupula, L.remate, L.oculo + 0.05, 0.22, 0);
  m.icosaedro("laton", 0, L.remate + 0.1, 0, 0.13);
  // six pendant lamps between the seats and the shelves (a look: the light is the sun, the fires and the room's)
  for (let k = 0; k < 6; k++) {
    const a = (30 + 60 * k) * RAD, x = 6.4 * Math.cos(a), z = 6.4 * Math.sin(a);
    m.barra("bronce", [x, SALA.alto - 0.2, z], [x, 3.3, z], 0.025);
    m.tronco("laton", 8, x, z, 3.22, 3.32, 0.2, 0.06);
    m.esfera("luz", x, 3.12, z, 0.11, 8);
  }
  guardar(m, "techo artesonado a 4 m, linterna de cristal con cúpula y seis lámparas colgantes");
}

// ---- the table: bronze pedestal with gears, walnut top Ø 3.60, petrol-green baize, brass rail, eight leather places ----
{
  const m = casa("mesa"), { radio, alto, pano, riel } = MESA;
  m.tronco("bronce", 8, 0, 0, 0, 0.1, 0.75, 0.62);
  for (let k = 0; k < 4; k++) { const a = (45 + 90 * k) * RAD; m.barra("bronce", [0.15 * Math.cos(a), 0.55, 0.15 * Math.sin(a)], [0.62 * Math.cos(a), 0.08, 0.62 * Math.sin(a)], 0.07); }
  m.tronco("bronce", 8, 0, 0, 0.1, alto - 0.12, 0.16, 0.13);
  m.engranaje("laton", 0, 0.42, 0, "x", 0.26, 0.05, 12, 0.05).engranaje("laton", 0, 0.42, 0, "z", 0.26, 0.05, 12, 0.05);
  m.tronco("bronce", 8, 0, 0, alto - 0.16, alto - 0.07, 0.4, 0.95);
  m.tronco("madera", 32, 0, 0, alto - 0.07, alto, radio - 0.02, radio - 0.02, 0);         // the walnut top
  m.anillo("laton", 32, 0, 0, alto - 0.09, alto + 0.012, radio + 0.025, radio - 0.04, 0, true); // brass edge
  m.tronco("pano", 32, 0, 0, alto, alto + 0.004, pano, pano, 0);                          // the green baize
  m.anillo("laton", 32, 0, 0, alto, alto + 0.012, riel + 0.012, riel - 0.012, 0);        // the rail round it
  for (const s of ASIENTOS) {                                                             // each place: a leather pad with a brass tick
    const p = enPared(s.angulo, (riel + radio) / 2 + 0.01, 0);
    m.caja(s.dj ? "ebano" : "cuero", p.x, alto, p.z, 0.62, 0.006, 0.13, 90 - s.angulo);
    const q = enPared(s.angulo, riel + 0.035, 0);
    m.caja("laton", q.x, alto, q.z, 0.05, 0.01, 0.03, 90 - s.angulo);
  }
  const w = MAPA.ancho / 2 + 0.04, h = MAPA.alto / 2 + 0.04, y = alto + 0.004;
  m.caja("ebano", 0, y, 0, 2 * w + 0.05, 0.006, 2 * h + 0.05);                            // the map's bed (shows as its frame)
  for (const [sx, sz] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) m.caja("laton", sx * w, y + 0.006, sz * h, 0.08, 0.006, 0.08, 45);
  m.caja("senal", 0, y + 0.006, -h, 0.22, 0.004, 0.02);                                   // a turquoise tick: the director's edge (north)
  guardar(m, "mesa redonda de nogal Ø 3,60 con paño verde, riel de latón, ocho puestos y el lecho del mapa");
}

// ---- the chairs: walnut and leather, a brass rail; the director's has a taller back with an astrolabe. Front = +z ----
function silla(nombre, dj) {
  const m = casa(nombre), y = 0.46, alto = dj ? 1.32 : 1.0;
  for (const [sx, sz] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) m.caja("madera", sx * 0.23, 0, sz * 0.22, 0.06, y, 0.06);
  m.caja("madera", 0, y - 0.05, 0, 0.56, 0.05, 0.52);
  m.caja("cuero", 0, y, 0.01, 0.52, 0.07, 0.48);                                          // the cushion
  m.caja("madera", 0, y, -0.25, 0.56, alto - y, 0.06);                                    // the back
  m.caja("cuero", 0, y + 0.14, -0.215, 0.44, alto - y - 0.24, 0.02);                      // its leather panel
  m.caja("laton", 0, alto, -0.25, 0.6, 0.04, 0.08);
  for (const s of [-1, 1]) { m.caja("madera", s * 0.27, y + 0.07, -0.02, 0.06, 0.2, 0.46); m.caja("laton", s * 0.27, y + 0.27, -0.02, 0.07, 0.02, 0.48); }
  if (dj) { m.engranaje("laton", 0, alto + 0.17, -0.25, "z", 0.13, 0.03, 0, 0, 16); m.aro("laton", [0, alto + 0.17, -0.25], 0.17, [1, 0, 0], [0, 1, 0], 16, 0.02); m.barra("laton", [-0.2, alto + 0.17, -0.25], [0.2, alto + 0.17, -0.25], 0.015); }
  guardar(m, dj ? "silla de quien dirige: respaldo alto con astrolabio (no un trono)" : "silla de jugador, nogal y cuero");
}
silla("silla", false);
silla("silla-dj", true);

// ---- the narrative crystal: a faceted prism with an amber core in a brass gimbal, hanging from the lantern's oculus ----
{
  const m = casa("cristal"), { y, alto, radio, aro } = CRISTAL, h = alto / 2;
  m.tronco("cristal", 6, 0, 0, y, y + h, radio, 0);                                       // upper point
  m.tronco("cristal", 6, 0, 0, y - h, y, 0, radio);                                       // lower point
  m.icosaedro("luz", 0, y, 0, 0.075);                                                     // the amber core
  m.aro("laton", [0, y, 0], aro, [1, 0, 0], [0, 1, 0], 20, 0.025);                        // the gimbal: three rings
  m.aro("laton", [0, y, 0], aro - 0.05, [0, 0, 1], [0, 1, 0], 20, 0.022);
  m.aro("bronce", [0, y, 0], aro + 0.05, [1, 0, 0], [0, 0, 1], 24, 0.03);
  for (let k = 0; k < 3; k++) { const a = (k * 120 + 90) * RAD; m.barra("bronce", [(aro + 0.05) * Math.cos(a), y, (aro + 0.05) * Math.sin(a)], [0, y + aro + 0.32, 0], 0.012); }
  m.barra("laton", [0, y + aro + 0.32, 0], [0, SALA.linterna.cupula, 0], 0.02);          // the long rod to the oculus
  m.esfera("laton", 0, y + aro + 0.32, 0, 0.035, 6);
  guardar(m, "el cristal narrativo: prisma facetado con núcleo ámbar en un cardán de latón");
}

// ---- the dice station: the gear tower, its tray towards the table (the house's tower, in the cabinet's metals) ----
{
  const m = casa("torre-dados"), P = 0.8, top = 2.2, bz = -0.15;
  m.caja("madera", 0, 0, 0.25, 0.9, P, 1.4);
  m.caja("laton", 0, P - 0.03, 0.25, 0.94, 0.04, 1.44);
  m.caja("bronce", 0, P, bz, 0.56, top - P, 0.56);
  for (const [sx, sz] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) m.caja("laton", sx * 0.29, P, bz + sz * 0.29, 0.06, top - P, 0.06);
  m.caja("ebano", 0, P + 0.02, bz + 0.285, 0.34, 0.22, 0.02);
  m.caja("laton", 0, P + 0.24, bz + 0.29, 0.4, 0.04, 0.04);
  m.engranaje("laton", 0.3, 1.62, bz, "x", 0.22, 0.04, 12, 0.055).engranaje("laton", 0.3, 1.2, bz - 0.12, "x", 0.14, 0.04, 9, 0.045).engranaje("laton", -0.3, 1.45, bz, "x", 0.2, 0.04, 11, 0.05);
  m.engranaje("bronce", -0.33, 1.45, bz, "x", 0.07, 0.04, 0).engranaje("bronce", 0.33, 1.62, bz, "x", 0.08, 0.04, 0);
  m.engranaje("pergamino", 0.1, 1.85, bz + 0.29, "z", 0.09, 0.02, 0, 0, 12).engranaje("laton", 0.1, 1.85, bz + 0.285, "z", 0.11, 0.015, 0, 0, 12);
  m.caja("bronce", 0.1, 1.85, bz + 0.3, 0.012, 0.075, 0.012, 25);
  m.caja("laton", 0, top, bz, 0.66, 0.06, 0.66);
  m.tronco("laton", 8, 0, bz, top + 0.06, TORRE.boca.y, 0.26, TORRE.boca.radio);
  m.tronco("ebano", 8, 0, bz, TORRE.boca.y - 0.012, TORRE.boca.y + 0.002, TORRE.boca.radio - 0.05, TORRE.boca.radio - 0.05);
  m.anillo("senal", 16, 0, bz, TORRE.boca.y, TORRE.boca.y + 0.02, TORRE.boca.radio + 0.01, TORRE.boca.radio - 0.04, 0);
  const tz = 0.62, { ancho, fondo } = TORRE.bandeja;
  m.caja("bronce", 0, P, tz, ancho, 0.05, fondo);
  m.caja("pano", 0, P + 0.05, tz, ancho - 0.08, 0.04, fondo - 0.08);
  m.caja("laton", 0, P + 0.05, tz + fondo / 2 - 0.02, ancho, 0.08, 0.04);
  m.caja("laton", -ancho / 2 + 0.02, P + 0.05, tz, 0.04, 0.08, fondo).caja("laton", ancho / 2 - 0.02, P + 0.05, tz, 0.04, 0.08, fondo);
  guardar(m, "estación de dados: torre de engranajes con bandeja de paño (frente = +z)");
}

// ---- the director's console: a walnut desk facing the table, a slanted writing board, pigeonholes, the small dice tray.
//      Front (+z) = towards the table; whoever uses it stands behind (−z). The two lanterns are separate pieces. ----
{
  const m = casa("consola"), { ancho, fondo, alto } = CONSOLA, b = CONSOLA.bandeja;
  for (const [sx, sz] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) m.caja("madera", sx * (ancho / 2 - 0.06), 0, sz * (fondo / 2 - 0.06), 0.08, alto - 0.05, 0.08);
  m.caja("madera", 0, alto - 0.05, 0, ancho, 0.05, fondo);
  m.caja("madera", 0, alto - 0.3, fondo / 2 - 0.04, ancho - 0.1, 0.25, 0.04);           // the front apron (towards the table)
  for (let k = -1; k <= 1; k++) m.caja("laton", k * 0.55, alto - 0.19, fondo / 2, 0.12, 0.025, 0.02);
  // the slanted board: rises towards the table, so the director reads from behind
  const P = giro(0, 0, 0);
  m.cara("cuero", P(-0.55, alto + 0.01, -0.3), P(0.55, alto + 0.01, -0.3), P(0.55, alto + 0.16, 0.12), P(-0.55, alto + 0.16, 0.12), [0, alto + 2, -1]);
  m.caja("madera", 0, alto, 0.12, 1.14, 0.16, 0.03);
  for (const s of [-1, 1]) {                                                               // pigeonholes at both ends
    m.caja("madera", s * 0.76, alto, -0.05, 0.3, 0.45, 0.5);
    for (let k = 0; k < 3; k++) m.caja("pergamino", s * 0.76, alto + 0.04 + k * 0.14, -0.3, 0.24, 0.08, 0.02);
  }
  // the small tray (local, towards the table), with the turquoise edge: the live part
  const bz = Math.hypot(b.x - CONSOLA.x, b.z - CONSOLA.z);
  m.caja("bronce", 0, alto, bz, b.ancho + 0.04, 0.03, b.fondo + 0.04).caja("pano", 0, alto + 0.03, bz, b.ancho - 0.04, 0.01, b.fondo - 0.04);
  m.anillo("senal", 4, 0, bz, alto + 0.03, alto + 0.045, (b.ancho + 0.04) / Math.SQRT2, (b.ancho - 0.02) / Math.SQRT2, Math.PI / 4);
  guardar(m, "consola de quien dirige: escritorio de nogal con atril inclinado, casilleros y bandeja de dados (frente = +z, hacia la mesa)");
}

// ---- two lanterns (fanales) beside the console: brass post, glass box; the engine's fire burns inside ----
{
  const m = casa("fanal");
  m.tronco("bronce", 6, 0, 0, 0, 0.06, 0.2, 0.18).barra("bronce", [0, 0.06, 0], [0, 1.45, 0], 0.06);
  m.caja("laton", 0, 1.45, 0, 0.32, 0.04, 0.32).caja("cristal", 0, 1.49, 0, 0.28, 0.34, 0.28);
  for (const [sx, sz] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) m.caja("laton", sx * 0.14, 1.49, sz * 0.14, 0.03, 0.34, 0.03);
  m.tronco("laton", 4, 0, 0, 1.83, 1.98, 0.24, 0.02, Math.PI / 4);
  guardar(m, "fanal de latón y cristal sobre poste (el fuego del motor arde dentro, a 1,62 m)");
}

// ---- the consultation plate: a brass easel holding a frame; the picture is an engine hole placed in front. Front = +z ----
{
  const m = casa("placa"), { ancho, alto, y } = PLACAS[0], w = ancho / 2 + 0.05, h = alto / 2 + 0.05;
  m.barra("bronce", [-0.42, 0, 0.18], [-0.2, y + h + 0.1, -0.02], 0.045).barra("bronce", [0.42, 0, 0.18], [0.2, y + h + 0.1, -0.02], 0.045);
  m.barra("bronce", [0, 0, -0.45], [0, y + h, -0.04], 0.04);
  m.caja("ebano", 0, y - h, -0.035, 2 * w, 2 * h, 0.03);                                 // the dark backing
  for (const [a, b] of [[[-w, y - h], [w, y - h]], [[-w, y + h], [w, y + h]], [[-w, y - h], [-w, y + h]], [[w, y - h], [w, y + h]]]) m.barra("laton", [a[0], a[1], -0.01], [b[0], b[1], -0.01], 0.06);
  m.caja("laton", 0, y - h - 0.09, 0, 2 * w + 0.12, 0.05, 0.12);                          // the ledge
  m.caja("senal", 0, y - h - 0.04, 0.05, 0.3, 0.012, 0.02);                              // turquoise tick: the director fills it
  m.engranaje("laton", 0, y + h + 0.1, -0.02, "z", 0.08, 0.02, 10, 0.03);
  guardar(m, "placa de consulta: caballete de bronce con marco de latón (la imagen es un hueco del motor)");
}

// ---- the archive: a map chest of thin drawers, an open atlas and rolled maps on it. Front = +z ----
{
  const m = casa("archivo"), { ancho, fondo, alto } = ARCHIVO;
  m.caja("madera", 0, 0, 0, ancho, alto, fondo);
  for (let k = 0; k < 6; k++) { const y = 0.1 + k * 0.14; m.caja("ebano", 0, y, fondo / 2, ancho - 0.1, 0.11, 0.015); m.caja("laton", -0.5, y + 0.04, fondo / 2 + 0.01, 0.14, 0.025, 0.02).caja("laton", 0.5, y + 0.04, fondo / 2 + 0.01, 0.14, 0.025, 0.02); }
  m.caja("laton", 0, alto, 0, ancho + 0.04, 0.025, fondo + 0.04);
  m.caja("cuero", -0.35, alto + 0.025, 0.05, 0.7, 0.03, 0.5, -6).caja("pergamino", -0.35, alto + 0.055, 0.05, 0.66, 0.012, 0.46, -6); // the atlas
  for (let k = 0; k < 4; k++) m.tronco("pergamino", 6, 0.45 + k * 0.08, -0.15 + (k % 2) * 0.06, alto + 0.03, alto + 0.09, 0.035, 0.035);
  m.caja("bronce", 0.7, alto + 0.025, 0.2, 0.3, 0.12, 0.22);                              // a box of index cards
  guardar(m, "archivo: planero de cajones finos con atlas abierto y mapas enrollados");
}

// ---- the preparation table: a long walnut table, half under a green cloth, with a brass-bound chest ----
{
  const m = casa("preparacion"), { ancho, fondo, alto } = PREPARACION;
  for (const [sx, sz] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) m.caja("madera", sx * (ancho / 2 - 0.08), 0, sz * (fondo / 2 - 0.08), 0.08, alto - 0.05, 0.08);
  m.caja("madera", 0, alto - 0.05, 0, ancho, 0.05, fondo);
  m.caja("pano", 0.6, alto - 0.3, 0, 1.2, 0.3, fondo + 0.02).caja("pano", 0.6, alto, 0, 1.2, 0.006, fondo + 0.02);   // the cloth over the right half
  m.caja("madera", 0.6, alto + 0.006, -0.18, 0.5, 0.26, 0.32).caja("laton", 0.6, alto + 0.006, -0.18, 0.52, 0.03, 0.34).caja("laton", 0.6, alto + 0.24, -0.18, 0.52, 0.03, 0.34);
  m.caja("laton", 0.6, alto + 0.12, -0.01, 0.06, 0.08, 0.02);
  m.caja("pergamino", -0.4, alto, 0.22, 0.42, 0.01, 0.3, 8).caja("ebano", -0.85, alto, -0.25, 0.1, 0.18, 0.1);
  guardar(m, "mesa de preparación: lo que aún no está en el tablero (paño verde y cofre)");
}

// ---- the celestial globe on a brass tripod ----
{
  const m = casa("globo"), { radio, y } = GLOBO;
  for (let k = 0; k < 3; k++) { const a = (k * 120) * RAD; m.barra("bronce", [0.5 * Math.cos(a), 0, 0.5 * Math.sin(a)], [0.12 * Math.cos(a), y - radio - 0.05, 0.12 * Math.sin(a)], 0.05); }
  m.tronco("bronce", 8, 0, 0, y - radio - 0.1, y - radio + 0.02, 0.16, 0.08);
  m.esfera("pano", 0, y, 0, radio, 14);
  m.aro("laton", [0, y, 0], radio + 0.06, [1, 0, 0], [0, 0, 1], 28, 0.04);                // horizon ring
  const t = 23 * RAD; m.aro("laton", [0, y, 0], radio + 0.03, [Math.cos(t), Math.sin(t), 0], [0, 0, 1], 28, 0.03); // the ecliptic
  m.aro("bronce", [0, y, 0], radio + 0.1, [Math.sin(t), Math.cos(t), 0], [1, 0, 0], 28, 0.035); // the meridian, tilted
  for (let k = 0; k < 18; k++) { const a = k * 2.4, b = Math.acos(1 - (2 * (k + 0.5)) / 18); m.icosaedro("luz", radio * 1.005 * Math.sin(b) * Math.cos(a), y + radio * 1.005 * Math.cos(b), radio * 1.005 * Math.sin(b) * Math.sin(a), 0.016); }
  guardar(m, "globo celeste verde petróleo con estrellas, aros de latón y trípode de bronce");
}

// ---- the glass case with the armillary sphere. Front = +z ----
{
  const m = casa("vitrina"), { ancho, fondo, alto } = VITRINA, base = 0.85;
  m.caja("madera", 0, 0, 0, ancho, base, fondo).caja("laton", 0, base, 0, ancho + 0.04, 0.03, fondo + 0.04);
  m.caja("cristal", 0, base + 0.03, 0, ancho - 0.04, alto - base - 0.15, fondo - 0.04);
  for (const [sx, sz] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) m.caja("laton", sx * (ancho / 2 - 0.02), base, sz * (fondo / 2 - 0.02), 0.04, alto - base - 0.1, 0.04);
  m.caja("madera", 0, alto - 0.12, 0, ancho + 0.06, 0.12, fondo + 0.06).caja("laton", 0, alto, 0, ancho + 0.1, 0.04, fondo + 0.1);
  const c = [0, base + 0.55, 0];                                                           // the armillary sphere
  m.tronco("bronce", 6, 0, 0, base + 0.03, base + 0.1, 0.12, 0.08).barra("bronce", [0, base + 0.1, 0], [0, c[1] - 0.22, 0], 0.03);
  m.aro("laton", c, 0.22, [1, 0, 0], [0, 0, 1], 18, 0.018).aro("laton", c, 0.22, [1, 0, 0], [0, 1, 0], 18, 0.018).aro("laton", c, 0.22, [0, 0, 1], [0, 1, 0], 18, 0.018);
  const t = 23 * RAD; m.aro("bronce", c, 0.19, [Math.cos(t), Math.sin(t), 0], [0, 0, 1], 18, 0.025);
  m.esfera("luz", c[0], c[1], c[2], 0.045, 8);
  m.caja("cuero", -0.38, base + 0.03, 0.05, 0.2, 0.05, 0.14).caja("pergamino", 0.38, base + 0.03, 0.02, 0.22, 0.02, 0.16, 12);
  guardar(m, "vitrina de curiosidades con esfera armilar");
}

// ---- the wall clock over the door (inside): a parchment face, brass gear rim, hands ----
{
  const m = casa("reloj"), { x, y, z, radio } = RELOJ;
  m.engranaje("laton", x, y, z + 0.02, "z", radio, 0.04, 24, 0.05);
  m.engranaje("pergamino", x, y, z - 0.01, "z", radio - 0.06, 0.02, 0, 0, 24);
  for (let k = 0; k < 12; k++) { const a = (k * 30) * RAD, r0 = radio - 0.14, r1 = radio - 0.09; m.barra("ebano", [x + r0 * Math.cos(a), y + r0 * Math.sin(a), z - 0.025], [x + r1 * Math.cos(a), y + r1 * Math.sin(a), z - 0.025], 0.018); }
  m.barra("ebano", [x, y, z - 0.03], [x + 0.12, y + 0.18, z - 0.03], 0.025).barra("ebano", [x, y, z - 0.035], [x - 0.25, y - 0.04, z - 0.035], 0.016);
  m.engranaje("bronce", x + 0.5, y + 0.22, z + 0.01, "z", 0.13, 0.03, 10, 0.04).engranaje("bronce", x - 0.52, y + 0.18, z + 0.01, "z", 0.1, 0.03, 8, 0.035);
  guardar(m, "reloj mural de engranajes sobre la puerta");
}

// ---- the miniatures: round brass bases, readable silhouettes from above (§11.K). Heroes: a cloaked figure; creatures:
//      a hunched body with horns, a wider base. Each is its own model (they move). ----
for (const mini of MINIATURAS) {
  const m = new Modelo(`mini-${mini.id}`).material("base", "ambar", { metal: 0.55, rugoso: 0.38 }).material("cuerpo", mini.color, { rugoso: 0.6 }).material("oscuro", "broncesombra", { rugoso: 0.8 });
  if (mini.tipo === "heroe") {
    m.tronco("base", 12, 0, 0, 0, 0.012, 0.042, 0.04);
    m.tronco("cuerpo", 8, 0, 0, 0.012, 0.085, 0.032, 0.016);
    m.esfera("cuerpo", 0, 0.1, 0, 0.019, 8);
    m.barra("oscuro", [0.026, 0.012, 0.01], [0.026, 0.12, 0.01], 0.008);                   // a staff or a spear
  } else {
    m.tronco("base", 12, 0, 0, 0, 0.012, 0.065, 0.062);
    m.icosaedro("cuerpo", 0, 0.06, 0, 0.05, 0.9);
    m.esfera("cuerpo", 0, 0.11, 0.025, 0.026, 8);
    m.barra("oscuro", [-0.014, 0.125, 0.025], [-0.035, 0.16, 0.0], 0.01).barra("oscuro", [0.014, 0.125, 0.025], [0.035, 0.16, 0.0], 0.01);
  }
  guardar(m, `${mini.nombre} (${mini.tipo})`);
}

// ---- the invisible collider: an empty node, no mesh, zero draw calls (the gallery's) ----
{
  const pad = (b, c) => Buffer.concat([b, Buffer.alloc((4 - (b.length % 4)) % 4, c)]);
  const j = pad(Buffer.from(JSON.stringify({ asset: { version: "2.0", generator: "plantillas/galeria modelos.mjs", copyright: "CC0 1.0 (Numen Games, own work)" }, scene: 0, scenes: [{ nodes: [0] }], nodes: [{ name: "choque" }] })), 0x20);
  const cab = Buffer.alloc(20);
  cab.writeUInt32LE(0x46546c67, 0); cab.writeUInt32LE(2, 4); cab.writeUInt32LE(20 + j.length, 8); cab.writeUInt32LE(j.length, 12); cab.writeUInt32LE(0x4e4f534a, 16);
  const bytes = Buffer.concat([cab, j]);
  writeFileSync(new URL("choque.glb", DIR), bytes);
  hechos.choque = { fichero: "modelos/choque.glb", sha: createHash("sha256").update(bytes).digest("hex"), bytes: bytes.length, tris: 0, materiales: 0, min: [0, 0, 0], max: [0, 0, 0], vueltas: 0, notas: "choque invisible: un nodo vacío, sin malla (el mismo de la galería)" };
}

// ---- the whole cabinet as ONE model ----
export const PIEZAS = [
  ...["suelo", "muro", "biblioteca", "techo", "mesa", "cristal", "reloj"].map((modelo) => ({ modelo })),
  ...ASIENTOS.map((a) => ({ modelo: a.dj ? "silla-dj" : "silla", x: a.x, z: a.z, ry: a.ry })),
  { modelo: "torre-dados", x: TORRE.x, z: TORRE.z, ry: TORRE.ry },
  { modelo: "consola", x: CONSOLA.x, z: CONSOLA.z, ry: CONSOLA.ry },
  ...CONSOLA.fanales.map((f) => ({ modelo: "fanal", x: f.x, z: f.z })),
  ...PLACAS.map((p) => ({ modelo: "placa", x: p.x, z: p.z, ry: p.ry })),
  { modelo: "archivo", x: ARCHIVO.x, z: ARCHIVO.z, ry: ARCHIVO.ry },
  { modelo: "preparacion", x: PREPARACION.x, z: PREPARACION.z, ry: PREPARACION.ry },
  { modelo: "globo", x: GLOBO.x, z: GLOBO.z },
  { modelo: "vitrina", x: VITRINA.x, z: VITRINA.z, ry: VITRINA.ry },
];
if (process.argv[1] && import.meta.url.endsWith(process.argv[1].split("/").pop())) {
  const m = casa("gabinete");
  for (const p of PIEZAS) m.unir(piezas[p.modelo], p);
  guardar(m, `el gabinete entero: ${PIEZAS.length} piezas fijas en un solo modelo`);
  writeFileSync(new URL("modelos.json", DIR), JSON.stringify(hechos, null, 1) + "\n");
  for (const [n, h] of Object.entries(hechos)) console.log(`${n.padEnd(22)} ${String(h.tris).padStart(6)} tris · ${h.materiales} mat · ${String(h.bytes).padStart(8)} B · vueltas ${h.vueltas} · ${h.sha.slice(0, 12)}`);
  const t = hechos.gabinete;
  console.log(`gabinete.glb: ${t.tris} triángulos, ${t.materiales} materiales, ${t.bytes} bytes, caja ${JSON.stringify(t.min)} → ${JSON.stringify(t.max)}`);
  if (t.materiales > 12) throw new Error("más de 12 materiales en gabinete.glb");
  const malas = Object.entries(hechos).filter(([, h]) => h.vueltas);
  if (malas.length) console.log("OJO caras al revés:", malas.map(([n, h]) => `${n} ${h.vueltas}`).join(", "));
}
