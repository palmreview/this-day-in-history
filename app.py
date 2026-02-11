# This Day in History — Newspapers (Chronicling America via loc.gov)
# Version: 0.2.1
# Date: 2026-02-11
#
# Fixes:
# - Uses certifi CA bundle to avoid SSL CERTIFICATE_VERIFY_FAILED on some environments
# - Adds "Known-good example query" + single-year test + safer decade-step scan
# - Shows useful HTTP/HTML error snippets instead of silently failing
#
# Requirements:
#   pip install streamlit certifi
#
# Run:
#   streamlit run app.py

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import certifi
import ssl
import streamlit as st

APP_VERSION = "0.2.1"
BASE_COLLECTION_URL = "https://www.loc.gov/collections/chronicling-america/"


def build_query_url(
    start_date: str,
    end_date: str,
    *,
    state: str = "ALL",
    keyword: str = "",
    front_pages_only: bool = True,
    dl: str = "page",
    count: int = 25,
) -> str:
    params = {
        "fo": "json",
        "dl": dl,
        "start_date": start_date,
        "end_date": end_date,
        "c": str(max(1, min(int(count), 100))),
    }

    if front_pages_only:
        params["front_pages_only"] = "true"

    keyword = (keyword or "").strip()
    if keyword:
        params["qs"] = "+".join(keyword.split())
        params["ops"] = "AND"
        params["searchType"] = "Advanced"

    if state and state != "ALL":
        params["location_state"] = state

    return BASE_COLLECTION_URL + "?" + urllib.parse.urlencode(params)


@st.cache_data(show_spinner=False, ttl=60 * 30)
def fetch_json_debug(url: str) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """
    Returns (payload_or_none, debug_info)
    debug_info includes: ok, status, content_type, error, snippet, url
    Uses certifi CA bundle for HTTPS verification.
    """
    debug: Dict[str, Any] = {
        "ok": False,
        "status": None,
        "content_type": None,
        "error": None,
        "snippet": None,
        "url": url,
    }

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ThisDayInHistoryStreamlit/0.2.1",
            "Accept": "application/json",
        },
    )

    ctx = ssl.create_default_context(cafile=certifi.where())

    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            status = getattr(resp, "status", None)
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read()
    except urllib.error.HTTPError as e:
        debug["status"] = e.code
        debug["error"] = f"HTTPError {e.code}: {e.reason}"
        try:
            raw = e.read()
            debug["snippet"] = raw[:600].decode("utf-8", errors="replace")
        except Exception:
            pass
        return None, debug
    except Exception as e:
        debug["error"] = f"{type(e).__name__}: {e}"
        return None, debug

    debug["status"] = status
    debug["content_type"] = content_type

    text = raw.decode("utf-8", errors="replace")

    # If blocked or an error page is returned, it may be HTML
    if "json" not in (content_type or "").lower():
        debug["error"] = f"Non-JSON response (Content-Type: {content_type})"
        debug["snippet"] = text[:600]
        return None, debug

    try:
        payload = json.loads(text)
        debug["ok"] = True
        return payload, debug
    except Exception as e:
        debug["error"] = f"JSON parse failed: {e}"
        debug["snippet"] = text[:600]
        return None, debug


