"""Seed the DB with fake events so the UI can develop independently of ingest.

Usage: uv run python -m app.seed_dummy
Idempotent: dummy events upsert on their stable IDs. Real scraped data is untouched.
"""
import json
from datetime import datetime, timedelta

from db import get_conn, init_db

NOW = datetime.now().replace(minute=0, second=0, microsecond=0)


def day(offset: int, hour: int) -> str:
    return (NOW.replace(hour=hour) + timedelta(days=offset)).isoformat(timespec="minutes")


# (title, host, venue, neighborhood, day_offset, start_hr, end_hr, rsvp, tags,
#  booze_score, booze_why, logistics_score, logistics_why)
DUMMY = [
    ("AI Founders Happy Hour", "Cerebral Valley", "The Pearl", "Dogpatch", 0, 18, 21, "approval",
     ["party", "networking"],
     0.92, "Explicit 'open bar' in description; two drink sponsors listed.",
     0.80, "Weeknight 6pm, transit-friendly."),
    ("Gallery Opening: New Coastal Works", "Slash Gallery", "Slash SF", "Mission", 0, 19, 22, "open",
     ['art'],
     0.75, "Gallery openings reliably pour wine; 'reception' language present.",
     0.85, "Tonight in the Mission."),
    ("Fintech Product Launch Party", "Mercury", "SVN West", "SoMa", 1, 17, 20, "application",
     ['party', 'networking'],
     0.88, "'Cocktails and canapés provided' in description; corporate sponsor.",
     0.70, "Tomorrow 5pm; application gate adds friction."),
    ("SF Beer Week Kickoff", "SF Brewers Guild", "Fort Mason", "Marina", 2, 18, 23, "open",
     ['festival'],
     0.30, "Beer-centric but ticketed tastings — drinks are paid, not free.",
     0.55, "Marina is a trek; $45 ticket."),
    ("Climate Tech Networking Night", "SF Climate Week", "Shack15", "Embarcadero", 2, 18, 21, "approval",
     ['networking', 'climate'],
     0.70, "Sponsored networking at Shack15 — host's past events had a bar.",
     0.75, "Weeknight, Ferry Building location."),
    ("Free Yoga in Dolores Park", "SF Rec & Parks", "Dolores Park", "Mission", 3, 10, 11, "open",
     [],
     0.02, "Morning fitness event — no alcohol plausible.",
     0.60, "Weekend morning."),
    ("Design Systems Meetup", "SF Design Guild", "Figma HQ", "SoMa", 3, 18, 20, "open",
     ['networking', 'talk'],
     0.55, "Corporate-hosted meetup — beer/wine likely but unstated.",
     0.75, "Weeknight downtown."),
    ("Warehouse Vinyl Night", "Secret Disco", "Undisclosed, SoMa", "SoMa", 4, 21, 2, "waitlist",
     ['music', 'party'],
     0.15, "Cash bar almost certain at a ticketed warehouse party.",
     0.35, "Starts 9pm Friday; waitlisted."),
    ("Startup Demo Day + Reception", "Alchemist Accelerator", "The Village", "Mid-Market", 5, 16, 19, "open",
     ['networking'],
     0.82, "'Reception to follow' — demo day receptions reliably have open wine/beer.",
     0.65, "Saturday afternoon."),
    ("Neighborhood Cleanup + Coffee", "Refuse Refuse", "Alamo Square", "NoPa", 6, 9, 11, "open",
     [],
     0.01, "Morning volunteer event with coffee.",
     0.50, "Sunday morning."),
]


def seed() -> None:
    init_db()
    now_iso = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        for i, (title, host, venue, hood, d, h1, h2, rsvp, tags,
                bs, bw, ls, lw) in enumerate(DUMMY):
            eid = f"dummy:{i}"
            ends = day(d + (1 if h2 < h1 else 0), h2 % 24)
            conn.execute(
                """INSERT INTO events (id, source, source_id, url, title, description,
                     host_name, venue_name, neighborhood, starts_at, ends_at,
                     is_free, rsvp_type, raw, scraped_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET starts_at=excluded.starts_at,
                     ends_at=excluded.ends_at, scraped_at=excluded.scraped_at""",
                (eid, "dummy", str(i), f"https://example.com/event/{i}", title,
                 f"Dummy description for {title}.", host, venue, hood,
                 day(d, h1), ends, 1, rsvp, json.dumps({}), now_iso),
            )
            conn.execute("DELETE FROM event_tags WHERE event_id = ?", (eid,))
            conn.executemany(
                "INSERT INTO event_tags (event_id, tag) VALUES (?, ?)",
                [(eid, t) for t in tags],
            )
            for scorer, score, why in [("booze", bs, bw), ("logistics", ls, lw)]:
                conn.execute(
                    """INSERT INTO scores (event_id, scorer, score, rationale, scored_at)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(event_id, scorer) DO UPDATE SET
                         score=excluded.score, rationale=excluded.rationale""",
                    (eid, scorer, score, why, now_iso),
                )
    print(f"Seeded {len(DUMMY)} dummy events into sloshbot.db")


if __name__ == "__main__":
    seed()
