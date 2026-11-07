# Gabinete de los Mil Mundos · UI design (C1)

Status: design, v1 [PROPUESTA]. Authority: guion §13 (layout, interaction, accessibility, notebook, pointer) and §7.7 (screen texts: the only copy of every sentence; this file uses their codes, never rewrites them). Nothing here is implemented; a panel whose function is missing shows «Pending implementation», never a fake success (guion §0, §5). Preview: `maqueta-hud.html` (static wireframe, desktop + phone; not the engine).
Look: the house tokens already in the engine (`--n-fondo #14110F`, `--n-sup`, `--n-elev #292420`, `--n-texto #F9EBDC`, `--n-texto2 #C4B5A6`, `--n-linea #3A332D`, `--n-accion #017C8D`, `--n-ambar #EFA517`, `--n-turquesa #018EA1`, `--n-verdemar #A6DAD5`, `--n-grana #D33440`, `--n-hecho #8FC46B`; Geist / Geist Mono; control radius 6 px). **No new token.** Steampunk lives in the 3D objects (brass, leather, amber); the utilitarian UI stays sober (guion §13) and borrows only the amber as accent.

## 1. Measured contrast (WCAG 2.x relative luminance, computed with a script on the token values)
| Pair | Ratio | Use | Verdict |
|---|---|---|---|
| texto on fondo | 16.07 | body text | AAA |
| texto2 on fondo | 9.41 | secondary text, dates | AAA |
| texto on elev | 13.12 | cards | AAA |
| texto2 on elev | 7.68 | secondary on cards | AAA |
| ambar on fondo | 9.01 | accent text, focus ring, active tab marker | AAA |
| fondo on ambar | 9.01 | text on amber buttons | AAA |
| #fff on accion | 4.92 | primary button text | AA (normal text) |
| turquesa on fondo | 4.82 | links, ring | AA |
| verdemar on fondo | 12.19 | status text | AAA |
| **linea on fondo** | **1.51** | panel borders | **fails 3:1**: borders are decoration only; a control is identified by its text/icon and its fill, never by the border alone |
Not measured: contrast over the 3D scene behind translucent panels (varies with the view). So **panels on the scene are opaque** (`--n-sup` is .9 alpha; use alpha 1 behind any text block, see §8).

## 2. Layout, desktop (≥ 1024 px wide) — guion §13.1 made exact
```
┌──────────────────────────────────────────────────────────────────────────┐
│ [Objective ▾ min]                                      [Cam][?][⚙][net/save]│  top bar, 48 px high
│ UI_xxx text, 1–2 lines                                                   │
│ ┌─Notebook┐                                                              │
│ │ drawer  │                  (centre: free, only the                     │
│ │ left    │                   contextual label at focus)                 │
│ │ 360–480 │                                                 ┌─Table───┐  │
│ │         │                                                 │ panel   │  │
│ └─────────┘                                                 │ 320 w   │  │
│        ┌── Dialogue: lower third, 56 % wide, centred ──┐    └─────────┘  │
│        │ Senet: subtitle … [ option 1 ] [ option 2 ]   │ [Toast area]    │
└──────────────────────────────────────────────────────────────────────────┘
```
| Zone | Component | Size | Behaviour |
|---|---|---|---|
| Top left | Current objective | 320 × auto (max 3 lines), 14–16 px × user scale | minimisable to a 40 px chip «Objective»; retracts when the notebook opens; never over an NPC face or the table centre (it sits above them: the camera framing C-4 keeps faces below 35 % of height) |
| Top right | Camera, Help, Settings, network/save state | four 44 × 44 targets | each shows its shortcut as text on hover **and** focus; save state is text («Saved 14:03», «Could not save» UI_1092) plus icon |
| Centre | Contextual label | one line, name + verb, appears only at focus | e.g. «Rima's tray · Roll dice»; no permanent giant cards; it does not move the camera (guion §13.5) |
| Lower third | NPC dialogue | max 56 % width, 3 lines of subtitle + options as full-text buttons | never over the speaker's face; options are real buttons, text complete (no voice needed) |
| Lower right | Table panel | 320 w, collapsible to a 44 px tab | quick sheet, dice, turns; one tab open at a time. Sits in the right column **above** the dialogue band; **while an NPC dialogue is open it collapses to its tab** (found in the wireframe: a centred 56 %-wide dialogue overlaps a 320 px panel on any window under ~1500 px) |
| Left | Notebook / archive drawer | 360–480 px, user-resizable | slides in 150 ms (opacity + transform only) |
| Private | DJ console | separate protected surface | **never appears because you hold the Staff** (guion §13.1); only with real permission |
| Toast area | Short notices (UI_030, UI_1090…, captions of sounds) | stacks above the dialogue, max 2 visible | each stays 5 s, 8 s if the user scales text ≥ 150 %, and all go to the log |

