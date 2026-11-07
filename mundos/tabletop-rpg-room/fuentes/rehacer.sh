#!/usr/bin/env bash
# Rehace la sala de rol v2 (el Gabinete) desde cero: regenera modelos, aventura y trazado, para el motor, vacía lo que guardó el mundo
# (objetos, elementos, entorno y los ficheros subidos), arranca y vuelve a colocar todo con construir.mjs.
# Uso: rehacer.sh
set -e
G=/var/home/uruk/arkhitektonian-lab/.hermes/profiles/ursa/work/plantillas/gabinete-mil-mundos
cd "$G"
node modelos.mjs >/dev/null && node mapa.mjs >/dev/null && node aventura.mjs >/dev/null && node trazado.mjs
bash servicios.sh parar >/dev/null
node -e '
const fs = require("fs"), d = process.argv[1] + "/mundo/datos";
for (const f of ["objetos", "elementos", "entorno"]) for (const x of ["json", "jsonl"]) fs.rmSync(`${d}/${f}.${x}`, { force: true });
fs.rmSync(`${d}/activos`, { recursive: true, force: true });
console.log("mundo vaciado: objetos, elementos, entorno y activos");' "$G"
bash servicios.sh arrancar >/dev/null
sleep 2
MUNDO_DIR="$G/mundo" timeout 600 node construir.mjs ws://127.0.0.1:8881
