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

Reply with ONLY a JSON object: {{"score": <float>, "rationale": "<one sentence, cite the specific evidence>"}}"""

    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL, max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        text = re.sub(r"^```(json)?|```$", "", text, flags=re.M).strip()
        data = json.loads(text)
        s = float(data["score"])
        return {"score": max(0.0, min(1.0, s)), "rationale": str(data["rationale"])}
    except Exception as e:
        print(f"  booze LLM failed for {event['id']}: {e} — using heuristic")
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
    result = _llm(event) or _heuristic(event)
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
        }
    return result
