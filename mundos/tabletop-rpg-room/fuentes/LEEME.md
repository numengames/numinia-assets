El Gabinete de los Mil Mundos · sala universal de rol (versión 2.1)
=====================================================================

Un gabinete de maravillas steampunk para jugar a cualquier juego de rol: con su sistema, uno inventado o ninguno.
Hecho según el guion «El Gabinete de los Mil Mundos: el Atlas Despierto», v2.1 (2026-11-04), entrega A.
Sucede a El Gabinete v2 (`plantillas/gabinete-rol`), que se queda como estaba.

Qué hay
- La sala de la v2: mesa redonda de nogal (Ø 3,60) para ocho, la silla alta de quien dirige, la torre de engranajes,
  la consola, el cristal narrativo, el archivo, la mesa de preparación, el globo y la vitrina. Nueve escenas.
- EL BÁCULO, uno solo, en su soporte junto a la silla alta. Quien lo lleva dirige.
- La práctica «El Atlas Despierto», en el mismo mundo: Senet (Game Master), Rima (dados), Lyra (archivo),
  Pictor (mapas) y Cepheus (custodia). Doce misiones seguidas, de M1 «La lente de Perspectiva» a M12 «Memoria del
  gabinete», unos 40 minutos.
- La maqueta de la práctica (casilla, puerta, lámpara, Llave de Mercurio, pedestal y Atlas) está en la mesa de
  preparación: la mesa redonda queda libre para quien juega de verdad.

Cómo se juega
- E junto a una silla para sentarse; E junto al báculo para cogerlo; «Pasar el báculo» para pasarlo.
- /tirar 2d10+3 junto a la torre, o pulse la torre. «En secreto»: la cifra solo la ve usted.
- J abre el cuaderno con los pasos; V cambia la cámara.
- Hablar no ejecuta nada: cada acción es un clic o una tecla.

Archivos
- aventura.mjs escribe todo lo de `mundo/aventura` y `mundo/entorno` (node aventura.mjs).
- recorrido.mjs recorre la práctica entera con la máquina de misiones del motor (node recorrido.mjs mundo).
- servicios.sh arrancar | pagina | parar | estado: en local, motor en 8881 y página en http://127.0.0.1:5321/.
- zip.sh hace `gabinete-mil-mundos.zip` (sin claves ni roles).
- ENTREGA.md: qué está hecho, qué es parcial y qué espera al motor.