Toasts and captions live in an `aria-live="polite"` region; errors (`UI_1090–UI_1094`) `role="alert"`. Everything is DOM, not canvas.

## 3. Layout, phone (portrait ≤ 600 px) — one layer (guion §13.3)
- **One tool layer at a time**: the top shows the objective as a single line chip (taps to expand to a bottom sheet). Notebook, sheet, dice, chat, ruler each open as a **bottom sheet** (max 70 % of height, drag handle plus a visible «Close» button); opening one closes the other.
- Bottom bar (56 px + safe area): **Menu · Notebook · Table · Chat · Camera** — five targets ≥ 48 × 48 px with a visible text label under each icon (no icon-only controls).
- Dialogue: lower third above the bar, options stacked full width, 48 px tall.
- Settings and help in the menu. The contextual label is a **button in the bottom bar area** («Roll dice»), so no precise tap on a small 3D object is needed (guion §13.2.5).
- Landscape: the sheets turn into a right-hand drawer 45 % wide; the objective chip stays.
- **No hover anywhere.** Every shortcut is also a visible button. Gestures: one-finger drag looks, two-finger pinch is the camera zoom only; nothing requires a three-finger gesture.
- **Phones stay cool (rule 2 of the engine):** no `backdrop-filter` blur, no animated gradients or shadows, no particle HUD, no per-frame DOM updates (the HUD updates on events only), transitions only on opacity/transform ≤ 150 ms, panels opaque. Frame caps and pixel ratio are the engine's (`MOVIL-FRIO.md`); the HUD adds no timers. (Not measured on a real phone: engine item X1.)

## 4. Layout, VR (headset) — guion §13.3, C-18 (the camera belongs to the user)
- Panels are **world-anchored** (not stuck to the head), placed on the table's far side at **1.2–1.8 m**, centre at eye height −10°, **never moved by the system once the user looks at them**. The user can grab and re-place them; the position is remembered.
- Size: default panel 0.9 m wide × 0.6 m at 1.5 m (≈ 33° wide). Body text character height **4 cm at 1.5 m ≈ 1.5°** (design target; **not verified in a headset**). Buttons ≥ 6 × 6 cm (≈ 2.3°), 1 cm gaps.
- Selection is **explicit**: ray + trigger (or pinch) on a visible button; the ray shows the same label as the desktop focus; no dwell-select, no gaze-select. Every action is a button with text, never a 3D-object-only gesture.
- Only the objects change in the world; no camera animation, no vignette movement from us (C-18). The sequences (`SEQ_xxx`) animate the object, not the viewer.
- Long documents (the Atlas page, a sheet) open on a **large reading panel** (1.2 m × 0.8 m), scrollable with the thumbstick; never micro text on a 3D book.
- Captions (sound) appear in a small panel below the table edge, not head-locked. 
- Render cost: a panel is one canvas texture, redrawn **only when its content changes**; one quad per panel; no transparency stacks. (Not measured: engine item for VR cost.)

