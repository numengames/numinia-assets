#!/usr/bin/env bash
# El Gabinete (sala de rol v2) como zip mundo@2 (datos/, aventura/, entorno/) con las herramientas del propio motor: copia + comprobar.
# Empaqueta una copia LIMPIA de mundo/: los objetos, elementos y ficheros que dejó construir.mjs, la aventura y el
# entorno; NO lo que dejó jugar en local (quién reclamó el mundo: roles; la última escena puesta: director y entorno en
# directo; sesiones) ni las claves (claves.env nunca va en un zip).
# Uso: zip.sh  → work/plantillas/gabinete-mil-mundos/gabinete-mil-mundos.zip (se arrastra a la sala de Mundos de numinia.com)
set -e
P=/var/home/uruk/arkhitektonian-lab/.hermes/profiles/ursa
W=${GABINETE_MOTOR:-$P/work/ec-motor-main}
G=$P/work/plantillas/gabinete-mil-mundos
L=$P/cache/scratch/mil-mundos-zip/mundo
rm -rf "$L"; mkdir -p "$L/datos"
cp -r "$G/mundo/aventura" "$G/mundo/entorno" "$L/"
cp "$G/mundo/mundo.json" "$L/" 2>/dev/null || true
for f in objetos elementos; do for x in json jsonl; do [ -f "$G/mundo/datos/$f.$x" ] && cp "$G/mundo/datos/$f.$x" "$L/datos/"; done; done
cp -r "$G/mundo/datos/activos" "$L/datos/"
cd "$W"
MOTOR_MUNDO=$L node packages/server/src/entrada.ts copia > "$G/gabinete-mil-mundos.zip"
ls -la "$G/gabinete-mil-mundos.zip" | awk '{print $5 " bytes"}'
node packages/server/src/entrada.ts comprobar < "$G/gabinete-mil-mundos.zip" && echo "comprobar: ok (exit 0)"
node -e 'const z=require("fs").readFileSync(process.argv[1]); let n=0,p=0; const c={}; while((p=z.indexOf("PK\x01\x02",p))>=0){const l=z.readUInt16LE(p+28); const r=z.toString("utf8",p+46,p+46+l); const k=r.split("/").slice(0,2).join("/"); c[k]=(c[k]||0)+1; if(/claves\.env|roles\.jsonl/.test(r)) console.log("¡OJO! no debería ir en el zip:", r); p+=46+l; n++;} console.log("entradas:",n, JSON.stringify(c))' "$G/gabinete-mil-mundos.zip"
