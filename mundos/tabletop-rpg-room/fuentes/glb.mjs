// glb.mjs — a tiny writer of low-poly glTF 2.0 binaries (.glb) for the gallery template: flat-shaded pieces in the house
// palette (STD-023, closed: 16 colours, no new hex), one primitive per material, deterministic bytes (same input, same
// sha256). Own work, CC0. No dependencies.
//
// Axes as the engine: x east, y up, z south. Every helper writes triangles counter-clockwise seen from outside, so the
// face normal (computed by the engine when it bakes the model, and here for the NORMAL attribute) points out.
import { createHash } from "node:crypto";

/** STD-023 palette (sRGB hex) → glTF wants linear factors. */
export const PALETA = {
  noche: "#14110F", basalto: "#1E1A17", broncesombra: "#292420", bronce: "#3A332D", ceniza: "#8A7D72",
  arenavelada: "#C4B5A6", arena: "#F9EBDC", verdemar: "#A6DAD5", turquesa: "#018EA1", turquesahonda: "#016E7D",
  ambar: "#EFA517", ambartostado: "#7A5100", coral: "#F35059", grana: "#D33440", granahonda: "#B02330", verde: "#8FC46B",
};
const lin = (c) => { const s = c / 255; return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; };
export const linear = (hex) => [1, 3, 5].map((i) => +lin(parseInt(hex.slice(i, i + 2), 16)).toFixed(4));

const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const norm = (v) => { const l = Math.hypot(...v) || 1; return [v[0] / l, v[1] / l, v[2] / l]; };
const r4 = (v) => Math.round(v * 10000) / 10000;

export class Modelo {
  constructor(nombre) { this.nombre = nombre; this.mats = new Map(); this.tris = new Map(); this.centros = []; this.pieza = null; }
  /** The piece being written: its centre, to check that every face looks out of it (null = not convex, unchecked). */
  centro(c) { if (c) { this.centros.push(c); this.pieza = this.centros.length - 1; } else this.pieza = null; return this; }
  /** A material from the palette: name, palette key, and options {metal, rugoso, alfa, brillo (emissive 0–1),
   * soloBrillo (only its own light: black base, so no lamp or sun changes its colour — stars)}. */
  material(id, color, o = {}) {
    const m = { name: id, pbrMetallicRoughness: { baseColorFactor: [...(o.soloBrillo ? [0, 0, 0] : linear(PALETA[color])), o.alfa ?? 1], metallicFactor: o.metal ?? 0, roughnessFactor: o.rugoso ?? 0.85 } };
    if (o.alfa !== undefined && o.alfa < 1) { m.alphaMode = "BLEND"; m.doubleSided = true; }
    if (o.brillo || o.soloBrillo) m.emissiveFactor = linear(PALETA[color]).map((c) => +(c * (o.brillo ?? 1)).toFixed(4));
    this.mats.set(id, m); this.tris.set(id, []);
    return this;
  }
  /** One triangle (three points) in material `mat`. */
  tri(mat, a, b, c) { this.tris.get(mat).push([a, b, c, this.pieza]); }
  quad(mat, a, b, c, d) { this.tri(mat, a, b, c); this.tri(mat, a, c, d); }

  /** Add another model's triangles, placed at (x, y, z) and turned `ry` degrees about y (three.js: a point (px, pz)
   * goes to (px cos + pz sin, −px sin + pz cos), as the engine turns an object): many pieces, one model, fewer draws. */
  unir(otro, { x = 0, y = 0, z = 0, ry = 0 } = {}) {
    const R = (ry * Math.PI) / 180, c = Math.cos(R), s = Math.sin(R);
    const t = ([px, py, pz]) => [x + px * c + pz * s, y + py, z - px * s + pz * c];
    const base = this.centros.length;
    for (const k of otro.centros) this.centros.push(t(k));
    for (const [id, lista] of otro.tris) {
      if (!lista.length) continue;
      if (!this.mats.has(id)) { this.mats.set(id, otro.mats.get(id)); this.tris.set(id, []); }
      const destino = this.tris.get(id);
      for (const [a, b, d, p] of lista) destino.push([t(a), t(b), t(d), p === null || p === undefined ? null : base + p]);
    }
    return this;
  }

