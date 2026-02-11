# This Day in History — Newspapers (Chronicling America via loc.gov)
# Version: 0.2.4
# Date: 2026-02-11
#
# Updates in 0.2.4:
# - Adds "Use today" button that forces the date picker to today's date
# - Shows Today's date vs Selected month/day clearly in the UI
# - Shows the exact query window (start/end) used for each request
# - Keeps: certifi SSL fix, known-good query, single-year test, safer scan, debug output
# - Keeps: +/- 3 day query window + exact month/day filtering using URL date extraction

from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import certifi
import ssl
import streamlit as st

APP_VERSION = "0.2.4"
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
            "User-Agent": "ThisDayInHistoryStreamlit/0.2.4",
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
            debug["snippet"] = raw[:800].decode("utf-8", errors="replace")
        except Exception:
            pass
        return None, debug
    except Exception as e:
        debug["error"] = f"{type(e).__name__}: {e}"
        return None, debug

    debug["status"] = status
    debug["content_type"] = content_type

    text = raw.decode("utf-8", errors="replace")

    if "json" not in (content_type or "").lower():
        debug["error"] = f"Non-JSON response (Content-Type: {content_type})"
        debug["snippet"] = text[:800]
        return None, debug

    try:
        payload = json.loads(text)
        debug["ok"] = True
        return payload, debug
    except Exception as e:
        debug["error"] = f"JSON parse failed: {e}"
        debug["snippet"] = text[:800]
        return None, debug


def parse_results(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    results = payload.get("results", [])
    return results if isinstance(results, list) else []


def parse_item_date(item: Dict[str, Any]) -> Optional[dt.date]:
    """
    Priority:
      1) ISO date in item['date'] / created_published_date / created_published (or lists)
      2) ISO date embedded in 'aka' URLs (common)
      3) ISO date embedded in 'url'
    """

    def parse_iso10(s: str) -> Optional[dt.date]:
        s = (s or "").strip()
        if len(s) >= 10:
            try:
                return dt.date.fromisoformat(s[:10])
            except Exception:
                return None
        return None

    for key in ("date", "created_published_date", "created_published"):
        v = item.get(key)
        if isinstance(v, str):
            d = parse_iso10(v)
            if d:
                return d
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, str):
                    d = parse_iso10(x)
                    if d:
                        return d

    aka = item.get("aka")
    if isinstance(aka, list):
        for u in aka:
            if isinstance(u, str):
                m = re.search(r"/(\d{4}-\d{2}-\d{2})/", u)
                if m:
                    try:
                        return dt.date.fromisoformat(m.group(1))
                    except Exception:
                        pass
    elif isinstance(aka, str):
        m = re.search(r"/(\d{4}-\d{2}-\d{2})/", aka)
        if m:
            try:
                return dt.date.fromisoformat(m.group(1))
            except Exception:
                pass

    url = item.get("url")
    if isinstance(url, str):
        m = re.search(r"/(\d{4}-\d{2}-\d{2})/", url)
        if m:
            try:
                return dt.date.fromisoformat(m.group(1))
            except Exception:
                pass

    return None


def filter_exact_month_day(results: List[Dict[str, Any]], month: int, day: int) -> List[Dict[str, Any]]:
    exact: List[Dict[str, Any]] = []
    for item in results:
        d = parse_item_date(item)
        if d and d.month == month and d.day == day:
            exact.append(item)
    return exact


def best_image_url(item: Dict[str, Any]) -> Optional[str]:
    img = item.get("image_url")
    if isinstance(img, list) and img:
        return str(img[0])
    if isinstance(img, str) and img.strip():
        return img.strip()
    return None


def item_title(item: Dict[str, Any]) -> str:
    return str(item.get("title") or "Untitled")


def item_date_str(item: Dict[str, Any]) -> str:
    d = parse_item_date(item)
    return d.isoformat() if d else str(item.get("date") or item.get("created_published_date") or item.get("created_published") or "").strip()


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
    d = item_date_str(item)
    link = item_link(item)
    img = best_image_url(item)
    snippet = item_snippet(item)

    st.markdown(f"**{t}**")
    if d:
        st.write(f"Parsed date: **{d}**")

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


def clamp_day(year: int, month: int, day: int) -> int:
    if month == 12:
        next_month = dt.date(year + 1, 1, 1)
    else:
        next_month = dt.date(year, month + 1, 1)
    last_day = (next_month - dt.timedelta(days=1)).day
    return max(1, min(day, last_day))


def make_window(year: int, month: int, day: int, window_days: int = 3) -> Tuple[str, str]:
    safe_day = clamp_day(year, month, day)
    center = dt.date(year, month, safe_day)
    start = center - dt.timedelta(days=window_days)
    end = center + dt.timedelta(days=window_days)
    return start.isoformat(), end.isoformat()


def decade_step_most_recent(
    month: int,
    day: int,
    *,
    state: str,
    keyword: str,
    front_pages_only: bool,
) -> Tuple[Optional[int], Optional[Dict[str, Any]], str, Dict[str, Any], int, Tuple[str, str]]:
    years = list(range(1960, 1689, -10))
    last_debug: Dict[str, Any] = {}

    def query_year(y: int):
        start_date, end_date = make_window(y, month, day, window_days=3)
        url = build_query_url(
            start_date,
            end_date,
            state=state,
            keyword=keyword,
            front_pages_only=front_pages_only,
            count=100,
        )
        payload, dbg = fetch_json_debug(url)
        return (start_date, end_date), url, payload, dbg

    hit_decade_start: Optional[int] = None

    for y in years:
        win, url, payload, dbg = query_year(y)
        last_debug = dbg
        if payload:
            results = parse_results(payload)
            exact = filter_exact_month_day(results, month, day)
            if exact:
                hit_decade_start = y
                break
        time.sleep(0.15)

    if hit_decade_start is None:
        return None, None, "", last_debug, 0, ("", "")

    for y in range(min(hit_decade_start + 9, 1963), hit_decade_start - 1, -1):
        win, url, payload, dbg = query_year(y)
        last_debug = dbg
        if payload:
            results = parse_results(payload)
            exact = filter_exact_month_day(results, month, day)
            if exact:
                return y, exact[0], url, dbg, len(exact), win
        time.sleep(0.15)

    return None, None, "", last_debug, 0, ("", "")


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


