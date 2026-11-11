# Gabinete de los Mil Mundos · Sound design (C1)

Status: design, v1 [PROPUESTA]. No audio file exists yet; C3 builds and wires them. Guion §12 (sound identity and audio rules) is the authority; this file adds sources, levels and the cue list per station.
Machine-readable twin: `sonido.json` (schema `sonido-gabinete@1`). **Edit the JSON first, then the tables below** (the tables are a copy for people).

## 1. Identity in one paragraph
A warm observatory-library. Wood, paper, brass, one glass crystal. Sounds **recognise an interaction, they never ask for a quick answer**: no clock ticking, no looping mechanism, no fanfare. Every tone is short (most under 1 s, the longest 2.5 s). Voice is the reference level; nothing is louder than a person speaking. Silence is a valid way to play.

## 2. Buses and mix (design targets, none measured)
| Bus | Default | Who controls | Rule |
|---|---|---|---|
| `voice` (people talking, dialogue audio if any) | 0 dB reference | user | never ducked, never compressed by us; every other bus peaks below it |
| `effects` | -6 dB | user slider | each sound has its own peak (table §4) |
| `ui` | -6 dB | user slider | clicks and confirmations, always quiet |
| `ambient` | -6 dB | user slider | room tone, 2 loops only |
| `music` | -8 dB | user slider and DJ (allowed campaigns) | **no default track** (guion §12.1: no exclusive theme); the gramophone starts silent |

- Mute all = no sound at all, and **every state marker stays visible** (chat, mission text, results, notebook checks). Muting never hides information.
- **Every cue that carries meaning has a caption** (text in the toast area or the log, see UI.md §9). A caption is not subtitles of speech; it names the event: «[Dice rolling]». Cues marked `silent` carry no information (steps, room tone, a label appearing).
- No synthetic voices are generated (guion §12.2). Dialogue is subtitled text; sound never carries the only copy of a message.
- Sound is **pre-rendered**: no convolution reverb, no live synthesis, no spatial audio filters on phones. Stereo position only for the stations and only on desktop and VR; on phone, mono centre.
- One dice sound per throw, not per die, even for 3d6; the result tick (`dice_result`) plays once per result line, max 3 per second.
- `turn_next` plays once per turn change, nothing while idle.

## 3. Budget for phones (design targets, not measured)
Format ogg/opus mono 48 kbps. Effect ≤ 40 kB, loop ≤ 120 kB, all effects ≤ 450 kB, 12 simultaneous voices on desktop, **6 on phones**. Loaded **lazily by station** (E0–E1 sounds first; the rest when the previous station starts), so none of it counts in the 1 MB gz page budget before first draw. The two loops are decoded once and kept; one-shot sounds are dropped when the station ends. No audio work while the tab is hidden. There is a master mute and four sliders.

## 4. The sounds, one source each
Sources are named **at pack level**. The four Kenney packs were read on their official pages on 2026-11-07 and each states «Creative Commons CC0» (Interface Sounds 100 files, Casino Audio 50, Impact Sounds 130, RPG Audio 50). **Not done:** I could not download the packs from this machine (direct fetches are refused), so the exact file names are **not checked**: the «Spec / pick» column says which kind of file to choose, and C3 must record the exact file name and its sha256 in the ficha when it adds each piece. The «own, procedural» sounds are made by a script from the numbers in the table, so they are Numen Games' own work (CC0, `made_by: person with AI`) and no outside licence is involved. If a Kenney file cannot be downloaded, the replacement must be CC0 and named in the ficha; otherwise escalate (never place a sound with unclear rights).
Peak is in dB **relative to voice** (0 dB), a design target.

