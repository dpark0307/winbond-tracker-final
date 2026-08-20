#!/usr/bin/env python3
"""
Winbond vs Competitor Flash Price Tracker
--------------------------------------------
Pulls current DigiKey pricing for a basket of NOR/NAND flash parts across
four manufacturers (Winbond, Macronix, GigaDevice, ISSI) so you can track
the competitive price gap over time, not just Winbond's own pricing.

Each part is matched to be the closest same-density, same-interface
equivalent across manufacturers (see PARTS below). A few competitor
slots are intentionally left as None where no confirmed, in-stock
DigiKey SKU could be found yet.

SETUP (one-time):
1. Set these two environment variables on your machine:
     DIGIKEY_CLIENT_ID
     DIGIKEY_CLIENT_SECRET
2. Install the one dependency this script needs:
     pip install requests
   (or: py -m pip install requests)

USAGE:
    python track_prices.py
    (or: py track_prices.py)

Each run appends one row per part to prices.csv in the same folder.
"""

import os
import sys
import csv
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------
# CONFIG — basket of parts, grouped by density/type, tagged by manufacturer
# Edit freely: add/remove rows, or fill in a competitor part number where
# a manufacturer entry is currently None.
#
# IMPORTANT — TR (Tape & Reel) standardization:
# Winbond, Macronix, and ISSI base part numbers (no suffix) are Tube-
# packaged SKUs. GigaDevice's "R"-ending part numbers ARE already TR by
# convention. Comparing a Tube price against a TR price is not
# apples-to-apples - TR is typically priced differently (see today's
# W25Q128JVSIQ $3.97 Tube vs W25Q128JVSIQ-TR $2.88 TR @ 2000 units).
# Every part below is now the TR SKU so all four manufacturers are
# compared on the same packaging basis where possible.
# ---------------------------------------------------------------------
PARTS = [
    # density,  type,   manufacturer,   part_number
    ("128Mb",   "NOR",  "Winbond",      "W25Q128JVSIQ-TR"),
    ("128Mb",   "NOR",  "Macronix",     "MX25L12833FM2I-10G"),  # Macronix has no separate TR SKU - Tube is standard
    ("128Mb",   "NOR",  "GigaDevice",   "GD25Q128ESIGR"),  # already TR by convention
    ("128Mb",   "NOR",  "ISSI",         "IS25LP128-JBLE-TR"),

    ("64Mb",    "NOR",  "Winbond",      "W25Q64JVSSIQ-TR"),
    ("64Mb",    "NOR",  "Macronix",     "MX25L6433FM2I-08G"),  # Macronix has no separate TR SKU - Tube is standard
    ("64Mb",    "NOR",  "GigaDevice",   "GD25Q64ESIGR"),  # already TR by convention
    ("64Mb",    "NOR",  "ISSI",         "IS25LP064A-JBLE-TR"),

    ("32Mb",    "NOR",  "Winbond",      "W25Q32JVSSIQ-TR"),
    ("32Mb",    "NOR",  "Macronix",     "MX25L3233FM2I-08G"),  # Macronix has no separate TR SKU - Tube is standard
    ("32Mb",    "NOR",  "GigaDevice",   "GD25Q32ESIGR"),  # already TR by convention
    ("32Mb",    "NOR",  "ISSI",         "IS25LP032D-JNLE-TR"),

    ("1Gb",     "NAND", "Winbond",      "W25N01KVZEIR-TR"),  # real successor (K-series) - GVZEIR is same discontinued family as GVZEIG
    ("1Gb",     "NAND", "Macronix",     "MX35LF1GE4AB-Z4I"),  # already WSON, effectively reel-only
    ("1Gb",     "NAND", "GigaDevice",   "GD5F1GQ5UEYIGR"),  # confirmed via DigiCross cross-ref
    ("1Gb",     "NAND", "ISSI",         "IS37SML01G1-LLI-TR"),

    ("2Gb",     "NAND", "Winbond",      "W25N02KVZEIR-TR"),
    ("2Gb",     "NAND", "Macronix",     "MX35LF2GE4AD-Z4I"),  # already WSON, effectively reel-only
    ("2Gb",     "NAND", "GigaDevice",   "GD5F2GQ5UEYIGR"),  # confirmed via DigiCross cross-ref
    ("2Gb",     "NAND", "ISSI",         None),
]

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prices.csv")

