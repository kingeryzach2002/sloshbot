"""Resident Advisor (ra.co) source — unauthenticated GraphQL API reverse-engineered
in the feasibility spike (scratchpad/spike_ra.py).

Discovery: POST https://ra.co/graphql, operation GET_EVENT_LISTINGS, matching
what ra.co's own frontend sends for an area's event-listings page. The HTML
site itself is behind DataDome bot protection, but the GraphQL endpoint answers
plain `requests` calls fine as long as browser-like headers (Origin/Referer/
User-Agent) are sent.

DEFENSIVE NOTE: this is an unauthenticated third-party endpoint that could
start blocking (DataDome challenge, Cloudflare page, rate limiting) at any
time. `_post_graphql` refuses to treat a non-200, non-JSON, or GraphQL-error
response as "zero results" — it raises instead, so ingest.run reports this
source as FAILED rather than silently recording an empty-but-successful scrape.
"""
import json
import re
from datetime import date, datetime, timezone, timedelta

import requests

GRAPHQL_URL = "https://ra.co/graphql"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://ra.co",
    "Referer": "https://ra.co/events/us/sanfrancisco",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

AREA_QUERY = """
query($areaUrlName: String, $countryUrlCode: String) {
  area(areaUrlName: $areaUrlName, countryUrlCode: $countryUrlCode) {
    id
    name
    urlName
    country { name urlCode }
  }
}
"""

# Reverse-engineered query matching what ra.co's own frontend sends for the
# area event-listings page (operation name GET_EVENT_LISTINGS).
EVENT_LISTINGS_QUERY = """
query GET_EVENT_LISTINGS($filters: FilterInputDtoInput, $filterOptions: FilterOptionsInputDtoInput, $pageSize: Int, $page: Int, $sort: SortInputDtoInput) {
  eventListings(filters: $filters, filterOptions: $filterOptions, pageSize: $pageSize, page: $page, sort: $sort) {
    data {
      id
      listingDate
      event {
        id
        date
        startTime
        endTime
        title
        content
        interestedCount
        isTicketed
        flyerFront
        cost
        contentUrl
        venue {
          id
          name
          address
          contentUrl
          area {
            id
            name
            country { id name urlCode }
          }
        }
        artists {
          id
          name
        }
        promoters {
          id
          name
        }
      }
    }
    totalResults
  }
}
"""

SF_AREA_URL_NAME = "sanfrancisco"
SF_COUNTRY_URL_CODE = "us"
DATE_WINDOW_DAYS = 14
LISTINGS_PAGE_SIZE = 50


def _post_graphql(query: str, variables: dict, label: str) -> dict:
    """POST a GraphQL query and return its `data`. Raises on anything that
    smells like blocking/breakage instead of returning an empty result, so
    callers (and ingest.run) see this source fail loudly rather than
    silently report zero events.
    """
    try:
        resp = requests.post(
            GRAPHQL_URL,
            headers=HEADERS,
            json={"query": query, "variables": variables},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"ra.co GraphQL request failed ({label}): {exc}") from exc

    if resp.status_code != 200:
        raise RuntimeError(
            f"ra.co GraphQL returned HTTP {resp.status_code} for {label} "
            f"(possible blocking/rate-limit): {resp.text[:300]!r}"
        )

    ctype = resp.headers.get("content-type", "")
    if "json" not in ctype.lower():
        raise RuntimeError(
            f"ra.co GraphQL returned non-JSON content-type {ctype!r} for {label} "
            f"(likely a Cloudflare/DataDome challenge page): {resp.text[:300]!r}"
        )

    try:
        body = resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"ra.co GraphQL returned unparseable JSON for {label}: {resp.text[:300]!r}"
        ) from exc

    if body.get("errors"):
        raise RuntimeError(f"ra.co GraphQL errors for {label}: {body['errors']}")

    data = body.get("data")
    if data is None:
        raise RuntimeError(f"ra.co GraphQL response missing 'data' for {label}: {body!r}")

    return data


