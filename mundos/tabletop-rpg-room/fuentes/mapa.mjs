// mapa.mjs — the pictures of «El Gabinete», drawn as SVG/HTML in the house palette and photographed with Playwright's
// Chromium into rotulos/*.png (rotulos/rotulos.json: sha256 of each, what construir.mjs copies into the world):
//   mapa-ruinas.png      the sample map on the table (§57: «crea un mapa de ruinas»): 26 × 17 squares, square grid, one
//                        square = 1,5 m in the story (§32: distances in scene units, not room metres); the director
//                        drops their own plan in the same hole.
//   placa-manual.png     the west consultation plate: the minimum manual of the room (§67), what you click.
//   placa-inscripcion.png the east plate: a handout (§59.5: «una inscripción antigua»), the kind of picture the director
//                        puts on a plate for everyone.
// Run: node mapa.mjs  (one headless Chromium at a time on this PC: it takes cache/scratch/fotos.lock)
import { chromium } from "../../numinia-engine/node_modules/playwright-core/index.mjs";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync, openSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { MAPA, PLACAS } from "./medidas.mjs";
import { PALETA as P } from "./glb.mjs";

const CHROME = "/var/home/uruk/arkhitektonian-lab/.cache/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-linux64/chrome-headless-shell";
const FUENTES = new URL("../../numinia-engine/apps/plaza/public/fuentes/", import.meta.url).pathname;
const DIR = new URL("./rotulos/", import.meta.url).pathname;
mkdirSync(DIR, { recursive: true });
const b64 = (f) => readFileSync(`${FUENTES}${f}`).toString("base64");
const FONTS = `@font-face{font-family:Geist;src:url(data:font/woff2;base64,${b64("Geist-Variable.woff2")}) format("woff2");font-weight:100 900}
@font-face{font-family:GeistMono;src:url(data:font/woff2;base64,${b64("GeistMono-Variable.woff2")}) format("woff2");font-weight:100 900}
@font-face{font-family:Bebas;src:url(data:font/woff2;base64,${b64("BebasNeue-Regular.woff2")}) format("woff2")}`;

