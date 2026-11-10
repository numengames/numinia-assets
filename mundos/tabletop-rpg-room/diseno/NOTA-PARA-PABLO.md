# Gabinete C1 · Nota para Pablo: interfaz y sonido

Qué hay (carpeta `mundos/tabletop-rpg-room/diseno/`, rama `mundos/tabletop-rpg-room-v2`):
- `UI.md`: dónde va cada cosa en escritorio, móvil y VR; ficha, cuaderno (J), regla, cristal; lista de accesibilidad con lo medido y lo no comprobado.
- `maqueta-hud.html`: dibujo estático (escritorio y móvil) que puede abrir en el navegador. No es el motor.
- `SONIDO.md` y `sonido.json`: la identidad sonora, 39 sonidos con su fuente y 55 señales repartidas de E0 a E13.

Lo que decidí
- No hay colores ni valores nuevos: todo sale de las fichas de color de la casa. Lo steampunk queda en los objetos 3D; la interfaz es sobria.
- En el móvil, una sola capa a la vez (hojas desde abajo), botones de 48 px con texto, sin desenfoque ni degradados animados, para que no caliente.
- En VR, los paneles quedan fijos en el mundo a 1,2–1,8 m, se elige con rayo y botón (nada de mirar para elegir) y la cámara es de quien juega.
- La música empieza en silencio: el gramófono no trae pista por defecto. Silenciar no oculta ninguna información: cada sonido con significado tiene un texto.

Lo que medí: el contraste de los colores de la casa (texto sobre fondo 16:1, ámbar 9:1, botón turquesa con texto blanco 4,9:1). El borde de los paneles solo da 1,5:1, así que nunca es lo único que marca un control.

Lo que NO pude comprobar
- Nada se ha oído ni probado en un móvil o unas gafas; los tamaños y volúmenes son objetivos de diseño.
- Los sonidos de Kenney (4 paquetes, todos CC0 según su página oficial) no los pude descargar desde aquí: los nombres exactos de archivo quedan por confirmar al construirlos. Los 14 sonidos «propios» (acordes del Atlas, cristal, báculo, ambientes) se generan por script desde las cifras de la tabla: obra de Numen Games, CC0.
- Los textos de los avisos sonoros son propuesta; las palabras de la casa son de Antunj.

Una cosa para decidir
- El guion (§12) pone los acordes del Atlas en E2/E3/E6/E12, pero el flujo enciende lentes en E2, E3, E4 y E12. Seguí el flujo (E4, no E6). ¿Lo confirma?
