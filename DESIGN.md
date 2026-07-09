# sloshbot — design system

The visual and interaction language for sloshbot, decided one layer at a time
in the design lab (July 2026). This is the record of **what** we chose and
**why**, so the decisions don't have to be re-litigated.

The design is now **fully ported** onto the real app — there are no separate
prototype files anymore. The living implementation is the source of truth:

- **Canonical look:** the real templates — `app/templates/week.html` (the
  weekly view) is the reference build; `home.html` (Tonight), `calendar.html`
  (time-grid), and `map.html` (Leaflet) are its adaptations.
- **Shared chrome & cards:** `app/templates/_components.html` — the two-tier
  header/nav and the tactile "coaster" card system, included by every view.
- **The tokens:** `app/templates/_tokens.html` — every color, size, radius,
  shadow, and font as a named variable. Change the system there.

> The earlier `design_*.html` exploration surfaces and `PORT_SPEC.md` were
> removed once the port landed; this document is their durable replacement.

---

## The concept

**A speakeasy.** Warm near-black, candle-lit, brass used scarcely — the one
thing that's "lit" is the thing worth your attention. sloshbot predicts one
thing: **how likely an event has free booze.** Everything in the design serves
that single signal.

The voice is a **wry bartender** — dry wit in the seams (empty states, the
verbal read, feedback), but functional controls stay plainly clear.

---

## The eight decisions

### 1. Color — "Brass & Oxblood"
One warm temperature, committed. The biggest "generic app" tell we removed was a
cool sage meta-text color fighting the warm ground; everything is warm now.
- **Ground:** brown-biased near-blacks (`--bg` `--card` `--card2`), not grey.
- **Text ladder:** pinned to *measured* contrast — `--text` 15:1, `--meta` 9:1,
  `--muted` 7:1, `--faint` 5:1 on the card. All pass accessibility (AA).
- **Rule that matters:** *value (lightness) carries readability; hue/chroma
  carry mood.* Mute the **marks** (borders, glows, the coupe) as much as you
  like; never mute **text** below its contrast target to calm it down.
- **Accent:** brass `--gold`, spent scarcely. Semantic colors come from the
  material world — oxblood `--red`, a muted olive `--green` — not stock
  success/error hues.

### 2. Motion
One easing curve everywhere: `--ease`, a slight overshoot. That overshoot *is*
the app's personality; put it on every transform transition.

### 3. Type — three roles, one job each
- **Fraunces roman** = *names* (card titles, day headers, the wordmark). Size
  carries the tier: confident titles 25px, maybe titles 19px.
- **Fraunces italic** = *the voice* — reserved for the event blurb (see §7) and
  wordmark. Because italic *means* "voice", don't use it decoratively elsewhere.
- **Hanken Grotesk** = everything functional (body, meta, buttons, labels, the
  nav tabs). A warm humanist sans; replaced the system-default font.
- Datelines use **oldstyle figures**; align any columns of digits with
  `tabular-nums`. Micro-labels are uppercase + letter-spaced.

### 4. Composition
- A single **wide column you scroll** (~720px), never a grid of narrow "chip"
  cards. On mobile the column carries a 20px (14px on small phones) side inset
  so content never clips the edge.
- **Grouped by day** under Fraunces-roman headers (sticky as you scroll).
- **Booze threshold gate:** events at/above the threshold show by default; the
  rest of each day tuck into a "*N more, less sure*" disclosure. **The threshold
  slider and this gate are the same control** — dragging it re-pours each day.
- Cards separated generously (18px).

### 5. Material — tactile "coasters"
- One light source (from above). Elevation is a ramp of tokens
  (`--elev-rest/hover/press`, `--elev-glow-*` for confident, `--elev-pop`).
- **Whisper grain** (`--grain`): a barely-there noise tile at 0.03 opacity under
  the card surface. Felt, not seen.
- **Two-tier press system** (a rule, written in `_tokens.html`):
  SURFACES lift · KEYS depress · STAMPS rotate · FLAT controls change color only.

### 6. Shape
- **Four radii, nothing else:** `--r-card` 16 · `--r-control` 8 · `--r-stamp` 4
  · `--r-pill` 99. (The coupe drawing and circular slider thumbs are exempt.)
- **1px hairline borders** everywhere; 1.5px reserved only for the coupe glass.

### 7. Information design
- The **coupe glass** is the score — its fill = booze likelihood. It drives the
  tier. The raw % never appears on the scan card; it lives only in the coupe's
  tooltip and the expanded drawer.
- **Blurb leads, reasoning follows.** The card's italic standfirst is what the
  event *is*; the booze reasoning ("why the pour") only appears when you click a
  card open. These are two distinct texts — see §9 (AI blurbs).
- The **top meta line shows the neighborhood**, not the (often full-address)
  venue string. The venue-with-neighborhood detail lives in the expanded drawer.
- **Distance is a filter, not a score.** It never feeds the booze number.