## 5. Quick sheet and full sheet (E4: `ficha_jugador`, `ficha_eco`, `mini_iria`)
Three scales of the same card (guion E4): **icon** (on the miniature: name tag, 24 px min), **quick card** (lower-right table panel), **full panel** (expanded, scrollable, left drawer or large reading panel in VR).
- Quick card (320 w): name (Geist 600, 18), concept (texto2), 3 key attributes as «label: value» pairs, public/private chip, buttons «Open full sheet», «Link miniature». No more than 8 lines.
- Full panel: sections Identity · Attributes · Notes · Resources · Inventory. Only fields the user may edit show an input; the others are text. Edit = explicit field focus, «Save» and «Discard» buttons; unsaved changes show «Unsaved changes» with the discard confirmation. **Closing never loses confirmed edits and never auto-saves unconfirmed ones silently** (guion E4).
- **Privacy is stated in words and icon, never colour alone**: chip «Public» (open-eye icon) or «DJ only» (lock icon). Player view of the creature (Eco sin Contorno): name, silhouette, authorised facts; hidden attributes and notes are **not rendered at all** (not greyed): no blank rows that hint at them.
- Inventory: list with a count and an «Equip» button per item (text «Equipped» when done). Deleting a miniature does not delete the sheet: the sheet shows «No miniature linked» (E4 error rule).
- States: empty («No items yet»), loading (text «Loading sheet…» after 400 ms, no spinner-only), error («Action not available», UI_1090), read-only («Read only: you may view this sheet»).

## 6. Notebook (key J) — guion §13.4
- Opens as the left drawer (phone: bottom sheet). `J` toggles; `Esc` closes; focus returns to the element that opened it.
- Tabs, in this order: **Missions · Clues · Records · Controls · Sequences**. Keyboard: `←/→` between tabs, `Tab` into the list. Tab labels are text.
- Mission list: each step is a row: empty box «☐ Open the notebook» · checked box «☑ … · 2026-11-07 14:03» (check **and** date/time text; colour `--n-hecho` is extra, never the only signal) · missing function «Pending implementation» (texto2, italic, no box).
- Controls tab: click, drag, key V / wheel, Esc, open/close panel, return to own view; **no voice commands are listed** (there are none).
- Sequences tab: «Watch again» buttons; replay grants no reward twice (label: «Replay: no new reward»).
- Registers: dice history, with public/private badge per line (text «Private to you»); the table's history is selectable text.
- Size: width configurable 360–480 px (drag handle **and** the «Narrower / Wider» buttons for those who cannot drag); text scale from the setting.
- Behaviour: opening retracts the objective chip; pressing J again restores it. Double-pressing «Accept» never duplicates a mission (E1 error rule).