def show_no_match_diagnostics(results: List[Dict[str, Any]]):
    with st.expander("Why no exact matches? (sample parsed dates)", expanded=False):
        for it in results[:10]:
            aka = it.get("aka")
            aka_sample = aka[0] if isinstance(aka, list) and aka else (aka if isinstance(aka, str) else None)
            parsed = parse_item_date(it)
            st.write(
                {
                    "parsed_date": parsed.isoformat() if parsed else None,
                    "date_field": it.get("date"),
                    "aka_sample": aka_sample,
                    "url": it.get("url"),
                }
            )


def main():
    st.set_page_config(page_title="This Day in History — Newspapers", layout="wide")
    st.title("🗞️ This Day in History — Newspapers")
    st.caption(f"App v{APP_VERSION} · loc.gov Chronicling America collection search.")

    today = dt.date.today()

    # Session-backed date so we can "force reset" to today reliably
    if "chosen_date" not in st.session_state:
        st.session_state["chosen_date"] = today

    with st.sidebar:
        st.header("Controls")

        col_a, col_b = st.columns([1, 1])
        with col_a:
            if st.button("Use today", use_container_width=True):
                st.session_state["chosen_date"] = today
        with col_b:
            st.caption(f"Today: {today.isoformat()}")

        chosen = st.date_input("Pick a date (month/day used)", value=st.session_state["chosen_date"])
        # keep session state aligned with manual changes
        st.session_state["chosen_date"] = chosen

        year = st.number_input(
            "Test a specific year (recommended first)",
            min_value=1690,
            max_value=1963,
            value=1955,
            step=1,
        )

        st.write("---")
        front_pages_only = st.checkbox("Front pages only", value=True)
        keyword = st.text_input("Optional keyword (OCR search)", value="", help="Example: yankees, hurricane, election")

        states = [
            "ALL",
            "alabama","alaska","arizona","arkansas","california","colorado","connecticut","delaware",
            "florida","georgia","hawaii","idaho","illinois","indiana","iowa","kansas","kentucky",
            "louisiana","maine","maryland","massachusetts","michigan","minnesota","mississippi",
            "missouri","montana","nebraska","nevada","new hampshire","new jersey","new mexico",
            "new york","north carolina","north dakota","ohio","oklahoma","oregon","pennsylvania",
            "rhode island","south carolina","south dakota","tennessee","texas","utah","vermont",
            "virginia","washington","west virginia","wisconsin","wyoming"
        ]
        state = st.selectbox("Filter by state (optional)", states, index=0)

        st.write("---")
        st.caption("If you see HTTP 429/403 or HTML, you’re likely rate-limited. Wait a few minutes and retry.")

    month, day = chosen.month, chosen.day

    top1, top2 = st.columns(2)
    with top1:
        st.markdown(f"### Selected month/day: **{chosen.strftime('%B %d')}**")
    with top2:
        st.markdown(f"### System today: **{today.strftime('%B %d')}**")

    st.caption("If these don’t match, click **Use today** in the sidebar.")

    c1, c2, c3 = st.columns(3)

    with c1:
        if st.button("✅ Known-good example query", use_container_width=True):
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
        if st.button("🔎 Test exact month/day in chosen year", use_container_width=True):
            start_date, end_date = make_window(int(year), month, day, window_days=3)
            st.caption(f"Query window: {start_date} → {end_date}")

            url = build_query_url(
                start_date,
                end_date,
                state=state,
                keyword=keyword,
                front_pages_only=front_pages_only,
                count=100,
            )
            payload, dbg = fetch_json_debug(url)
            st.link_button("Open query used", url)

            if not payload:
                show_debug(dbg)
            else:
                results = parse_results(payload)
                exact = filter_exact_month_day(results, month, day)

                if not exact:
                    st.warning(
                        "Valid JSON response, but no items matched the exact month/day after filtering. "
                        "Try unchecking Front pages only, removing state/keyword filters, or picking another year."
                    )
                    show_no_match_diagnostics(results)
                else:
                    st.success(f"Got {len(exact)} exact match(es). Showing first:")
                    render_item(exact[0])

    with c3:
        if st.button("⏪ Find most recent available (exact match)", use_container_width=True):
            with st.spinner("Scanning by decade (+/- 3-day windows), then filtering to exact month/day…"):
                y, item, url, dbg, cnt, win = decade_step_most_recent(
                    month, day, state=state, keyword=keyword, front_pages_only=front_pages_only
                )

            if item and y and url:
                st.success(f"Found an exact {chosen.strftime('%b %d')} match in **{y}**. ({cnt} exact match(es) that year)")
                st.caption(f"Query window used: {win[0]} → {win[1]}")
                st.link_button("Open query used", url)
                render_item(item)
            else:
                st.error("No exact match found (or request blocked).")
                show_debug(dbg)

    st.write("---")
    st.markdown("#### Notes")
    st.markdown(
        "- If you see results for **Aug 01** (like `1955-08-01`), your selected date is Aug 1.\n"
        "- Click **Use today** to force the selection back to today.\n"
        "- The app filters exact month/day using the real date embedded in `aka`/`url`."
    )


if __name__ == "__main__":
    main()