### 8. Voice & copy
Wry bartender, applied only where it has no functional cost.
- Feedback: **"any booze?"** → **yep / nope**
- Disclosure: **"N more, less sure"**; the weekly day tally reads **"N shown · N on tap"**
- Empty state: **"nothing worth pouring above N% today"**
- Reasoning label: **"why the pour"**
- Functional controls stay plain: filter labels, distance, **+ calendar** (the
  hold-to-calendar button) — clarity first, wit second.

---

## 9. AI blurbs — the italic standfirst

The card's italic serif line (`.ta-why` / `.evt-pop-blurb`) is a **model-written
one-liner describing what the event actually is** — not a truncation of the
scraped description (which cut mid-word and dragged in listing cruft).

- **Where it comes from:** the *existing* booze scoring call
  (`scoring/scorers/booze.py::_llm`, Claude Haiku) now returns a `blurb`
  alongside `score` + `rationale` in the same JSON — **zero extra API calls** for
  every newly-scored event. A shared `BLURB_INSTRUCTION` also powers a
  standalone `generate_blurb()` used only by the backfill.
- **Rules (the prompt):** one concrete clause, **≤90 characters** (~8–14 words),
  present tense; says what happens, prefers specific nouns; **never** mentions
  free drinks / odds (that's the rationale's job), price, or "Join us" boilerplate.
  A `_clip_blurb()` backstop trims any overshoot at a word boundary (no ellipsis).
- **Storage:** a `blurb` column on the `scores` table (booze scorer's row).
- **Fallback:** serving prefers the stored blurb, falling back to the mechanical
  `_blurb(description)` trim so nothing renders empty for an unscored event.
- **Distinct from the rationale:** the blurb is *description*; the "why the pour"
  rationale is *booze reasoning*. Never collapse the two.

---

## 10. Navigation & chrome — the two-tier header

`_components.html` defines one **sticky two-tier header** shared by every view:

- **Row 1 (global):** the Fraunces-italic **`sloshbot` wordmark** on the left,
  and the four **view tabs** on the right — Tonight · This week · Calendar · Map.
  Tabs are quiet Hanken labels, **not pills**: the active view is lit brass with
  a 2px underslung brass bar (an `::after` that scales in); inactive is `--muted`,
  hover is `--gold`. Reads like a bar menu's section headers. Vertically centered.
- **Row 2 (per-view):** each view's own control bar (the coupe slider + filters),
  pinned just beneath at `top: var(--hdr-h)`. Kept visually separate from nav so
  "where am I" and "what am I filtering" never blur together.

---

## 11. Filters & persistence

- **Filter sets by view:**
  - *Tonight* — distance, free, tags, source (no slider; it already sorts by booze).
  - *This week / Calendar* — the coupe **"chance of booze" slider** + distance,
    free, tags, source.
  - *Map* — the slider + distance, tonight, free, tags, source, plus an
    always-visible, **resettable home-address setter** (pre-filled; "Update home").
- **The slider lights, it doesn't (always) hide.** On Calendar and Map, raising
  the threshold **dims** below-bar items and **lights** those that clear it —
  nothing is removed. On the Map, **pin size is fixed by each event's booze %**
  (a `pow(t, 1.7)` curve, ~5–18px), so the slider only re-lights pins, never
  resizes them.
- **Cross-page persistence.** All filters live in one localStorage object,
  `slosh.filters` = `{thresh, maxMi, free, tags[], sources[]}`, written
  merge-patched (each view only overwrites the keys it owns). Set a filter on any
  view and it carries to the others. (A leftover tag/source selection therefore
  narrows every view — a "filters active" cue is a candidate if that surprises.)

---

## The signature control: the coupe slider

The booze-threshold filter is the identity centerpiece: a slider whose **thumb is
a glass** that fills with gold as you raise the threshold ("chance of booze —
NN%"). It sets what's shown vs. tucked away per day. Built on the native range
input (keyboard-accessible) with the thumb styled directly — no JS-positioned
overlay (an earlier version drifted).

---

## Implementation status

The port is **complete** — all four views run on this system (shared tokens,
`_components.html` chrome + coaster cards) against live data, with real
interactions wired (feedback → `/feedback/...`, "+ calendar" → the event's gcal
link, cross-page filter persistence, Leaflet + CARTO dark tiles on the map).
There is no longer an "old look" to migrate; edit the real templates and the
tokens directly.

---

## Open items (deferred)

- ~~**Booze-only score** (remove the arbitrary `logistics` scorer).~~ **Done** —
  the backend is booze-only.
- ~~**AI blurb from the scoring pass**, not client-side truncation.~~ **Done** —
  see §9; the booze call returns the blurb, backfilled by
  `scoring/backfill_blurbs.py` and self-healed each pipeline cycle.
- **Venue names sometimes contain the street address** (scraper quality). The
  card now leads with the neighborhood and strips the venue at the first comma as
  a guard; the real fix is still at ingestion.
- **Tag/source filter counts** in the popovers are static (full-dataset), not
  recomputed as other filters narrow the set.
- **Feedback placement** — kept on-card; a separate "which did you go to?" view
  was considered and declined for complexity.
