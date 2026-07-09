"""Free-booze likelihood scorer. MODULAR — the pipeline runs without it.

Two layers:
1. Heuristic regex signals (always available, used as fallback and sanity floor).
2. LLM judgment via the Anthropic API (claude-haiku-4-5) when ANTHROPIC_API_KEY
   is set — reads the full description and returns score + one-line rationale.
"""
import json
import os
import re

MODEL = "claude-haiku-4-5-20251001"

# Shared spec for the italic serif standfirst line on cards. Kept in ONE place
# so the in-scoring call (_llm) and the standalone backfill call (generate_blurb)
# ask for exactly the same thing.
BLURB_INSTRUCTION = """Also write a "blurb": one short, concrete phrase describing WHAT THE EVENT IS. Rules:
- HARD LENGTH LIMIT: at most 90 characters (about 8-14 words). Shorter is better. A single clause, NOT an elaborate multi-part sentence. Do not pad to reach the limit.
- Present tense. Say what actually happens — prefer specific nouns over hype.
- Do NOT mention free drinks, booze, an open bar, or any probability/odds (a separate line handles that).
- Do NOT mention ticket price, and no "RSVP" / "Join us" / marketing boilerplate.
- No trailing ellipsis.
- If the description is empty or useless, infer a plain blurb from the title, venue, and neighborhood.
Length + voice target (match the LENGTH, not the content): "Y Combinator S26 founders get professional headshots at Corgi Cafe while meeting their batch"."""

# Hard backstop: the model sometimes overshoots the char limit, so clip to the
# last full word within `limit` (no ellipsis — a clean phrase, not a cut-off).
def _clip_blurb(text: str | None, limit: int = 95) -> str | None:
    if not text:
        return None
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return cut or text[:limit]

# (pattern, score-if-matched, label). First strong match anchors the heuristic.
# "Alcohol is present" and "alcohol is free" are different claims — a bar venue
# or a happy-hour promo is evidence of the former, not the latter, so those no
# longer score above the confident threshold on text alone.
SIGNALS = [
    (r"open bar|hosted bar", 0.95, "explicit open/hosted bar"),
    (r"complimentary (drinks|cocktails|wine|beer)", 0.9, "complimentary drinks"),
    (r"drinks (are |will be )?(provided|included|on us)", 0.9, "drinks provided"),
    (r"free (drinks|beer|wine|cocktails|booze)", 0.9, "free drinks"),
    (r"(wine|cocktail) reception|reception to follow", 0.7, "reception language"),
    (r"refreshments", 0.5, "refreshments (ambiguous)"),
    (r"beer (and|&) wine", 0.3, "beer & wine mentioned — bar menu language, not sponsorship"),
    (r"happy hour", 0.2, "happy hour framing — usually discounted, not free"),
    (r"cover charge", 0.15, "cover charge — paid door, drinks not implied free"),
    (r"cash bar", 0.1, "cash bar — drinks not free"),
    (r"no host bar|no-host bar", 0.1, "no-host bar"),
    (r"byob", 0.15, "BYOB"),
]


def _heuristic(event: dict) -> dict:
    text = f"{event.get('title') or ''} {event.get('description') or ''}".lower()
    for pattern, s, label in SIGNALS:
        if re.search(pattern, text):
            return {"score": s, "rationale": f"heuristic: {label}"}
    return {"score": 0.25, "rationale": "heuristic: no drink signals in text"}