def parse_results(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    results = payload.get("results", [])
    return results if isinstance(results, list) else []


def best_image_url(item: Dict[str, Any]) -> Optional[str]:
    img = item.get("image_url")
    if isinstance(img, list) and img:
        return str(img[0])
    if isinstance(img, str) and img.strip():
        return img.strip()
    return None


def item_title(item: Dict[str, Any]) -> str:
    return str(item.get("title") or "Untitled")


def item_date(item: Dict[str, Any]) -> str:
    return str(item.get("date") or item.get("created_published_date") or item.get("created_published") or "").strip()


def item_link(item: Dict[str, Any]) -> Optional[str]:
    url = item.get("url")
    return url if isinstance(url, str) and url.startswith("http") else None


def item_snippet(item: Dict[str, Any], max_chars: int = 700) -> str:
    txt = item.get("full_text")
    if isinstance(txt, str) and txt.strip():
        s = " ".join(txt.split())
        return (s[:max_chars] + "…") if len(s) > max_chars else s

    desc = item.get("description")
    if isinstance(desc, list) and desc:
        s = " ".join(str(desc[0]).split())
        return (s[:max_chars] + "…") if len(s) > max_chars else s
    if isinstance(desc, str) and desc.strip():
        s = " ".join(desc.split())
        return (s[:max_chars] + "…") if len(s) > max_chars else s
    return ""


def render_item(item: Dict[str, Any]):
    t = item_title(item)
    d = item_date(item)
    link = item_link(item)
    img = best_image_url(item)
    snippet = item_snippet(item)

    st.markdown(f"**{t}**")
    if d:
        st.write(f"Date field: {d}")

    cols = st.columns([1, 1])
    with cols[0]:
        if img:
            st.image(img, use_container_width=True)
        else:
            st.info("No image_url found for this result (the item link may still show the page).")

    with cols[1]:
        if link:
            st.link_button("Open item on loc.gov", link)
        if snippet:
            st.markdown("**OCR / snippet:**")
            st.write(snippet)


def decade_step_most_recent(
    month: int,
    day: int,
    *,
    state: str,
    keyword: str,
    front_pages_only: bool,
) -> Tuple[Optional[int], Optional[Dict[str, Any]], str, Dict[str, Any]]:
    """
    Fewer requests than year-by-year:
    - check decade anchors (1960, 1950, ...)
    - once a decade hits, walk within that decade for the most recent hit
    """
    years = list(range(1960, 1689, -10))
    last_debug: Dict[str, Any] = {}

    def query_year(y: int):
        date_str = f"{y:04d}-{month:02d}-{day:02d}"
        url = build_query_url(
            date_str,
            date_str,
            state=state,
            keyword=keyword,
            front_pages_only=front_pages_only,
            count=25,
        )
        payload, dbg = fetch_json_debug(url)
        return url, payload, dbg

    hit_decade_start: Optional[int] = None

    for y in years:
        url, payload, dbg = query_year(y)
        last_debug = dbg
        if payload:
            results = parse_results(payload)
            if results:
                hit_decade_start = y
                break
        time.sleep(0.12)

    if hit_decade_start is None:
        return None, None, "", last_debug

    for y in range(min(hit_decade_start + 3, 1963), hit_decade_start - 1, -1):
        url, payload, dbg = query_year(y)
        last_debug = dbg
        if payload:
            results = parse_results(payload)
            if results:
                return y, results[0], url, dbg
        time.sleep(0.12)

    return None, None, "", last_debug


def show_debug(dbg: Dict[str, Any]):
    if not dbg:
        return
    st.write("**Debug:**")
    st.write(f"- Status: {dbg.get('status')}")
    st.write(f"- Content-Type: {dbg.get('content_type')}")
    if dbg.get("error"):
        st.error(dbg["error"])
    if dbg.get("snippet"):
        st.code(dbg["snippet"])
    with st.expander("Request URL", expanded=False):
        st.code(dbg.get("url", ""))


def main():
    st.set_page_config(page_title="This Day in History — Newspapers", layout="wide")
    st.title("🗞️ This Day in History — Newspapers")
    st.caption(f"App v{APP_VERSION} · Data via loc.gov Chronicling America collection search.")

    with st.sidebar:
        st.header("Controls")

        chosen = st.date_input("Pick a date (month/day used)", value=dt.date.today())
        year = st.number_input(
            "Test a specific year (recommended first)",
            min_value=1690,
            max_value=1963,
            value=1924,
            step=1,
        )

        st.write("---")
        front_pages_only = st.checkbox("Front pages only", value=True)
        keyword = st.text_input("Optional keyword (OCR search)", value="", help="Example: yankees, hurricane, election")

        states = [
            "ALL",
            "alabama",
            "alaska",
            "arizona",
            "arkansas",
            "california",
            "colorado",
            "connecticut",
            "delaware",
            "florida",
            "georgia",
            "hawaii",
            "idaho",
            "illinois",
            "indiana",
            "iowa",
            "kansas",
            "kentucky",
            "louisiana",
            "maine",
            "maryland",
            "massachusetts",
            "michigan",
            "minnesota",
            "mississippi",
            "missouri",
            "montana",
            "nebraska",
            "nevada",
            "new hampshire",
            "new jersey",
            "new mexico",
            "new york",
            "north carolina",
            "north dakota",
            "ohio",
            "oklahoma",
            "oregon",
            "pennsylvania",
            "rhode island",
            "south carolina",
            "south dakota",
            "tennessee",
            "texas",
            "utah",
            "vermont",
            "virginia",
            "washington",
            "west virginia",
            "wisconsin",
            "wyoming",
        ]
        state = st.selectbox("Filter by state (optional)", states, index=0)

        st.write("---")
        st.caption("If you see HTTP 429/403 or HTML, you’re likely rate-limited. Wait a few minutes and retry.")

    month, day = chosen.month, chosen.day
    st.markdown(f"### Target date: **{chosen.strftime('%B %d')}**")

    c1, c2, c3 = st.columns(3)

    with c1:
        if st.button("✅ Known-good example query", use_container_width=True):
            # Official docs-like example: Oct–Dec 1924, keyword 'cat', California
            url = (
                "https://www.loc.gov/collections/chronicling-america/"
                "?dl=page&end_date=1924-12-31&qs=cat&start_date=1924-10-01&location_state=california&fo=json"
            )
            payload, dbg = fetch_json_debug(url)
            st.link_button("Open query used", url)

            if not payload:
                show_debug(dbg)
            else:
                results = parse_results(payload)
                st.success(f"Got {len(results)} result(s). Showing first:")
                if results:
                    render_item(results[0])

    with c2:
        if st.button("🔎 Test exact day in chosen year", use_container_width=True):
            date_str = f"{int(year):04d}-{month:02d}-{day:02d}"
            url = build_query_url(
                date_str,
                date_str,
                state=state,
                keyword=keyword,
                front_pages_only=front_pages_only,
                count=25,
            )
            payload, dbg = fetch_json_debug(url)
            st.link_button("Open query used", url)

            if not payload:
                show_debug(dbg)
            else:
                results = parse_results(payload)
                if not results:
                    st.warning(
                        "Valid JSON response but 0 results for this exact date/year. "
                        "Try turning off Front pages only or removing filters."
                    )
                else:
                    st.success(f"Got {len(results)} result(s). Showing first:")
                    render_item(results[0])

    with c3:
        if st.button("⏪ Find most recent available (safer scan)", use_container_width=True):
            with st.spinner("Scanning by decade to reduce requests…"):
                y, item, url, dbg = decade_step_most_recent(
                    month, day, state=state, keyword=keyword, front_pages_only=front_pages_only
                )

            if item and y and url:
                st.success(f"Found a hit in **{y}**.")
                st.link_button("Open query used", url)
                render_item(item)
            else:
                st.error("No hit found (or request blocked).")
                show_debug(dbg)

    st.write("---")
    st.markdown("#### Troubleshooting checklist")
    st.markdown(
        "- First click **Known-good example query**. If that fails, SSL or network is the issue.\n"
        "- If you get **HTTP 429 / 403** or an HTML snippet, wait a few minutes (rate limit) and retry.\n"
        "- If exact day/year returns 0 results, uncheck **Front pages only** and remove state/keyword filters.\n"
        "- Once single-year works, try **safer scan**."
    )


if __name__ == "__main__":
    main()
