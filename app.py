# This Day in History — Newspapers (Chronicling America via loc.gov)
# Version: 0.5.10
# Date: 2026-02-19
#
# New in 0.5.7 (keeps existing functionality):
# - FIX: OCR-first is now robust by fetching Chronicling America /ocr/ text when loc.gov JSON lacks full_text.
# - FIX: PDF fallback URL construction handles more loc.gov URL variants and provides better debug info.
#
# Keeps:
# - High-res PDF rendering for Crop -> OCR (via pypdfium2) + JPEG fallback
# - Search tab: surprise me, random year, keyword, require-keyword filtering, diagnostics
# - Save/share + downloads
# - Save-to-library (Supabase) with category dropdown + “add new category”
# - Library tab: filter/search/sort + in-app preview (image + OCR)
# - Crop image -> OCR -> download as .txt/.md (local; Pillow+pytesseract+tesseract)

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import random
import re
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import certifi
import ssl
import streamlit as st

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# Optional: Crop+OCR deps
PIL_OK = True
try:
    from PIL import Image, ImageDraw, ImageOps
except Exception:
    PIL_OK = False
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore
    ImageOps = None  # type: ignore

TESS_OK = True
try:
    import pytesseract
except Exception:
    TESS_OK = False
    pytesseract = None  # type: ignore

# Optional: PDF -> image rendering (for crisp pages)
PDFIUM_OK = True
try:
    import pypdfium2 as pdfium
except Exception:
    PDFIUM_OK = False
    pdfium = None  # type: ignore

# Optional: OpenAI (PDF summarization)
OPENAI_OK = True
try:
    from openai import OpenAI
except Exception:
    OPENAI_OK = False
    OpenAI = None  # type: ignore

# ---- Library helper (local module) ----
LIBRARY_OK = True
try:
    from supabase_links import insert_link, list_links
except Exception:
    LIBRARY_OK = False
    insert_link = None
    list_links = None

APP_VERSION = "0.5.11"
BASE_COLLECTION_URL = "https://www.loc.gov/collections/chronicling-america/"

TOPIC_PRESETS = [
    "yankees",
    "baseball",
    "florida",
    "hurricane",
    "space",
    "election",
    "war",
    "stock market",
    "weather",
    "train",
    "ship",
]

# Supabase config (set these in .streamlit/secrets.toml)
SUPABASE_URL = st.secrets.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = st.secrets.get("SUPABASE_ANON_KEY", "")
LINKS_TABLE = "user_links"

# OpenAI config (set this in .streamlit/secrets.toml)
OPENAI_API_KEY = (st.secrets.get("OPENAI_API_KEY", "") or "").strip()
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"

PDF_SUMMARY_PRESET_PROMPT = """You are my newspaper research assistant.
Summarize this full newspaper page for a modern reader.

Output:
1) 8–12 bullet summary of the most important items on the page
2) Notable names / places / organizations (bulleted)
3) Dates mentioned (bulleted)
4) 3–5 “Why it matters” takeaways
5) If there are sports items, summarize them separately under a SPORTS header
Keep it factual; if scan quality is uncertain, say so.
Avoid long verbatim quotes (short phrases only).
"""

# ----------------------------
# Prompt modes (for deeper analysis / transcription)
# ----------------------------
DOC_MODE_PRESETS: Dict[str, str] = {
    "Summary + Deep dive (default)": PDF_SUMMARY_PRESET_PROMPT,
    "Historical analysis": (
        "You are my newspaper research assistant.\n"
        "Analyze this newspaper page for historical understanding.\n\n"
        "Output:\n"
        "1) What is the main event/story of the day on this page? (3–6 bullets)\n"
        "2) Historical context: what was happening locally, nationally, and internationally?\n"
        "3) Tone/bias/framing: note loaded language, omissions, and assumptions typical of the era\n"
        "4) Key people/places/organizations (bulleted, with short roles)\n"
        "5) Timeline of dates/events mentioned\n"
        "6) Why it matters: 3–5 takeaways\n"
        "7) SPORTS section if present\n\n"
        "Keep it factual; if scan/OCR quality is uncertain, say so. Avoid long verbatim quotes."
    ),
    "Word-for-word transcription": (
        "You are performing historical transcription.\n"
        "Transcribe the newspaper content word-for-word as best as possible.\n\n"
        "Rules:\n"
        "- Do NOT summarize.\n"
        "- Preserve spelling, capitalization, and punctuation as printed.\n"
        "- Keep paragraph breaks; if columns are obvious, separate columns with a blank line.\n"
        "- If text is unclear, mark it as [unclear].\n"
        "- Avoid adding any commentary.\n\n"
        "If the page is long, transcribe the most prominent stories first, then continue until you reach a reasonable length."
    ),
    "Extract names & roles": (
        "Extract ALL named people, places, and organizations from the page.\n"
        "For each, provide a short role/why they are mentioned.\n"
        "Then group them into categories: People / Places / Organizations.\n"
        "Do not summarize the whole page beyond what's needed for roles."
    ),
    "Sports only": (
        "Focus ONLY on sports content on the page.\n"
        "Summarize games, teams, scores, standings, player names, and any notable sports headlines.\n"
        "If there is no sports content, say 'No sports items found.'"
    ),
    "Timeline only": (
        "Create a chronological timeline of events mentioned on the page.\n"
        "Include explicit dates and also relative references (e.g., 'yesterday', 'last week') with inferred dates if possible.\n"
        "If dates are uncertain, note uncertainty."
    ),

    "Medical & Health History": """You are my newspaper research assistant specializing in historical medicine and public health.

Analyze this newspaper page and extract all references to:

• Diseases or epidemics
• Medical treatments or surgical procedures
• Public health efforts or warnings
• Hospitals, doctors, or medical institutions
• Patent medicines, tonics, elixirs, or health products
• Advertisements claiming health benefits

For each medical reference:
- Describe what is being claimed or reported
- Explain the historical medical understanding at that time
- Briefly compare with modern medical knowledge (if relevant)

Separate clearly:
1) Legitimate medical reporting
2) Health-related advertisements or commercial products

Then provide:
• Notable diseases mentioned
• Notable medical figures or institutions
• Cultural attitudes toward illness reflected in the page
• 3–5 historical significance takeaways

Keep it factual.
Do not modernize language unless explaining context.
If scan quality is uncertain, say so.
Avoid long verbatim quotes (short phrases only).
""",
}


# ----------------------------
# Basic helpers
# ----------------------------
def app_today_date() -> dt.date:
    if ZoneInfo is not None:
        try:
            return dt.datetime.now(ZoneInfo("America/New_York")).date()
        except Exception:
            pass
    return dt.date.today()


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


def build_query_url(
    start_date: str,
    end_date: str,
    *,
    keyword: str = "",
    front_pages_only: bool = True,
    dl: str = "page",
    count: int = 100,
) -> str:
    params = {
        "fo": "json",
        "dl": dl,
        "start_date": start_date,
        "end_date": end_date,
        "dates": f"{start_date}/{end_date}",
        "c": str(max(1, min(int(count), 100))),
        "searchType": "Advanced",
    }

    if front_pages_only:
        params["front_pages_only"] = "true"

    keyword = (keyword or "").strip()
    if keyword:
        params["qs"] = "+".join(keyword.split())
        params["ops"] = "AND"

    return BASE_COLLECTION_URL + "?" + urllib.parse.urlencode(params)


def add_fo_json(url: str) -> str:
    if not url:
        return url
    if "fo=json" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}fo=json"


def supabase_ready() -> bool:
    return bool(LIBRARY_OK and SUPABASE_URL and SUPABASE_ANON_KEY and insert_link and list_links)


def openai_ready() -> bool:
    return bool(OPENAI_OK and OPENAI_API_KEY and OpenAI is not None)


def _looks_like_error_payload(x: Any) -> bool:
    return isinstance(x, dict) and any(k in x for k in ("message", "error", "hint", "details", "code", "http_status"))


def safe_filename(s: str, default: str = "newspaper_page") -> str:
    s = (s or "").strip()
    if not s:
        s = default
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^A-Za-z0-9._-]+", "", s)
    return s[:120] if len(s) > 120 else s


