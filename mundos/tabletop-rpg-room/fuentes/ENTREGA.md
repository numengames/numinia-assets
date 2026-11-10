Proyecto: Numinia — El Gabinete de los Mil Mundos: el Atlas Despierto (`gabinete-mil-mundos-v2.1`)
Para: Pablo
De: Ursa
Fecha: 2026-11-04 (UTC)
Estado: entrega A del guion (§16.8) construida y probada en local. Sin subir a ningún servidor.

# Entrega A

Guion: `attachments/Numinia_Gabinete_Mil_Mundos_Guion_Completo_v2_1.md`. Carpeta: `work/plantillas/gabinete-mil-mundos`.
La v2 (`plantillas/gabinete-rol`) no se toca. Motor: `numinia-engine` main 0.104.0 (`ec-motor-main`, d2a27eb6), sin cambios.

## Decisiones aplicadas
- **Mismo mundo** (Pablo, 2026-11-04): la práctica va dentro del Gabinete real. Por eso la maqueta de la práctica está
  en la mesa de preparación (noreste) y la mesa redonda queda libre; y M10 no obliga a coger el báculo: si alguien lo
  lleva, hay partida en curso y no se le quita. Basta acercarse al soporte.
- Todo de usted (el guion tuteaba). Ningún sello ni recompensa (§0).
- Límites de texto del motor: diálogo ≤ 400 B, respuesta ≤ 100 B, paso ≤ 160 B, aviso ≤ 40 B; aventura.mjs los
  comprueba y se niega a escribir si uno no cabe.
- Las posiciones de §3.2 van giradas: en el motor se entra por la puerta sur mirando al norte.
- El soporte del báculo sigue junto a la silla alta (a 1,49 m del centro caería dentro del mapa).
- Las misiones no son repetibles: en el motor, una misión repetible queda «disponible» y su persona saludaría siempre
  igual. Repetir una estación queda pendiente.

## Recorrido (probado)
`node recorrido.mjs mundo` recorre las 12 misiones con la máquina de misiones del motor, como un jugador: habla con
quien da cada una, usa las cosas y vuelve. Resultado: 12/12, sin atascos. Al final Senet abre con «la mesa está abierta»
y Cepheus con su presentación. `tools/aventura.ts`: «Todo en orden». El motor la carga: 12 misiones, 5 personas,
16 cosas, 1 secuencia, 9 escenas, 1 báculo, 2 torres de dados.

| Misión | Quién la da | Lo que se hace |
|---|---|---|
| M1 La lente de Perspectiva | Senet | examinar el espejo, volver |
| M2 La lente del Azar | Rima | tirar 1d20, 3d6 y 2d10+5; decírselo a Rima |
| M3 La identidad tiene ficha | Lyra | coger la ficha de Iria, consultar la del Eco |
| M4 Cartografía de lo posible | Pictor | coger la miniatura de Iria, dejarla en la casilla |
| M5 La puerta y la bruma | Pictor | abrir la puerta de la maqueta, encender la lámpara |
| M6 El Eco toma su turno | Pictor | girar el reloj de turnos |
| M7 Los secretos del Atlas | Lyra | leer la página del Atlas, robar una carta, consultar la tabla |
| M8 La mesa habitada | Lyra | poner música en el gramófono |
| M9 Una imagen adquiere forma | Senet | proyectar en el cristal, coger la Llave de Mercurio |
| M10 El Báculo de Senet | Senet | ir al soporte del báculo (cogerlo si está libre) |
| M11 El Atlas Despierto | Senet | pista, luz, llave, pedestal, Atlas (secuencia SEQ_040) |
| M12 Memoria del gabinete | Senet | cierre: mesa libre o aprender a dirigir |

## Lo que funciona y lo que no

| Función | Estado | Nota |
|---|---|---|
| Sala, mesa, sillas, escenas, mapa | IMPLEMENTADA | de la v2 |
| Un solo báculo, cogerlo y pasarlo | IMPLEMENTADA | el servidor decide quién lo lleva |
| Dados que tira el servidor, en secreto o no | IMPLEMENTADA | |
| Cuaderno de pasos (J), cámara (V) | IMPLEMENTADA | |
| Práctica de 12 misiones con 5 personas | IMPLEMENTADA | probada con recorrido.mjs |
| Comprobar que se tiró de verdad (M2) | PARCIAL | Rima pregunta; no hay paso «tira» [MOTOR] |
| Comprobar que se cogió el báculo (M10) | PARCIAL | cuenta acercarse al soporte; no hay paso «coge el báculo» [MOTOR] |
| Comprobar que se escribió en el chat (M8) | PARCIAL | solo cuenta el gramófono [MOTOR] |
| Senet lleva el báculo y lo cede si se lo piden | BLOQUEADA | [MOTOR] |
| Ficha editable y vínculo ficha–miniatura | BLOQUEADA | hoy son cosas que se cogen [MOTOR] |
| Mover y medir miniaturas en el mapa, niebla, visión | BLOQUEADA | [MOTOR] |
| Mazos de cartas y tablas con resultado al azar | BLOQUEADA | hoy solo se pulsan [MOTOR] |
| Cristal que materializa una entidad | BLOQUEADA | hoy solo se pulsa [MOTOR] |
| Repetir una estación | BLOQUEADA | misión repetible = saludo en bucle [MOTOR] |
| Ruta para aprender a dirigir (E14–E18) | PROPUESTA | entrega C; necesita un báculo de ensayo que no quite el real |
| Figuras de Rima, Lyra, Pictor y Cepheus | PARCIAL | todos usan el robot de la casa |
| Cosas de la práctica como modelos 3D | PARCIAL | hoy son bloques de color |

## Cómo verlo
`bash servicios.sh arrancar` y `bash servicios.sh pagina`, y abrir http://127.0.0.1:5321/. Ya están en marcha.

## Pendiente de mirar
- No hay fotos todavía: falta mirar con los ojos que los bloques de la maqueta se vean bien en la mesa de preparación.
- Las cosas de la práctica las ven todos, también quien está jugando una partida (el motor no oculta cosas por persona).
