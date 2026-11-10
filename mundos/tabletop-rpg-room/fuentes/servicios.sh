#!/usr/bin/env bash
# El Gabinete de los Mil Mundos (v2.1) en local: el motor (puerto 8881) sobre plantillas/gabinete-mil-mundos/mundo y la página (5321),
# como servicios de usuario mil-mundos-motor y mil-mundos-pagina. El motor es work/ec-motor-main (main 0.104 con node_modules); no se toca, solo se arranca.
# Senet y los suyos son pnj con guion, no habitantes IA.
# Uso: servicios.sh arrancar|pagina|parar|estado|dueño
P=/var/home/uruk/arkhitektonian-lab/.hermes/profiles/ursa/work
W=${GABINETE_MOTOR:-$P/ec-motor-main}
M=$P/plantillas/gabinete-mil-mundos/mundo
NODE=$(command -v node)
quitar() { systemctl --user is-active -q "$1" && systemctl --user stop "$1"; systemctl --user reset-failed "$1" 2>/dev/null; true; }
case "${1:-arrancar}" in
  arrancar)
    mkdir -p "$M"
    quitar mil-mundos-motor
    systemd-run --user --unit=mil-mundos-motor --working-directory="$W" \
      -E PORT=8881 -E HOST=127.0.0.1 -E MOTOR_CONSTRUIR=todos -E MOTOR_MUNDO="$M" \
      nice -n 19 "$NODE" packages/server/src/main.ts
    sleep 4; bash "$0" estado ;;
  pagina)
    quitar mil-mundos-pagina
    systemd-run --user --unit=mil-mundos-pagina --working-directory="$W" -E PLAZA_PORT=5321 -E PLAZA_SERVER_PORT=8881 \
      nice -n 19 "$NODE" node_modules/vite/bin/vite.js --config apps/plaza/vite.config.mjs
    sleep 5; curl -s -o /dev/null -w "página http %{http_code}: http://127.0.0.1:5321/\n" http://127.0.0.1:5321/ ;;
  parar) quitar mil-mundos-motor; quitar mil-mundos-pagina; echo parado ;;
  dueño) # el código para «/dueño <código>» en el chat (quien dirige): lo hizo el motor, vive en mundo/claves.env (nunca en el zip)
    grep -o '^MOTOR_ADMIN=.*' "$M/claves.env" | cut -d= -f2- ;;
  estado)
    for u in mil-mundos-motor mil-mundos-pagina; do echo "$u: $(systemctl --user is-active $u 2>/dev/null)"; done
    curl -s -m 3 http://127.0.0.1:8881/health; echo
    journalctl --user -u mil-mundos-motor -n 10 --no-pager -o cat 2>/dev/null ;;
esac
