// aventura.mjs — writes the world files of «El Gabinete de los Mil Mundos» (v2.1) into mundo/ (the world folder).
// Script: attachments/Numinia_Gabinete_Mil_Mundos_Guion_Completo_v2_1.md (2026-11-04). This is its «Entrega A» (§16.8):
// the tutorial walk E0–E13 with what the engine CAN check today; the rest is listed in ENTREGA.md as [MOTOR].
//   aventura/ficha.json   what the world is (ficha@1)
//   aventura/pnj          Senet (game master, host), Rima (dice), Lyra (archive), Pictor (maps), Cepheus (custody)
//   aventura/misiones     M1–M12 of §8, one after another, each given by the person of its station
//   aventura/cosas        the things a step clicks (E): mirror, Iria's sheet, the Eco's sheet, miniature, square, door,
//                         lamp, turn clock, Atlas page, deck, table, gramophone, crystal, the Mercury Key, pedestal, Atlas
//   aventura/secuencias   SEQ_040: the Atlas wakes (only the player who wakes it sees it)
//   aventura/escenas      the director's scenes (escena@1), unchanged from v2
//   entorno/*.json        the room's light and engine pieces (seats, ONE staff, voice bubble, dice towers, counters)
// Rules kept from the script: every text the player reads goes in «usted» (Pablo, 2026-11-03); a step is done only by an
// event the SERVER sees (a click/E on a thing, a conversation, arriving somewhere: §6.0); nothing is said by voice to act;
// no badge, seal or reward (§0: «sin sellos ni recompensas»). Texts are checked in bytes before writing (§7).
// Positions: the engine's convention (−z north; one arrives at the south door looking north at the table). The script's
// §3.2 has the arrival at the north looking south, so its anchors are mirrored in z. Run: node aventura.mjs
import { mkdirSync, writeFileSync, rmSync, readdirSync, readFileSync } from "node:fs";
import { MESA, ASIENTOS, TORRE, CONSOLA, SOPORTE_BACULO, BURBUJA, APARICION, PREPARACION, PLACAS, polar } from "./medidas.mjs";

const M = new URL("./mundo/", import.meta.url).pathname;
const ROTULOS = JSON.parse(readFileSync(new URL("./rotulos/rotulos.json", import.meta.url), "utf8"));
const ROBOT = "047f5e5fb3bb6d378bd1df16ca6137f2a596c99b3a1b5690b4020c05aaf6f319"; // the house robot (library, CC0)
for (const d of ["aventura/pnj", "aventura/misiones", "aventura/cosas", "aventura/escenas", "aventura/secuencias", "entorno"]) { rmSync(M + d, { recursive: true, force: true }); mkdirSync(M + d, { recursive: true }); }
const escribir = (ruta, o) => writeFileSync(M + ruta, JSON.stringify(o, null, 2) + "\n");
/** giro for a library model that faces −z (the house robot). */
const mirar = (x, z, tx, tz) => Math.round((Math.atan2(-(tx - x), -(tz - z)) * 180) / Math.PI);
const HUECO_MAPA = "mapa";
const VERSION = "gabinete-mil-mundos-v2.1";

// ---------- byte limits of the script (§7) = the engine's (EXTENDER «Reglas para quien escribe») ----------
const bytes = (s) => Buffer.byteLength(s, "utf8");
const fallos = [];
const tope = (s, n, donde) => { if (bytes(s) > n) fallos.push(`${donde}: ${bytes(s)} bytes > ${n} «${s.slice(0, 50)}…»`); return s; };