def _resolve_sf_area_id() -> int:
    """Resolve the SF area id live via the `area` query — do not hardcode a
    guessed id, since RA's internal area ids are not documented and could
    change.
    """
    data = _post_graphql(
        AREA_QUERY,
        {"areaUrlName": SF_AREA_URL_NAME, "countryUrlCode": SF_COUNTRY_URL_CODE},
        "area lookup",
    )
    area = data.get("area")
    if not area or not area.get("id"):
        raise RuntimeError(f"ra.co area lookup for San Francisco returned nothing: {data!r}")
    return int(area["id"])


def _fetch_listings(area_id: int, date_from: str, date_to: str, limit: int) -> list[dict]:
    listings: list[dict] = []
    page = 1
    while len(listings) < limit:
        page_size = min(LISTINGS_PAGE_SIZE, limit - len(listings))
        variables = {
            "filters": {
                "areas": {"eq": area_id},
                "listingDate": {"gte": date_from, "lte": date_to},
            },
            "filterOptions": {},
            "pageSize": page_size,
            "page": page,
            "sort": {"listingDate": {"order": "ASCENDING"}},
        }
        data = _post_graphql(
            EVENT_LISTINGS_QUERY, variables, f"event listings page={page}"
        )
        listing_block = data.get("eventListings") or {}
        batch = listing_block.get("data") or []
        if not batch:
            break
        listings.extend(batch)
        total = listing_block.get("totalResults")
        if total is not None and len(listings) >= total:
            break
        page += 1
    return listings[:limit]


def _parse_cost(cost: str | None) -> tuple[int | None, float | None, float | None]:
    """RA's `cost` field is a compact free-text string ("0", "$5", "0-10",
    "$10+"). Best-effort parse into (is_free, price_min, price_max).
    """
    if cost is None:
        return None, None, None
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", cost)]
    if cost.strip() == "0" or (nums and max(nums) == 0):
        return 1, 0.0, 0.0
    if nums:
        return 0, min(nums), max(nums)
    return None, None, None  # unparseable free-text cost


def _lineup_line(artists: list[dict]) -> str | None:
    names = [a.get("name") for a in artists if a.get("name")]
    if not names:
        return None
    return "Lineup: " + ", ".join(names)


def _map(listing: dict) -> dict:
    ev = listing.get("event") or {}
    venue = ev.get("venue") or {}
    artists = ev.get("artists") or []
    promoters = ev.get("promoters") or []
    host_name = promoters[0]["name"] if promoters else None
    host_url = (
        f"https://ra.co{promoters[0]['contentUrl']}"
        if promoters and promoters[0].get("contentUrl")
        else None
    )

    content = (ev.get("content") or "").strip()
    lineup = _lineup_line(artists)
    description = "\n\n".join(part for part in (content, lineup) if part) or None

    is_free, price_min, price_max = _parse_cost(ev.get("cost"))

    return {
        "id": f"ra:{ev.get('id')}",
        "source": "ra",
        "source_id": str(ev.get("id")) if ev.get("id") is not None else None,
        "url": f"https://ra.co{ev.get('contentUrl')}" if ev.get("contentUrl") else None,
        "title": ev.get("title"),
        "description": description,
        "host_name": host_name,
        "host_url": host_url,
        "venue_name": venue.get("name"),
        "address": venue.get("address"),
        "neighborhood": None,  # RA doesn't provide neighborhood-level granularity
        # RA's startTime/endTime are already full local-time ISO timestamps
        # (e.g. "2026-07-09T22:00:00.000"); use directly per ARCHITECTURE.md.
        "starts_at": ev.get("startTime"),
        "ends_at": ev.get("endTime"),
        "is_free": is_free,
        "price_min": price_min,
        "price_max": price_max,
        "rsvp_type": "open" if ev.get("isTicketed") else None,
        "image_url": ev.get("flyerFront"),
        "lat": None,  # geocoder backfills from address
        "lon": None,
        "raw": json.dumps(ev),
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch(limit: int = 50) -> list[dict]:
    area_id = _resolve_sf_area_id()
    today = date.today()
    date_to = today + timedelta(days=DATE_WINDOW_DAYS)
    listings = _fetch_listings(area_id, today.isoformat(), date_to.isoformat(), limit)
    return [_map(item) for item in listings if item.get("event") and item["event"].get("id")]