| Id | What | Source | Spec / pick | Bus | Peak (dB vs voice) | Max |
|---|---|---|---|---|---|---|
| `amb_room` | Room tone: faint mechanism and paper murmur, loop | own, procedural | Pink noise 120-900 Hz band-pass, 2 slow amplitude swells, a rare paper rustle (filtered noise burst, 1 per 20 s). Seamless 20 s loop, mono. | ambient | -30 | 20000 ms |
| `amb_vestibule` | Vestibule resonance, loop | own, procedural | Two detuned sines 82.4 Hz + 123.5 Hz, 6 s swell, soft low-pass 600 Hz. Seamless 12 s loop, mono. | ambient | -32 | 12000 ms |
| `step_wood` | Step on wooden floor | Kenney RPG Audio | footstep-type files; 4 variants, pick at build | effects | -22 | 400 ms |
| `mech_access` | Access mechanism / ceremonial door | Kenney RPG Audio | door open/close + creak files; layered with 1 Impact Sounds wood-heavy file | effects | -18 | 1800 ms |
| `page_flip` | Page turn | Kenney RPG Audio | book flip files; 3 variants | effects | -22 | 500 ms |
| `book_open` | Book / notebook opens | Kenney RPG Audio | book open file | effects | -20 | 700 ms |
| `book_close` | Book / notebook closes | Kenney RPG Audio | book close file | effects | -20 | 600 ms |
| `drawer` | Drawer / cabinet | Kenney Impact Sounds | soft wood impact, low-passed 3 kHz at build | effects | -22 | 500 ms |
| `bell_min` | Minimal call bell | own, procedural | Bell partials 1 : 2.76 : 5.40 : 8.93 of 1318.5 Hz (E6), gains 0 / -9 / -15 / -21 dB, decay 700 ms. | effects | -24 | 900 ms |
| `dice_wood` | Dice on wood tray | Kenney Casino Audio | die/dice throw files; 3 variants | effects | -16 | 900 ms |
| `dice_metal` | Metal dice on brass tray | Kenney Casino Audio | die throw file + Impact Sounds metal-light layer, high-passed 400 Hz | effects | -16 | 900 ms |
| `dice_bone` | Bone/resin dice, d6 group | Kenney Casino Audio | dice throw (multiple) file | effects | -16 | 1000 ms |
| `dice_result` | Result lands (single clear tick) | Kenney Interface Sounds | one short confirm/tick file | ui | -18 | 250 ms |
| `ui_click` | Button / label press | Kenney Interface Sounds | click file (soft) | ui | -20 | 150 ms |
| `ui_toggle` | Switch on/off (panel, option) | Kenney Interface Sounds | toggle/switch files (2: on, off) | ui | -20 | 200 ms |
| `ui_confirm` | Action accepted (step checked, saved) | Kenney Interface Sounds | confirmation file (soft, low pitch) | ui | -18 | 400 ms |
| `ui_error` | Action refused (UI_1090-UI_1094) | Kenney Interface Sounds | error/back file, the mildest one | ui | -20 | 350 ms |
| `ui_send` | Chat message sent | Kenney Interface Sounds | soft select file | ui | -22 | 250 ms |
| `ui_whisper` | Whisper channel opens | Kenney Interface Sounds | toggle file pitched -3 semitones at build | ui | -22 | 300 ms |
| `map_unfold` | Map unfolds | Kenney RPG Audio | cloth file + Casino Audio card slide file layered | effects | -20 | 1200 ms |
| `mini_set` | Miniature set down on board | Kenney Impact Sounds | soft wood-light impact, 3 variants | effects | -20 | 300 ms |
| `door_stage` | Stage door opens/closes (diorama) | Kenney RPG Audio | door file at -6 dB and 1.5 x pitch (small scale) | effects | -22 | 900 ms |
| `lamp_switch` | Lamp on/off (diorama) | Kenney Interface Sounds | switch file, low-passed 4 kHz | effects | -22 | 250 ms |
| `turn_next` | Turn marker advances (single mechanical tick, never repeating) | Kenney Impact Sounds | metal-light impact, 1 file | effects | -20 | 350 ms |
| `macro_run` | Safe practice action runs | Kenney Interface Sounds | soft confirm file | ui | -20 | 400 ms |
| `card_shuffle` | Shuffle deck | Kenney Casino Audio | card shuffle file | effects | -20 | 1500 ms |
| `card_draw` | Draw a card | Kenney Casino Audio | card slide/place file | effects | -20 | 500 ms |
| `table_roll` | Random table picks an entry | Kenney Casino Audio | chip/dice-light file + ui_confirm layered | effects | -20 | 800 ms |
| `crystal_rise` | Crystal Narrative: tonal rise while the form is proposed | own, procedural | Sine sweep 392 -> 783.99 Hz over 1.6 s, + octave -12 dB, gentle tremolo 5 Hz 8 %, low-pass 5 kHz. | effects | -18 | 1800 ms |
| `crystal_chime` | Crystal Narrative: short harmony and glassy resonance when the object appears | own, procedural | Chord G4 B4 D5 (392.00 / 493.88 / 587.33 Hz), partial ratios 1 : 2.9 : 5.4, decay 1.4 s. | effects | -18 | 1800 ms |
| `crystal_fade` | Crystal Narrative: object withdrawn | own, procedural | Chime G4 B4 D5 reversed envelope then 300 ms fall to 261.63 Hz. | effects | -22 | 1000 ms |
| `staff_take` | Staff: you hold it (possession signature) | own, procedural | Two notes D3 -> A3 (146.83 / 220.00 Hz), warm triangle + sine, 450 ms each, soft attack 40 ms, ring of 1.2 s. | effects | -18 | 1700 ms |
| `staff_give` | Staff: you hand it over (release signature) | own, procedural | Mirror of staff_take: A3 -> D3, same timbre. | effects | -18 | 1700 ms |
| `lens_perspectiva` | Atlas chord 1 (Perspective) | own, procedural | C major C4 E4 G4 (261.63 / 329.63 / 392.00 Hz), sine + 2nd harmonic -12 dB, attack 15 ms, 1.2 s decay. | effects | -16 | 1300 ms |
| `lens_azar` | Atlas chord 2 (Chance) | own, procedural | G major G3 B3 D4 (196.00 / 246.94 / 293.66 Hz), same timbre. | effects | -16 | 1300 ms |
| `lens_forma` | Atlas chord 3 (Form) | own, procedural | F major F3 A3 C4 (174.61 / 220.00 / 261.63 Hz), same timbre. | effects | -16 | 1300 ms |
| `lens_relato` | Atlas chord 4 (Story) | own, procedural | A minor A3 C4 E4 (220.00 / 261.63 / 329.63 Hz), same timbre. | effects | -16 | 1300 ms |
| `atlas_close` | Atlas closing chord (all four lenses) | own, procedural | C major spread C3 G3 E4 C5 G5, same timbre, 2.4 s decay, no fanfare. | effects | -14 | 2500 ms |
| `safe_save` | Secure archive / save confirmed (calm) | own, procedural | Two soft sines C3 + G3 (130.81 / 196.00 Hz) 600 ms, low-pass 1.2 kHz. | effects | -24 | 900 ms |