// ---------- where everyone stands (§3.2, mirrored in z; recomputed for this room) ----------
const P = {
  senet: { x: 0, z: 4.3 },          // between the door and the table, ~3.3 m from the arrival, facing it (C-4)
  rima: { x: 4.55, z: 1.75 },       // beside the gear tower (east)
  lyra: { x: -6.25, z: 1.45 },      // by the archive (west)
  pictor: { x: 3.7, z: -3.9 },      // by the preparation table (north-east): maps and miniatures
  cepheus: { x: -4.2, z: -4.6 },    // by the glass case (north-west): the custody
};
const ARCH = { x: -6.7, y: 1.0 };   // the archive desk's top (the desk is at polar(180, 7.2), facing the table)
const enMesa = (x, z, dy = 0.04) => ({ x, z, y: MESA.colocar + dy });
// SAME WORLD (Pablo, 2026-11-04): the practice runs in the real Gabinete, so its model (map square, door, lamp, key, pedestal,
// Atlas) sits on the preparation table — the round table stays free for whoever is playing. k = metres along the table.
const RP = Math.hypot(PREPARACION.x, PREPARACION.z), TX = -PREPARACION.z / RP, TZ = PREPARACION.x / RP;
const enPrep = (k, dy = 0.04) => ({ x: Math.round((PREPARACION.x + TX * k) * 100) / 100, z: Math.round((PREPARACION.z + TZ * k) * 100) / 100, y: PREPARACION.alto + dy });
const prep = enPrep(-1.05);
const COSAS = {
  espejo: { nombre: "Espejo del avatar", pos: { x: -2.7, z: 7.1, y: 1.2 }, radio: 1.8, aviso: "examinar el espejo", color: "#A17C44", escala: 0.5,
    descripcion: "El espejo refleja a su avatar. Puede cambiar el encuadre de cámara (V), pero su posición y su mirada siguen siendo suyas." },
  "ficha-iria": { nombre: "Ficha de Iria", pos: { x: ARCH.x, z: -0.45, y: ARCH.y }, radio: 1.7, aviso: "coger la ficha de Iria", color: "#E8DCC2", escala: 0.16,
    descripcion: "Iria es un personaje ficticio de práctica. Sus campos (nombre, concepto, notas, recursos) no obedecen a ningún reglamento." },
  "ficha-eco": { nombre: "Ficha del Eco sin Contorno", pos: { x: ARCH.x, z: 0.45, y: ARCH.y }, radio: 1.7, aviso: "consultar la criatura", color: "#274A47", escala: 0.16,
    descripcion: "El Eco sin Contorno es una criatura de ensayo. En esta vista solo se ven su nombre y su silueta; lo demás es de quien dirige." },
  "mini-iria": { nombre: "Miniatura de Iria", pos: prep, radio: 1.7, aviso: "coger la miniatura de Iria", color: "#3FA7A0", escala: 0.12,
    descripcion: "La miniatura de Iria. Una miniatura no es una ficha: son piezas distintas que pueden vincularse." },
  casilla: { nombre: "Casilla marcada del mapa", pos: enPrep(-0.7, 0.01), radio: 1.6, aviso: "dejar a Iria en la casilla", color: "#E7B66D", escala: 0.1,
    descripcion: "Una casilla permitida del mapa de práctica, junto a la puerta de la maqueta." },
  "puerta-maqueta": { nombre: "Puerta de la maqueta", pos: enPrep(-0.35, 0.06), radio: 1.6, aviso: "abrir la puerta de la maqueta", color: "#745838", escala: 0.14,
    descripcion: "La puerta separa dos zonas de la maqueta. Abrirla cambia lo que un personaje puede descubrir." },
  luminaria: { nombre: "Lámpara de escena", pos: enPrep(0, 0.08), radio: 1.6, aviso: "encender la lámpara", color: "#E7B66D", escala: 0.12,
    descripcion: "Luminaria de la escena. La luz cambia la escena, nunca la cámara de quien juega." },
  "reloj-turnos": { nombre: "Reloj de turnos", pos: enMesa(-1.45, 0.4, 0.04), radio: 2.2, aviso: "girar el reloj de turnos", color: "#A17C44", escala: 0.14,
    descripcion: "Reloj de latón en el borde de la mesa. Marca quién interviene ahora y quién después, sin imponer reglas de combate." },
  "pagina-atlas": { nombre: "Página del Atlas", pos: { x: PLACAS[0].x + 0.35, z: PLACAS[0].z - 0.2, y: 1.15 }, radio: 1.8, aviso: "leer la página del Atlas", color: "#E8DCC2", escala: 0.18,
    descripcion: "El Atlas anota una frase: «Lo que la niebla oculta, la luz puede devolverlo a los ojos»." },
  baraja: { nombre: "Baraja de ensayo", pos: { x: ARCH.x, z: -1.1, y: ARCH.y }, radio: 1.7, aviso: "robar una carta", color: "#1B2934", escala: 0.12,
    descripcion: "Barajar, robar y revelar son operaciones distintas; una carta boca abajo conserva su contenido privado." },
  "tabla-azar": { nombre: "Tabla «Recuerdos de Cámara»", pos: { x: ARCH.x, z: 1.1, y: ARCH.y }, radio: 1.7, aviso: "consultar la tabla", color: "#E8DCC2", escala: 0.14,
    descripcion: "Tabla de recuerdos: una selección al azar devuelve una frase breve de ambientación." },
  gramofono: { nombre: "Gramófono", pos: { x: 2.9, z: 3.7, y: 0.9 }, radio: 1.8, aviso: "poner música", color: "#A17C44", escala: 0.3,
    descripcion: "Gramófono y mezclador de la mesa. La música de la partida va aparte del sonido de la sala." },
  cristal: { nombre: "Cristal Narrativo", pos: { x: 0, z: 0, y: 2.45 }, radio: 2.8, aviso: "proyectar en el cristal", color: "#E7B66D", escala: 0.05,
    descripcion: "El Cristal Narrativo admite una propuesta de entidad con nombre, descripción, autor, posición y visibilidad." },
  llave: { nombre: "Llave de Mercurio", pos: enPrep(0.35, 0.03), radio: 1.6, aviso: "coger la Llave de Mercurio", color: "#C8CCD2", escala: 0.08,
    descripcion: "Llave de Mercurio: objeto de práctica, sacado de la biblioteca. Puede colocarse, recogerse y retirarse." },
  pedestal: { nombre: "Pedestal de la maqueta", pos: enPrep(0.7, 0.05), radio: 1.6, aviso: "colocar la llave en el pedestal", color: "#745838", escala: 0.12,
    descripcion: "Un pedestal en la cámara cerrada de la maqueta, con una ranura del tamaño de una llave." },
  atlas: { nombre: "Atlas de los Mil Mundos", pos: enPrep(1.05, 0.1), radio: 1.6, aviso: "activar el Atlas", color: "#A17C44", escala: 0.22,
    descripcion: "Cuatro lentes —Perspectiva, Azar, Forma y Relato— han de devolver al Atlas su capacidad para abrir mundos." },
};

// ---------- what the world is ----------
escribir("aventura/ficha.json", {
  formato: "ficha@1",
  nombre: "El Gabinete de los Mil Mundos",
  descripcion: "Sala universal de rol: un gabinete de maravillas steampunk para jugar a cualquier juego, con su sistema, uno inventado o ninguno. Mesa para ocho, el báculo de quien dirige, dados que tira el servidor y el mapa sobre la mesa. Senet le enseña la sala en «El Atlas Despierto», una práctica de unos 40 minutos.",
  notas: `${VERSION} (work/plantillas/gabinete-mil-mundos), según el guion v2.1 del 2026-11-04. Sucede a El Gabinete v2 (plantillas/gabinete-rol), que se conserva.`,
});

// ---------- people ----------
/** One dialogue node: text (≤400 B) and up to 4 answers (≤100 B each). */
const nodo = (id, texto, opciones) => ({ [id]: { texto: tope(texto, 400, `nodo ${id}`), opciones: opciones.map((o) => ({ ...o, texto: tope(o.texto, 100, `respuesta de ${id}`) })) } });
const adios = (texto = "Gracias.") => ({ texto, accion: { tipo: "despedir" } });
const sigue = (ir, texto = "Continuar.") => ({ texto, ir });
const da = (mision, ir, texto) => ({ texto, accion: { tipo: "dar_mision", mision }, ...(ir ? { ir } : {}) });
const avanza = (mision, ir, texto, paso) => ({ texto, si: { mision, estado: "activa", paso }, accion: { tipo: "avanzar_paso", mision }, ...(ir ? { ir } : {}) });
const pnj = (id, nombre, oficio, voz, escala, descripcion, entradas, nodos, notas) => {
  const p = P[id];
  escribir(`aventura/pnj/${id}.json`, { formato: "pnj@1", id, nombre, oficio: tope(oficio, 40, `oficio ${id}`), voz, descripcion,
    apariencia: { modelo: ROBOT, escala }, posicion: p, giro: mirar(p.x, p.z, id === "senet" ? APARICION.x : MESA.x, id === "senet" ? APARICION.z : MESA.z), radio: 3,
    dialogo: { entradas, nodos: Object.assign({}, ...nodos) }, notas });
};
const si = (mision, estado, paso) => ({ si: { mision, estado, ...(paso ? { paso } : {}) } });