def _llm(event: dict) -> dict | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic

    prompt = f"""Estimate the probability (0.0-1.0) that this San Francisco event serves FREE alcoholic drinks to attendees (open bar, hosted drinks, wine at a reception, sponsor-provided beer, etc.). A cash bar, drink tickets you pay for, or a paid tasting = NOT free.

Calibration rules (learned from ground truth on this scene):
- BASE RATE IS HIGH for free evening social events in SF: community meetups, hack nights, founder talks, viewing parties, dance socials, and startup-scene gatherings provide free drinks MORE OFTEN THAN NOT, even when the listing never mentions drinks. A free evening social with no drink info should land 0.45-0.6, not 0.2.
- PAID craft classes / workshops (garden, paint, pottery, cooking): score ≤0.2 unless free alcohol is EXPLICITLY stated. "Sip and X" with a paid ticket means the drink is bundled into the price — that is NOT free booze.
- Food-framed casual socials (sandwiches 🥪, lunch, coffee, snacks, potluck): food language is NOT drink evidence. Without separate alcohol signals, stay ≤0.35.
- Networking events are not automatic: a "founders & friends" mixer without sponsor logos, venue-with-bar, or drink language can easily be dry. Explicit evidence should move you up, not the word "networking" alone.
- BARS, CLUBS, AND NIGHTLIFE VENUES DEFAULT TO A CASH BAR. The event being held at a bar, having a DJ, or mentioning "happy hour" / drink specials is evidence that alcohol is FOR SALE there, not evidence that it's free to you. Score ≤0.25 for this pattern unless the listing explicitly promises free/hosted/complimentary drinks or an open bar — do not let "there's a bar" push the score up on its own.
- Ticketed nightclub/DJ nights (cover charge, doors at, 21+ line, RA-style listings) are almost always pay-your-own-drinks. Treat a cover charge as a negative signal for free booze, not a neutral one — paying to get in makes it less likely drinks are also comped.
- Morning, fitness, family, and outdoor-public-space events stay very low.
- Use the full 0-1 range and commit; avoid clustering at 0.25/0.75.

Also consider: explicit language ("drinks provided", "grab a drink", "reception"), event type norms (gallery openings and corporate launch parties usually pour), host type (venture-backed company or sponsor = likely hosted bar), and ticket price.

EVENT
Title: {event.get('title')}
Host: {event.get('host_name')}
Venue: {event.get('venue_name')} ({event.get('neighborhood') or 'unknown neighborhood'})
Ticket: {'free' if event.get('is_free') else f"${event.get('price_min')}-${event.get('price_max')}" if event.get('price_min') is not None else 'unknown'}
Description: {(event.get('description') or '')[:2500]}

{BLURB_INSTRUCTION}

Reply with ONLY a JSON object: {{"score": <float>, "rationale": "<one sentence, cite the specific evidence>", "blurb": "<the one-sentence event blurb>"}}"""

    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL, max_tokens=320,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        text = re.sub(r"^```(json)?|```$", "", text, flags=re.M).strip()
        data = json.loads(text)
        s = float(data["score"])
        blurb = data.get("blurb")
        return {"score": max(0.0, min(1.0, s)), "rationale": str(data["rationale"]),
                "blurb": _clip_blurb(str(blurb) if blurb else None)}
    except Exception as e:
        print(f"  booze LLM failed for {event['id']}: {e} — using heuristic")
        return None


def generate_blurb(event: dict) -> str | None:
    """Standalone Haiku call that returns JUST the card blurb (or None on any
    failure / no API key). Used by the blurb backfill so it never re-scores an
    event — the in-pipeline path gets its blurb from _llm above for free."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic

    prompt = f"""Write a one-line blurb for this San Francisco event, for the italic standfirst line on an event card.

{BLURB_INSTRUCTION}

EVENT
Title: {event.get('title')}
Host: {event.get('host_name')}
Venue: {event.get('venue_name')} ({event.get('neighborhood') or 'unknown neighborhood'})
Description: {(event.get('description') or '')[:2500]}

Reply with ONLY a JSON object: {{"blurb": "<the one-sentence event blurb>"}}"""

    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL, max_tokens=320,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        text = re.sub(r"^```(json)?|```$", "", text, flags=re.M).strip()
        blurb = json.loads(text).get("blurb")
        return _clip_blurb(str(blurb) if blurb else None)
    except Exception as e:
        print(f"  blurb generation failed for {event.get('id')}: {e}")
        return None


def _host_reputation(host_name: str | None) -> tuple[float, int, int] | None:
    """(as_promised_ratio, ok, total) from booze feedback on this host's events."""
    if not host_name:
        return None
    from db import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT f.verdict, count(*) AS n FROM feedback f
               JOIN events e ON e.id = f.event_id
               WHERE e.host_name = ? AND f.lens = 'booze' GROUP BY f.verdict""",
            (host_name,)).fetchall()
    counts = {r["verdict"]: r["n"] for r in rows}
    ok = counts.get("as_promised", 0)
    total = ok + counts.get("not_as_promised", 0)
    return (ok / total, ok, total) if total else None


def score(event: dict) -> dict:
    # Heuristic fallback has no blurb; the LLM path carries one (may be None).
    result = _llm(event) or {**_heuristic(event), "blurb": None}
    blurb = result.get("blurb")
    rep = _host_reputation(event.get("host_name"))
    if rep:
        ratio, ok, total = rep
        w = total / (total + 2)  # trust grows with observations: 1 obs -> 33%, 4 -> 67%
        blended = (1 - w) * result["score"] + w * ratio
        result = {
            "score": round(blended, 2),
            "rationale": (f"{result['rationale']} [host record: {ok}/{total} "
                          f"delivered drinks as promised → adjusted "
                          f"{result['score']:.2f}→{blended:.2f}]"),
            "blurb": blurb,  # host-reputation blend adjusts the score, not the blurb
        }
    return result