## 5. Cue list per station (E0–E13)
Trigger = the real, verified event (never the mere visual). A cue never fires for a function that is [MOTOR] and absent (guion: no false success). E14–E18 (DJ route): `safe_save` for «secure archive», UI sounds for the console, nothing new until those stations are built. Lens chords at E2/E3/E4/E12 follow guion §12.1 (the guion lists E2/E3/E6/E12; E4 is the Form lens in the flow of §6, E6 lights no lens: **flagged for Ursa/Antunj**, the JSON follows §6).

**E0**

| Cue | Trigger | Sound | Caption (en / es) |
|---|---|---|---|
| `c0_ambient` | Instance loaded, scene ready | `amb_vestibule` | silent |
| `c0_door` | Ceremonial door shows open (scene start, once) | `mech_access` | [Mechanism: the door is open] / [Mecanismo: la puerta está abierta] |
| `c0_steps` | Avatar walks on wood (rate by speed, silent when still) | `step_wood` | silent |
| `c0_label` | «Talk to Senet» appears | `ui_click` | silent |

**E1**

| Cue | Trigger | Sound | Caption (en / es) |
|---|---|---|---|
| `c1_ambient` | Dialogue with Senet starts | `amb_room` | silent |
| `c1_book` | Notebook handed over / opened first time | `book_open` | [Notebook received] / [Cuaderno recibido] |
| `c1_accept` | «Learn to use the room» pressed | `ui_confirm` | [Practice started] / [Práctica iniciada] |