// Senet: E1 welcome and M1; E9 a word on talking; M9 crystal, M10 staff, M11 Atlas, M12 closing (SEN_010…SEN_260, §7.1)
pnj("senet", "Senet", "Game Master del Gabinete", "hombre-joven", 0.54,
  "Game Master de Numinia y maestro de ceremonias del Gabinete. Enseña la sala a quien llega; no impone ningún reglamento.",
  [
    { ...si("m1-perspectiva", "disponible"), nodo: "sen010" },
    { ...si("m1-perspectiva", "activa", "senet"), nodo: "sen070" },
    { ...si("m1-perspectiva", "activa"), nodo: "sen060" },
    { ...si("m8-mesa-habitada", "activa"), nodo: "sen080" },
    { ...si("m9-cristal", "disponible"), nodo: "sen090" },
    { ...si("m9-cristal", "activa", "senet"), nodo: "sen110" },
    { ...si("m9-cristal", "activa"), nodo: "sen100" },
    { ...si("m10-baculo", "disponible"), nodo: "sen120" },
    { ...si("m10-baculo", "activa", "senet"), nodo: "sen140" },
    { ...si("m10-baculo", "activa"), nodo: "sen130" },
    { ...si("m11-atlas", "disponible"), nodo: "sen150" },
    { ...si("m11-atlas", "activa"), nodo: "sen170" },
    { ...si("m12-memoria", "disponible"), nodo: "sen190" },
    { ...si("m12-memoria", "activa"), nodo: "sen210" },
    { ...si("m12-memoria", "hecha"), nodo: "libre" },
    { nodo: "sen900" },
  ],
  [
    nodo("sen010", "Bienvenido al Gabinete de los Mil Mundos. Aquí no viene a jugar una sola historia: viene a aprender a dar forma a todas.", [sigue("sen020")]),
    nodo("sen020", "Yo soy Senet. Este báculo significa la voz de quien dirige la partida. Unas veces lo llevaré yo; otras podrá sostenerlo usted.", [sigue("sen030")]),
    nodo("sen030", "El Atlas del gabinete ha perdido cuatro lentes. No necesita vencer a nadie para recuperarlas: basta con aprender a usar lo que tiene ante usted.", [sigue("sen040")]),
    nodo("sen040", "¿Prefiere descubrir la sala conmigo o entrar ya a la mesa libre?", [
      da("m1-perspectiva", "sen050", "Aprender a usar la sala."),
      { texto: "Entrar directamente a la mesa libre.", ir: "libre" },
    ]),
    nodo("sen050", "Comencemos por lo esencial. Camine hasta el espejo, junto a la puerta. Su punto de vista le pertenece y nadie debe arrebatárselo.", [adios("Voy al espejo.")]),
    nodo("sen060", "Puede pasar de primera a tercera persona con la tecla V o la rueda. Su cuaderno se abre con la J: ahí están sus pasos. Examine el espejo y vuelva conmigo.", [adios("De acuerdo.")]),
    nodo("sen070", "Ya sabe dónde está, cómo mira y dónde consultar su camino. La primera lente despierta. Rima le espera junto a la torre de engranajes, al este.", [
      avanza("m1-perspectiva", null, "Voy con Rima.", "senet"),
    ]),
    nodo("sen080", "En una mesa de rol la conversación es el centro de todo. Por eso, aquí hablar no activa dados ni objetos: las acciones necesitan un clic consciente.", [adios("Entendido.")]),
    nodo("sen090", "La ficción puede adoptar forma. Este cristal recibe propuestas y las manifiesta en la mesa, pero ninguna palabra pronunciada lo activa por sí sola.", [sigue("sen100a")]),
    nodo("sen100a", "Acérquese a la mesa y proyecte en el cristal con un clic. Después coja la Llave de Mercurio en la mesa de práctica, al noreste, y vuelva conmigo.", [
      da("m9-cristal", null, "Probaré el cristal."),
    ]),
    nodo("sen100", "Proyecte en el cristal con un clic y coja la Llave de Mercurio en la mesa de práctica, al noreste. Luego vuelva conmigo.", [adios("Voy.")]),
    nodo("sen110", "Una imagen no basta. Esa llave es una entidad de práctica: tiene nombre, estado y un lugar en el registro. Cuando la suelte, volverá a su sitio.", [
      avanza("m9-cristal", null, "Entendido.", "senet"),
    ]),
    nodo("sen120", "Ahora conocerá el signo del Game Master. El báculo no es una regla de ningún juego; es la herramienta de dirección de esta sala.", [sigue("sen130a")]),
    nodo("sen130a", "Está en su soporte, junto a la silla alta. Si está libre, tómelo con E y devuélvalo enseguida. Si alguien lo lleva, hay una partida en curso: no la interrumpa; acérquese al soporte y vuelva.", [
      da("m10-baculo", null, "Tomaré el báculo."),
    ]),
    nodo("sen130", "Vaya al soporte del báculo, junto a la silla alta. Si está libre, tómelo con E y déjelo de nuevo en su soporte; si alguien lo lleva, no interrumpa la partida.", [adios("Voy.")]),
    nodo("sen140", "Un solo báculo, una sola dirección activa: aquí nadie se lo quita a quien dirige una partida. La narración no es la administración.", [
      avanza("m10-baculo", null, "Está en su soporte, o lo lleva quien dirige.", "senet"),
      adios("Aún lo llevo."),
    ]),
    nodo("sen150", "Queda una última lente. El Eco sin Contorno ha desplazado la inscripción del Atlas. Las herramientas ya son suyas: obsérvelas y úselas.", [sigue("sen160")]),
    nodo("sen160", "Lea la pista, utilice la luz, tire dos dados en la torre si lo desea y coloque la Llave de Mercurio en el pedestal. Después, active el Atlas.", [
      da("m11-atlas", null, "Despertaré el Atlas."),
    ]),
    nodo("sen170", "El azar no cierra la historia. Si los dados dudan, vuelva a mirar la luz y complete el gesto: la llave, el pedestal y el Atlas.", [adios("Sigo.")]),
    nodo("sen190", "El Atlas ha despertado. Ha conocido la mesa, el azar, las fichas, los escenarios y el poder del relato compartido.", [sigue("sen200")]),
    nodo("sen200", "Su práctica queda guardada en el cuaderno: puede volver, repetir una estación o abrir la sala para una partida cualquiera.", [
      da("m12-memoria", "sen210", "Cerrar la práctica."),
    ]),
    nodo("sen210", "Si quiere aprender a dirigir una sesión, Cepheus le espera. Si prefiere jugar, el gabinete ya está a su disposición.", [
      avanza("m12-memoria", "libre", "Ir a la mesa libre.", "senet"),
      avanza("m12-memoria", "dirigir", "Aprender a dirigir.", "senet"),
      adios("Más tarde."),
    ]),
    nodo("dirigir", "Hable con Cepheus, junto a la vitrina del noroeste. El ensayo de dirección aún se está preparando; él le dirá lo que ya puede ver.", [adios("Gracias, Senet.")]),
    nodo("libre", "La mesa está abierta. Siéntese con E junto a una silla; quien lleve el báculo pone las escenas y los dados los tira la torre. Si quiere aprender, aquí estaré.", [adios("Gracias.")]),
    nodo("sen900", "Consulte el cuaderno (J) si necesita recordar el siguiente paso. No hay prisa.", [adios("Gracias.")]),
  ],
  "Agente DJ de la casa (agents/senet), aquí con guion y sin modelo de lenguaje (coste cero). [MOTOR] Senet aún no puede llevar el báculo ni aceptar una solicitud: el báculo está en su soporte (ENTREGA.md).");