  /** Box: centre x, base y0, centre z, size sx × sy × sz, turned `giro` degrees about y around its own centre. */
  caja(mat, x, y0, z, sx, sy, sz, giro = 0) {
    const R = (giro * Math.PI) / 180, c = Math.cos(R), s = Math.sin(R);
    const p = (dx, dy, dz) => [x + dx * c + dz * s, y0 + dy, z - dx * s + dz * c];
    const hx = sx / 2, hz = sz / 2;
    const v = [p(-hx, 0, -hz), p(hx, 0, -hz), p(hx, 0, hz), p(-hx, 0, hz), p(-hx, sy, -hz), p(hx, sy, -hz), p(hx, sy, hz), p(-hx, sy, hz)];
    this.centro([x, y0 + sy / 2, z]);
    this.quad(mat, v[3], v[2], v[6], v[7]); // +z (south)
    this.quad(mat, v[1], v[0], v[4], v[5]); // -z
    this.quad(mat, v[2], v[1], v[5], v[6]); // +x
    this.quad(mat, v[0], v[3], v[7], v[4]); // -x
    this.quad(mat, v[4], v[7], v[6], v[5]); // top
    this.quad(mat, v[0], v[1], v[2], v[3]); // bottom
    return this;
  }
  /** n-sided frustum (a prism when r0 = r1): centre x, z; from y0 (radius r0) to y1 (radius r1); `fase` turns it. */
  tronco(mat, n, x, z, y0, y1, r0, r1, fase = Math.PI / n, tapas = true) {
    const pt = (i, y, r) => { const a = fase + (2 * Math.PI * i) / n; return [x + r * Math.cos(a), y, z + r * Math.sin(a)]; };
    this.centro([x, (y0 + y1) / 2, z]);
    for (let i = 0; i < n; i++) {
      const a0 = pt(i, y0, r0), a1 = pt(i + 1, y0, r0), b0 = pt(i, y1, r1), b1 = pt(i + 1, y1, r1);
      if (r1 === 0) this.tri(mat, a0, b0, a1);       // a cone: one triangle per side, never a degenerate one
      else if (r0 === 0) this.tri(mat, a0, b0, b1);  // upside down
      else this.quad(mat, a0, b0, b1, a1);           // seen from outside: counter-clockwise (angle grows from +x to +z)
    }
    if (tapas) {
      for (let i = 1; i < n - 1; i++) {
        if (r1 > 0) this.tri(mat, pt(0, y1, r1), pt(i + 1, y1, r1), pt(i, y1, r1)); // top, facing up
        if (r0 > 0) this.tri(mat, pt(0, y0, r0), pt(i, y0, r0), pt(i + 1, y0, r0)); // bottom, facing down
      }
    }
    return this;
  }
  /** n-sided ring (a basin wall): outer radius ro, inner ri, from y0 to y1. `fondo` (mesa-rol): also its underside, for
   * a ring you see from below (the drum over the windows, a cornice, a hanging lamp). */
  anillo(mat, n, x, z, y0, y1, ro, ri, fase = Math.PI / n, fondo = false) {
    const pt = (i, y, r) => { const a = fase + (2 * Math.PI * i) / n; return [x + r * Math.cos(a), y, z + r * Math.sin(a)]; };
    this.centro(null);
    for (let i = 0; i < n; i++) {
      this.quad(mat, pt(i, y0, ro), pt(i, y1, ro), pt(i + 1, y1, ro), pt(i + 1, y0, ro)); // outer face
      this.quad(mat, pt(i + 1, y0, ri), pt(i + 1, y1, ri), pt(i, y1, ri), pt(i, y0, ri)); // inner face (looks in)
      this.quad(mat, pt(i, y1, ro), pt(i, y1, ri), pt(i + 1, y1, ri), pt(i + 1, y1, ro)); // top
      if (fondo) this.quad(mat, pt(i + 1, y0, ro), pt(i + 1, y0, ri), pt(i, y0, ri), pt(i, y0, ro)); // underside
    }
    return this;
  }
  /** (mesa-rol) The INSIDE of an n-sided frustum, seen from within (a ceiling under a conical roof): side faces only,
   * looking towards the axis. Both radii > 0. */
  troncoDentro(mat, n, x, z, y0, y1, r0, r1, fase = Math.PI / n) {
    const pt = (i, y, r) => { const a = fase + (2 * Math.PI * i) / n; return [x + r * Math.cos(a), y, z + r * Math.sin(a)]; };
    this.centro(null);
    for (let i = 0; i < n; i++) this.quad(mat, pt(i, y0, r0), pt(i + 1, y0, r0), pt(i + 1, y1, r1), pt(i, y1, r1));
    return this;
  }
  /** (mesa-rol) A flat four-point face whose normal looks towards point `hacia` (a rib on a ceiling, a strip). */
  cara(mat, a, b, c, d, hacia) {
    const n = cross(sub(b, a), sub(c, a)), v = sub(hacia, a);
    this.centro(null);
    if (n[0] * v[0] + n[1] * v[1] + n[2] * v[2] >= 0) this.quad(mat, a, b, c, d); else this.quad(mat, d, c, b, a);
    return this;
  }
  /** (mesa-rol) A gear standing up: a toothed disc centred at (x, y, z) whose axis is `eje` ("x" or "z"), radius r to the
   * root of the teeth, teeth `alto` long (`dientes` 0 = a plain disc: a gauge, a wheel), `grosor` thick along the axis.
   * Star-shaped round its centre: faces are wound by hand (front cap looks +eje). */
  engranaje(mat, x, y, z, eje, r, grosor, dientes = 10, alto = 0.06, lados = 0) {
    const A = eje === "x" ? [1, 0, 0] : [0, 0, 1], U = eje === "x" ? [0, 1, 0] : [1, 0, 0], V = eje === "x" ? [0, 0, 1] : [0, 1, 0];
    const perfil = []; // [angle, radius] counter-clockwise seen from +A (U × V = A)
    if (dientes > 0) {
      const paso = (2 * Math.PI) / dientes;
      for (let k = 0; k < dientes; k++) {
        const a = k * paso;
        perfil.push([a, r], [a + paso * 0.18, r + alto], [a + paso * 0.5, r + alto], [a + paso * 0.68, r]);
      }
    } else { const n = lados || 12; for (let k = 0; k < n; k++) perfil.push([(2 * Math.PI * k) / n, r]); }
    const h = grosor / 2, P = (a, rr, s) => [x + rr * (Math.cos(a) * U[0] + Math.sin(a) * V[0]) + s * h * A[0], y + rr * (Math.cos(a) * U[1] + Math.sin(a) * V[1]) + s * h * A[1], z + rr * (Math.cos(a) * U[2] + Math.sin(a) * V[2]) + s * h * A[2]];
    const cf = [x + h * A[0], y + h * A[1], z + h * A[2]], cb = [x - h * A[0], y - h * A[1], z - h * A[2]];
    this.centro(null);
    for (let i = 0; i < perfil.length; i++) {
      const [a0, r0] = perfil[i], [a1, r1] = perfil[(i + 1) % perfil.length];
      const f0 = P(a0, r0, 1), f1 = P(a1, r1, 1), b0 = P(a0, r0, -1), b1 = P(a1, r1, -1);
      this.tri(mat, cf, f0, f1);       // front cap, looks +A
      this.tri(mat, cb, b1, b0);       // back cap, looks −A
      this.quad(mat, b0, b1, f1, f0);  // rim, looks out
    }
    return this;
  }
  /** Icosahedron of radius r centred at (x, y, z): 20 faces (a tree crown, an orange, a sculpture). */
  icosaedro(mat, x, y, z, r, sy = 1) {
    const t = (1 + Math.sqrt(5)) / 2;
    const raw = [[-1, t, 0], [1, t, 0], [-1, -t, 0], [1, -t, 0], [0, -1, t], [0, 1, t], [0, -1, -t], [0, 1, -t], [t, 0, -1], [t, 0, 1], [-t, 0, -1], [-t, 0, 1]];
    const v = raw.map((p) => { const n = norm(p); return [x + n[0] * r, y + n[1] * r * sy, z + n[2] * r]; });
    const f = [[0, 11, 5], [0, 5, 1], [0, 1, 7], [0, 7, 10], [0, 10, 11], [1, 5, 9], [5, 11, 4], [11, 10, 2], [10, 7, 6], [7, 1, 8],
      [3, 9, 4], [3, 4, 2], [3, 2, 6], [3, 6, 8], [3, 8, 9], [4, 9, 5], [2, 4, 11], [6, 2, 10], [8, 6, 7], [9, 8, 1]];
    this.centro([x, y, z]);
    for (const [a, b, c] of f) this.tri(mat, v[a], v[b], v[c]);
    return this;
  }
  /** Flat slab of a polygon given east → south → west → north (the angle grows from +x towards +z, like `tronco`), from
   * y0 to y1. */
  losa(mat, puntos, y0, y1) {
    const n = puntos.length, top = puntos.map(([x, z]) => [x, y1, z]), bot = puntos.map(([x, z]) => [x, y0, z]);
    this.centro([puntos.reduce((s, p) => s + p[0], 0) / n, (y0 + y1) / 2, puntos.reduce((s, p) => s + p[1], 0) / n]);
    for (let i = 1; i < n - 1; i++) { this.tri(mat, top[0], top[i + 1], top[i]); this.tri(mat, bot[0], bot[i], bot[i + 1]); }
    for (let i = 0; i < n; i++) { const j = (i + 1) % n; this.quad(mat, bot[i], top[i], top[j], bot[j]); }
    return this;
  }