def sha256_hex(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def is_pdf_bytes(data: bytes) -> bool:
    """Strict-ish PDF check: looks for %PDF- header after stripping leading whitespace/NULs.
    Also rejects obvious HTML error pages that some fallbacks return.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < 5:
        return False
    head = bytes(data[:4096])
    # Reject obvious HTML
    if re.search(br"(?is)^\s*<(?:!doctype\s+html|html|head|body)\b", head):
        return False
    stripped = bytes(data).lstrip(b"\x00\t\r\n\f\x0b ")
    return stripped.startswith(b"%PDF-")


# ----------------------------
# HTTP / JSON (loc.gov)
# ----------------------------
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
        headers={"User-Agent": f"ThisDayInHistoryStreamlit/{APP_VERSION}", "Accept": "application/json"},
    )
    ctx = ssl.create_default_context(cafile=certifi.where())

    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            debug["status"] = getattr(resp, "status", None)
            debug["content_type"] = resp.headers.get("Content-Type", "")
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

    text = raw.decode("utf-8", errors="replace")
    if "json" not in (debug["content_type"] or "").lower():
        debug["error"] = f"Non-JSON response (Content-Type: {debug['content_type']})"
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
    r = payload.get("results", [])
    return r if isinstance(r, list) else []


# ----------------------------
# Item parsing
# ----------------------------
def parse_item_date(item: Dict[str, Any]) -> Optional[dt.date]:
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
    out: List[Dict[str, Any]] = []
    for it in results:
        d = parse_item_date(it)
        if d and d.month == month and d.day == day:
            out.append(it)
    return out


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
    return d.isoformat() if d else str(item.get("date") or "").strip()


def item_link(item: Dict[str, Any]) -> Optional[str]:
    u = item.get("url")
    return u if isinstance(u, str) and u.startswith("http") else None


# ----------------------------
# OCR text (loc.gov JSON) + highlighting
# ----------------------------
def highlight_html(text: str, query: str) -> str:
    if not text:
        return ""
    q = (query or "").strip()
    safe = html.escape(text)
    if not q:
        return safe

    terms = [t for t in re.split(r"\s+", q) if t]
    if not terms:
        return safe

    for t in sorted(set(terms), key=len, reverse=True):
        et = html.escape(t)
        safe = re.sub(rf"(?i)\b({re.escape(et)})\b", r"<mark>\1</mark>", safe)
    return safe


def pick_ocr_text(payload: Dict[str, Any], max_chars: int = 4000) -> str:
    candidates: List[str] = []
    v = payload.get("full_text")
    if isinstance(v, str) and v.strip():
        candidates.append(v)
    v = payload.get("text")
    if isinstance(v, str) and v.strip():
        candidates.append(v)

    res = payload.get("results")
    if isinstance(res, list) and res:
        for it in res[:3]:
            if isinstance(it, dict):
                for k in ("full_text", "text"):
                    vv = it.get(k)
                    if isinstance(vv, str) and vv.strip():
                        candidates.append(vv)

    if not candidates:
        return ""
    s = " ".join(candidates[0].split())
    return (s[:max_chars] + "…") if len(s) > max_chars else s


@st.cache_data(show_spinner=False, ttl=60 * 60)
def fetch_page_ocr_text(page_url: str) -> str:
    """Fetch OCR text for a loc.gov Chronicling America page.

    Strategy (no manual crop/range needed):
    1) Try loc.gov JSON fields (full_text/text) if present.
    2) If missing/empty, try Chronicling America plain-text OCR endpoint: /seq-{sp}/ocr/
       This often works even when loc.gov JSON doesn't include full_text.
    """
    if not page_url:
        return ""

    # 1) loc.gov JSON
    url = add_fo_json(page_url)
    payload, _dbg = fetch_json_debug(url)
    if payload:
        txt = pick_ocr_text(payload, max_chars=200_000)
        if txt.strip():
            return txt

    # 2) Chronicling America OCR endpoint (plain text)
    ocr_url = chroniclingamerica_ocr_url_from_loc_page(page_url)
    if not ocr_url:
        # try converting /resource/ -> /item/ and attempt again
        ocr_url = chroniclingamerica_ocr_url_from_loc_page(resource_url_to_item_url(page_url))

    if not ocr_url:
        return ""

    ctx = ssl.create_default_context(cafile=certifi.where())
    req = urllib.request.Request(
        ocr_url,
        headers={
            "User-Agent": f"ThisDayInHistoryStreamlit/{APP_VERSION}",
            "Accept": "text/plain,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            raw = resp.read() or b""
            # reject HTML
            head = raw[:2048]
            if re.search(br"(?is)^\s*<(?:!doctype\s+html|html|head|body)\b", head):
                return ""
            text = raw.decode("utf-8", errors="replace")
            return " ".join(text.split())
    except Exception:
        return ""


def filter_by_keyword_client_side(items: List[Dict[str, Any]], keyword: str) -> List[Dict[str, Any]]:
    kw = (keyword or "").strip()
    if not kw:
        return items

    kept: List[Dict[str, Any]] = []
    for it in items:
        link = item_link(it)
        if not link:
            continue
        ocr = fetch_page_ocr_text(link)
        if ocr and kw.lower() in ocr.lower():
            kept.append(it)
    return kept


# ----------------------------
# Keyword diagnostics
# ----------------------------
def find_keyword_context(text: str, keyword: str, window: int = 80) -> str:
    if not text or not keyword:
        return ""
    m = re.search(re.escape(keyword), text, flags=re.IGNORECASE)
    if not m:
        return ""
    start = max(0, m.start() - window)
    end = min(len(text), m.end() + window)
    excerpt = " ".join(text[start:end].split())
    return excerpt


@st.cache_data(show_spinner=False, ttl=60 * 30)
def keyword_diagnostics_cached(page_url: str, keyword: str) -> Tuple[bool, str]:
    ocr = fetch_page_ocr_text(page_url)
    if not ocr:
        return False, ""
    hit = keyword.lower() in ocr.lower()
    ctx = find_keyword_context(ocr, keyword) if hit else ""
    return hit, ctx


def show_keyword_diagnostics(items: List[Dict[str, Any]], keyword: str, limit: int = 12):
    kw = (keyword or "").strip()
    if not kw:
        st.info("Enter a keyword to see diagnostics.")
        return

    shown = items[: max(1, int(limit))]
    for it in shown:
        url = item_link(it) or ""
        if not url:
            st.write(f"❌ {item_date_str(it)} — {item_title(it)} (no url)")
            st.write("---")
            continue

        hit, ctx = keyword_diagnostics_cached(url, kw)
        st.write(f"{'✅' if hit else '❌'} {item_date_str(it)} — {item_title(it)}")
        st.write(url)
        if hit and ctx:
            st.code(ctx)
        st.write("---")


# ----------------------------
# Pending-value application (Streamlit-safe)
# ----------------------------
def apply_pending_before_widgets():
    if "pending_year_input" in st.session_state:
        st.session_state["year_input"] = int(st.session_state.pop("pending_year_input"))
    if "pending_keyword" in st.session_state:
        st.session_state["keyword"] = str(st.session_state.pop("pending_keyword"))
    if "pending_require_keyword" in st.session_state:
        st.session_state["require_keyword"] = bool(st.session_state.pop("pending_require_keyword"))
    if "pending_result_index" in st.session_state:
        st.session_state["result_index"] = int(st.session_state.pop("pending_result_index"))


def get_year() -> int:
    return int(st.session_state.get("year_input", 1955))


def run_search_and_store():
    chosen: dt.date = st.session_state["chosen_date"]
    year: int = get_year()
    keyword: str = st.session_state.get("keyword", "")
    front_pages_only: bool = bool(st.session_state.get("front_pages_only", True))
    require_keyword: bool = bool(st.session_state.get("require_keyword", False))

    month, day = chosen.month, chosen.day
    start_date, end_date = make_window(year, month, day, window_days=3)

    url = build_query_url(
        start_date,
        end_date,
        keyword=keyword,
        front_pages_only=front_pages_only,
        count=100,
    )
    st.session_state["last_query_url"] = url

    payload, dbg = fetch_json_debug(url)
    if not payload:
        st.session_state["last_error"] = dbg
        st.session_state["last_exact_all"] = []
        st.session_state["last_exact_final"] = []
        st.session_state["result_index"] = 0
        return

    results = parse_results(payload)

    # If we got very few results, widen the window to improve the chance of finding this month/day.
    # Chronicling America coverage can be sparse for a given year.
    if len(results) < 5:
        start_date2, end_date2 = make_window(year, month, day, window_days=14)
        url2 = build_query_url(
            start_date2,
            end_date2,
            keyword=keyword,
            front_pages_only=front_pages_only,
            count=100,
        )
        payload2, _dbg2 = fetch_json_debug(url2)
        if payload2 and isinstance(payload2, dict):
            results2 = parse_results(payload2)
            if len(results2) > len(results):
                results = results2
                st.session_state["last_query_url_wide"] = url2
    
    exact = filter_exact_month_day(results, month, day)
    st.session_state["last_exact_all"] = exact

    # If there are no exact month/day matches, keep a "near matches" fallback so the app still shows results.
    using_near = False
    near = exact
    if not exact:
        using_near = True
        near = results[:50] if isinstance(results, list) else []

    final = near
    if require_keyword and (keyword or "").strip():
        with st.spinner("Filtering by keyword in OCR (client-side)…"):
            final = filter_by_keyword_client_side(exact, keyword)

    st.session_state["last_exact_final"] = final
    st.session_state["using_near_matches"] = using_near
    st.session_state["last_error"] = {}
    st.session_state["result_index"] = 0


# ----------------------------
# Share params
# ----------------------------
def apply_params_if_present_once():
    if st.session_state.get("_params_applied", False):
        return

    qp = st.query_params
    if not qp:
        st.session_state["_params_applied"] = True
        return

    def get1(key: str) -> Optional[str]:
        v = qp.get(key)
        if v is None:
            return None
        if isinstance(v, list):
            return v[0] if v else None
        return str(v)

    d = get1("d")
    y = get1("y")
    k = get1("k")
    fp = get1("fp")
    rk = get1("rk")
    i = get1("i")

    if d:
        try:
            st.session_state["chosen_date"] = dt.date.fromisoformat(d)
        except Exception:
            pass
    if y:
        try:
            st.session_state["year_input"] = int(y)
        except Exception:
            pass
    if k is not None:
        st.session_state["keyword"] = k
    if fp in ("0", "1"):
        st.session_state["front_pages_only"] = (fp == "1")
    if rk in ("0", "1"):
        st.session_state["require_keyword"] = (rk == "1")
    if i:
        try:
            st.session_state["result_index"] = max(0, int(i))
        except Exception:
            pass

    st.session_state["_params_applied"] = True


def build_share_params() -> Dict[str, str]:
    chosen: dt.date = st.session_state["chosen_date"]
    params = {
        "d": chosen.isoformat(),
        "y": str(get_year()),
        "k": (st.session_state.get("keyword", "") or "").strip(),
        "fp": "1" if bool(st.session_state.get("front_pages_only", True)) else "0",
        "rk": "1" if bool(st.session_state.get("require_keyword", False)) else "0",
        "i": str(int(st.session_state.get("result_index", 0))),
    }
    if not params["k"]:
        params.pop("k", None)
    return params


def current_pick_payload() -> Dict[str, Any]:
    exact: List[Dict[str, Any]] = st.session_state.get("last_exact_final", []) or []
    idx = int(st.session_state.get("result_index", 0))
    idx = max(0, min(idx, len(exact) - 1)) if exact else 0

    item = exact[idx] if exact else None
    link = item_link(item) if item else None
    img = best_image_url(item) if item else None
    ocr = fetch_page_ocr_text(link) if link else ""

    return {
        "app_version": APP_VERSION,
        "selected_date": st.session_state["chosen_date"].isoformat(),
        "year": get_year(),
        "keyword": (st.session_state.get("keyword", "") or "").strip(),
        "front_pages_only": bool(st.session_state.get("front_pages_only", True)),
        "require_keyword": bool(st.session_state.get("require_keyword", False)),
        "result_index": idx,
        "result_count": len(exact),
        "item": {
            "title": item_title(item) if item else None,
            "date": item_date_str(item) if item else None,
            "url": link,
            "image_url": img,
        },
        "ocr_excerpt": ocr[:1200] if ocr else "",
        "query_url": st.session_state.get("last_query_url", ""),
    }


def current_pick_markdown() -> str:
    p = current_pick_payload()
    item = p.get("item", {}) or {}
    lines = []
    lines.append("# This Day in History — Pick")
    lines.append("")
    lines.append(f"- Selected date: **{p['selected_date']}**")
    lines.append(f"- Year: **{p['year']}**")
    if p.get("keyword"):
        lines.append(f"- Keyword: **{p['keyword']}**")
        lines.append(f"- Require keyword: **{p['require_keyword']}**")
    lines.append(f"- Front pages only: **{p['front_pages_only']}**")
    lines.append("")
    lines.append("## Item")
    lines.append(f"- Title: {item.get('title')}")
    lines.append(f"- Date: {item.get('date')}")
    lines.append(f"- URL: {item.get('url')}")
    if item.get("image_url"):
        lines.append(f"- Image: {item.get('image_url')}")
    lines.append("")
    if p.get("ocr_excerpt"):
        lines.append("## OCR excerpt")
        lines.append("")
        lines.append(p["ocr_excerpt"])
        lines.append("")
    lines.append("## Query URL")
    lines.append(p.get("query_url") or "")
    return "\n".join(lines)


# ----------------------------
# Library categories (robust)
# ----------------------------
@st.cache_data(show_spinner=False, ttl=60 * 10)
def _cached_library_categories() -> List[str]:
    defaults = ["History", "Baseball", "Yankees", "Florida", "Weather", "Space", "Arcade", "Other"]
    if not supabase_ready():
        return defaults

    cats: List[str] = []

    try:
        from supabase_links import list_categories  # optional helper
        res = list_categories(SUPABASE_URL, SUPABASE_ANON_KEY, LINKS_TABLE, limit=300)
        if _looks_like_error_payload(res):
            return defaults
        if isinstance(res, list):
            for x in res:
                if isinstance(x, str):
                    cats.append(x)
                elif isinstance(x, dict):
                    v = x.get("category")
                    if isinstance(v, str):
                        cats.append(v)
    except Exception:
        res = list_links(SUPABASE_URL, SUPABASE_ANON_KEY, LINKS_TABLE, limit=300)
        if _looks_like_error_payload(res):
            return defaults
        if isinstance(res, list):
            for r in res:
                if isinstance(r, dict):
                    v = r.get("category")
                    if isinstance(v, str):
                        cats.append(v)

    merged: List[str] = []
    seen = set()
    for c in defaults + cats:
        if not isinstance(c, str):
            continue
        c = c.strip()
        if c and c not in seen:
            merged.append(c)
            seen.add(c)
    return merged


def _clear_library_caches():
    try:
        _cached_library_categories.clear()
    except Exception:
        pass


# ----------------------------
# URL normalization & loc.gov asset helpers
# ----------------------------
def resource_url_to_item_url(u: str) -> str:
    if not u:
        return u
    base = u.split("?", 1)[0].rstrip("/")
    m = re.search(r"/resource/([^/]+)/(\d{4}-\d{2}-\d{2})/(ed-\d+)$", base)
    if not m:
        return u
    lccn, date_s, ed = m.group(1), m.group(2), m.group(3)
    return f"https://www.loc.gov/item/{lccn}/{date_s}/{ed}/"


def chroniclingamerica_pdf_url_from_loc_page(page_url: str) -> Optional[str]:
    if not page_url:
        return None

    parsed = urllib.parse.urlparse(page_url)
    qs = urllib.parse.parse_qs(parsed.query)
    sp_raw = (qs.get("sp") or ["1"])[0]
    try:
        sp_int = max(1, int(str(sp_raw)))
    except Exception:
        sp_int = 1

    m = re.search(
        r"/(resource|item)/(?P<lccn>[a-z]{2}\d{8})/(?P<date>\d{4}-\d{2}-\d{2})/(?P<ed>ed-\d+)/?",
        page_url,
    )
    if not m:
        return None

    lccn = m.group("lccn")
    date_s = m.group("date")
    ed = m.group("ed")
    return f"https://chroniclingamerica.loc.gov/lccn/{lccn}/{date_s}/{ed}/seq-{sp_int}.pdf"


def chroniclingamerica_ocr_url_from_loc_page(page_url: str) -> Optional[str]:
    """Construct Chronicling America OCR endpoint for a loc.gov page URL.
    Example: .../seq-1/ocr/ returns plain text OCR.
    """
    if not page_url:
        return None
    parsed = urllib.parse.urlparse(page_url)
    qs = urllib.parse.parse_qs(parsed.query)
    sp_raw = (qs.get("sp") or ["1"])[0]
    try:
        sp_int = max(1, int(str(sp_raw)))
    except Exception:
        sp_int = 1
    m = re.search(
        r"/(resource|item)/(?P<lccn>[a-z]{2}\d{8})/(?P<date>\d{4}-\d{2}-\d{2})/(?P<ed>ed-\d+)/?",
        page_url,
    )
    if not m:
        return None
    lccn = m.group("lccn")
    date_s = m.group("date")
    ed = m.group("ed")
    return f"https://chroniclingamerica.loc.gov/lccn/{lccn}/{date_s}/{ed}/seq-{sp_int}/ocr/"


@st.cache_data(show_spinner=False, ttl=60 * 60)
def fetch_loc_page_json(page_url: str) -> Optional[Dict[str, Any]]:
    if not page_url:
        return None
    payload, _dbg = fetch_json_debug(add_fo_json(page_url))
    return payload


def _extract_pdf_url(payload: Dict[str, Any]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None

def _find_pdf_urls_in_payload(payload: Dict[str, Any], limit: int = 30) -> List[str]:
    """
    Find PDF URLs nested in loc.gov JSON. Prefer direct NDNP storage-services PDFs.
    """
    found: List[str] = []
    if not isinstance(payload, dict):
        return found

    def walk(x: Any):
        if len(found) >= limit:
            return
        if isinstance(x, str):
            s = x.strip()
            if s.startswith("http") and ".pdf" in s.lower():
                found.append(s)
            return
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
            return
        if isinstance(x, list):
            for v in x:
                walk(v)
            return

    walk(payload)

    # normalize + dedupe
    out: List[str] = []
    seen = set()
    for u in found:
        u2 = u.strip()
        if u2.startswith("//"):
            u2 = "https:" + u2
        if u2.startswith("http"):
            # keep query (some downloads might depend on it), but dedupe on full string
            if u2 not in seen:
                out.append(u2)
                seen.add(u2)

    def score(u: str) -> Tuple[int, int]:
        lu = u.lower()
        s = 0
        if "tile.loc.gov/storage-services/service/ndnp" in lu:
            s += 100
        if lu.endswith(".pdf"):
            s += 10
        return (s, -len(u))

    out.sort(key=score, reverse=True)
    return out

    v = payload.get("pdf")
    if isinstance(v, list) and v:
        u = str(v[0]).strip()
        return u if u.startswith("http") else None
    if isinstance(v, str) and v.strip():
        u = v.strip()
        return u if u.startswith("http") else None
    return None


@st.cache_data(show_spinner=False, ttl=60 * 60)
def fetch_loc_pdf_url(page_url: str) -> Optional[str]:
    """
    Return the best candidate PDF URL for a loc.gov newspaper page.

    Tries:
      1) loc.gov JSON `pdf`
      2) any nested PDF URLs (prefer tile.loc.gov/storage-services/service/ndnp/*.pdf)
      3) /item/ fallback JSON
      4) Chronicling America constructed seq-{sp}.pdf (may redirect to tile.loc.gov)
    """
    if not page_url:
        return None

    payload = fetch_loc_page_json(page_url)
    if payload:
        u = _extract_pdf_url(payload)
        if u:
            return u
        more = _find_pdf_urls_in_payload(payload)
        if more:
            return more[0]

    item_url = resource_url_to_item_url(page_url)
    if item_url != page_url:
        payload2 = fetch_loc_page_json(item_url)
        if payload2:
            u2 = _extract_pdf_url(payload2)
            if u2:
                return u2
            more2 = _find_pdf_urls_in_payload(payload2)
            if more2:
                return more2[0]

    return chroniclingamerica_pdf_url_from_loc_page(page_url)


@st.cache_data(show_spinner=False, ttl=60 * 60)
def fetch_loc_image_url(page_url: str) -> Optional[str]:
    if not page_url:
        return None

    def normalize(u: str) -> str:
        u = (u or "").strip()
        if u.startswith("//"):
            u = "https:" + u
        return u

    def extract_image_url(payload: Dict[str, Any]) -> Optional[str]:
        img = payload.get("image_url")
        if isinstance(img, list) and img:
            u = normalize(str(img[0]))
            return u if u.startswith("http") else None
        if isinstance(img, str) and img.strip():
            u = normalize(img)
            return u if u.startswith("http") else None

        item = payload.get("item")
        if isinstance(item, dict):
            img2 = item.get("image_url")
            if isinstance(img2, list) and img2:
                u = normalize(str(img2[0]))
                return u if u.startswith("http") else None
            if isinstance(img2, str) and img2.strip():
                u = normalize(img2)
                return u if u.startswith("http") else None

        return None

    payload = fetch_loc_page_json(page_url)
    if payload:
        u = extract_image_url(payload)
        if u:
            return u

    item_url = resource_url_to_item_url(page_url)
    if item_url != page_url:
        payload2 = fetch_loc_page_json(item_url)
        if payload2:
            u2 = extract_image_url(payload2)
            if u2:
                return u2

    return None


@st.cache_data(show_spinner=False, ttl=60 * 60)
def fetch_image_pil(image_url: str) -> Optional["Image.Image"]:
    if not PIL_OK or Image is None:
        return None
    if not image_url:
        return None

    req = urllib.request.Request(
        image_url,
        headers={"User-Agent": f"ThisDayInHistoryStreamlit/{APP_VERSION}"},
    )
    ctx = ssl.create_default_context(cafile=certifi.where())

    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            data = resp.read()
        img = Image.open(BytesIO(data)).convert("RGB")
        return img
    except Exception:
        return None

# ----------------------------
# Image OCR fallback (no PDF needed)
# ----------------------------
def _extract_all_image_urls_from_payload(payload: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    if not isinstance(payload, dict):
        return urls

    def add_img(v: Any):
        if isinstance(v, list):
            for u in v:
                if isinstance(u, str) and u.strip():
                    urls.append(u.strip())
        elif isinstance(v, str) and v.strip():
            urls.append(v.strip())

    add_img(payload.get("image_url"))
    item = payload.get("item")
    if isinstance(item, dict):
        add_img(item.get("image_url"))

    out: List[str] = []
    seen = set()
    for u in urls:
        if u.startswith("//"):
            u = "https:" + u
        if u.startswith("http") and u not in seen:
            out.append(u)
            seen.add(u)
    return out


def fetch_all_image_urls(page_url: str) -> List[str]:
    """Return all candidate image URLs for a loc.gov page (tries /item/ fallback)."""
    if not page_url:
        return []

    payload = fetch_loc_page_json(page_url)
    urls = _extract_all_image_urls_from_payload(payload) if payload else []

    if not urls:
        item_url = resource_url_to_item_url(page_url)
        if item_url != page_url:
            payload2 = fetch_loc_page_json(item_url)
            urls = _extract_all_image_urls_from_payload(payload2) if payload2 else []

    return urls


def pick_best_image_url(urls: List[str]) -> List[str]:
    """
    Return candidate image URLs ordered best->worst for OCR.

    - Prefer IIIF JPG/PNG URLs we can upscale to full size.
    - De-prioritize raw JP2/TIFF because Pillow often can't decode them without extra codecs.
    """
    if not urls:
        return []

    def is_iiif(u: str) -> bool:
        lu = u.lower()
        return ("/image-services/iiif/" in lu) or ("/iiif/" in lu and "default.jpg" in lu)

    def score(u: str) -> Tuple[int, int, int]:
        lu = u.lower()

        fmt = 0
        if ".jpg" in lu or ".jpeg" in lu:
            fmt = 30
        elif ".png" in lu:
            fmt = 25
        elif ".jp2" in lu or "jp2" in lu:
            fmt = 5
        elif ".tif" in lu or ".tiff" in lu:
            fmt = 3

        iiif_bonus = 20 if is_iiif(u) else 0

        size_hint = 0
        if "/full/full/" in lu:
            size_hint += 20
        if "pct:" in lu:
            mm = re.search(r"pct:(\d{1,3})", lu)
            if mm:
                try:
                    pct = int(mm.group(1))
                    size_hint += max(0, 15 - pct // 10)
                except Exception:
                    pass
        if "full" in lu:
            size_hint += 5
        if "large" in lu:
            size_hint += 3

        return (iiif_bonus, fmt, size_hint)

    return [u for u in sorted(urls, key=score, reverse=True)]


@st.cache_data(show_spinner=False, ttl=60 * 60 * 6)
def fetch_image_ocr_text(page_url: str) -> str:
    """
    Attempt full-page OCR from the best available loc.gov page image (no PDF).
    This does NOT require crop selection.

    Strategy:
    - Gather all image_url candidates from loc.gov JSON (with /item/ fallback)
    - Prefer IIIF URLs and request FULL size for OCR (avoid pct thumbnails)
    - Try multiple candidates until one decodes successfully
    """
    if not page_url:
        return ""
    if not (PIL_OK and TESS_OK and Image is not None and pytesseract is not None):
        return ""

    urls = fetch_all_image_urls(page_url)
    candidates = pick_best_image_url(urls)
    if not candidates:
        return ""

    def iiif_full(u: str) -> str:
        # Convert IIIF thumbnail URLs like .../full/pct:25/0/default.jpg -> .../full/full/0/default.jpg
        uu = re.sub(r"/full/pct:\d{1,3}/", "/full/full/", u)
        # Convert explicit size like /full/1000,/0/default.jpg -> /full/full/0/default.jpg
        uu = re.sub(r"/full/!?\d{2,5},\d{0,5}/", "/full/full/", uu)
        uu = re.sub(r"/full/!?\d{2,5},/", "/full/full/", uu)
        return uu

    for u in candidates[:10]:
        u2 = iiif_full(u)
        pil = fetch_image_pil(u2)
        if pil is None:
            continue

        try:
            pre = preprocess_for_ocr(
                pil,
                upscale=2,
                grayscale=True,
                autocontrast=True,
                threshold=False,
                thresh_value=160,
            )
            txt = ocr_image_region(pre, psm=4)  # 4=columns
            cleaned = " ".join((txt or "").split())
            if len(cleaned) >= 1200:
                return cleaned
        except Exception:
            continue

    return ""
# ----------------------------
# Robust PDF downloading (validate real PDF bytes)
# ----------------------------
@dataclass
class PdfFetchResult:
    ok: bool
    pdf_url: str
    final_url: str
    content_type: str
    bytes_len: int
    is_pdf: bool
    error: str
    snippet: str


def _download_url_bytes(url: str, *, timeout: int = 60) -> Tuple[Optional[bytes], PdfFetchResult]:
    ctx = ssl.create_default_context(cafile=certifi.where())

    headers = {
        "User-Agent": f"ThisDayInHistoryStreamlit/{APP_VERSION}",
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
    }

    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
            ct = resp.headers.get("Content-Type", "") or ""
            final_url = getattr(resp, "url", url) or url
    except urllib.error.HTTPError as e:
        try:
            raw_err = e.read()
            snippet = raw_err[:400].decode("utf-8", errors="replace")
        except Exception:
            snippet = ""
        meta = PdfFetchResult(
            ok=False,
            pdf_url=url,
            final_url=url,
            content_type="",
            bytes_len=0,
            is_pdf=False,
            error=f"HTTPError {e.code}: {e.reason}",
            snippet=snippet,
        )
        return None, meta
    except Exception as e:
        meta = PdfFetchResult(
            ok=False,
            pdf_url=url,
            final_url=url,
            content_type="",
            bytes_len=0,
            is_pdf=False,
            error=f"{type(e).__name__}: {e}",
            snippet="",
        )
        return None, meta

    snippet = ""
    if raw and not is_pdf_bytes(raw):
        # likely HTML or text error; capture a peek
        snippet = raw[:400].decode("utf-8", errors="replace")

    meta = PdfFetchResult(
        ok=True,
        pdf_url=url,
        final_url=final_url,
        content_type=ct,
        bytes_len=len(raw) if raw else 0,
        is_pdf=is_pdf_bytes(raw) if raw else False,
        error="",
        snippet=snippet,
    )
    return raw, meta


@st.cache_data(show_spinner=False, ttl=60 * 60)
def fetch_pdf_bytes_best(page_url: str) -> Tuple[Optional[bytes], PdfFetchResult]:
    """
    Try multiple candidate PDF URLs and return the first one that downloads as a real PDF.
    """
    if not page_url:
        meta = PdfFetchResult(False, "", "", "", 0, False, "No page_url provided", "")
        return None, meta

    tried: List[PdfFetchResult] = []

    # Candidate 1: loc.gov (api + fallback)
    pdf_url1 = fetch_loc_pdf_url(page_url)
    if pdf_url1:
        b1, m1 = _download_url_bytes(pdf_url1)
        tried.append(m1)
        if b1 and m1.is_pdf:
            return b1, m1

    # Candidate 2: Chronicling America constructed (even if loc.gov gave something)
    pdf_url2 = chroniclingamerica_pdf_url_from_loc_page(page_url)
    if pdf_url2 and (pdf_url2 != pdf_url1):
        b2, m2 = _download_url_bytes(pdf_url2)
        tried.append(m2)
        if b2 and m2.is_pdf:
            return b2, m2

    # Nothing worked: return the "best" error (prefer the first attempted)
    if tried:
        # pick first attempt and attach a combined hint in error
        first = tried[0]
        first.ok = False
        if not first.error:
            first.error = "Downloaded content was not a PDF."
        return None, first

    meta = PdfFetchResult(False, "", "", "", 0, False, "No PDF URL could be determined.", "")
    return None, meta


# ----------------------------
# OpenAI PDF summarization + follow-up chat (cheaper + robust keys)
# ----------------------------
#
# Cost strategy (cheap follow-ups):
# - Compute a content hash (sha256) for each PDF.
# - Upload a PDF to OpenAI Files API ONCE per sha256 and reuse the returned file_id.
# - First request attaches the file_id + your initial prompt.
# - Follow-ups ONLY send new question text using Responses API `previous_response_id`.
#
# Streamlit stability strategy (avoid key collisions / weird rerun behavior):
# - Use a per-thread key: thread_key = "<namespace>::<pdf_sha256>".
# - Derive widget keys from a stable short hash of thread_key.

def _ensure_openai_state():
    if "oa_file_id_by_sha" not in st.session_state or not isinstance(st.session_state.get("oa_file_id_by_sha"), dict):
        st.session_state["oa_file_id_by_sha"] = {}  # sha256 -> file_id
    if "oa_threads" not in st.session_state or not isinstance(st.session_state.get("oa_threads"), dict):
        st.session_state["oa_threads"] = {}  # thread_key -> state dict
    if "oa_pdf_bytes_by_sha" not in st.session_state or not isinstance(st.session_state.get("oa_pdf_bytes_by_sha"), dict):
        # Only used for user-uploaded PDFs (we already have the bytes); avoids re-reading the uploader.
        st.session_state["oa_pdf_bytes_by_sha"] = {}  # sha256 -> bytes


def pdf_sha256(pdf_bytes: bytes) -> str:
    return sha256_hex(pdf_bytes)


def stable_doc_id_from_url(url: str) -> str:
    u = (url or "").strip()
    h = hashlib.md5(u.encode("utf-8")).hexdigest()  # nosec - non-crypto use (stable id only)
    return f"loc:{h}"


def make_thread_key(*, namespace: str, doc_id: str) -> str:
    ns = (namespace or "doc").strip() or "doc"
    did = (doc_id or "").strip() or "unknown"
    return f"{ns}::{did}"


def _wk(prefix: str, thread_key: str, suffix: str = "") -> str:
    # Stable Streamlit widget key (short + deterministic)
    h = hashlib.md5(thread_key.encode("utf-8")).hexdigest()  # nosec - non-crypto use (keying only)
    sfx = f"_{suffix}" if suffix else ""
    return f"{prefix}_{h}{sfx}"


def _get_thread_state(thread_key: str, *, title_hint: str, source_url: str, doc_id: str) -> Dict[str, Any]:
    _ensure_openai_state()
    stt = st.session_state["oa_threads"].get(thread_key)
    if not isinstance(stt, dict):
        stt = {}
    stt.setdefault("thread_key", thread_key)
    stt.setdefault("doc_id", doc_id)
    stt.setdefault("title_hint", title_hint)
    stt.setdefault("source_url", source_url)

    # Mode can be: "ocr" (text-only) or "pdf" (file_id + pdf)
    stt.setdefault("mode", "")
    stt.setdefault("ocr_len", 0)

    stt.setdefault("pdf_sha", "")
    stt.setdefault("file_id", "")
    stt.setdefault("last_response_id", "")
    stt.setdefault("messages", [])  # [{"role": "user"|"assistant", "text": str}]
    if not isinstance(stt.get("messages"), list):
        stt["messages"] = []
    return stt


def _save_thread_state(thread_key: str, stt: Dict[str, Any]) -> None:
    _ensure_openai_state()
    st.session_state["oa_threads"][thread_key] = stt


def upload_pdf_once(pdf_bytes: bytes) -> str:
    """Return a file_id for this PDF. Uploads only once per PDF sha."""
    if not openai_ready():
        return ""
    if not is_pdf_bytes(pdf_bytes):
        return ""

    _ensure_openai_state()
    sha = pdf_sha256(pdf_bytes)

    # 1) Session cache (fast, per-user)
    cached = st.session_state["oa_file_id_by_sha"].get(sha)
    if isinstance(cached, str) and cached:
        return cached

    # 2) App cache (shared across reruns/users on the same server process)
    #    (Does NOT persist across server restarts.)
    file_id = _upload_pdf_once_cached(sha, pdf_bytes=pdf_bytes)
    if file_id:
        st.session_state["oa_file_id_by_sha"][sha] = file_id
    return file_id


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def _upload_pdf_once_cached(pdf_sha: str, *, pdf_bytes: bytes) -> str:
    if not openai_ready():
        return ""
    if not is_pdf_bytes(pdf_bytes):
        return ""

    client = OpenAI(api_key=OPENAI_API_KEY)
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name
        try:
            up = client.files.create(file=open(tmp_path, "rb"), purpose="user_data")
            return str(up.id)
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
    except Exception:
        return ""


def _trim_text_for_model(s: str, *, max_chars: int = 60_000) -> Tuple[str, bool]:
    s = (s or "")
    if len(s) <= max_chars:
        return s, False
    return s[:max_chars] + "\n\n[TRUNCATED]\n", True


def ocr_good_enough(text: str) -> bool:
    """Heuristic: loc.gov OCR is usually usable if it's non-trivial and not extremely garbled."""
    t = (text or "").strip()
    if len(t) < 1200:
        return False
    head = t[:4000]
    # Too many replacement chars often indicates broken text extraction
    if head.count("�") / max(1, len(head)) > 0.01:
        return False
    return True


def openai_first_pass_with_pdf(
    *,
    model: str,
    pdf_bytes: bytes,
    prompt: str,
    title_hint: str,
    source_url: str,
) -> Tuple[str, str, str]:
    """Returns (text, response_id, file_id)."""
    if not openai_ready():
        return (
            "OpenAI summarization is not configured. Add OPENAI_API_KEY to secrets + install the SDK.",
            "",
            "",
        )

    if not is_pdf_bytes(pdf_bytes):
        return ("The bytes provided are not a valid PDF (missing %PDF header).", "", "")

    file_id = upload_pdf_once(pdf_bytes)
    if not file_id:
        return ("OpenAI upload failed (no file_id returned).", "", "")

    meta_lines = []
    if title_hint:
        meta_lines.append(f"Title: {title_hint}")
    if source_url:
        meta_lines.append(f"Source URL: {source_url}")
    meta_block = "\n".join(meta_lines).strip()


    user_text = (prompt or "").strip()
    if meta_block:
        user_text = f"{user_text}\n\n---\n{meta_block}\n---"





    client = OpenAI(api_key=OPENAI_API_KEY)
    try:
        resp = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_file", "file_id": file_id},
                        {"type": "input_text", "text": user_text},
                    ],
                }
            ],
        )
        out = (resp.output_text or "").strip() or "(No output returned.)"
        return out, str(getattr(resp, "id", "") or ""), file_id
    except Exception as e:
        return f"OpenAI request failed: {type(e).__name__}: {e}", "", file_id


def openai_first_pass_with_text(
    *,
    model: str,
    ocr_text: str,
    prompt: str,
    title_hint: str,
    source_url: str,
) -> Tuple[str, str]:
    """Returns (text, response_id). OCR/text-only: no PDF upload, cheapest path."""
    if not openai_ready():
        return ("OpenAI is not configured.", "")

    meta_lines = []
    if title_hint:
        meta_lines.append(f"Title: {title_hint}")
    if source_url:
        meta_lines.append(f"Source URL: {source_url}")
    meta_block = "\n".join(meta_lines).strip()


    body, truncated = _trim_text_for_model(ocr_text, max_chars=60_000)
    note = "NOTE: OCR text was truncated for length.\n\n" if truncated else ""



    user_text = (prompt or "").strip()
    if meta_block:
        user_text = f"{user_text}\n\n---\n{meta_block}\n---\n\n{note}OCR TEXT:\n{body}"







    else:
        user_text = f"{user_text}\n\n{note}OCR TEXT:\n{body}"




    client = OpenAI(api_key=OPENAI_API_KEY)
    try:
        resp = client.responses.create(model=model, input=user_text)
        out = (resp.output_text or "").strip() or "(No output returned.)"
        return out, str(getattr(resp, "id", "") or "")
    except Exception as e:
        return f"OpenAI request failed: {type(e).__name__}: {e}", ""


def openai_followup(
    *,
    model: str,
    question: str,
    previous_response_id: str,
) -> Tuple[str, str]:
    """Ask a follow-up using previous_response_id. Returns (text, new_response_id)."""
    if not openai_ready():
        return ("OpenAI is not configured.", "")

    q = (question or "").strip()
    if not q:
        return ("", previous_response_id or "")

    client = OpenAI(api_key=OPENAI_API_KEY)
    try:
        resp = client.responses.create(
            model=model,
            previous_response_id=previous_response_id,
            input=q,
        )
        out = (resp.output_text or "").strip() or "(No output returned.)"
        return out, str(getattr(resp, "id", "") or "")
    except Exception as e:
        return f"OpenAI request failed: {type(e).__name__}: {e}", previous_response_id or ""




def extract_story_options(summary_text: str, max_items: int = 12) -> List[Tuple[str, str]]:
    """
    Best-effort extraction of "story" chunks from a page summary so the user can focus follow-up prompts.

    Works with common formats:
      - Numbered sections: "1) ...", "2. ...", "3 - ..."
      - Headings like "STORY 1:" or "ITEM 1:"
      - Otherwise falls back to paragraph blocks.
    Returns list of (label, text).
    """
    s = (summary_text or "").strip()
    if not s:
        return []

    # Normalize newlines
    s = s.replace("\r\n", "\n").replace("\r", "\n")

    # Prefer explicit numbered blocks
    # Split while keeping the number as a label when possible.
    numbered = list(re.finditer(r"(?m)^\s*(\d{1,2})\s*[\)\.\:\-]\s+", s))
    if len(numbered) >= 2:
        chunks: List[Tuple[str, str]] = []
        starts = [m.start() for m in numbered] + [len(s)]
        nums = [m.group(1) for m in numbered]
        for i in range(len(numbered)):
            block = s[starts[i]:starts[i+1]].strip()
            if block:
                label = f"Item {nums[i]}"
                chunks.append((label, block))
            if len(chunks) >= max_items:
                break
        return chunks

    # Headings like "STORY 1:" / "ITEM 1:"
    heading = list(re.finditer(r"(?m)^\s*(story|item)\s*(\d{1,2})\s*[:\-]\s*", s, flags=re.IGNORECASE))
    if len(heading) >= 2:
        chunks = []
        starts = [m.start() for m in heading] + [len(s)]
        labels = [f"{m.group(1).title()} {m.group(2)}" for m in heading]
        for i in range(len(heading)):
            block = s[starts[i]:starts[i+1]].strip()
            if block:
                chunks.append((labels[i], block))
            if len(chunks) >= max_items:
                break
        return chunks

    # Fallback: paragraph blocks (skip tiny ones)
    paras = [p.strip() for p in re.split(r"\n\s*\n+", s) if p.strip()]
    chunks = []
    for i, p in enumerate(paras[:max_items]):
        if len(p) < 120:
            continue
        label = f"Section {len(chunks)+1}"
        chunks.append((label, p))
    return chunks[:max_items]
def render_doc_chat_ui(
    *,
    namespace: str,
    doc_id: str,
    mode: str,
    title_hint: str,
    source_url: str,
    default_prompt: str = PDF_SUMMARY_PRESET_PROMPT,
    ocr_text: str = "",
    pdf_bytes: Optional[bytes] = None,
    ui_prefix: str = "",
):
    """Reusable doc chat UI.
    mode:
      - "ocr": initial prompt uses OCR text only (no PDF download/upload).
      - "pdf": initial prompt uses PDF (uploads once by sha; follow-ups cheap via previous_response_id).
    """
    if not openai_ready():
        if not OPENAI_API_KEY:
            st.info("Add OPENAI_API_KEY to .streamlit/secrets.toml to enable summaries.")
        elif not OPENAI_OK:
            st.info("Install OpenAI SDK:  python3 -m pip install openai")
        else:
            st.info("OpenAI is not ready.")
        return

    thread_key = make_thread_key(namespace=namespace, doc_id=doc_id)
    state = _get_thread_state(thread_key, title_hint=title_hint, source_url=source_url, doc_id=doc_id)

    # Prompt mode (separate from OCR/PDF mode). Changing this replaces the initial prompt text.
    prompt_key = _wk("oa_prompt", thread_key, ui_prefix)
    prompt_mode_key = _wk("oa_prompt_mode", thread_key, ui_prefix)

    def _apply_prompt_preset():
        sel = str(st.session_state.get(prompt_mode_key, "Summary + Deep dive (default)"))
        st.session_state[prompt_key] = DOC_MODE_PRESETS.get(sel, default_prompt)

    # Initialize prompt to the selected mode preset (only before widgets render)
    if prompt_mode_key not in st.session_state:
        st.session_state[prompt_mode_key] = "Summary + Deep dive (default)"
    if prompt_key not in st.session_state:
        st.session_state[prompt_key] = DOC_MODE_PRESETS.get(str(st.session_state[prompt_mode_key]), default_prompt)

    st.selectbox(
        "Prompt mode",
        options=list(DOC_MODE_PRESETS.keys()),
        index=list(DOC_MODE_PRESETS.keys()).index(str(st.session_state[prompt_mode_key]))
        if str(st.session_state[prompt_mode_key]) in DOC_MODE_PRESETS
        else 0,
        help="Choose a preset analysis mode. Changing this will replace the Initial prompt text box below.",
        key=prompt_mode_key,
        on_change=_apply_prompt_preset,
    )

    # Lock mode for this thread once it starts (so follow-ups remain consistent)
    if state.get("mode"):
        mode = state["mode"]
    else:
        state["mode"] = mode
        if mode == "ocr":
            state["ocr_len"] = len((ocr_text or "").strip())
        _save_thread_state(thread_key, state)

    model = st.selectbox(
        "Model",
        options=[DEFAULT_OPENAI_MODEL, "gpt-4o"],
        index=0,
        help="gpt-4o-mini is cheaper/faster; gpt-4o can be stronger on messy scans.",
        key=_wk("oa_model", thread_key, ui_prefix),
    )

    prompt = st.text_area(
        "Initial prompt (edit if you want)",
        value=default_prompt,
        height=170,
        key=prompt_key,
    )

    b1, b2, b3 = st.columns([0.42, 0.33, 0.25])
    with b1:
        run_initial = st.button("Run initial summary", use_container_width=True, key=_wk("oa_run_initial", thread_key, ui_prefix))
    with b2:
        new_thread = st.button("New thread (same item)", use_container_width=True, key=_wk("oa_new_thread", thread_key, ui_prefix))
    with b3:
        reset = st.button("Reset chat", use_container_width=True, key=_wk("oa_reset", thread_key, ui_prefix))

    if new_thread:
        salt = dt.datetime.utcnow().isoformat()
        fresh_doc_id = f"{doc_id}:{hashlib.md5(salt.encode()).hexdigest()[:6]}"  # nosec
        fresh_key = make_thread_key(namespace=namespace, doc_id=fresh_doc_id)
        fresh = _get_thread_state(fresh_key, title_hint=title_hint, source_url=source_url, doc_id=fresh_doc_id)
        fresh["mode"] = mode
        if mode == "ocr":
            fresh["ocr_len"] = len((ocr_text or "").strip())
        _save_thread_state(fresh_key, fresh)
        st.success("Started a new thread ✅")
        st.rerun()

    if reset:
        state["messages"] = []
        state["last_response_id"] = ""
        _save_thread_state(thread_key, state)
        st.rerun()

    # Status line
    if state.get("last_response_id"):
        st.success("Context loaded ✅ (follow-ups are cheap)")
    else:
        if mode == "ocr":
            st.info(f"OCR-first mode ✅ (no PDF upload). OCR chars: {state.get('ocr_len', 0):,}")
        else:
            st.info("PDF mode: first run uploads once, then follow-ups are cheap.")

    if run_initial:
        if mode == "ocr":
            if not (ocr_text or "").strip():
                st.error("No OCR text was available for this page.")
            else:
                with st.spinner("Summarizing OCR text…"):
                    out, resp_id = openai_first_pass_with_text(
                        model=model,
                        ocr_text=ocr_text,
                        prompt=prompt,
                        title_hint=title_hint,
                        source_url=source_url,
                    )
                if resp_id:
                    state["last_response_id"] = resp_id
                state["messages"].append({"role": "user", "text": prompt.strip()})
                state["messages"].append({"role": "assistant", "text": out})
                _save_thread_state(thread_key, state)
                st.rerun()
        else:
            b = pdf_bytes
            if not b and state.get("pdf_sha"):
                b = st.session_state.get("oa_pdf_bytes_by_sha", {}).get(state["pdf_sha"], b"")
            if not b or not is_pdf_bytes(b):
                st.error("Could not find valid PDF bytes for this document.")
            else:
                sha = pdf_sha256(b)
                state["pdf_sha"] = sha
                _ensure_openai_state()
                if sha not in st.session_state["oa_pdf_bytes_by_sha"]:
                    st.session_state["oa_pdf_bytes_by_sha"][sha] = b
                with st.spinner("Summarizing PDF…"):
                    out, resp_id, file_id = openai_first_pass_with_pdf(
                        model=model,
                        pdf_bytes=b,
                        prompt=prompt,
                        title_hint=title_hint,
                        source_url=source_url,
                    )
                if file_id:
                    state["file_id"] = file_id
                    st.session_state["oa_file_id_by_sha"][sha] = file_id
                if resp_id:
                    state["last_response_id"] = resp_id
                state["messages"].append({"role": "user", "text": prompt.strip()})
                state["messages"].append({"role": "assistant", "text": out})
                _save_thread_state(thread_key, state)
                st.rerun()

    # Display chat
    if state["messages"]:
        st.write("---")
        st.subheader("Conversation")
        for msg in state["messages"][-30:]:
            with st.chat_message(msg["role"]):
                st.write(msg["text"])

    # Follow-up form
    if state.get("last_response_id"):
        st.write("---")

        # Focus selector: pick a specific story/section from the latest assistant output
        latest_assistant = ""
        for _m in reversed(state.get("messages", [])):
            if _m.get("role") == "assistant":
                latest_assistant = str(_m.get("text") or "")
                break

        story_opts = extract_story_options(latest_assistant, max_items=12)
        focus_labels = ["Whole page"]
        focus_map: Dict[str, str] = {"Whole page": ""}

        for (lbl, txt) in story_opts:
            preview = re.sub(r"\s+", " ", txt).strip()
            preview = (preview[:120] + "…") if len(preview) > 120 else preview
            key_label = f"{lbl} — {preview}" if preview else lbl
            # Ensure uniqueness
            if key_label in focus_map:
                key_label = f"{key_label} ({len(focus_map)+1})"
            focus_labels.append(key_label)
            focus_map[key_label] = txt

        focus_pick = st.selectbox(
            "Focus follow-ups on",
            options=focus_labels,
            index=0,
            help="Optional: narrow your follow-up question to one item/section from the summary.",
            key=_wk("oa_focus_pick", thread_key, ui_prefix),
        )
        focus_text = focus_map.get(focus_pick, "")
        with st.form(key=_wk("oa_followup_form", thread_key, ui_prefix), clear_on_submit=True):
            q = st.text_area(
                "Follow-up question",
                value="",
                height=80,
                placeholder="Ask for more details, names, context…",
                key=_wk("oa_followup_text", thread_key, ui_prefix),
            )
            ask = st.form_submit_button("Ask follow-up")

        
        if ask and q.strip():
            user_q = q.strip()

            selected_prompt_mode = str(st.session_state.get(prompt_mode_key, "Summary + Deep dive (default)"))
            mode_instruction = ""
            if selected_prompt_mode == "Word-for-word transcription":
                mode_instruction = (
                    "Mode: Word-for-word transcription. Do NOT summarize. "
                    "Preserve wording/spelling/punctuation as printed as best as possible. "
                    "Use [unclear] where needed."
                )
            elif selected_prompt_mode == "Sports only":
                mode_instruction = "Mode: Sports only. Focus ONLY on sports content; otherwise say none found."
            elif selected_prompt_mode == "Extract names & roles":
                mode_instruction = "Mode: Extract names & roles. List people/places/organizations and short roles."
            elif selected_prompt_mode == "Timeline only":
                mode_instruction = "Mode: Timeline only. Produce a chronological timeline of dated events mentioned."
            elif selected_prompt_mode == "Historical analysis":
                mode_instruction = (
                    "Mode: Historical analysis. Emphasize context, framing/bias, and why-it-matters; keep factual."
                )
            else:
                mode_instruction = "Mode: Summary + deep dive. Keep responses aligned to the summary + historical relevance."

            send_q = user_q
            if focus_text:
                send_q = (
                    f"{mode_instruction}\n\n"
                    "Focus ONLY on the following section from the page summary. "
                    "If the user asks something outside it, say so and stay within the section.\n\n"
                    f"{focus_text}\n\nUser question: {user_q}"
                )
            else:
                send_q = f"{mode_instruction}\n\nUser question: {user_q}"

            state["messages"].append({"role": "user", "text": user_q})
            with st.spinner("Thinking…"):
                out, new_id = openai_followup(model=model, question=send_q, previous_response_id=state["last_response_id"])
            if new_id:
                state["last_response_id"] = new_id
            state["messages"].append({"role": "assistant", "text": out})
            _save_thread_state(thread_key, state)
            st.rerun()
# ----------------------------
# HIGH-RES PDF rendering helpers (crisp previews for crop)
# ----------------------------
@st.cache_data(show_spinner=False, ttl=60 * 60)
def render_pdf_page_to_pil(pdf_bytes: bytes, page_index: int = 0, scale: float = 2.7) -> Optional["Image.Image"]:
    if not (PIL_OK and Image is not None and PDFIUM_OK and pdfium is not None):
        return None
    try:
        doc = pdfium.PdfDocument(pdf_bytes)
        page = doc.get_page(page_index)
        bitmap = page.render(scale=float(scale))
        pil = bitmap.to_pil()
        page.close()
        doc.close()
        return pil.convert("RGB")
    except Exception:
        return None


@st.cache_data(show_spinner=False, ttl=60 * 60)
def fetch_best_page_pil(page_url: str, pdf_scale: float = 2.7) -> Optional["Image.Image"]:
    if not page_url:
        return None

    # Prefer PDF rendering (crisp) if we can fetch a real PDF
    b, meta = fetch_pdf_bytes_best(page_url)
    if b and meta.is_pdf and PDFIUM_OK:
        pil = render_pdf_page_to_pil(b, page_index=0, scale=float(pdf_scale))
        if pil is not None:
            return pil

    # Fallback to image_url JPEG
    img_url = fetch_loc_image_url(page_url)
    if img_url:
        return fetch_image_pil(img_url)

    return None


# ----------------------------
# OCR preprocessing + overlay helpers
# ----------------------------
def preprocess_for_ocr(
    img: "Image.Image",
    *,
    upscale: int = 1,
    grayscale: bool = True,
    autocontrast: bool = True,
    threshold: bool = False,
    thresh_value: int = 170,
) -> "Image.Image":
    out = img

    if upscale and upscale > 1:
        w, h = out.size
        out = out.resize((w * upscale, h * upscale), resample=Image.Resampling.LANCZOS)

    if grayscale:
        out = out.convert("L")

    if autocontrast:
        out = ImageOps.autocontrast(out)

    if threshold:
        out = out.point(lambda p: 255 if p > thresh_value else 0)

    return out


def ocr_image_region(img: "Image.Image", *, psm: int = 6) -> str:
    if not TESS_OK or pytesseract is None:
        raise RuntimeError("pytesseract is not installed")
    config = f"--oem 3 --psm {int(psm)}"
    return pytesseract.image_to_string(img, config=config) or ""


def make_overlay_preview(
    pil: "Image.Image",
    crop_box: Tuple[int, int, int, int],
    *,
    max_w: int = 1100,
) -> "Image.Image":
    if ImageDraw is None:
        return pil
    x1, y1, x2, y2 = crop_box
    w, h = pil.size

    scale = 1.0
    if w > max_w:
        scale = max_w / float(w)

    prev = pil
    if scale < 1.0:
        prev = pil.resize((int(w * scale), int(h * scale)), resample=Image.Resampling.LANCZOS)

    draw = ImageDraw.Draw(prev)
    rx1, ry1, rx2, ry2 = int(x1 * scale), int(y1 * scale), int(x2 * scale), int(y2 * scale)

    for i in range(0, 3):
        draw.rectangle([rx1 - i, ry1 - i, rx2 + i, ry2 + i], outline=(255, 0, 0))

    return prev


# ----------------------------
# PDF + Summarize panel (USED IN BOTH SEARCH + LIBRARY)
# ----------------------------
def render_pdf_panel_for_url(page_url: str, *, title_hint: str = "newspaper_page", key_prefix: str = ""):
    """Panel for a loc.gov newspaper page URL.
    OCR-first for summarization (cheapest), with PDF fallback only when needed.
    Also allows opening/downloading the PDF (download fetch is on-demand).
    """
    if not page_url:
        return

    pdf_url = fetch_loc_pdf_url(page_url) or ""
    if not pdf_url:
        st.info("No PDF URL could be determined for this page.")
        return

    # Open link (no download)
    st.link_button("Open PDF", pdf_url, use_container_width=True)

    # On-demand download (only fetch bytes if the user opens this expander)
    with st.expander("⬇️ Download PDF (fetches bytes)", expanded=False):
        pdf_bytes, meta = fetch_pdf_bytes_best(page_url)
        if pdf_bytes and meta.is_pdf:
            fname = safe_filename(title_hint, default="newspaper_page") + ".pdf"
            st.download_button(
                "Download PDF",
                data=pdf_bytes,
                file_name=fname,
                mime="application/pdf",
                use_container_width=True,
                key=f"{key_prefix}dl_pdf_{sha256_hex(str(meta.final_url or meta.pdf_url or pdf_url).encode('utf-8'))[:12]}",
            )
            st.caption(f"Downloaded {len(pdf_bytes):,} bytes from: {meta.final_url or meta.pdf_url or pdf_url}")
        else:
            st.warning("Could not download a valid PDF file for this page.")
            with st.expander("PDF download debug", expanded=False):
                st.write("Tried URL:", meta.pdf_url or pdf_url)
                st.write("Final URL:", meta.final_url)
                st.write("Content-Type:", meta.content_type)
                st.write("Bytes:", meta.bytes_len)
                st.write("Error:", meta.error or "n/a")
                if meta.snippet:
                    st.code(meta.snippet)

    # Summarize + follow-up chat (OCR-first)
    with st.expander("🤖 Summarize + follow-up chat (OCR-first, cheaper)", expanded=False):
        # Try OCR first (no PDF download/upload)
        with st.spinner("Checking loc.gov OCR text…"):
            ocr = fetch_page_ocr_text(page_url) or ""

        doc_id = stable_doc_id_from_url(page_url)

        if ocr_good_enough(ocr):
            st.caption(f"Using loc.gov OCR text ({len(ocr):,} chars). No PDF upload needed.")
            render_doc_chat_ui(
                namespace="loc_ocr",
                doc_id=doc_id,
                mode="ocr",
                title_hint=title_hint,
                source_url=page_url,
                ocr_text=ocr,
                pdf_bytes=None,
            )
        else:
            st.warning("OCR text looks missing/weak for this page. Trying OCR from the page image (no PDF)…")

            img_ocr = ""
            if PIL_OK and TESS_OK:
                with st.spinner("Running OCR on the best available page image…"):
                    img_ocr = fetch_image_ocr_text(page_url)
            else:
                st.caption("Image OCR fallback not available (install Pillow + pytesseract + Tesseract).")

            if ocr_good_enough(img_ocr):
                st.caption(f"Using image-based OCR ({len(img_ocr):,} chars). No PDF upload needed.")
                render_doc_chat_ui(
                    namespace="loc_img_ocr",
                    doc_id=doc_id,
                    mode="ocr",
                    title_hint=title_hint,
                    source_url=page_url,
                    ocr_text=img_ocr,
                    pdf_bytes=None,
                )
                return

            st.warning("Image OCR still looks weak. Falling back to PDF (will download + upload once).")
            with st.spinner("Fetching validated PDF bytes…"):
                pdf_bytes, meta = fetch_pdf_bytes_best(page_url)

            if not (pdf_bytes and meta.is_pdf):
                st.error("PDF fallback failed: could not fetch a valid PDF for this page.")
                with st.expander("PDF fallback debug", expanded=False):
                    st.write("Tried URL:", meta.pdf_url or pdf_url)
                    st.write("Final URL:", meta.final_url)
                    st.write("Content-Type:", meta.content_type)
                    st.write("Bytes:", meta.bytes_len)
                    st.write("Error:", meta.error or "n/a")
                    if meta.snippet:
                        st.code(meta.snippet)
                return
            render_doc_chat_ui(
                namespace="loc_pdf",
                doc_id=doc_id,
                mode="pdf",
                title_hint=title_hint,
                source_url=page_url,
                ocr_text="",
                pdf_bytes=pdf_bytes,
                ui_prefix=key_prefix,
            )
# ----------------------------
# Save-to-library (Search tab)
# ----------------------------
def render_save_to_library(item: Dict[str, Any]):
    if not supabase_ready():
        if not LIBRARY_OK:
            st.info("Library: supabase_links.py not found/importable (or missing functions).")
        elif not (SUPABASE_URL and SUPABASE_ANON_KEY):
            st.info("Library: Supabase is not configured in Streamlit secrets.")
        else:
            st.info("Library: Not ready.")
        return

    link = item_link(item)
    if not link:
        st.warning("This result has no URL, so it can't be saved.")
        return

    d = parse_item_date(item)
    default_year = d.year if d else 0

    with st.expander("⭐ Save this result to My Library (Supabase)", expanded=False):
        c1, c2, c3 = st.columns([1, 1, 1])

        with c1:
            cats = _cached_library_categories()
            cats_plus = cats + ["➕ Add new category…"]
            picked = st.selectbox("Category", cats_plus, index=0, key="lib_category_pick")
            if picked == "➕ Add new category…":
                new_cat = st.text_input("New category name", key="lib_new_category")
                category = (new_cat or "").strip() or "Uncategorized"
            else:
                category = picked

        with c2:
            year_val = st.number_input(
                "Year (content year)",
                min_value=0,
                max_value=3000,
                value=int(default_year),
                step=1,
                key="lib_year_val",
            )

        with c3:
            is_fav = st.checkbox("Favorite", value=True, key="lib_fav")

        tags_csv = st.text_input("Tags (comma-separated)", value="loc.gov,newspaper", key="lib_tags")
        notes = st.text_area("Notes (optional)", value="", key="lib_notes")

        chosen = st.session_state.get("chosen_date")
        chosen_str = chosen.isoformat() if isinstance(chosen, dt.date) else ""
        search_year = get_year()
        keyword = (st.session_state.get("keyword", "") or "").strip()
        require_kw = bool(st.session_state.get("require_keyword", False))
        meta_line = (
            f"Picked via This Day in History v{APP_VERSION} | selected_date={chosen_str} "
            f"| search_year={search_year} | keyword={keyword} | require_keyword={require_kw}"
        )

        if st.button("Save to Library", use_container_width=True, key="lib_save_btn"):
            tags = [t.strip() for t in (tags_csv or "").split(",") if t.strip()]
            if not tags:
                tags = ["loc.gov"]

            row = {
                "title": item_title(item),
                "url": link,
                "category": category,
                "tags": tags,
                "source": "loc.gov",
                "year": int(year_val) if year_val else None,
                "format": "Article",
                "notes": (notes.strip() + ("\n\n" if notes.strip() else "") + meta_line).strip(),
                "is_favorite": bool(is_fav),
            }

            res = insert_link(SUPABASE_URL, SUPABASE_ANON_KEY, LINKS_TABLE, row)

            if _looks_like_error_payload(res):
                st.error("Save failed (Supabase error):")
                st.json(res)
            else:
                st.success("Saved ✅")
                _clear_library_caches()


# ----------------------------
# Library Viewer Tab
# ----------------------------
def _row_matches_text(r: Dict[str, Any], q: str) -> bool:
    if not q:
        return True
    ql = q.lower()

    title = str(r.get("title") or "").lower()
    url = str(r.get("url") or "").lower()
    notes = str(r.get("notes") or "").lower()

    tags = r.get("tags")
    if isinstance(tags, list):
        tags_str = " ".join([str(x) for x in tags]).lower()
    else:
        tags_str = str(tags or "").lower()

    cat = str(r.get("category") or "").lower()
    src = str(r.get("source") or "").lower()

    blob = " | ".join([title, url, notes, tags_str, cat, src])
    return ql in blob


def _safe_dt(s: Any) -> str:
    return str(s or "")


def render_library_viewer():
    st.subheader("🔖 My Library")
    st.caption("Browse what you’ve saved. Use filters + search, then preview/crop/OCR articles.")

    if not supabase_ready():
        if not LIBRARY_OK:
            st.error("Library is not available: supabase_links.py not found/importable.")
        elif not (SUPABASE_URL and SUPABASE_ANON_KEY):
            st.error("Library is not configured: add SUPABASE_URL and SUPABASE_ANON_KEY in secrets.toml.")
        else:
            st.error("Library not ready.")
        return

    c1, c2, c3, c4 = st.columns([1.2, 0.8, 0.8, 1.2])
    with c1:
        cats = ["(any)"] + _cached_library_categories()
        cat_pick = st.selectbox("Category", cats, index=0, key="viewer_cat")
    with c2:
        year_txt = st.text_input("Year", value="", placeholder="e.g., 1955", key="viewer_year")
        year_val = int(year_txt) if year_txt.strip().isdigit() else None
    with c3:
        fav_only = st.checkbox("Favorites only", value=False, key="viewer_favs")
    with c4:
        sort_mode = st.selectbox(
            "Sort",
            ["Newest first", "Oldest first", "Year desc", "Year asc", "Title A→Z"],
            index=0,
            key="viewer_sort",
        )

    q = st.text_input("Search (title/notes/tags/url)", value="", key="viewer_q")
    fetch_limit = st.slider("Max rows to load", min_value=50, max_value=1000, value=300, step=50, key="viewer_limit")

    category = None if cat_pick == "(any)" else cat_pick

    rows = list_links(
        SUPABASE_URL,
        SUPABASE_ANON_KEY,
        LINKS_TABLE,
        year=year_val,
        category=category,
        favorite_only=fav_only,
        limit=int(fetch_limit),
    ) or []

    if _looks_like_error_payload(rows):
        st.error("Library fetch failed (Supabase error):")
        st.json(rows)
        return

    if not isinstance(rows, list):
        st.error("Unexpected library response.")
        st.write(rows)
        return

    filtered = [r for r in rows if isinstance(r, dict) and _row_matches_text(r, q.strip())]

    def sort_key(r: Dict[str, Any]):
        if sort_mode == "Title A→Z":
            return (str(r.get("title") or "").lower(),)
        if sort_mode == "Year asc":
            return (r.get("year") if r.get("year") is not None else -1, _safe_dt(r.get("created_at")))
        if sort_mode == "Year desc":
            return (-(r.get("year") if r.get("year") is not None else -1), _safe_dt(r.get("created_at")))
        if sort_mode == "Oldest first":
            return (_safe_dt(r.get("created_at")),)
        return (_safe_dt(r.get("created_at")),)

    reverse = sort_mode in ("Newest first", "Year desc")
    try:
        filtered.sort(key=sort_key, reverse=reverse)
    except Exception:
        pass

    st.write(f"Showing **{len(filtered)}** item(s).")

    if not filtered:
        st.info("No saved items match your filters/search.")
        return

    labels = []
    for r in filtered:
        y = r.get("year")
        ytxt = str(y) if y is not None else "—"
        cat2 = (r.get("category") or "—")
        title2 = (r.get("title") or "Untitled")
        labels.append(f"{ytxt} · {cat2} · {title2[:80]}")

    idx = st.selectbox("Pick an item", list(range(len(filtered))), format_func=lambda i: labels[i], key="viewer_pick")

    r = filtered[int(idx)]
    row_id = r.get("id", f"idx{idx}")
    keyp = f"lib_{row_id}_"

    title = r.get("title") or "Untitled"
    url = r.get("url") or ""
    year = r.get("year")
    cat = r.get("category") or ""
    tags = r.get("tags") or []
    notes = r.get("notes") or ""
    created_at = r.get("created_at") or ""

    st.markdown(f"### {title}")
    meta_cols = st.columns([1, 1, 1, 1])
    meta_cols[0].write(f"**Year:** {year if year is not None else '—'}")
    meta_cols[1].write(f"**Category:** {cat or '—'}")
    meta_cols[2].write(f"**Favorite:** {'✅' if r.get('is_favorite') else '—'}")
    meta_cols[3].write(f"**Saved:** {str(created_at)[:19] if created_at else '—'}")

    if url:
        st.link_button("Open article", url, use_container_width=True)

    if url:
        render_pdf_panel_for_url(url, title_hint=f"{title}_{year or ''}", key_prefix=f"lib_{row_id}_")

    if isinstance(tags, list):
        st.write("**Tags:**", ", ".join([str(t) for t in tags]) if tags else "—")
    else:
        st.write("**Tags:**", str(tags))

    if notes:
        with st.expander("Notes", expanded=True):
            st.write(notes)
    else:
        with st.expander("Notes", expanded=False):
            st.caption("No notes saved for this item.")

    # In-app preview (image + OCR)
    with st.expander("🧾 In-app preview (image + OCR)", expanded=False):
        st.caption("Loads the loc.gov page image (when available) and OCR text into the app.")
        preview_kw = st.text_input(
            "Highlight keyword in preview (optional)",
            value="",
            key=keyp + "preview_kw",
            placeholder="e.g., yankees",
        )

        pcols = st.columns([1, 1])
        with pcols[0]:
            load_image = st.checkbox("Load image preview", value=True, key=keyp + "load_img")
        with pcols[1]:
            load_ocr = st.checkbox("Load OCR preview", value=True, key=keyp + "load_ocr")

        if st.button("Load preview", use_container_width=True, key=keyp + "load_preview_btn"):
            if not url:
                st.warning("No URL for this item.")
            else:
                if load_image:
                    with st.spinner("Loading image…"):
                        img_url = fetch_loc_image_url(url)
                    if img_url:
                        st.image(img_url, use_container_width=True)
                        st.caption("Note: This preview may be low-res. Use the Crop section for high-res PDF rendering.")
                    else:
                        st.info("No page image available for this item via API (even after /item/ fallback).")

                if load_ocr:
                    with st.spinner("Loading OCR…"):
                        ocr = fetch_page_ocr_text(url)
                    if ocr:
                        st.markdown(highlight_html(ocr[:4000], (preview_kw or "").strip()), unsafe_allow_html=True)
                    else:
                        st.info("No OCR text available via API.")

    # Crop -> OCR -> Document (HIGH-RES)
    with st.expander("✂️ Crop image → OCR → Document (High-res)", expanded=False):
        st.caption(
            "This uses high-res PDF rendering when available (recommended for crisp text). "
            "Tip: crop a full column chunk, then tighten."
        )

        if not url:
            st.warning("This saved item has no URL.")
            return

        if not PIL_OK or Image is None or ImageOps is None:
            st.warning("This feature needs Pillow. Install: `python3 -m pip install pillow`")
            return

        if not TESS_OK or pytesseract is None:
            st.warning("This feature needs pytesseract. Install: `python3 -m pip install pytesseract`")
            st.info("You also need the Tesseract engine. On Mac: `brew install tesseract`")
            return

        if not PDFIUM_OK:
            st.info("For crisp pages, install PDF rendering: `python3 -m pip install pypdfium2`")

        pdf_scale = st.slider("PDF render quality", 1.5, 4.0, 2.7, 0.1, key=keyp + "pdf_scale")
        with st.spinner("Loading page image (prefers PDF)…"):
            pil = fetch_best_page_pil(url, pdf_scale=float(pdf_scale))

        if pil is None:
            st.error("Could not load a usable page image (PDF/JPEG).")
            return

        w, h = pil.size
        st.write(f"Image size: **{w} × {h}**")
        if w <= 900 or h <= 1200:
            st.warning(
                "This image still looks low-res. If you want crisp text, install `pypdfium2` "
                "and try increasing PDF render quality."
            )

        pc1, pc2, pc3 = st.columns([1.2, 1, 1])
        with pc1:
            psm = st.selectbox(
                "OCR mode (PSM)",
                options=[6, 4, 11, 7, 8],
                index=0,
                help="6=block/paragraph (best default), 4=columns, 11=sparse text, 7=single line, 8=single word",
                key=keyp + "psm",
            )
        with pc2:
            upscale = st.selectbox("Upscale", options=[1, 2, 3], index=1, help="Helps small print", key=keyp + "upscale")
        with pc3:
            thresh_on = st.checkbox("B/W threshold", value=True, help="Often helps old newsprint", key=keyp + "thresh_on")

        gc1, gc2, gc3 = st.columns([1, 1, 1])
        with gc1:
            grayscale = st.checkbox("Grayscale", value=True, key=keyp + "gray")
        with gc2:
            autocontrast = st.checkbox("Autocontrast", value=True, key=keyp + "ac")
        with gc3:
            thresh_val = st.slider("Threshold level", 80, 230, 160, 5, key=keyp + "thresh_val", disabled=not thresh_on)

        st.write("---")
        st.subheader("Crop box")

        cA, cB = st.columns(2)
        with cA:
            x1 = st.slider("x1", 0, w - 1, 0, key=keyp + "crop_x1")
            y1 = st.slider("y1", 0, h - 1, 0, key=keyp + "crop_y1")
        with cB:
            x2 = st.slider("x2", 1, w, min(w, max(1, w // 2)), key=keyp + "crop_x2")
            y2 = st.slider("y2", 1, h, min(h, max(1, h // 2)), key=keyp + "crop_y2")

        left = max(0, min(int(x1), int(x2) - 1))
        top = max(0, min(int(y1), int(y2) - 1))
        right = max(left + 1, max(int(x1) + 1, int(x2)))
        bottom = max(top + 1, max(int(y1) + 1, int(y2)))
        crop_box = (left, top, right, bottom)

        pw1, ph1, pw2, ph2 = (left / w) * 100, (top / h) * 100, (right / w) * 100, (bottom / h) * 100
        st.caption(f"Crop box as % of image: x {pw1:.1f}% → {pw2:.1f}% · y {ph1:.1f}% → {ph2:.1f}%")

        overlay = make_overlay_preview(pil, crop_box, max_w=1100)
        st.image(overlay, caption="Full page preview with crop rectangle", use_container_width=True)

        crop = pil.crop(crop_box)

        fit = st.checkbox(
            "Fit crop previews to page width (can look smudgy if crop is small)",
            value=False,
            key=keyp + "fit_crop",
        )
        if fit:
            st.image(crop, caption="Cropped region (raw)", use_container_width=True)
        else:
            st.image(crop, caption="Cropped region (raw)", width=min(1000, crop.size[0]))

        pre = preprocess_for_ocr(
            crop,
            upscale=int(upscale),
            grayscale=bool(grayscale),
            autocontrast=bool(autocontrast),
            threshold=bool(thresh_on),
            thresh_value=int(thresh_val),
        )

        if fit:
            st.image(pre, caption="Cropped region (preprocessed for OCR)", use_container_width=True)
        else:
            st.image(pre, caption="Cropped region (preprocessed for OCR)", width=min(1000, pre.size[0]))

        text_key = keyp + "last_crop_ocr_text"

        if st.button("Run OCR on this crop", use_container_width=True, key=keyp + "run_crop_ocr"):
            try:
                with st.spinner("Running OCR…"):
                    text = ocr_image_region(pre, psm=int(psm))
            except Exception as e:
                st.error(
                    "OCR failed. Make sure Tesseract is installed.\n\n"
                    "Mac: `brew install tesseract`\n\n"
                    f"Details: {type(e).__name__}: {e}"
                )
                text = ""

            if text.strip():
                st.session_state[text_key] = text
                st.success("OCR complete ✅")
            else:
                st.warning(
                    "No text detected. Try: bigger crop, PSM=6 or 4, Upscale=2, tweak threshold. "
                    "Also raise PDF render quality if the page looks soft."
                )

        txt = st.session_state.get(text_key, "")
        if txt:
            st.text_area("Extracted text", value=txt, height=260, key=keyp + "ocr_text_area")

            doc_name = st.text_input("Document filename", value="clipping.txt", key=keyp + "clip_doc_name")
            st.download_button(
                "⬇️ Download as .txt",
                data=txt.encode("utf-8"),
                file_name=doc_name if doc_name.endswith(".txt") else (doc_name + ".txt"),
                mime="text/plain",
                use_container_width=True,
                key=keyp + "dl_txt",
            )

            md = f"# Newspaper Clipping\n\nSource: {url}\n\n---\n\n{txt}\n"
            st.download_button(
                "⬇️ Download as .md",
                data=md.encode("utf-8"),
                file_name="clipping.md",
                mime="text/markdown",
                use_container_width=True,
                key=keyp + "dl_md",
            )


# ----------------------------
# Search Tab rendering
# ----------------------------
def render_item(item: Dict[str, Any], keyword: str):
    st.markdown(f"**{item_title(item)}**")
    st.write(f"Parsed date: **{item_date_str(item)}**")

    cols = st.columns([1, 1])
    with cols[0]:
        img = best_image_url(item)
        if img:
            st.image(img, use_container_width=True)
        else:
            st.info("No image_url found for this item (the link may still show the page).")

    with cols[1]:
        link = item_link(item)
        if link:
            st.link_button("Open item on loc.gov", link, use_container_width=True)
            render_pdf_panel_for_url(link, title_hint=f"{item_title(item)}_{item_date_str(item)}", key_prefix=f"search_{st.session_state.get('result_index',0)}_")

    render_save_to_library(item)

    link = item_link(item)
    ocr = fetch_page_ocr_text(link) if link else ""
    if ocr:
        st.markdown("**OCR text (highlighted):**" if (keyword or "").strip() else "**OCR text:**")
        st.markdown(highlight_html(ocr, keyword), unsafe_allow_html=True)
    else:
        st.info("No OCR text available for this page via API.")


def show_debug(dbg: Dict[str, Any]):
    if not dbg:
        return
    if dbg.get("error"):
        st.error(dbg["error"])
    with st.expander("Request URL", expanded=False):
        st.code(dbg.get("url", ""))
    if dbg.get("snippet"):
        with st.expander("Response snippet", expanded=False):
            st.code(dbg["snippet"])


def render_search_app():
    today = app_today_date()

    st.session_state.setdefault("chosen_date", today)
    st.session_state.setdefault("year_input", 1955)
    st.session_state.setdefault("front_pages_only", True)
    st.session_state.setdefault("keyword", "")
    st.session_state.setdefault("require_keyword", False)

    st.session_state.setdefault("result_index", 0)
    st.session_state.setdefault("last_exact_all", [])
    st.session_state.setdefault("last_exact_final", [])
    st.session_state.setdefault("last_query_url", "")
    st.session_state.setdefault("last_error", {})
    st.session_state.setdefault("using_near_matches", False)

    apply_params_if_present_once()
    apply_pending_before_widgets()

    if st.session_state.pop("auto_search_once", False):
        run_search_and_store()

    with st.sidebar:
        st.header("Controls")

        with st.expander("Supabase status (debug)", expanded=False):
            st.write("Helper import OK:", bool(LIBRARY_OK))
            st.write("Has SUPABASE_URL:", bool(SUPABASE_URL))
            st.write("Has SUPABASE_ANON_KEY:", bool(SUPABASE_ANON_KEY))
            st.write("Table:", LINKS_TABLE)

        with st.expander("Crisp images (debug)", expanded=False):
            st.write("Pillow:", bool(PIL_OK))
            st.write("pypdfium2:", bool(PDFIUM_OK))
            st.caption("Install for crisp crop images: `python3 -m pip install pypdfium2`")

        with st.expander("ChatGPT summary (debug)", expanded=False):
            st.write("OpenAI SDK:", bool(OPENAI_OK))
            st.write("Has OPENAI_API_KEY:", bool(OPENAI_API_KEY))
            st.caption("Enable summaries: add OPENAI_API_KEY to secrets + `pip install openai`")

        st.write("---")

        if st.button("Use today (America/New_York)", use_container_width=True):
            st.session_state["chosen_date"] = today
            st.session_state["result_index"] = 0
            st.rerun()

        st.date_input("Pick a date (month/day used)", key="chosen_date")

        ycols = st.columns([1, 1])
        with ycols[0]:
            if st.button("🎲 Random year", use_container_width=True):
                st.session_state["pending_year_input"] = random.randint(1690, 1963)
                st.session_state["pending_result_index"] = 0
                st.session_state["auto_search_once"] = True
                st.rerun()
        with ycols[1]:
            st.number_input("Year", min_value=1690, max_value=1963, step=1, key="year_input")

        st.checkbox("Front pages only", key="front_pages_only")
        st.text_input("Keyword (optional)", key="keyword")
        st.checkbox(
            "Require keyword (filter results)",
            key="require_keyword",
            help="If enabled, we only keep pages whose OCR actually contains the keyword.",
        )

        st.write("---")
        st.subheader("Surprise me")

        sm_cols = st.columns([1, 1])
        with sm_cols[0]:
            st.checkbox("Random topic keyword", value=True, key="surprise_topic")
        with sm_cols[1]:
            st.checkbox("Auto-require keyword", value=False, key="surprise_require")

        if st.button("✨ Surprise me", use_container_width=True):
            st.session_state["pending_year_input"] = random.randint(1690, 1963)
            if bool(st.session_state.get("surprise_topic", True)):
                st.session_state["pending_keyword"] = random.choice(TOPIC_PRESETS)
            if bool(st.session_state.get("surprise_require", False)):
                st.session_state["pending_require_keyword"] = True
            st.session_state["pending_result_index"] = 0
            st.session_state["auto_search_once"] = True
            st.rerun()

        st.write("---")
        if st.button("Search this date", use_container_width=True):
            run_search_and_store()
            st.rerun()

    chosen: dt.date = st.session_state["chosen_date"]
    st.markdown(f"### Selected: **{chosen.strftime('%B %d, %Y')}** · Year: **{get_year()}**")

    dbg = st.session_state.get("last_error", {}) or {}
    if dbg:
        show_debug(dbg)

    exact_all: List[Dict[str, Any]] = st.session_state.get("last_exact_all", []) or []
    exact: List[Dict[str, Any]] = st.session_state.get("last_exact_final", []) or []

    keyword = (st.session_state.get("keyword", "") or "").strip()
    require_keyword = bool(st.session_state.get("require_keyword", False))

    if not exact_all and not exact:
        st.info("No matches loaded yet. Use the sidebar and click **Search this date** (or **Surprise me**).")
        return

    if bool(st.session_state.get("using_near_matches")):
        st.warning("No exact month/day matches were found for that year. Showing *near matches* from a wider date window instead.")
        wide = st.session_state.get("last_query_url_wide")
        if wide:
            st.caption("Widened-window query was used to find near matches.")

    if keyword:
        if require_keyword:
            st.write(f"Exact matches on this day: **{len(exact_all)}** → Keyword matches: **{len(exact)}**")
        else:
            st.write(f"Exact matches on this day: **{len(exact_all)}** (keyword not filtering; only highlighting)")

    if keyword:
        with st.expander("Keyword diagnostics", expanded=False):
            st.caption(
                "This checks the OCR text for the first pages in the exact-date set, "
                "and shows ✅/❌ plus a short excerpt around the first match."
            )
            limit = st.slider("How many pages to check", min_value=5, max_value=30, value=12, step=1)
            show_keyword_diagnostics(exact_all, keyword, limit=limit)

    if not exact:
        st.warning("No results after filtering. Try unchecking **Require keyword** or use a different keyword.")
        return

    n = len(exact)
    idx = int(st.session_state.get("result_index", 0))
    idx = max(0, min(idx, n - 1))
    st.session_state["result_index"] = idx

    top = st.columns([1, 2, 1])
    with top[0]:
        if st.button("⬅️ Prev", use_container_width=True, disabled=(idx == 0)):
            st.session_state["result_index"] = idx - 1
            st.rerun()
    with top[1]:
        labels = [f"{i+1}/{n} — {item_date_str(exact[i])} — {item_title(exact[i])[:70]}" for i in range(n)]
        choice = st.selectbox("Pick a result", list(range(n)), format_func=lambda i: labels[i], index=idx)
        if choice != idx:
            st.session_state["result_index"] = int(choice)
            st.rerun()
    with top[2]:
        if st.button("Next ➡️", use_container_width=True, disabled=(idx >= n - 1)):
            st.session_state["result_index"] = idx + 1
            st.rerun()

    st.caption(f"Showing result **{idx+1} of {n}**")
    if st.session_state.get("last_query_url"):
        st.link_button("Open query used", st.session_state["last_query_url"])

    render_item(exact[idx], keyword)

    st.write("---")
    st.subheader("Save / Share")

    share_params = build_share_params()
    st.query_params.update(share_params)
    st.caption("Copy the URL from your browser bar — it now includes the share parameters.")

    with st.expander("Share parameters (debug)", expanded=False):
        st.json(share_params)

    pick_json = current_pick_payload()
    pick_md = current_pick_markdown()

    dcols = st.columns(2)
    with dcols[0]:
        st.download_button(
            "⬇️ Download pick (Markdown)",
            data=pick_md.encode("utf-8"),
            file_name="this_day_in_history_pick.md",
            mime="text/markdown",
            use_container_width=True,
        )
    with dcols[1]:
        st.download_button(
            "⬇️ Download pick (JSON)",
            data=json.dumps(pick_json, indent=2).encode("utf-8"),
            file_name="this_day_in_history_pick.json",
            mime="application/json",
            use_container_width=True,
        )


# ----------------------------
# PDF Summarizer Tab (Upload PDF and run preset)
# ----------------------------
def render_pdf_summarizer_tab():
    st.subheader("📄 Summarizer")
    st.caption("Summarize a loc.gov page via OCR-first (cheapest) with PDF fallback, or summarize an uploaded/direct-PDF.")

    mode = st.radio(
        "Choose a source",
        ["loc.gov page URL (OCR-first)", "Upload a PDF", "Direct PDF URL"],
        index=0,
        horizontal=True,
        key="sum_mode",
    )

    if mode == "loc.gov page URL (OCR-first)":
        url = st.text_input(
            "loc.gov page URL",
            value="",
            placeholder="https://www.loc.gov/resource/.../?sp=1   or   https://www.loc.gov/item/.../?sp=1",
            key="sum_loc_url",
        ).strip()

        if not url:
            st.info("Paste a loc.gov page URL to begin.")
            return

        title_hint = safe_filename(url.split("/")[-2] if url.endswith("/") else url.split("/")[-1], default="locgov_page")
        doc_id = stable_doc_id_from_url(url)

        with st.spinner("Loading loc.gov OCR text…"):
            ocr = fetch_page_ocr_text(url) or ""

        if ocr_good_enough(ocr):
            st.caption(f"Using loc.gov OCR ({len(ocr):,} chars). No PDF download/upload needed.")
            render_doc_chat_ui(
                namespace="sum_loc_ocr",
                doc_id=doc_id,
                mode="ocr",
                title_hint=title_hint,
                source_url=url,
                ocr_text=ocr,
                pdf_bytes=None,
            )
            return

        st.warning("OCR looks weak/missing for this page. Falling back to PDF (downloads + uploads once).")
        with st.spinner("Fetching validated PDF bytes…"):
            pdf_bytes, meta = fetch_pdf_bytes_best(url)

        if not (pdf_bytes and meta.is_pdf):
            st.error("Could not fetch a valid PDF from that loc.gov page URL.")
            with st.expander("Fetch debug", expanded=False):
                st.write("Tried URL:", meta.pdf_url or url)
                st.write("Final URL:", meta.final_url)
                st.write("Content-Type:", meta.content_type)
                st.write("Bytes:", meta.bytes_len)
                st.write("Error:", meta.error or "n/a")
                if meta.snippet:
                    st.code(meta.snippet)
            return

        render_doc_chat_ui(
            namespace="sum_loc_pdf",
            doc_id=doc_id,
            mode="pdf",
            title_hint=title_hint,
            source_url=url,
            ocr_text="",
            pdf_bytes=pdf_bytes,
                ui_prefix=key_prefix,
        )
        return

    if mode == "Upload a PDF":
        up = st.file_uploader("Upload a PDF", type=["pdf"], accept_multiple_files=False)
        if up is None:
            st.info("Upload a .pdf to begin.")
            return

        pdf_bytes = up.read() or b""
        if not is_pdf_bytes(pdf_bytes):
            st.error("This upload does not look like a valid PDF (missing %PDF header).")
            return

        sha = pdf_sha256(pdf_bytes)
        doc_id = f"pdf:{sha}"
        st.write(f"Loaded: **{up.name}** · {len(pdf_bytes):,} bytes")

        render_doc_chat_ui(
            namespace="sum_upload",
            doc_id=doc_id,
            mode="pdf",
            title_hint=up.name,
            source_url="(uploaded PDF)",
            ocr_text="",
            pdf_bytes=pdf_bytes,
                ui_prefix=key_prefix,
        )
        return

    # Direct PDF URL
    url = st.text_input(
        "Direct PDF URL",
        value="",
        placeholder="https://chroniclingamerica.loc.gov/lccn/.../seq-1.pdf",
        key="sum_pdf_url",
    ).strip()

    if not url:
        st.info("Paste a direct PDF URL to begin.")
        return

    with st.spinner("Downloading…"):
        b, meta = _download_url_bytes(url, timeout=60)

    if not (b and meta.is_pdf):
        st.error("That URL did not return a valid PDF.")
        with st.expander("Fetch debug", expanded=False):
            st.write("Tried URL:", meta.pdf_url or url)
            st.write("Final URL:", meta.final_url)
            st.write("Content-Type:", meta.content_type)
            st.write("Bytes:", meta.bytes_len)
            st.write("Error:", meta.error or "n/a")
            if meta.snippet:
                st.code(meta.snippet)
        return

    title_hint = safe_filename((meta.final_url or url).split("/")[-1] or "pdf")
    sha = pdf_sha256(b)
    doc_id = f"pdf:{sha}"

    st.write(f"Loaded: **{title_hint}** · {len(b):,} bytes")
    render_doc_chat_ui(
        namespace="sum_pdfurl",
        doc_id=doc_id,
        mode="pdf",
        title_hint=title_hint,
        source_url=meta.final_url or url,
        ocr_text="",
        pdf_bytes=b,
    )



# ----------------------------
# This Date in History (Wikipedia "On this day")
# ----------------------------
@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def fetch_onthisday(month: int, day: int) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """
    Fetch "On this day" data for Wikipedia (English) for the given month/day.

    Uses Wikimedia Feed API (preferred) and falls back to RESTBase endpoints if needed.

    Returns: (payload_or_none, debug_dict)
    """
    mm = int(month)
    dd = int(day)
    mm2 = f"{mm:02d}"
    dd2 = f"{dd:02d}"

    debug: Dict[str, Any] = {
        "ok": False,
        "status": None,
        "content_type": None,
        "error": None,
        "url": None,
        "snippet": None,
        "fallback_used": None,
    }

    ctx = ssl.create_default_context(cafile=certifi.where())
    headers = {
        "User-Agent": f"ThisDayInHistoryStreamlit/{APP_VERSION}",
        "Accept": "application/json",
    }

    # Preferred: Wikimedia Feed API
    urls = [
        f"https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/all/{mm2}/{dd2}",
        # Fallbacks: RESTBase endpoints (some installations are picky about padding)
        f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/all/{mm2}/{dd2}",
    ]

    for u in urls:
        debug["url"] = u
        req = urllib.request.Request(u, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                raw = resp.read()
                debug["status"] = getattr(resp, "status", None)
                debug["content_type"] = resp.headers.get("Content-Type", "") or ""
        except urllib.error.HTTPError as e:
            debug["status"] = e.code
            debug["error"] = f"HTTPError {e.code}: {e.reason}"
            try:
                raw_err = e.read()
                debug["snippet"] = raw_err[:600].decode("utf-8", errors="replace")
            except Exception:
                pass
            continue
        except Exception as e:
            debug["error"] = f"{type(e).__name__}: {e}"
            continue

        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
            if isinstance(payload, dict):
                debug["ok"] = True
                debug["error"] = None
                debug["snippet"] = None
                debug["fallback_used"] = u
                return payload, debug
        except Exception as e:
            debug["error"] = f"JSON parse failed: {e}"
            debug["snippet"] = raw[:600].decode("utf-8", errors="replace")
            continue

    # If /all is unavailable, try category endpoints and assemble
    assembled: Dict[str, Any] = {"events": [], "births": [], "deaths": [], "holidays": []}
    cat_urls = {
        "events": f"https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/events/{mm2}/{dd2}",
        "births": f"https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/births/{mm2}/{dd2}",
        "deaths": f"https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/deaths/{mm2}/{dd2}",
        "holidays": f"https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/holidays/{mm2}/{dd2}",
    }
    any_ok = False
    last_error = None
    for k, u in cat_urls.items():
        req = urllib.request.Request(u, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                raw = resp.read()
            payload = json.loads(raw.decode("utf-8", errors="replace"))
            if isinstance(payload, dict) and isinstance(payload.get(k), list):
                assembled[k] = payload.get(k) or []
                any_ok = True
        except Exception as e:
            last_error = e

    if any_ok:
        debug["ok"] = True
        debug["fallback_used"] = "category_endpoints"
        debug["url"] = "api.wikimedia.org/feed/v1/wikipedia/en/onthisday/<category>/MM/DD"
        debug["error"] = None
        return assembled, debug

    debug["error"] = debug["error"] or (f"{type(last_error).__name__}: {last_error}" if last_error else "Unknown error")
    return None, debug



def _format_onthisday_item(it: Dict[str, Any]) -> str:
    year = it.get("year")
    txt = (it.get("text") or "").strip()
    pages = it.get("pages") if isinstance(it.get("pages"), list) else []
    # add 1-2 reference titles (no raw URLs; keep tidy)
    refs: List[str] = []
    for p in pages[:2]:
        if isinstance(p, dict):
            title = (p.get("title") or "").strip()
            if title:
                refs.append(title)
    ref_txt = f" — _({', '.join(refs)})_" if refs else ""
    y = f"**{year}** — " if year is not None else ""
    return f"{y}{txt}{ref_txt}"


def render_this_date_in_history_tab():
    st.subheader("📅 This Date in History")
    st.caption("Key historical events, births, deaths, and holidays for the selected month/day.")

    # Use a separate widget key to avoid collision with the Search tab's chosen_date widget.
    # Provide a button to sync back to the main chosen_date via a Streamlit-safe callback.
    chosen: dt.date = st.session_state.get("chosen_date") or app_today_date()

    def _sync_to_main():
        # Streamlit-safe: this runs as a callback
        st.session_state["chosen_date"] = st.session_state.get("history_date_picker", chosen)

    cols = st.columns([1.2, 1])
    with cols[0]:
        picked = st.date_input("Date (month/day)", value=chosen, key="history_date_picker", on_change=_sync_to_main)
    with cols[1]:
        st.button("Use this date in Search", on_click=_sync_to_main, use_container_width=True)

    month = picked.month
    day = picked.day

    payload, dbg = fetch_onthisday(month, day)
    if not payload:
        st.error("Could not load 'This Date in History' data.")
        with st.expander("Debug", expanded=False):
            st.write("URL:", dbg.get("url"))
            st.write("Status:", dbg.get("status"))
            st.write("Content-Type:", dbg.get("content_type"))
            st.write("Error:", dbg.get("error"))
            if dbg.get("snippet"):
                st.code(dbg.get("snippet"))
        return

    # Payload may be "all" format or assembled categories
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    births = payload.get("births") if isinstance(payload.get("births"), list) else []
    deaths = payload.get("deaths") if isinstance(payload.get("deaths"), list) else []
    holidays = payload.get("holidays") if isinstance(payload.get("holidays"), list) else []

    def _fmt_item(it: Dict[str, Any]) -> str:
        # Wikimedia feed items commonly use 'year' + 'text'
        year = it.get("year")
        text = it.get("text") or it.get("pages", [{}])[0].get("extract", "")
        if year is not None:
            return f"**{year}** — {text}"
        return str(text or "").strip()

    if holidays:
        st.markdown("### Holidays & observances")
        for it in holidays[:12]:
            if isinstance(it, dict):
                s = _fmt_item(it)
                if s:
                    st.markdown(f"- {s}")

    st.markdown("### Events")
    if events:
        for it in events[:20]:
            if isinstance(it, dict):
                s = _fmt_item(it)
                if s:
                    st.markdown(f"- {s}")
    else:
        st.info("No events found for this date.")

    st.markdown("### Births")
    if births:
        for it in births[:18]:
            if isinstance(it, dict):
                s = _fmt_item(it)
                if s:
                    st.markdown(f"- {s}")

    st.markdown("### Deaths")
    if deaths:
        for it in deaths[:18]:
            if isinstance(it, dict):
                s = _fmt_item(it)
                if s:
                    st.markdown(f"- {s}")

    with st.expander("Source/debug", expanded=False):
        st.caption("Data from Wikimedia 'On this day' feed (English Wikipedia).")
        st.write("Fetch method:", dbg.get("fallback_used") or "unknown")
        st.write("URL:", dbg.get("url"))

def main():
    st.set_page_config(page_title="This Day in History — Newspapers", layout="wide")
    st.title("🗞️ This Day in History — Newspapers")
    st.caption(f"App v{APP_VERSION}")

    tab_search, tab_history, tab_library, tab_pdf = st.tabs(["🗞️ Search", "📅 This Date in History", "🔖 My Library", "📄 PDF Summarizer"])

    with tab_search:
        render_search_app()

    with tab_history:
        render_this_date_in_history_tab()

    with tab_library:
        render_library_viewer()

    with tab_pdf:
        render_pdf_summarizer_tab()


if __name__ == "__main__":
    main()