// Rima: E3 / M2, dice (RIM_010…RIM_040, §7.2)
pnj("rima", "Rima", "Maestra del azar", "mujer-mayor", 0.52,
  "Artificiera del gabinete. Cuida la torre de engranajes y sus dados.",
  [
    { ...si("m2-azar", "disponible"), nodo: "rim010" },
    { ...si("m2-azar", "activa", "rima"), nodo: "rim040" },
    { ...si("m1-perspectiva", "hecha"), nodo: "rim900" },
    { nodo: "hola" },
  ],
  [
    nodo("rim010", "Soy Rima. En esta torre caben dados pequeños, grandes y combinados. Ninguna regla se impone si usted no la elige.", [sigue("rim020")]),
    nodo("rim020", "Pulse la torre para tirar un dado de veinte caras. El resultado lo calcula el servidor antes de que el dado termine de rodar, y toda la mesa ve el mismo.", [sigue("rim030")]),
    nodo("rim030", "Después pruebe tres dados de seis y la fórmula de dos dados de diez más cinco: escriba /tirar 3d6 y /tirar 2d10+5 en el chat, junto a la torre. Mire cada valor y el total.", [
      da("m2-azar", null, "Voy a tirar."),
    ]),
    nodo("rim040", "Una tirada puede ser pública o privada: marque «en secreto» y la cifra solo la verá usted. ¿Ha tirado ya los tres?", [
      avanza("m2-azar", "rim050", "Sí: 1d20, 3d6 y 2d10+5.", "rima"),
      adios("Todavía no."),
    ]),
    nodo("rim050", "Bien. El azar ya responde. Lyra le espera en el archivo, al oeste.", [adios("Voy con Lyra.")]),
    nodo("rim900", "Elija dados o escriba una fórmula y confirme. La torre tira para todos.", [adios("Gracias.")]),
    nodo("hola", "Soy Rima. Senet le recibirá primero, junto a la entrada; luego venga a verme.", [adios("De acuerdo.")]),
  ],
  "Plantilla del Gabinete de los Mil Mundos. [MOTOR] La misión aún no comprueba la tirada (no hay paso «tira»): la ve toda la mesa y Rima pregunta.");

