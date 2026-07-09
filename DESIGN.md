# sloshbot — design system

The visual and interaction language for sloshbot, decided one layer at a time
in the design lab (July 2026). This is the record of **what** we chose and
**why**, so the decisions don't have to be re-litigated and so the look can be
ported to every page consistently.

- **Live reference:** `app/templates/design_real.html` — the weekly view, built
  on real data. This is the canonical look; when in doubt, match it.
- **The tokens:** `app/templates/_tokens.html` — every color, size, radius,
  shadow, and font as a named variable. Change the system there.
- **Prototype-only files:** `design_lab.html` (the three-direction comparison)
  and `design_real.html` are exploration surfaces. They inline the fonts as
  large data blobs; production loads fonts from Google Fonts via `_tokens.html`.

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
- **Fraunces roman** = *names* (card titles, day headers). Size carries the
  tier: confident titles `--fs-title-lg` (25px), maybe titles `--fs-title` (19px).
- **Fraunces italic** = *the voice* — reserved for the event blurb/pitch. Because
  italic *means* "voice", don't use it decoratively elsewhere.
- **Hanken Grotesk** = everything functional (body, meta, buttons, labels). A
  warm humanist sans; replaced the system-default font, which read as generic.
- Datelines use **oldstyle figures**; align any columns of digits with
  `tabular-nums`. Micro-labels are uppercase + letter-spaced at `--fs-label`.

### 4. Composition
- A single **wide column you scroll** (~720px), never a grid of narrow "chip"
  cards.
- **Grouped by day** under Fraunces-roman headers (sticky as you scroll).
- **Booze threshold gate:** events at/above the threshold show by default; the
  rest of each day tuck into a "*N more, less sure*" disclosure. **The threshold
  slider and this gate are the same control** — dragging it re-pours each day.
- Cards separated generously (`--gap-card`, 18px).

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
- Alongside the coupe, a **verbal read** (see below) instead of a number.
- **Blurb leads, reasoning follows.** The card subtitle is what the event *is*
  (its description). The booze reasoning ("why the pour") only appears when you
  click a card open.
- **Distance is a filter, not a score.** It never feeds the booze number.

**Verbal read bands** (booze % → label):
| booze | read |
|-------|------|
| 90–100 | basically an open bar |
| 75–89  | probably |
| 60–74  | good odds |
| 40–59  | coin flip |
| 25–39  | bring your own |
| 0–24   | bone dry |

### 8. Voice & copy
Wry bartender, applied only where it has no functional cost.
- Feedback: **"any booze?"** → **yep / nope**
- Disclosure: **"N more, less sure"**
- Day count: **"N on tap"**
- Empty state: **"nothing worth pouring above N% today"**
- Reasoning label: **"why the pour"**
- Functional controls stay plain: filter labels, distance, **+ hold** — clarity
  first, wit second.

---

## The signature control: the coupe slider

The booze-threshold filter is the identity centerpiece: a slider whose **thumb is
a glass** that fills with gold as you raise the threshold ("chance of booze —
NN%"). It sets what's shown vs. tucked away per day. Built on the native range
input (keyboard-accessible) with the thumb styled directly — no JS-positioned
overlay (an earlier version drifted).

---

## How to port this to the other pages (home, week, calendar, map)

The other pages (`home.html`, `week.html`, `calendar.html`, `map.html`) still use
the *old* look (a different palette, an "arc + meter" card, went/skipped
feedback). Porting means moving them onto this system. In plain terms:

1. **Include the tokens** — add `{% include "_tokens.html" %}` to each page's
   `<head>` (it can replace the old `_theme.html` token block). Now every page
   speaks the same color/type/space language.
2. **Rebuild the shared card** — `_card.html` becomes the new coaster card
   (title + coupe + verbal read + blurb, click-to-expand meta, booze feedback).
   Every page that shows an event inherits it automatically.
3. **Swap the sidebar for the filter bar** — replace `_sidebar.html` with the
   sticky top filter bar (booze slider, distance, tags, source, free).
4. **Restyle each page's own layout** using tokens only — no hardcoded hexes or
   sizes; reference `var(--…)` so future changes stay in one place.

The **weekly view is done** and is the template to copy from. `calendar.html`
(a time-grid) and `map.html` (Leaflet) will need their own adaptations of the
material — same tokens, different layout.

---

## Open items (deferred, mostly backend)

- **Remove the `logistics` scorer entirely** — it's arbitrary; the score should
  be booze-only. (Backend refactor; the prototype already shows booze-only.)
- **Blurb + rationale should come from the scoring pass**, not client-side
  truncation of the raw description. One model call can return score + reasoning
  + a clean blurb together.
- **Venue names sometimes contain the street address** (scraper quality). The
  card strips at the first comma as a guard; the real fix is at ingestion.
- **Tag/source filter counts** in the popovers are static (full-dataset), not
  recomputed as other filters narrow the set — decide when porting.
- **Feedback placement** — kept on-card for now; a separate "which did you go
  to?" view was considered and declined for complexity.