  /** (gabinete) A quad wound so that its normal looks away from `dentro` (the piece's centre). */
  quadFuera(mat, a, b, c, d, dentro) {
    const n = cross(sub(b, a), sub(c, a)), m = [(a[0] + c[0]) / 2 - dentro[0], (a[1] + c[1]) / 2 - dentro[1], (a[2] + c[2]) / 2 - dentro[2]];
    if (n[0] * m[0] + n[1] * m[1] + n[2] * m[2] >= 0) this.quad(mat, a, b, c, d); else this.quad(mat, d, c, b, a);
    return this;
  }
  /** (gabinete) A bar of square section `g` from point a to point b, in any direction (rings, rails, a staff, ribs). */
  barra(mat, a, b, g = 0.04) {
    const d = norm(sub(b, a)), ref = Math.abs(d[1]) < 0.9 ? [0, 1, 0] : [1, 0, 0];
    const u = norm(cross(d, ref)), v = cross(d, u), h = g / 2;
    const P = (p, su, sv) => [p[0] + (u[0] * su + v[0] * sv) * h, p[1] + (u[1] * su + v[1] * sv) * h, p[2] + (u[2] * su + v[2] * sv) * h];
    const k = [[-1, -1], [1, -1], [1, 1], [-1, 1]], A = k.map(([s, t]) => P(a, s, t)), B = k.map(([s, t]) => P(b, s, t));
    const c = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2];
    this.centro(c);
    for (let i = 0; i < 4; i++) { const j = (i + 1) % 4; this.quadFuera(mat, A[i], A[j], B[j], B[i], c); }
    this.quadFuera(mat, A[0], A[1], A[2], A[3], c).quadFuera(mat, B[0], B[1], B[2], B[3], c);
    return this;
  }
  /** (gabinete) A ring of `n` bars: centre c, radius r, in the plane spanned by unit vectors u and v. */
  aro(mat, c, r, u, v, n = 16, g = 0.03) {
    const P = (i) => { const a = (2 * Math.PI * i) / n; return [c[0] + r * (Math.cos(a) * u[0] + Math.sin(a) * v[0]), c[1] + r * (Math.cos(a) * u[1] + Math.sin(a) * v[1]), c[2] + r * (Math.cos(a) * u[2] + Math.sin(a) * v[2])]; };
    for (let i = 0; i < n; i++) this.barra(mat, P(i), P(i + 1), g);
    return this;
  }
  /** (gabinete) A low-poly sphere (`seg` around, seg/2 from pole to pole), radius r, flattened by sy. */
  esfera(mat, x, y, z, r, seg = 10, sy = 1) {
    const filas = Math.max(3, Math.round(seg / 2)), c = [x, y, z];
    const P = (i, j) => { const t = (Math.PI * j) / filas, a = (2 * Math.PI * i) / seg; return [x + r * Math.sin(t) * Math.cos(a), y + r * sy * Math.cos(t), z + r * Math.sin(t) * Math.sin(a)]; };
    this.centro(c);
    for (let j = 0; j < filas; j++) for (let i = 0; i < seg; i++) {
      const a = P(i, j), b = P(i + 1, j), d = P(i + 1, j + 1), e = P(i, j + 1);
      if (j === 0) { const n = cross(sub(d, a), sub(e, a)), m = sub(a, c); if (n[0] * m[0] + n[1] * m[1] + n[2] * m[2] >= 0) this.tri(mat, a, d, e); else this.tri(mat, a, e, d); }
      else if (j === filas - 1) { const n = cross(sub(b, a), sub(e, a)), m = sub(e, c); if (n[0] * m[0] + n[1] * m[1] + n[2] * m[2] >= 0) this.tri(mat, a, b, e); else this.tri(mat, a, e, b); }
      else this.quadFuera(mat, a, b, d, e, c);
    }
    return this;
  }

  /** Triangles whose normal points INTO their own piece (a box, a prism, an icosahedron): must be 0. */
  vueltas() {
    let mal = 0;
    for (const lista of this.tris.values()) for (const [a, b, c, p] of lista) {
      if (p === null || p === undefined) continue;
      const k = this.centros[p], n = cross(sub(b, a), sub(c, a)), m = [(a[0] + b[0] + c[0]) / 3, (a[1] + b[1] + c[1]) / 3, (a[2] + b[2] + c[2]) / 3];
      if (n[0] * (m[0] - k[0]) + n[1] * (m[1] - k[1]) + n[2] * (m[2] - k[2]) < -1e-9) mal++;
    }
    return mal;
  }

  /** The .glb bytes, its sha256 and its numbers (triangles, materials, box). `o.copyright` / `o.generador` replace the
   * defaults (a model made from someone else's data carries their licence: the sky of the theatre, HYG CC BY-SA). */
  glb(o = {}) {
    const meshesPrims = [], accessors = [], views = [], chunks = [];
    let offset = 0, triTotal = 0;
    const min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
    const matIds = [...this.mats.keys()].filter((id) => this.tris.get(id).length);
    matIds.forEach((id, mi) => {
      const lista = this.tris.get(id), P = new Float32Array(lista.length * 9), N = new Float32Array(lista.length * 9);
      const pmin = [Infinity, Infinity, Infinity], pmax = [-Infinity, -Infinity, -Infinity];
      lista.forEach(([a, b, c], i) => {
        const n = norm(cross(sub(b, a), sub(c, a)));
        [a, b, c].forEach((p, k) => {
          for (let d = 0; d < 3; d++) {
            const v = r4(p[d]); P[i * 9 + k * 3 + d] = v; N[i * 9 + k * 3 + d] = r4(n[d]);
            pmin[d] = Math.min(pmin[d], v); pmax[d] = Math.max(pmax[d], v);
          }
        });
      });
      for (let d = 0; d < 3; d++) { min[d] = Math.min(min[d], pmin[d]); max[d] = Math.max(max[d], pmax[d]); }
      for (const arr of [P, N]) { chunks.push(Buffer.from(arr.buffer)); views.push({ buffer: 0, byteOffset: offset, byteLength: arr.byteLength, target: 34962 }); offset += arr.byteLength; }
      accessors.push({ bufferView: views.length - 2, componentType: 5126, count: lista.length * 3, type: "VEC3", min: pmin.map((v) => Math.fround(v)), max: pmax.map((v) => Math.fround(v)) });
      accessors.push({ bufferView: views.length - 1, componentType: 5126, count: lista.length * 3, type: "VEC3" });
      meshesPrims.push({ attributes: { POSITION: accessors.length - 2, NORMAL: accessors.length - 1 }, material: mi });
      triTotal += lista.length;
    });
    const gltf = {
      asset: { version: "2.0", generator: o.generador ?? "plantillas/galeria glb.mjs", copyright: o.copyright ?? "CC0 1.0 (Numen Games, own work)" },
      scene: 0, scenes: [{ nodes: [0] }], nodes: [{ mesh: 0, name: this.nombre }],
      meshes: [{ name: this.nombre, primitives: meshesPrims }],
      materials: matIds.map((id) => this.mats.get(id)),
      accessors, bufferViews: views, buffers: [{ byteLength: offset }],
    };
    const bin = Buffer.concat(chunks), binP = Buffer.concat([bin, Buffer.alloc((4 - (bin.length % 4)) % 4)]);
    const json = Buffer.from(JSON.stringify(gltf), "utf8"), jsonP = Buffer.concat([json, Buffer.alloc((4 - (json.length % 4)) % 4, 0x20)]);
    const head = Buffer.alloc(12), jh = Buffer.alloc(8), bh = Buffer.alloc(8);
    head.writeUInt32LE(0x46546c67, 0); head.writeUInt32LE(2, 4); head.writeUInt32LE(12 + 8 + jsonP.length + 8 + binP.length, 8);
    jh.writeUInt32LE(jsonP.length, 0); jh.writeUInt32LE(0x4e4f534a, 4); bh.writeUInt32LE(binP.length, 0); bh.writeUInt32LE(0x004e4942, 4);
    const bytes = Buffer.concat([head, jh, jsonP, bh, binP]);
    return { bytes, sha: createHash("sha256").update(bytes).digest("hex"), tris: triTotal, materiales: matIds.length, min, max };
  }
}