// Lyra: E4 / M3 sheets; E8 / M7 documents, cards, tables; E9 / M8 chat and music (LYR_010…LYR_090, §7.3)
pnj("lyra", "Lyra", "Archivera", "mujer-mayor", 0.52,
  "Exégeta y bibliotecaria del gabinete. Guarda fichas, documentos, cartas y tablas.",
  [
    { ...si("m3-ficha", "disponible"), nodo: "lyr010" },
    { ...si("m3-ficha", "activa", "lyra"), nodo: "lyr040" },
    { ...si("m3-ficha", "activa"), nodo: "lyr030" },
    { ...si("m7-secretos", "disponible"), nodo: "lyr050" },
    { ...si("m7-secretos", "activa", "lyra"), nodo: "lyr070" },
    { ...si("m7-secretos", "activa"), nodo: "lyr060" },
    { ...si("m8-mesa-habitada", "disponible"), nodo: "lyr080" },
    { ...si("m8-mesa-habitada", "activa", "lyra"), nodo: "lyr090" },
    { ...si("m2-azar", "hecha"), nodo: "lyr900" },
    { nodo: "hola" },
  ],
  [
    nodo("lyr010", "Soy Lyra. Una miniatura no es una ficha, y una ficha no es un jugador: son piezas distintas que pueden vincularse.", [sigue("lyr020")]),
    nodo("lyr020", "Sobre el escritorio están la ficha de Iria y la del Eco sin Contorno. Coja la de Iria y consulte la del Eco: su aspecto puede conocerse sin revelar sus secretos.", [
      da("m3-ficha", null, "Voy al escritorio."),
    ]),
    nodo("lyr030", "La ficha de Iria y la del Eco están sobre el escritorio del archivo. Cuando las tenga vistas, vuelva conmigo.", [adios("Voy.")]),
    nodo("lyr040", "Pronto podrá editar sus notas y vincularla a su miniatura; de momento, llévela consigo. Pictor le espera junto a la mesa de preparación, al noreste.", [
      avanza("m3-ficha", null, "Voy con Pictor.", "lyra"),
    ]),
    nodo("lyr050", "Los relatos también se guardan. Lea la página del Atlas, en la placa de consulta; después pruebe la baraja y la tabla del archivo.", [sigue("lyr060a")]),
    nodo("lyr060a", "Puede robar una carta y consultar un resultado al azar. Cada operación queda registrada según su visibilidad.", [da("m7-secretos", null, "Voy a ello.")]),
    nodo("lyr060", "Le faltan la página del Atlas, la baraja o la tabla. Mire su cuaderno (J): ahí está lo que queda.", [adios("Voy.")]),
    nodo("lyr070", "La biblioteca conserva personajes, monstruos, mapas, documentos y reglas que los jugadores tienen derecho a utilizar. Guarde bien esa pista del Atlas.", [
      avanza("m7-secretos", null, "La guardo.", "lyra"),
    ]),
    nodo("lyr080", "Pruebe el chat (Intro) y escriba a la mesa. Nada de lo que escriba se interpreta como una orden. Luego ponga música en el gramófono, junto a la mesa.", [
      da("m8-mesa-habitada", null, "Lo probaré."),
    ]),
    nodo("lyr090", "El gramófono le ayuda a crear atmósfera. Ajuste el volumen en los ajustes de sonido para que las voces nunca queden ocultas. Senet le espera para el cristal.", [
      avanza("m8-mesa-habitada", null, "Voy con Senet.", "lyra"),
      adios("Aún no he puesto música."),
    ]),
    nodo("lyr900", "El archivo está abierto. Fichas, documentos, cartas y tablas esperan a quien dirija la próxima partida.", [adios("Gracias.")]),
    nodo("hola", "Soy Lyra, del archivo. Cuando haya pasado por Senet y por Rima, aquí le espero.", [adios("De acuerdo.")]),
  ],
  "Plantilla del Gabinete de los Mil Mundos. [MOTOR] Ficha editable, vínculo ficha–miniatura, inventario de práctica, mazo y tabla con resultado, envío de chat como paso.");

// Pictor: E5 / M4 maps and miniatures; E6 / M5 door and light; E7 / M6 turns (PIC_010…PIC_090, §7.4)
pnj("pictor", "Pictor", "Cartógrafo", "hombre-joven", 0.53,
  "Guardián del tablero y cartógrafo del gabinete. Prepara mapas, maquetas y miniaturas.",
  [
    { ...si("m4-cartografia", "disponible"), nodo: "pic010" },
    { ...si("m4-cartografia", "activa", "pictor"), nodo: "pic030" },
    { ...si("m4-cartografia", "activa"), nodo: "pic020b" },
    { ...si("m5-puerta", "disponible"), nodo: "pic040" },
    { ...si("m5-puerta", "activa", "pictor"), nodo: "pic060" },
    { ...si("m5-puerta", "activa"), nodo: "pic050b" },
    { ...si("m6-turno", "disponible"), nodo: "pic070" },
    { ...si("m6-turno", "activa", "pictor"), nodo: "pic090" },
    { ...si("m6-turno", "activa"), nodo: "pic080b" },
    { ...si("m3-ficha", "hecha"), nodo: "pic900" },
    { nodo: "hola" },
  ],
  [
    nodo("pic010", "Soy Pictor. Una mesa puede mostrar un plano, una maqueta o un mundo que se recorre.", [sigue("pic020")]),
    nodo("pic020", "Coja la miniatura de Iria de esta mesa de preparación y déjela en la casilla marcada de la maqueta, en esta misma mesa. Luego vuelva conmigo.", [
      da("m4-cartografia", null, "Voy con Iria."),
    ]),
    nodo("pic020b", "La miniatura de Iria y la casilla están las dos en esta mesa de práctica. La mesa redonda queda para quien juega.", [adios("Voy.")]),
    nodo("pic030", "Medir distancias y alturas sobre el mapa llegará pronto. La medida pertenece al escenario, no al tamaño decorativo de la miniatura.", [
      avanza("m4-cartografia", null, "Entendido.", "pictor"),
    ]),
    nodo("pic040", "No todos ven todo. Una puerta, una pared o una luz pueden cambiar lo que un personaje descubre.", [sigue("pic050")]),
    nodo("pic050", "En la maqueta de esta mesa hay una puerta y una lámpara. Abra primero la puerta y después encienda la lámpara, cada una con su clic.", [
      da("m5-puerta", null, "Voy a la maqueta."),
    ]),
    nodo("pic050b", "Primero la puerta de la maqueta, después la lámpara. Las dos están en esta mesa de práctica.", [adios("Voy.")]),
    nodo("pic060", "La información secreta no debe ocultarse solo con una cortina visual. También debe quedar fuera de quien no puede recibirla.", [
      avanza("m5-puerta", null, "Lo recordaré.", "pictor"),
    ]),
    nodo("pic070", "El Eco será nuestro compañero de prueba. Vamos a organizar una ronda sin imponer reglas de combate.", [sigue("pic080")]),
    nodo("pic080", "Gire el reloj de turnos, en el borde oeste de la mesa: Iria interviene primero y el Eco después. Luego vuelva conmigo.", [
      da("m6-turno", null, "Giraré el reloj."),
    ]),
    nodo("pic080b", "El reloj de turnos está en el borde oeste de la mesa redonda.", [adios("Voy.")]),
    nodo("pic090", "Una acción rápida bien hecha ahorra trabajo, pero nunca debe decidir en secreto qué reglas usa la mesa. Lyra le espera de nuevo en el archivo.", [
      avanza("m6-turno", null, "Voy con Lyra.", "pictor"),
    ]),
    nodo("pic900", "Todo desplazamiento y toda medida deben confirmarse con un gesto o un clic deliberado.", [adios("Gracias.")]),
    nodo("hola", "Soy Pictor, del tablero. Pase antes por Senet, Rima y Lyra.", [adios("De acuerdo.")]),
  ],
  "Plantilla del Gabinete de los Mil Mundos. [MOTOR] Mapa 2D/3D, mover y medir miniaturas en la mesa, visión y niebla, orden de turnos y acción rápida.");