DIGIKEY_TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
DIGIKEY_KEYWORD_SEARCH_URL = "https://api.digikey.com/products/v4/search/keyword"

# ---------------------------------------------------------------------
# AUTH — client_credentials (2-legged OAuth), no browser login needed
# ---------------------------------------------------------------------
def get_digikey_access_token(client_id, client_secret):
    resp = requests.post(
        DIGIKEY_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _normalize_part(s):
    """Lowercase and strip spaces/hyphens so trivial formatting
    differences (e.g. a stray space or hyphen) don't cause a real
    matching listing to be excluded."""
    return (s or "").strip().upper().replace("-", "").replace(" ", "")


def _qty1_price(price_breaks):
    """
    Given a list of price breaks, return (price, is_true_qty1).

    is_true_qty1=True means we found an actual BreakQuantity == 1 entry.
    is_true_qty1=False means no qty-1 break existed on this variation
    and we fell back to whatever the lowest-quantity break was instead
    (e.g. a Tape & Reel table that only starts at 2,000 units).

    This distinction matters: a fallback price from a bulk-only table
    (like TR starting at 2,000 units) is NOT comparable to a real qty-1
    price from a different variation (like Cut Tape/Digi-Reel starting
    at 1 unit) on the same part. Confirmed twice with real DigiKey data
    (GD25Q128ESIGR and GD25Q64ESIGR both had this exact split: a
    Cut Tape/Digi-Reel table with true qty-1 pricing, and a separate
    Tape & Reel table with no qty-1 entry, only 2,000+ units).
    """
    if not price_breaks:
        return None, False
    qty1 = [pb for pb in price_breaks if pb.get("BreakQuantity") == 1]
    if qty1:
        return qty1[0].get("UnitPrice"), True
    sorted_breaks = sorted(price_breaks, key=lambda pb: pb.get("BreakQuantity", 0))
    price = sorted_breaks[0].get("UnitPrice") if sorted_breaks else None
    return price, False


def fetch_digikey_price(part_number, access_token, client_id):
    """
    Three bugs fixed here after finding real, confirmed pricing errors:

    1. Exact string matching on ManufacturerProductNumber was too
       strict - any trivial formatting difference from DigiKey's API
       (stray space, hyphen) silently excluded the correct standard
       listing, leaving only pricier oddball listings as candidates.
       Fixed by normalizing both sides (strip spaces/hyphens, uppercase)
       before comparing.

    2. price_breaks[0] assumed the first price break was the qty-1
       price. Not guaranteed. Fixed by explicitly finding the break
       where BreakQuantity == 1.

    3. Confirmed twice with real DigiKey screenshots (GD25Q128ESIGR,
       GD25Q64ESIGR): when a part has BOTH a Cut Tape/Digi-Reel table
       (real qty-1 pricing) and a separate Tape & Reel table (bulk-only,
       e.g. starts at 2,000 units, no qty-1 entry), taking min() across
       every variation's "qty-1 or fallback" price let the TR table's
       2,000-unit price win simply because it's numerically lower than
       the real qty-1 price - even though it's not qty-1 pricing at all.
       Fixed: if ANY variation has a true qty-1 break, only consider
       those (ignore fallback-only variations entirely). Only fall back
       to comparing fallback prices if NO variation has true qty-1
       pricing.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "X-DIGIKEY-Client-Id": client_id,
        "X-DIGIKEY-Locale-Site": "US",
        "X-DIGIKEY-Locale-Language": "en",
        "X-DIGIKEY-Locale-Currency": "USD",
        "Content-Type": "application/json",
    }
    body = {
        "Keywords": part_number,
        "Limit": 10,
        "Offset": 0,
    }
    resp = requests.post(DIGIKEY_KEYWORD_SEARCH_URL, headers=headers, json=body, timeout=15)

    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}: {resp.text[:200]}"

    data = resp.json()
    products = data.get("Products") or data.get("products") or []
    if not products:
        return None, "No products found for this part number"

    target = _normalize_part(part_number)
    true_qty1_prices = []
    fallback_prices = []

    for product in products:
        mpn = _normalize_part(product.get("ManufacturerProductNumber"))
        if mpn != target:
            continue  # skip genuinely unrelated parts the keyword search also returned

        variations = product.get("ProductVariations") or []
        sources = variations if variations else [product]

        for source in sources:
            price, is_true_qty1 = _qty1_price(source.get("StandardPricing"))
            if price is None:
                continue
            if is_true_qty1:
                true_qty1_prices.append(price)
            else:
                fallback_prices.append(price)

    # Prefer real qty-1 pricing whenever it exists anywhere, even if a
    # fallback (bulk-only) price would have been numerically lower.
    candidate_prices = true_qty1_prices if true_qty1_prices else fallback_prices

    if not candidate_prices:
        return None, "No pricing found for an exact part number match (likely out of stock)"

    return min(candidate_prices), None


# ---------------------------------------------------------------------
# STORAGE — append to a plain CSV, one row per part per run
# ---------------------------------------------------------------------
FIELDNAMES = ["date", "density", "type", "manufacturer", "part_number", "price_usd", "pct_change_vs_last_pull", "explanation", "explanation_source"]


def load_last_prices():
    """
    Reads prices.csv to find each part's most recent saved price.
    Skips any malformed/incomplete rows instead of crashing the whole
    script.
    """
    last = {}
    if not os.path.exists(CSV_PATH):
        return last
    with open(CSV_PATH, newline="") as f:
        for row in csv.DictReader(f):
            part = row.get("part_number")
            price = row.get("price_usd")
            if not part or price is None or price == "":
                continue
            try:
                last[part] = float(price)
            except ValueError:
                continue
    return last


def append_rows(rows):
    file_exists = os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------
# EXPLANATIONS — two-tier system for flagged price moves (>=3%)
#
# Tier 1: KNOWN_EVENTS below. Hand-verified facts we've already
# researched (today's debugging work, confirmed lifecycle events,
# confirmed real market moves). These are free, instant, and 100%
# accurate since a human wrote them - never guessed by the API.
#
# Tier 2: Claude API fallback, only used when a flagged move has NO
# matching entry in KNOWN_EVENTS. Explains plausible real causes when
# the pattern looks genuine, but is explicitly allowed to say "likely
# data error" or "cause unconfirmed" rather than being forced to
# invent a story. This is a hard rule, not a style preference - see
# the system prompt below.
# ---------------------------------------------------------------------
KNOWN_EVENTS = [
    {
        "part_number": "W25N01GVZEIG",
        "date": "2026-07-30",
        "kind": "discontinued",
        "text": "Part reached Last Time Buy status per Winbond's official PCN "
                "(notification 2026-01-03, last order 2026-07-30). No further "
                "data expected - this is not a script failure.",
    },
    {
        "part_number": "W25Q32JVSSIQ",
        "date": "2026-07-24",
        "kind": "discontinued",
        "text": "Part confirmed End of Life per Mouser around this date. Any "
                "gap in tracking after this point reflects the part becoming "
                "unlisted through DigiKey/Mouser, not a script failure.",
    },
    {
        "part_number": "W25Q64JVSSIQ",
        "date": "2026-07-30",
        "kind": "real",
        "text": "Confirmed real increase (+32%). Matches the documented 2026 "
                "NOR Flash shortage: TrendForce reports NOR/SLC NAND contract "
                "prices up over 100% in 1H26 as fab capacity shifts toward "
                "AI/HBM production. Winbond has publicly confirmed a "
                "'continuous NOR price uptrend through 1H26 and beyond.' "
                "DigiKey also lists this part under temporary constrained "
                "supply, consistent with a genuine shortage-driven move.",
    },
    {
        "part_number": "W25Q128JVSIQ",
        "date": "2026-07-29",
        "kind": "real",
        "text": "Modest, plausible increase (+6%), one day ahead of a larger "
                "confirmed move on the 64Mb part. Consistent with the same "
                "documented 2026 NOR shortage moving through Winbond's "
                "lineup that week.",
    },
    {
        "part_number": "GD25Q64ESIGR",
        "date": "2026-07-22",
        "kind": "error",
        "text": "Likely data error, not a real price move. This value matched "
                "a 2,000-unit bulk Tape & Reel price tier exactly, not the "
                "true quantity-1 price - a known bug (fixed) where the "
                "fetch logic could pick a bulk-only price table over a "
                "table with real qty-1 pricing.",
    },
    {
        "part_number": "GD25Q128ESIGR",
        "date": "2026-07-10",
        "kind": "error",
        "text": "Likely data error, not a real price move. Same root cause as "
                "the GD25Q64ESIGR case on 2026-07-22 - matched a bulk "
                "Tape & Reel tier instead of the real quantity-1 price.",
    },
]


def _find_known_event(part_number, date_str):
    """Exact match on part_number + date. Returns the event dict, or
    None if this specific flagged move hasn't been pre-researched."""
    for event in KNOWN_EVENTS:
        if event["part_number"] == part_number and event["date"] == date_str:
            return event
    return None


CLAUDE_EXPLANATION_SYSTEM_PROMPT = """You explain flagged price movements \
(>=3% day-over-day change) in a distributor price tracker for flash \
memory chips.

You will be told the part, manufacturer, old price, new price, and \
percent change. You may be given web search results.

Give a short (2-4 sentence) explanation, then end with exactly one line: \
"Confidence: high/medium/low".

CRITICAL RULE: you are NEVER required to conclude a price move is real. \
If the data doesn't support a confident real-world explanation - no \
corroborating news, no stock-status change, an implausible magnitude, or \
a pattern matching known error signatures (e.g. the new price exactly \
matches a round bulk-quantity price point rather than a plausible \
single-unit price) - say so plainly: state it may be a data error or \
that the cause is unconfirmed, and say Confidence: low. Do not invent a \
plausible-sounding market story to fill the gap. An honest "unconfirmed" \
is always better than a fabricated explanation."""


def get_claude_explanation(part_number, manufacturer, old_price, new_price,
                            pct_change, api_key):
    """
    Tier 2 fallback: calls Claude (with web search) for any flagged move
    that has no entry in KNOWN_EVENTS. Returns explanation text, or a
    short fallback string if the API call fails - a failure here should
    never break the daily price pull.
    """
    if not api_key:
        return "AI explanation unavailable - ANTHROPIC_API_KEY not set."

    user_message = (
        f"Part: {part_number} ({manufacturer})\n"
        f"Old price: ${old_price:.4f}\n"
        f"New price: ${new_price:.4f}\n"
        f"Change: {pct_change:+.2f}%\n\n"
        f"Explain this price movement."
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 400,
                "system": CLAUDE_EXPLANATION_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_message}],
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        text_blocks = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        result = "\n".join(text_blocks).strip()
        return result if result else "AI explanation returned no content."
    except requests.RequestException as e:
        return f"AI explanation unavailable - API error: {e}"