// ---------- the ruins map ----------
function ruinas() {
  const [W, H] = MAPA.px, C = 60, cols = W / C, filas = H / C;
  const rej = [];
  for (let c = 0; c <= cols; c++) rej.push(`<line x1="${c * C}" y1="0" x2="${c * C}" y2="${H}"/>`);
  for (let r = 0; r <= filas; r++) rej.push(`<line x1="0" y1="${r * C}" x2="${W}" y2="${r * C}"/>`);
  const R = (c, r, w, h, cls) => `<rect class="${cls}" x="${c * C}" y="${r * C}" width="${w * C}" height="${h * C}"/>`;
  // ruins: broken walls (open rectangles drawn as segments), a courtyard, a collapsed tower, a well, an altar, rubble
  const muros = [
    [2, 2, 9, 2], [2, 2, 2, 8], [2, 10, 5, 10], [8, 10, 11, 10], [11, 2, 11, 6], [11, 8, 11, 10],     // the west hall, two gaps
    [14, 3, 21, 3], [21, 3, 21, 7], [14, 3, 14, 5], [14, 7, 14, 9], [14, 9, 18, 9],                     // the chapel, broken roofline
    [4, 12, 4, 15], [4, 15, 10, 15], [13, 13, 13, 15], [13, 15, 17, 15],                                // the south yard walls
  ];
  const lineas = muros.map(([a, b, c, d]) => `<line class="muro" x1="${a * C}" y1="${b * C}" x2="${c * C}" y2="${d * C}"/>`).join("");
  const rnd = (() => { let s = 7; return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296); })();
  const escombros = Array.from({ length: 70 }, () => { const x = rnd() * W, y = rnd() * H, r = 4 + rnd() * 10; return `<circle class="piedra" cx="${x.toFixed(0)}" cy="${y.toFixed(0)}" r="${r.toFixed(0)}"/>`; }).join("");
  const columnas = [[5, 5], [8, 5], [5, 7.5], [8, 7.5], [16, 5], [19, 5]].map(([c, r]) => `<circle class="columna" cx="${c * C}" cy="${r * C}" r="20"/>`).join("") +
    `<circle class="columna rota" cx="${8 * C}" cy="${7.5 * C}" r="20"/>`;
  return `<!doctype html><html><head><meta charset="utf-8"><style>${FONTS}
html,body{margin:0;width:${W}px;height:${H}px;background:${P.arena};overflow:hidden}svg{position:absolute;inset:0}
.rej line{stroke:${P.ceniza};stroke-width:1.2;opacity:.45}
.suelo{fill:${P.arenavelada};opacity:.75}.patio{fill:${P.verde};opacity:.28}
.muro{stroke:${P.bronce};stroke-width:12;stroke-linecap:square}
.piedra{fill:${P.ceniza};opacity:.55}.columna{fill:${P.bronce}}.rota{fill:none;stroke:${P.bronce};stroke-width:5;stroke-dasharray:8 6}
.pozo{fill:${P.turquesahonda};stroke:${P.bronce};stroke-width:6}.altar{fill:${P.ambartostado}}
.torre{fill:none;stroke:${P.bronce};stroke-width:10;stroke-dasharray:40 18}
text{font:600 24px GeistMono,monospace;letter-spacing:.12em;fill:${P.bronce};text-anchor:middle;text-transform:uppercase}
.tit{position:absolute;right:22px;bottom:16px;padding:10px 14px;background:${P.arena};border:2px solid ${P.ambartostado};text-align:right;font:500 30px/1.15 Geist,sans-serif;color:${P.noche}}
.tit small{display:block;font:500 16px/1.5 GeistMono,monospace;letter-spacing:.14em;color:${P.ambartostado};text-transform:uppercase}
.esc{position:absolute;left:22px;bottom:18px;font:500 16px GeistMono,monospace;color:${P.bronce};letter-spacing:.1em}
.esc i{display:inline-block;width:${C}px;height:10px;background:${P.bronce};margin-right:10px;vertical-align:middle}
.norte{position:absolute;left:22px;top:16px;font:400 40px Bebas,sans-serif;color:${P.turquesahonda}}
</style></head><body><svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
${R(2, 2, 9, 8, "suelo")}${R(14, 3, 7, 6, "suelo")}${R(4, 12, 13, 3, "patio")}
<g class="rej">${rej.join("")}</g>${escombros}${lineas}${columnas}
<circle class="torre" cx="${23 * C}" cy="${12 * C}" r="${2.4 * C}"/>
<circle class="pozo" cx="${10 * C}" cy="${13.5 * C}" r="28"/>
${R(16.6, 6, 2, 1, "altar")}
<text x="${6.5 * C}" y="${3.3 * C}">Sala de los pilares</text><text x="${17.5 * C}" y="${8.4 * C}">Capilla</text>
<text x="${23 * C}" y="${12.15 * C}">Torre caída</text><text x="${10 * C}" y="${15.6 * C}">Patio y pozo</text>
</svg><div class="norte">N ↑</div><div class="esc"><i></i>1 casilla = 1,5 m</div>
<div class="tit"><small>Plano de muestra · cámbialo por el tuyo</small>Las ruinas</div></body></html>`;
}