// Cepheus: the custody; the director's route (E14–E18) is not built yet (§16.8 Entrega C)
pnj("cepheus", "Cepheus", "Custodio de reglas", "hombre-joven", 0.53,
  "Custodio de reglas y permisos del gabinete. Instruye a quien quiere aprender a dirigir.",
  [
    { ...si("m12-memoria", "hecha"), nodo: "cep010" },
    { nodo: "hola" },
  ],
  [
    nodo("cep010", "Soy Cepheus. Una partida necesita autoridad, pero también límites. No es lo mismo poseer una campaña, dirigirla o sostener el báculo.", [sigue("cep020")]),
    nodo("cep020", "El ensayo de dirección aún se está preparando. Mientras tanto, observe una partida: quien lleva el báculo pone escenas; el dueño del mundo da los permisos.", [adios("Volveré.")]),
    nodo("hola", "Custodio la sala de quien dirige. Termine antes la práctica con Senet.", [adios("De acuerdo.")]),
  ],
  "Plantilla del Gabinete de los Mil Mundos. [MOTOR] La ruta de dirección (D1–D5) va en el mismo mundo (Pablo, 2026-11-04): necesita un báculo de ensayo que no quite el de verdad.");

// ---------- things ----------
for (const [id, c] of Object.entries(COSAS)) {
  escribir(`aventura/cosas/${id}.json`, { formato: "cosa@1", id, nombre: c.nombre, descripcion: c.descripcion, posicion: c.pos, radio: c.radio,
    aviso: tope(c.aviso, 40, `aviso ${id}`), apariencia: { escala: c.escala, color: c.color }, notas: "Plantilla del Gabinete de los Mil Mundos." });
}

// ---------- the quests: M1–M12 of §8, in a chain ----------
let previa = null;
const mision = (id, titulo, descripcion, pasos, textos, extra = {}) => {
  escribir(`aventura/misiones/${id}.json`, {
    formato: "mision@1", id, titulo: tope(titulo, 80, `título ${id}`), descripcion: tope(descripcion, 300, `descripción ${id}`),
    requisitos: { misiones: previa ? [previa] : [] },
    pasos: pasos.map((p) => ({ ...p, texto: tope(p.texto, 160, `paso ${id}.${p.id}`) })),
    textos: { aceptada: tope(textos[0], 160, `aceptada ${id}`), completada: tope(textos[1], 160, `completada ${id}`) },
    ...extra, // not repetible: a repeatable quest stays «disponible» and its person would greet forever (maquina.ts disponible)
  });
  previa = id;
};
const usa = (id, cosa, texto, grupo) => ({ id, tipo: "usa", cosa, texto, ...(grupo ? { grupo } : {}) });
const coge = (id, cosa, texto, grupo) => ({ id, tipo: "coge", cosa, texto, ...(grupo ? { grupo } : {}) });
const habla = (pnjId, texto) => ({ id: pnjId, tipo: "habla_con", pnj: pnjId, texto });