def get_explanation(part_number, manufacturer, old_price, new_price,
                     pct_change, date_str, anthropic_api_key):
    """Two-tier lookup: check KNOWN_EVENTS first (free, verified),
    only call the API for moves that aren't pre-researched."""
    known = _find_known_event(part_number, date_str)
    if known:
        return known["text"], "known"
    text = get_claude_explanation(
        part_number, manufacturer, old_price, new_price, pct_change, anthropic_api_key
    )
    return text, "ai"



def main():
    digikey_id = os.environ.get("DIGIKEY_CLIENT_ID")
    digikey_secret = os.environ.get("DIGIKEY_CLIENT_SECRET")
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")

    if not digikey_id or not digikey_secret:
        print("ERROR: DIGIKEY_CLIENT_ID and/or DIGIKEY_CLIENT_SECRET are not set.")
        sys.exit(1)

    if not anthropic_api_key:
        print("NOTE: ANTHROPIC_API_KEY not set - flagged moves without a "
              "KNOWN_EVENTS entry will be saved without an AI explanation.")

    print("Getting DigiKey access token...")
    try:
        digikey_token = get_digikey_access_token(digikey_id, digikey_secret)
    except requests.RequestException as e:
        print(f"ERROR: Failed to get DigiKey access token: {e}")
        sys.exit(1)

    last_prices = load_last_prices()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows_to_save = []

    active_parts = [p for p in PARTS if p[3] is not None]
    print(f"\nPulling prices for {len(active_parts)} part(s) on {today}:\n")

    current_group = None
    for density, ptype, manufacturer, part in PARTS:
        if part is None:
            print(f"  [{density} {ptype}] {manufacturer}: SKIPPED (no confirmed part number set)")
            continue

        group = (density, ptype)
        if group != current_group:
            print(f"\n  -- {density} {ptype} --")
            current_group = group

        price, error = fetch_digikey_price(part, digikey_token, digikey_id)

        if error:
            print(f"    {manufacturer:<12} {part:<22} FAILED - {error}")
            continue

        prev = last_prices.get(part)
        pct_change_value = ""
        explanation_text = ""
        explanation_source = ""
        if prev is not None and prev != 0:
            pct_change = ((price - prev) / prev) * 100
            pct_change_value = round(pct_change, 2)
            is_flagged = abs(pct_change) >= 3
            flag = "  <-- FLAGGED (>3% move)" if is_flagged else ""
            print(f"    {manufacturer:<12} {part:<22} ${price:.4f}  ({pct_change:+.2f}% vs last pull){flag}")

            if is_flagged:
                explanation_text, explanation_source = get_explanation(
                    part, manufacturer, prev, price, pct_change, today, anthropic_api_key
                )
                tag = "known event" if explanation_source == "known" else "AI-generated"
                print(f"        explanation ({tag}): {explanation_text[:120]}"
                      f"{'...' if len(explanation_text) > 120 else ''}")
        else:
            print(f"    {manufacturer:<12} {part:<22} ${price:.4f}  (first entry)")

        rows_to_save.append({
            "date": today,
            "density": density,
            "type": ptype,
            "manufacturer": manufacturer,
            "part_number": part,
            "price_usd": price,
            "pct_change_vs_last_pull": pct_change_value,
            "explanation": explanation_text,
            "explanation_source": explanation_source,
        })

    if rows_to_save:
        append_rows(rows_to_save)
        print(f"\nSaved {len(rows_to_save)} row(s) to {CSV_PATH}")
    else:
        print("\nNo prices were successfully pulled - nothing saved.")


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------
# README - one-time environment variable setup
# ---------------------------------------------------------------------
# Windows (PowerShell), run once, then RESTART your terminal:
#   setx DIGIKEY_CLIENT_ID "your-client-id-here"
#   setx DIGIKEY_CLIENT_SECRET "your-client-secret-here"
#   setx ANTHROPIC_API_KEY "your-anthropic-api-key-here"
#
# ANTHROPIC_API_KEY is optional. Without it, flagged price moves that
# aren't already in KNOWN_EVENTS get saved with a blank explanation
# instead of an AI-generated one - the script still runs fine.
#
# Then each day, just run:
#   py track_prices.py
# ---------------------------------------------------------------------
