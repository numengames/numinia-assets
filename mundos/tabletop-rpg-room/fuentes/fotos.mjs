// fotos.mjs — photos of «El Gabinete» in local with Playwright's Chromium (software GL), and the GPU cost (draw calls,
// triangles) at each one. Two pages: Lía (claims the world with /dueño, takes the staff and puts scenes) and Teo (a
// player). Usage: node fotos.mjs [base] — saves fotos/NN-*.png (needs servicios.sh arrancar + pagina, construir.mjs).
// One headless Chromium at a time on this PC: takes cache/scratch/fotos.lock.
import { chromium } from "../../numinia-engine/node_modules/playwright-core/index.mjs";
import { mkdirSync, readFileSync, openSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { TORRE, ASIENTOS, SOPORTE_BACULO, CONSOLA, PLACAS } from "./medidas.mjs";

const BASE = process.argv[2] ?? "http://127.0.0.1:5321/?webgl2&server=ws://127.0.0.1:8881";
const CHROME = "/var/home/uruk/arkhitektonian-lab/.cache/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-linux64/chrome-headless-shell";
const DIR = new URL("./fotos/", import.meta.url).pathname;
const ADMIN = readFileSync(new URL("./mundo/claves.env", import.meta.url), "utf8").match(/^MOTOR_ADMIN=(.*)$/m)?.[1]; // never printed
mkdirSync(DIR, { recursive: true });
const LOCK = new URL("../../../cache/scratch/fotos.lock", import.meta.url).pathname;
openSync(LOCK, "a");
try { execFileSync("flock", ["-w", "300", LOCK, "true"]); } catch { /* go on */ }
const browser = await chromium.launch({ executablePath: CHROME, args: ["--enable-unsafe-swiftshader", "--use-angle=swiftshader"] });
const errores = [], coste = {};
async function abrir() {
  const p = await (await browser.newContext({ viewport: { width: 1280, height: 720 } })).newPage();
  p.on("pageerror", (e) => errores.push(String(e.message).slice(0, 200)));
  p.on("console", (m) => { if (m.type() === "error") errores.push(m.text().slice(0, 200)); });
  await p.goto(BASE);
  await p.waitForFunction(() => globalThis.__numinia?.session?.entity >= 0, null, { timeout: 90000 });
  return p;
}
function mando(p) {
  const yo = () => p.evaluate(() => { const s = globalThis.__numinia.session.selfPosition(1); return { x: s.x, y: s.y, z: s.z }; });
  const cam = () => p.evaluate(() => globalThis.__numinia.view.cam.estado());
  const rumbo = async () => { const e = await cam(); return Math.atan2(e.pivote.x - e.camara.x, e.pivote.z - e.camara.z); };
  const girar = (a, b = 0) => p.evaluate(([a, b]) => globalThis.__numinia.view.cam.girar(a, b), [a, b]);
  const dif = (a, b) => { let d = a - b; while (d > Math.PI) d -= 2 * Math.PI; while (d < -Math.PI) d += 2 * Math.PI; return d; };
  let signo = null;
  async function mirarRumbo(objetivo) {
    for (let i = 0; i < 6; i++) {
      const r = await rumbo(), d = dif(objetivo, r);
      if (Math.abs(d) < 0.03) return;
      if (signo === null) { await girar(0.2); await p.waitForTimeout(250); signo = Math.sign(dif(await rumbo(), r)) || 1; continue; }
      await girar(d * signo); await p.waitForTimeout(300);
    }
  }
  const mirarA = async (x, z) => { const y = await yo(); await mirarRumbo(Math.atan2(x - y.x, z - y.z)); };
  async function andarHasta(x, z, tol = 0.45) {
    for (let i = 0; i < 8; i++) {
      const y = await yo(), d = Math.hypot(x - y.x, z - y.z);
      if (d < tol) return true;
      await mirarA(x, z);
      await p.keyboard.down("w"); await p.waitForTimeout(Math.min(4000, (d / 1.5) * 1000)); await p.keyboard.up("w");
      await p.waitForTimeout(600);
    }
    const y = await yo(); return Math.hypot(x - y.x, z - y.z) < tol * 2;
  }
  async function inclinar(obj) { await p.evaluate((obj) => { const c = globalThis.__numinia.view.cam; c.mirarHacia(c.estado().yaw, obj); }, obj); await p.waitForTimeout(700); }
  const medir = () => p.evaluate(() => { const i = globalThis.__numinia.view.renderer.info; return { llamadas: i.render.drawCalls ?? i.render.calls, triangulos: i.render.triangles }; });
  const foto = async (n, espera = 2000) => { await p.waitForTimeout(espera); await p.screenshot({ path: `${DIR}${n}.png` }); const y = await yo(); const m = await medir(); coste[n] = m; console.log(n, `en (${y.x.toFixed(2)}, ${y.z.toFixed(2)})`, JSON.stringify(m)); };
  const decir = (texto) => p.evaluate((texto) => globalThis.__numinia.session.features.send("chat", "decir", { texto, alcance: "cerca" }), texto);
  return { yo, mirarA, andarHasta, inclinar, foto, decir, acercar: (k) => p.evaluate((k) => globalThis.__numinia.view.cam.acercar(k), k) };
}

const lia = await abrir(); await lia.waitForTimeout(10000);
const L = mando(lia);
console.log("cajas sólidas que ve la simulación:", await lia.evaluate(() => globalThis.__numinia.session.solidos().length / 6));
await L.acercar(1.6); await L.inclinar(0.22);
await L.mirarA(0, 0); await L.foto("01-llegada");
await L.andarHasta(0, 4.2); await L.mirarA(0, 0); await L.inclinar(0.5); await L.foto("02-mesa-y-mapa");
await L.inclinar(-0.25); await L.foto("03-linterna-y-cristal");

const teo = await abrir(); await teo.waitForTimeout(9000);
const T = mando(teo);
await T.acercar(1.5);
await T.andarHasta(3.6, 3.0); await T.andarHasta(TORRE.bandeja.x - 0.9, TORRE.bandeja.z + 0.6); await T.mirarA(TORRE.x, TORRE.z); await T.inclinar(0.2);
await teo.evaluate(() => globalThis.__numinia.session.features.send("dados", "tirar", { sitio: "engranajes", dados: "2d10+3", oculta: false }));
await T.foto("04-torre-tirada-2d10+3", 2500);
console.log("tirada:", await lia.evaluate(() => document.querySelector("#dados-carta")?.textContent), "|", await teo.evaluate(() => document.querySelector("#dados-carta")?.textContent));

// Lía to the staff's stand, and the console
await L.andarHasta(-3.4, 2.4); await L.andarHasta(-3.7, -1.4); await L.andarHasta(SOPORTE_BACULO.x - 0.9, SOPORTE_BACULO.z + 0.6); await L.mirarA(SOPORTE_BACULO.x, SOPORTE_BACULO.z); await L.inclinar(0.15);
await L.foto("05-soporte-del-baculo");
await L.decir(`/dueño ${ADMIN}`); await lia.waitForTimeout(1500);
await lia.evaluate(() => globalThis.__numinia.session.features.send("baculo", "coger", { cosa: "b1" })).catch(() => {});
await lia.keyboard.press("e"); await lia.waitForTimeout(1500);
await L.andarHasta(CONSOLA.x - 0.2, CONSOLA.z - 0.9); await L.mirarA(0, 0); await L.inclinar(0.35); await L.foto("06-consola-con-baculo");
await L.andarHasta(PLACAS[0].x + 1.4, PLACAS[0].z - 0.4); await L.mirarA(PLACAS[0].x, PLACAS[0].z); await L.inclinar(0.05); await L.foto("07-placa-manual");

// the scenes, seen by Teo from beside the table
await T.andarHasta(2.6, 2.6); await T.andarHasta(1.8, 3.2); await T.mirarA(0, -0.6); await T.inclinar(0.3);
for (const [n, k] of [["08-escena-las-ruinas", 7], ["09-capa-gotica", 3], ["10-capa-cosmologica", 5], ["11-luz-de-trabajo", 2], ["12-escena-la-cripta", 8]]) { await L.decir(`/escena ${k}`); await T.foto(n, 4000); }
await L.decir("/escena 1"); await T.foto("13-vuelta-al-gabinete", 3500);
await L.decir("/tirar d20 oculta"); await T.foto("14-tirada-oculta-vista-por-teo", 1500);
console.log("carta de Teo tras la oculta:", await teo.evaluate(() => document.querySelector("#dados-carta")?.textContent));
writeFileSync(`${DIR}coste.json`, JSON.stringify(coste, null, 1) + "\n");
console.log("errores:", errores.length ? [...new Set(errores)].slice(0, 8).join("\n") : "ninguno");
await browser.close();
void ASIENTOS;