mision("m1-perspectiva", "La lente de Perspectiva", "Moverse, mirar y consultar: camine hasta el espejo, pruebe la tercera persona (V) y abra su cuaderno (J).", [
  usa("espejo", "espejo", "Camine hasta el espejo, junto a la puerta, y examínelo (E). Pruebe la tecla V"),
  habla("senet", "Vuelva con Senet"),
], ["Su cuaderno (J) le marca cada paso.", "Lente de Perspectiva encendida."]);
mision("m2-azar", "La lente del Azar", "Tire en la torre de engranajes un dado de veinte, tres de seis y la fórmula 2d10+5. Distinga una tirada pública de una privada.", [
  habla("rima", "Tire 1d20, 3d6 y 2d10+5 en la torre (pulse la torre o /tirar) y cuénteselo a Rima"),
], ["Rima le espera junto a la torre.", "Lente del Azar encendida."]);
mision("m3-ficha", "La identidad tiene ficha", "Lyra le enseña que una ficha, una miniatura y un jugador son piezas distintas.", [
  coge("iria", "ficha-iria", "Coja la ficha de Iria del escritorio del archivo", "Fichas del archivo"),
  usa("eco", "ficha-eco", "Consulte la ficha del Eco sin Contorno", "Fichas del archivo"),
  habla("lyra", "Vuelva con Lyra"),
], ["Las fichas están en el escritorio del archivo.", "Ya distingue ficha, miniatura y jugador."]);
mision("m4-cartografia", "Cartografía de lo posible", "Pictor le enseña la mesa como tablero: lleve la miniatura de Iria a una casilla permitida.", [
  coge("mini", "mini-iria", "Coja la miniatura de Iria de la mesa de preparación, al noreste"),
  usa("casilla", "casilla", "Déjela en la casilla marcada de la maqueta, en la misma mesa"),
  habla("pictor", "Vuelva con Pictor"),
], ["Pictor le espera junto a la mesa de preparación.", "Iria ya está en el mapa."]);
mision("m5-puerta", "La puerta y la bruma", "Una puerta y una luz cambian lo que un personaje descubre. Ábrala y encienda la lámpara de la maqueta.", [
  usa("puerta", "puerta-maqueta", "Abra la puerta de la maqueta, en la mesa de práctica"),
  usa("luz", "luminaria", "Encienda la lámpara de la maqueta"),
  habla("pictor", "Vuelva con Pictor"),
], ["Primero la puerta, después la luz.", "Lente de Forma encendida."]);
mision("m6-turno", "El Eco toma su turno", "Organice una ronda de práctica con Iria y el Eco, sin reglas de combate impuestas.", [
  usa("reloj", "reloj-turnos", "Gire el reloj de turnos, en el borde oeste de la mesa"),
  habla("pictor", "Vuelva con Pictor"),
], ["El reloj está en el borde de la mesa.", "La ronda de práctica ha terminado."]);
mision("m7-secretos", "Los secretos del Atlas", "Lea la página del Atlas, robe una carta de la baraja y consulte la tabla del archivo.", [
  usa("pagina", "pagina-atlas", "Lea la página del Atlas, en la placa de consulta del oeste", "Archivo"),
  usa("carta", "baraja", "Robe una carta de la baraja de ensayo", "Archivo"),
  usa("tabla", "tabla-azar", "Consulte la tabla «Recuerdos de Cámara»", "Archivo"),
  habla("lyra", "Vuelva con Lyra"),
], ["La pista, la baraja y la tabla, en cualquier orden.", "Ha guardado la pista del Atlas."]);
mision("m8-mesa-habitada", "La mesa habitada", "Escriba en el chat a la mesa y ponga música en el gramófono. Hablar no activa nada: cada acción es un clic.", [
  usa("musica", "gramofono", "Ponga música en el gramófono, junto a la mesa (y escriba algo en el chat: Intro)"),
  habla("lyra", "Vuelva con Lyra"),
], ["El gramófono está al sureste de la mesa.", "La mesa está habitada."]);
mision("m9-cristal", "Una imagen adquiere forma", "Proyecte en el Cristal Narrativo y recoja la Llave de Mercurio en la mesa de práctica.", [
  usa("cristal", "cristal", "Acérquese a la mesa y proyecte en el cristal (E)"),
  coge("llave", "llave", "Coja la Llave de Mercurio en la mesa de práctica, al noreste"),
  habla("senet", "Vuelva con Senet"),
], ["El cristal cuelga sobre la mesa.", "La Llave de Mercurio queda en el registro."]);
mision("m10-baculo", "El Báculo de Senet", "Vaya al soporte del báculo; si está libre, tómelo y devuélvalo. Un solo báculo, una sola dirección: si alguien dirige, no se le quita.", [
  { id: "soporte", tipo: "llega_a", texto: "Vaya al soporte del báculo, junto a la silla alta (tómelo con E solo si está libre)", x: SOPORTE_BACULO.x, z: SOPORTE_BACULO.z, radio: 1.8, lugar: "el soporte del báculo" },
  habla("senet", "Si lo tomó, devuélvalo a su soporte; luego vuelva con Senet"),
], ["El soporte está junto a la silla alta.", "Ha probado el báculo y lo ha devuelto."]);
mision("m11-atlas", "El Atlas Despierto", "La última lente: lea la pista, use la luz, coloque la Llave de Mercurio en el pedestal y despierte el Atlas.", [
  usa("pagina", "pagina-atlas", "Vuelva a leer la pista del Atlas", "Pistas"),
  usa("luz", "luminaria", "Encienda de nuevo la lámpara de la maqueta", "Pistas"),
  coge("llave", "llave", "Coja la Llave de Mercurio (si quiere, tire 2d6 en la torre)"),
  usa("pedestal", "pedestal", "Coloque la llave en el pedestal de la maqueta"),
  { ...usa("atlas", "atlas", "Active el Atlas, en el extremo de la mesa de práctica"), secuencia: "atlas-despierta" },
], ["Las herramientas ya son suyas.", "El Atlas ha despertado. Lente del Relato encendida."]);
mision("m12-memoria", "Memoria del gabinete", "Senet cierra la práctica y le abre la sala: mesa libre, repetir una estación o aprender a dirigir.", [
  habla("senet", "Hable con Senet"),
], ["Senet cierra la práctica.", "Práctica completada. La sala queda abierta."]);

// SEQ_040 (§11.3): the Atlas wakes, only for the player who wakes it (it never moves the camera)
escribir("aventura/secuencias/atlas-despierta.json", { formato: "secuencia@1", id: "atlas-despierta", duracion: 7, pistas: [
  { tipo: "sonido", t: 0.2, sonido: "magia" },
  { tipo: "texto", t: 0.4, texto: tope("Las cuatro lentes giran en sus anillos: Perspectiva, Azar, Forma…", 120, "SEQ_040 1") },
  { tipo: "texto", t: 3.2, texto: tope("…y Relato. La inscripción vuelve a leerse.", 120, "SEQ_040 2") },
  { tipo: "sonido", t: 5.2, sonido: "exito" },
  { tipo: "texto", t: 5.4, texto: tope("El Atlas ha despertado. Senet le espera.", 120, "SEQ_040 3") },
] });

// ---------- the director's scenes (unchanged from v2, in «usted») ----------
const mapa = (id, titulo) => ({ obra: `${ROTULOS[id].sha}.png`, titulo, hueco: HUECO_MAPA });
const ESC = [
  ["0-el-gabinete", { nombre: "El Gabinete", entorno: { tono: { exposicion: 1.0 } }, narracion: "Siéntense. La lámpara está encendida y el cristal respira. Empieza la sesión." }],
  ["1-luz-de-trabajo", { nombre: "Luz de trabajo (neutra)", preset: "día", entorno: { luz: { ambiente: 1.6 }, niebla: { cerca: 80, lejos: 300 }, tono: { exposicion: 1.15 } }, narracion: "Luz clara para leer fichas y mapas." }],
  ["2-gotica", { nombre: "Capa gótica", preset: "noche", entorno: { luz: { ambiente: 1.1, cielo: "#6b4a6b" }, niebla: { cerca: 12, lejos: 45, color: "#1a1418", delCielo: false }, tono: { exposicion: 0.9 } }, narracion: "Las sombras se alargan entre los libros." }],
  ["3-expedicion", { nombre: "Capa de expedición", preset: "amanecer", entorno: { tono: { exposicion: 1.05 } }, narracion: "Mapas abiertos, brújulas a punto. Se parte al alba." }],
  ["4-cosmologica", { nombre: "Capa cosmológica", preset: "noche", entorno: { cielo: { tipo: "degradado", arriba: "#03060f", horizonte: "#16325c" }, luz: { ambiente: 1.3, cielo: "#5a75b0" }, tono: { exposicion: 1.0 } }, narracion: "Sobre la linterna, las estrellas. El globo celeste parece girar." }],
  ["5-contemporanea", { nombre: "Capa sobria contemporánea", preset: "nublado", entorno: { luz: { ambiente: 1.3 }, tono: { exposicion: 1.0 } }, narracion: "Luz gris de oficina. Una partida de hoy." }],
  ["6-las-ruinas", { nombre: "Las ruinas", preset: "atardecer", entorno: { niebla: { cerca: 22, lejos: 90, color: "#6b5a48", delCielo: false }, tono: { exposicion: 0.95 } }, narracion: "Columnas rotas y un pozo en el patio. Algo se mueve en la capilla.", mapa: mapa("mapa-ruinas", "Las ruinas") }],
  ["7-la-cripta", { nombre: "La cripta", preset: "noche", entorno: { niebla: { cerca: 10, lejos: 32, color: "#0a0a0f", delCielo: false }, luz: { ambiente: 0.95, cielo: "#3a3a4a" }, tono: { exposicion: 0.8 } }, narracion: "El aire está frío y quieto. Algo se mueve más allá de la luz.", ambiente: "", mapa: mapa("mapa-cripta", "La cripta") }],
  ["8-tormenta", { nombre: "Tormenta", preset: "tormenta", entorno: { tono: { exposicion: 0.75 } }, narracion: "El cristal de la linterna tiembla con cada trueno." }],
];
for (const [id, e] of ESC) escribir(`aventura/escenas/${id}.json`, { schema: "numinia-engine/escena@1", ...e });

