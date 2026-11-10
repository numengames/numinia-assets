// medidas.mjs — the measures of «El Gabinete»: the universal role-playing room, v1.2 of the spec
// (attachments/Numinia_Sala_Rol_3D_v1_2.md, §8–11 and III.A). Shared by the models (modelos.mjs), the maps (mapa.mjs),
// the layout (trazado.mjs) and the world files (aventura.mjs).
//
// Convention of the spec (§8) = convention of the engine: Y up, floor XZ, metres, origin on the floor under the table.
// Angles in the plan («angulo») grow from +x (0°) towards +z (90°): 0° = east, 90° = south (the door), 270° = north
// (the director). A model faces +z at ry 0; ry = atan2(dx, dz) towards what it looks at.
//
// The room: a decagonal cabinet of wonders (§11.C: «planta circular, octogonal o decagonal»), X,Z −12…+12. Dark walnut
// walls lined with books, a clerestory of high windows over the shelves, a coffered ceiling at 4 m with a glass lantern
// over the table (the sun falls on the table: the table is the focus, §11.E.1). In the middle the table (Ø 3.60 m, top
// at 0.90, things stand at 0.92), eight seats at 2.55 m, the narrative crystal hanging over the map, and round the room
// the functional zones of §9: dice station, archive, consultation plates, the director's console, the staff's stand, the
// preparation area and the curiosities (§11.I.4).

const r3 = (v) => Math.round(v * 1000) / 1000;
const RAD = Math.PI / 180;
/** A point of the plan at `angulo` degrees and radius r. */
export const polar = (angulo, r) => ({ x: r3(r * Math.cos(angulo * RAD)), z: r3(r * Math.sin(angulo * RAD)) });
/** ry of something at (x, z) that looks at (tx, tz). */
export const rumboHacia = (x, z, tx = 0, tz = 0) => Math.round((Math.atan2(tx - x, tz - z) * 180) / Math.PI);
/** A point on a wall (or anything tangent to a circle): `t` metres along the tangent at `angulo`, radius r. A box
 * placed there with giro = 90 − angulo runs along the wall (its local z is the radial direction). */
export const enPared = (angulo, r, t = 0) => ({ x: r3(r * Math.cos(angulo * RAD) + t * Math.sin(angulo * RAD)), z: r3(r * Math.sin(angulo * RAD) - t * Math.cos(angulo * RAD)) });

/** The room: 10 sides, each centred on 90° + 36°·k (the door side is the south one, 90°; the director's wall the north
 * one, 270°). apotema = to the inner face of the wall. Ceiling at 4.00 (§8), the lantern over the table up to 6.6. */
export const SALA = {
  lados: 10, apotema: 11.4, grosor: 0.5, alto: 4.0, techo: 0.3,
  ventana: { y0: 3.0, y1: 3.7, ancho: 4.0 },           // the clerestory: one high window per side, over the shelves
  linterna: { radio: 3.0, y1: 5.6, cupula: 6.5, oculo: 0.5, remate: 6.75 },
};
SALA.medioLado = r3(SALA.apotema * Math.tan(18 * RAD));                 // half the inner length of a side: 3.704
SALA.medioLadoFuera = r3((SALA.apotema + SALA.grosor) * Math.tan(18 * RAD));
SALA.radioVertice = r3((SALA.apotema + SALA.grosor) / Math.cos(18 * RAD)); // the floor and ceiling decagon (outer)
export const LADOS = Array.from({ length: SALA.lados }, (_, k) => ({ angulo: (90 + 36 * k) % 360, puerta: k === 0 }));

/** The door: the south side, 2.4 m wide, 3.1 m high; the engine's door (interacciones.json) swings in it. */
export const PUERTA = { angulo: 90, x: 0, z: r3(SALA.apotema + SALA.grosor / 2), ancho: 2.4, alto: 3.1 };

/** The table (§8, §11.F): round, Ø 3.60, its visible top at 0.90; the placing plane (map, miniatures) at 0.92. The green
 * baize goes to `pano`, the brass rail at `riel`, and the rim between rail and edge holds the eight personal zones. */
export const MESA = { x: 0, z: 0, radio: 1.8, alto: 0.9, colocar: 0.92, pano: 1.55, riel: 1.6, escena: 1.55 };
/** The map: an engine hole lying flat on the table (rx −90), top edge to the north (the director). It fits inside the
 * active-scene radius (half diagonal 1.553 m ≈ 1.55, §8). px = the picture size of the sample maps. */
export const MAPA = { x: 0, y: MESA.colocar, z: 0, ancho: 2.6, alto: 1.7, rx: -90, ry: 0, px: [1560, 1020] };

/** The eight seats of the spec (§8): r 2.55, every 45°, seat_0 at +x. They look at the table. The director's chair is
 * seat_6 (270°, north, facing the door): same family, a taller back (§11.I.1: «sin convertirlo en un trono»).
 * pie = where to stand beside it to sit down (0.6 m to its side). */
export const RADIO_ASIENTOS = 2.55;
export const ASIENTO_DJ = 6;
export const ASIENTOS = Array.from({ length: 8 }, (_, i) => {
  const angulo = 45 * i, p = polar(angulo, RADIO_ASIENTOS), dj = i === ASIENTO_DJ;
  return { i, id: `seat_${i}`, x: p.x, z: p.z, ry: rumboHacia(p.x, p.z), angulo, dj, pie: polar(angulo - 13.5, RADIO_ASIENTOS),
    etiqueta: dj ? "Asiento de quien dirige" : `Asiento ${i + 1}` };
});