**E2**

| Cue | Trigger | Sound | Caption (en / es) |
|---|---|---|---|
| `c2_mirror` | Avatar view changes (V / wheel / button) | `ui_toggle` | [View changed] / [Vista cambiada] · UI_020 |
| `c2_notebook` | Notebook opens (J) / closes | `book_open` | silent |
| `c2_board` | Board view on / back to seat | `ui_toggle` | [Board view] / [Vista de tablero] |
| `c2_lens` | Four marks done: Perspective lens lights | `lens_perspectiva` | [Perspective completed] / [Perspectiva completada] · UI_030 |

**E3**

| Cue | Trigger | Sound | Caption (en / es) |
|---|---|---|---|
| `c3_throw_d20` | 1d20 thrown | `dice_wood` | [Dice rolling] / [Dados rodando] |
| `c3_throw_3d6` | 3d6 thrown as a group | `dice_bone` | [Dice rolling] / [Dados rodando] |
| `c3_throw_formula` | Formula 2d10+5 thrown | `dice_metal` | [Dice rolling] / [Dados rodando] |
| `c3_result` | Each result is written to the log | `dice_result` | [Result: <total>] / [Resultado: <total>] |
| `c3_invalid` | Invalid formula (nothing thrown) | `ui_error` | [Invalid dice formula] / [Fórmula de dado no válida] · UI_1093 |
| `c3_lens` | Chance lens lights | `lens_azar` | [Chance lens lit] / [Lente de Azar encendida] |

**E4**

| Cue | Trigger | Sound | Caption (en / es) |
|---|---|---|---|
| `c4_sheet_open` | Iria's sheet opens (quick card / full panel) | `page_flip` | silent |
| `c4_save_note` | Note edit saved | `ui_confirm` | [Saved] / [Guardado] |
| `c4_link` | Miniature linked to the character | `drawer` | [Miniature linked] / [Miniatura vinculada] |
| `c4_inventory` | Item added to inventory | `drawer` | [Item added] / [Objeto añadido] |
| `c4_lens` | Form lens (provisional) lights | `lens_forma` | [Form lens lit] / [Lente de Forma encendida] |

**E5**

| Cue | Trigger | Sound | Caption (en / es) |
|---|---|---|---|
| `c5_map` | 2D map unfolds, then the 3D model | `map_unfold` | [Map unfolded] / [Mapa desplegado] |
| `c5_move` | Miniature set on a valid square | `mini_set` | [Miniature moved] / [Miniatura movida] |
| `c5_invalid` | Invalid move (miniature stays) | `ui_error` | [Move not available] / [Acción no disponible] · UI_1090 |
| `c5_measure` | Distance measured (ruler) | `ui_confirm` | [Distance: <n>] / [Distancia: <n>] |

**E6**

| Cue | Trigger | Sound | Caption (en / es) |
|---|---|---|---|
| `c6_door` | Stage door opens/closes | `door_stage` | [Stage door: open|closed] / [Puerta de la maqueta: abierta|cerrada] |
| `c6_lamp` | Lamp on/off | `lamp_switch` | [Lamp: on|off] / [Lámpara: encendida|apagada] |
| `c6_preview` | Character / practice-DJ preview toggled | `ui_toggle` | [Preview: <mode>] / [Vista previa: <modo>] |

**E7**

| Cue | Trigger | Sound | Caption (en / es) |
|---|---|---|---|
| `c7_open` | Turn clock opens | `turn_next` | silent |
| `c7_next` | Next turn (one tick per advance, none while idle) | `turn_next` | [Turn: <name>] / [Turno: <nombre>] |
| `c7_macro` | Safe practice action confirmed and run | `macro_run` | [Practice action done] / [Acción de práctica hecha] |