// ---------- a plate: framed text in the cabinet's style ----------
const placaPx = [1200, 900];
function placa(titulo, sub, cuerpo, pie) {
  const [W, H] = placaPx;
  return `<!doctype html><html><head><meta charset="utf-8"><style>${FONTS}
html,body{margin:0;width:${W}px;height:${H}px;background:${P.arena};overflow:hidden}
.m{position:absolute;inset:22px;border:3px solid ${P.ambartostado};padding:42px 54px;box-sizing:border-box}
.m:before{content:"";position:absolute;inset:8px;border:1px solid ${P.ambartostado};opacity:.5}
small{font:600 19px GeistMono,monospace;letter-spacing:.16em;color:${P.turquesahonda};text-transform:uppercase}
h1{font:400 78px/1 Bebas,sans-serif;letter-spacing:.03em;color:${P.noche};margin:8px 0 22px}
.c{font:500 29px/1.42 Geist,sans-serif;color:${P.noche}}.c b{color:${P.ambartostado}}.c p{margin:0 0 12px}
.pie{position:absolute;left:54px;right:54px;bottom:36px;font:500 18px GeistMono,monospace;letter-spacing:.08em;color:${P.bronce}}
.ins{font:400 52px/1.3 Bebas,sans-serif;letter-spacing:.2em;color:${P.bronce};text-align:center;margin:40px 0}
</style></head><body><div class="m"><small>${sub}</small><h1>${titulo}</h1><div class="c">${cuerpo}</div><div class="pie">${pie}</div></div></body></html>`;
}
const MANUAL = placa("Cómo se usa la sala", "El Gabinete · manual mínimo", `
<p><b>Sentarse:</b> acércate a una silla y pulsa <b>E</b>. Otra vez E para levantarte.</p>
<p><b>Dados:</b> en la torre de engranajes (este) pulsa un dado, o escribe la orden <b>/tirar 2d10+3</b>. Tira el servidor: todos veis lo mismo. Marca <b>en secreto</b> y solo tú ves la cifra.</p>
<p><b>El báculo:</b> flota en su soporte junto a la silla alta. <b>E</b> para cogerlo. Quien lo lleva dirige: escenas, narración, luz y niebla. Pásalo con <b>/baculo pasar</b>.</p>
<p><b>El mapa y las placas:</b> quien dirige pulsa sobre ellos y elige su imagen.</p>`,
  "Hablar no ejecuta nada: cada acción es un clic o una tecla.");
const INSCRIPCION = placa("Inscripción", "Ayuda de juego · ejemplo", `
<div class="ins">LO QUE SE NOMBRA<br>SE ABRE</div>
<p>Grabada en el dintel de la capilla, bajo el polvo. Una mano más reciente ha rayado la última palabra.</p>`,
  "Quien dirige cambia esta placa por la suya (pulsar sobre ella).");

// ---------- photograph them (one Chromium at a time on this PC) ----------
const LOCK = new URL("../../../cache/scratch/fotos.lock", import.meta.url).pathname;
openSync(LOCK, "a");
try { execFileSync("flock", ["-w", "120", LOCK, "true"]); } catch { console.log("fotos.lock ocupado: sigo igualmente"); }
const navegador = await chromium.launch({ executablePath: CHROME, args: ["--disable-gpu"] });
const out = {};
for (const [id, html, px] of [["mapa-ruinas", ruinas(), MAPA.px], ["placa-manual", MANUAL, placaPx], ["placa-inscripcion", INSCRIPCION, placaPx]]) {
  const p = await navegador.newPage({ viewport: { width: px[0], height: px[1] } });
  await p.setContent(html, { waitUntil: "load" });
  await p.evaluate(() => document.fonts.ready);
  const png = await p.screenshot({ type: "png" });
  writeFileSync(`${DIR}${id}.png`, png);
  out[id] = { fichero: `rotulos/${id}.png`, sha: createHash("sha256").update(png).digest("hex"), bytes: png.length, px };
  await p.close();
}
await navegador.close();
// the crypt of the first table (plantillas/mesa-rol, «La cripta del espejo roto»): a second map for a scene
{ const png = readFileSync(new URL("../mesa-rol/rotulos/mapa-muestra.png", import.meta.url)); writeFileSync(`${DIR}mapa-cripta.png`, png);
  out["mapa-cripta"] = { fichero: "rotulos/mapa-cripta.png", sha: createHash("sha256").update(png).digest("hex"), bytes: png.length, px: [1320, 900] }; }
writeFileSync(`${DIR}rotulos.json`, JSON.stringify(out, null, 1) + "\n");
for (const [k, v] of Object.entries(out)) console.log(`${k}: ${v.bytes} B ${v.px.join("×")} ${v.sha.slice(0, 12)}`);
void PLACAS;