## 7. Ruler (E5, `mapa_puertas`, `diorama`) — measure from A to B
- Entered with the button «Measure» in the table panel (also key `M`, and menu). While active, the panel shows «Measuring: choose point A» → «choose point B» → result; `Esc` leaves. It **never moves a miniature** (E5 rule).
- Pointing: click/tap/ray on the board sets A then B. **Keyboard alternative:** arrow keys move a visible square cursor on the grid, `Enter` sets the point; or type coordinates (column + row) in two fields. Phones: tap two squares; magnifier is not used (no extra render).
- Result is **text first**: «A → B: 4 squares · 6 m» (unit comes from the map's own scale; if the map has none: squares only). The 3D line on the board is secondary; the line has a label and **dashed** style, not just a colour.
- Height difference (guion E5: «consult a slope»): second line «Height: +1 level».
- States: waiting A / waiting B / result (with «Measure again», «Clear») / invalid point («Action not available») / grid hidden (still measures).
- The result goes to the log. Cost: one line + one text label; no per-frame redraw.

## 8. Crystal panel (E10, `cristal`, `objeto_creado`) — «that imagination takes form»
Guion E10 rules: the form is opened by a click, text never runs as a macro, no pretending 3D art was generated.
- Steps shown as a visible 4-step bar with text: **1 Propose · 2 Preview · 3 Authorise · 4 Place** (current step «Step 2 of 4»).
- Step 1 form: choice «Create an object»; fields Name · Function · Proposal (counters in bytes **before** sending, with the limit shown: «212 / 256 bytes»; never truncate silently; error text if over). Button «Send proposal» (explicit).
- Step 2: preview card shows either the library model or the **labelled provisional marker** («Provisional marker — no new 3D model was generated»). 
- Step 3: «Authorise and place» (confirmation required, shared consequence) / «Reject».
- Step 4: object in the world with «Inspect» and «Withdraw». Withdraw is reversible: «Redo» appears for the session.
- States: empty form, validation error per field, sending, awaiting authorisation («Waiting for authorisation by the table master»), refused («Permission not enough», UI_1091), done, withdrawn.
- The text typed here is **material**, not instruction: displayed as plain text, escaped, never as a command (STD-050).
- Opaque panel; the crystal's own glow is a single emissive material, no screen-space effect (phones).

## 9. Notices, captions and the log
- Toasts and sound captions share one region (§2); one line each; the full list is in Notebook → Records («Log»). Sound captions are on by default **only when sound is muted or the user turns «Describe sounds» on** (setting, remembered), so players who hear are not flooded.
- Texts of success and error come only from §7.7 (`UI_030, UI_990, UI_1090–1095`). Captions of sounds are in `sonido.json` and are proposals.

## 10. Accessibility checklist (guion §13.3) — what is designed, what is verified
| Requirement | Design decision | Verified |
|---|---|---|
| Contrast | table §1; min text contrast 7:1 for body, 4.5:1 for the rest | measured on tokens; not over the live scene |
| Scalable text | setting 100 / 125 / 150 / 200 %; layouts reflow (drawer widens, chips wrap); nothing clips or truncates silently | not tested in a browser |
| Target size | desktop ≥ 44 × 44 px, phone ≥ 48 × 48 px, VR ≥ 6 cm | design |
| Keyboard | every verb reachable: `J` notebook, `M` measure, `Tab`/`Shift+Tab`, `Enter`/`Space`, `Esc` closes the top panel; a visible focus ring (3 px amber, 2 px offset) | design; no focus trap in panels except while a confirmation is open |
| Not colour alone | state = text + icon (✓ / lock / eye / turn number); turns show «Turn 2 · Iria», not a glow | design |
| Motion | `prefers-reduced-motion`: panels appear without slide; sequences `SEQ_xxx` stay skippable and local; no flashing > 3 Hz anywhere; crystal pulse ≤ 1 Hz and off under reduced motion | design |
| Screen readers | panels have `role="dialog"` + label; live regions (§2); object cards = the same `state.card` text agents read (engine rule 1) | design; not tested with a reader |
| Sound | every meaningful cue has a caption (`sonido.json`); mute keeps all information | design |
| Language | English default; Spanish with «usted»; text boxes and buttons sized for ~35 % longer strings | design |
| Persistence | text scale, panel widths, mute/sliders, «Describe sounds», reduced motion saved per user across sessions (guion §13.3) | design; needs [MOTOR] support |
| Headset | §4; no gaze-only, no dwell, no head-locked panels | not verified in a headset |
| Phone | §3; one layer, labels on icons | not verified on a phone |

## 11. Dice and other table panels (small)
Dice (E3): button per die type, a formula field with live validation (invalid → «Invalid dice formula», UI_1093, **nothing thrown**), «Roll» explicit, history list with «Public»/«Private to you» text. Turn clock (E7): ordered list «1 Iria · 2 Eco», current row marked with the word «Current»; «Next turn» button. The Staff holder is shown as the text «Staff: <name>» in the table panel, always (E11).

## 12. Handoff for C3 / engine
- Only tokens that already exist; every panel has a keyboard path and a text label; `state.card` for each thing (engine rule 1).
- A function that is [MOTOR] and absent: panel shows «Pending implementation»; the mission step stays unchecked.
- Open design decisions for Pablo/Antunj: wording of captions; whether «Describe sounds» defaults to on; whether panels in VR may be re-placed by the user (proposed: yes).