/** The narrative crystal (§11.H, §17): a faceted prism hanging over the map in a brass gimbal, amber core. High enough
 * not to cut the line between two seated people (eyes ~1.2 m) nor the top view of the map. */
export const CRISTAL = { x: 0, z: 0, y: 2.45, alto: 0.62, radio: 0.2, aro: 0.42, cuelga: SALA.alto };

/** The dice station (§9.2, §21): the gear tower to the east, its tray towards the table. The engine rolls; the 3D dice
 * fall on the tray (dados.json «bandeja»: dx, dz from the tower, height of its floor, size). */
export const TORRE = (() => {
  const x = 4.6, z = 0, ry = rumboHacia(x, z);
  const R = ry * RAD, c = Math.cos(R), s = Math.sin(R), mundo = (px, pz) => ({ x: r3(x + px * c + pz * s), z: r3(z - px * s + pz * c) });
  const bandeja = mundo(0, 0.62), plinto = mundo(0, 0.25);
  return { x, z, ry, alto: 2.47, boca: { y: 2.45, radio: 0.4 }, bandeja: { ...bandeja, dx: r3(bandeja.x - x), dz: r3(bandeja.z - z), y: 0.89, ancho: 0.8, fondo: 0.6 },
    plinto: { ...plinto, ancho: 0.9, fondo: 1.4, alto: 0.8 } };
})();

/** The director's console (§9.5): a writing desk behind the director's chair, facing the table, with its own small
 * dice tray (the director's hidden rolls) and two lanterns (the engine's fire burns in them). */
export const CONSOLA = (() => {
  const p = polar(270, 4.7), ry = rumboHacia(p.x, p.z);
  return { ...p, ry, ancho: 1.8, fondo: 0.8, alto: 0.95, bandeja: { ...polar(270, 4.42), y: 0.95, ancho: 0.42, fondo: 0.3 },
    fanales: [polar(259, 4.9), polar(281, 4.9)].map((q, k) => ({ ...q, y: 1.62, nombre: k ? "Fanal este de la consola" : "Fanal oeste de la consola" })) };
})();

/** The staff's stand (§9.6, §12): beside the director's chair. The engine draws the pedestal and the staff
 * (features/baculo); this is only where. */
export const SOPORTE_BACULO = polar(250, 3.45);

/** Consultation plates (§9.4, §39): two framed glass plates on brass easels, facing the table: engine holes where the
 * director puts a handout, a portrait, a clue (a picture). marco = the frame in metres; y = its centre. */
export const PLACAS = [150, 30].map((angulo, k) => {
  const p = polar(angulo, 5.3);
  return { ...p, angulo, ry: rumboHacia(p.x, p.z), y: 1.55, ancho: 1.2, alto: 0.9, nombre: k ? "Placa de consulta este" : "Placa de consulta oeste" };
});

/** The archive (§9.3): a map chest and a reading desk under the west shelves, with the library ladder. */
export const ARCHIVO = (() => { const p = polar(180, 7.2); return { ...p, ry: rumboHacia(p.x, p.z), ancho: 2.2, fondo: 1.0, alto: 0.95 }; })();
/** The preparation area (§9.7): a long table with what is not yet on the board (miniatures, a chest). North-east. */
export const PREPARACION = (() => { const p = polar(318, 6.6); return { ...p, ry: rumboHacia(p.x, p.z), ancho: 2.4, fondo: 0.9, alto: 0.9 }; })();
/** Curiosities (§11.I.4): the celestial globe (south-west) and the armillary sphere in its glass case (north-west). */
export const GLOBO = { ...polar(208, 6.5), radio: 0.62, y: 1.45 };
export const VITRINA = (() => { const p = polar(228, 7.6); return { ...p, ry: rumboHacia(p.x, p.z), ancho: 1.2, fondo: 0.6, alto: 2.1 }; })();
/** The wall clock over the door, inside (a gear face looking at the room). */
export const RELOJ = { x: 0, y: 3.45, z: r3(SALA.apotema - 0.06), radio: 0.42 };
/** The floor compass rose round the seats (a brass ring and eight points between the chairs). */
export const ROSA = { r0: 3.25, r1: 3.35, punta: 4.3 };

/** The voice bubble (features/burbujas): the table, the console, the tower and the staff inside; the shelves outside. */
export const BURBUJA = { x: 0, z: 0, radio: 5.4 };

/** Where people appear: inside, by the door, looking at the table. */
export const APARICION = { x: 0, z: 7.6, radio: 1, rumbo: rumboHacia(0, 7.6) };

/** Miniatures (§26, §34): low-poly figures on round brass bases. Two on the sample map, the rest on the preparation
 * table. The engine moves them as objects (whoever may build); their sheets live apart (§6: «la miniatura no es la
 * ficha»). kind: heroe | criatura. */
export const MINIATURAS = [
  { id: "arlen", nombre: "Miniatura: Arlen", tipo: "heroe", color: "turquesa", en: "mapa", x: -0.55, z: 0.42 },
  { id: "guardiana", nombre: "Miniatura: la guardiana", tipo: "heroe", color: "ambar", en: "mapa", x: -0.32, z: 0.5 },
  { id: "erudito", nombre: "Miniatura: el erudito", tipo: "heroe", color: "verdemar", en: "prep", t: -0.95 },
  { id: "exploradora", nombre: "Miniatura: la exploradora", tipo: "heroe", color: "coral", en: "prep", t: -0.7 },
  { id: "sombra", nombre: "Miniatura: criatura de sombra", tipo: "criatura", color: "granahonda", en: "prep", t: 0.35 },
  { id: "guardian-piedra", nombre: "Miniatura: guardián de piedra", tipo: "criatura", color: "ceniza", en: "prep", t: 0.75 },
];