**E8**

| Cue | Trigger | Sound | Caption (en / es) |
|---|---|---|---|
| `c8_doc` | Atlas page opens | `page_flip` | silent |
| `c8_shuffle` | Deck shuffled | `card_shuffle` | [Deck shuffled] / [Mazo barajado] |
| `c8_draw` | Card drawn | `card_draw` | [Card drawn] / [Carta robada] |
| `c8_table` | Random table run | `table_roll` | [Table: <entry>] / [Tabla: <entrada>] |
| `c8_bell` | Resource saved to the library (the minimal bell) | `bell_min` | [Resource saved] / [Recurso guardado] |

**E9**

| Cue | Trigger | Sound | Caption (en / es) |
|---|---|---|---|
| `c9_send` | Chat message sent | `ui_send` | [Message sent] / [Mensaje enviado] |
| `c9_whisper` | Whisper opens | `ui_whisper` | [Whisper channel] / [Canal de susurro] |
| `c9_music` | Gramophone starts / pauses a track the DJ or user chose (no default track) | `ui_toggle` | [Music: on|paused] / [Música: sonando|en pausa] |
| `c9_room` | Room tone under table observation | `amb_room` | silent |

**E10**

| Cue | Trigger | Sound | Caption (en / es) |
|---|---|---|---|
| `c10_form` | Crystal form opens | `ui_toggle` | silent |
| `c10_rise` | Proposal sent: tonal rise while it is previewed | `crystal_rise` | [Crystal: proposal projecting] / [Cristal: propuesta proyectándose] |
| `c10_appear` | Object (or labelled marker) created | `crystal_chime` | [Crystal: object created] / [Cristal: objeto creado] |
| `c10_withdraw` | Object withdrawn / redone | `crystal_fade` | [Crystal: object withdrawn] / [Cristal: objeto retirado] |

**E11**

| Cue | Trigger | Sound | Caption (en / es) |
|---|---|---|---|
| `c11_take` | Staff passes to the player | `staff_take` | [You hold the Staff] / [Usted tiene el Báculo] |
| `c11_give` | Staff returned to Senet | `staff_give` | [Staff returned to Senet] / [Báculo devuelto a Senet] |
| `c11_busy` | Staff request refused (already held) | `ui_error` | [The Staff is already taken] / [El Báculo ya está ocupado] · UI_1094 |

**E12**

| Cue | Trigger | Sound | Caption (en / es) |
|---|---|---|---|
| `c12_door` | Mechanism opens the door | `door_stage` | [Mechanism: door open] / [Mecanismo: puerta abierta] |
| `c12_roll` | 2d6 thrown | `dice_wood` | [Dice rolling] / [Dados rodando] |
| `c12_lens` | Story lens lights, then the Atlas wakes (4 chords in order, 1.2 s apart, then the closing chord) | `lens_relato` | [Atlas awake] / [Atlas despierto] |
| `c12_close` | Atlas closing chord after lens 4 | `atlas_close` | silent |

**E13**

| Cue | Trigger | Sound | Caption (en / es) |
|---|---|---|---|
| `c13_save` | Save confirmed (only after the save is real) | `safe_save` | [Progress saved] / [Progreso guardado] · UI_990 |
| `c13_fail` | Save failed | `ui_error` | [Could not save] / [No se pudo guardar] · UI_1092 |
| `c13_ambient` | Room stays alive for free table | `amb_room` | silent |

## 6. When several cues coincide
Priority: refusal (`ui_error`) > mission-completing state change (lens chords, `atlas_close`) > staff signatures > everything else. A lower one is dropped if a higher one started less than 300 ms ago, never queued (queued sounds arrive late and read as noise).

## 7. What I could not check
- Real loudness and how the tones sit together: **not listened to** (no audio device here). Levels are targets.
- File sizes against the budget: **not measured** (no files yet).
- Exact file names inside the Kenney packs (§4).
- Caption wording is a proposal; the house's words belong to Antunj (Exegetes). Spanish captions use «usted».