// ---------- environment: a closed cabinet, light falling through the lantern onto the table (unchanged from v2) ----------
escribir("entorno/entorno.json", {
  schema: "numinia-engine/entorno@1",
  preset: "propio",
  cielo: { tipo: "degradado", arriba: "#16325c", horizonte: "#C4B5A6", nubes: 0.15 },
  sol: { altura: 74, rumbo: 160, intensidad: 2.4, color: "#F9EBDC" },
  luz: { hdr: "", ambiente: 1.55, cielo: "#F9EBDC", suelo: "#7A5100" },
  niebla: { cerca: 40, lejos: 200, color: "#3A332D", delCielo: false },
  suelo: { tipo: "liso", color: "#3A332D", escala: 1 },
  tono: { curva: "neutro", exposicion: 1.12 },
  hitos: false,
  rejilla: false,
  aparicion: { x: APARICION.x, z: APARICION.z, radio: APARICION.radio },
  notas: "Plantilla del Gabinete de los Mil Mundos. Sol alto y cálido: entra por la linterna de cristal y cae sobre la mesa (la mesa es el foco). Las capas del Director modulan esto, nunca la sala.",
});
escribir("entorno/dados.json", { schema: "numinia-engine/dados@1", activa: true, torres: [
  { id: "engranajes", nombre: "La torre de engranajes", x: TORRE.x, y: 0, z: TORRE.z, radio: 2.4, dados: "1d20",
    bandeja: { dx: TORRE.bandeja.dx, dz: TORRE.bandeja.dz, alto: TORRE.bandeja.y, ancho: TORRE.bandeja.ancho, fondo: TORRE.bandeja.fondo } },
  { id: "consola", nombre: "La bandeja de quien dirige", x: CONSOLA.x, y: 0, z: CONSOLA.z, radio: 1.6, dados: "1d20",
    bandeja: { dx: Math.round((CONSOLA.bandeja.x - CONSOLA.x) * 1000) / 1000, dz: Math.round((CONSOLA.bandeja.z - CONSOLA.z) * 1000) / 1000, alto: CONSOLA.bandeja.y + 0.04, ancho: CONSOLA.bandeja.ancho, fondo: CONSOLA.bandeja.fondo } },
] });
escribir("entorno/pesca.json", { sitios: [] });
escribir("entorno/dardos.json", { sitios: [] });
escribir("entorno/asientos.json", { schema: "numinia-engine/asientos@1", activa: true, sitios: ASIENTOS.map((s) => ({ id: s.id.replace("_", "-"), x: s.x, y: 0, z: s.z, mira: s.ry, etiqueta: s.etiqueta })) });
escribir("entorno/burbujas.json", { schema: "numinia-engine/burbujas@1", activa: true, burbujas: [{ id: "mesa", x: BURBUJA.x, y: 0, z: BURBUJA.z, radio: BURBUJA.radio, modo: "aislada", etiqueta: "La mesa" }] });
// ONE staff, on its stand beside the director's chair (the script's §3.2 stand at 1.49 m from the centre would sit inside
// the map's zone, r 1.55: it stays by the director's chair, as in v2)
escribir("entorno/baculos.json", { schema: "numinia-engine/baculos@1", activa: true, cantidad: 1, sitios: [{ x: SOPORTE_BACULO.x, y: 0, z: SOPORTE_BACULO.z }] });
const enConsola = polar(270, 5.2);
escribir("entorno/contadores.json", { schema: "numinia-engine/contadores@1", contadores: [
  { id: "ronda", nombre: "Ronda", tipo: "contador", inicio: 0, tope: 0, x: enConsola.x - 0.7, z: enConsola.z, y: 1.9, donde: "ambos", manda: "dirige" },
  { id: "sesion", nombre: "Reloj de la sesión", tipo: "reloj", x: enConsola.x, z: enConsola.z, y: 1.9, donde: "cartel", manda: "dirige" },
  { id: "cuenta", nombre: "Cuenta atrás", tipo: "cuenta_atras", segundos: 600, x: enConsola.x + 0.7, z: enConsola.z, y: 1.9, donde: "cartel", manda: "dirige", evento: "cuenta-a-cero" },
] });

if (fallos.length) { console.error("textos que no caben:\n" + fallos.join("\n")); process.exit(1); }
const n = (d) => readdirSync(M + d).length;
console.log(`${VERSION}: ficha · pnj ${n("aventura/pnj")} · misiones ${n("aventura/misiones")} · cosas ${n("aventura/cosas")} · secuencias ${n("aventura/secuencias")} · escenas ${n("aventura/escenas")}`);
