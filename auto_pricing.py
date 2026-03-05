# AI HOTEL AUTO PRICING ENGINE – PRODUCTION READY
# ============================================================

import os
import re
import time
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Tuple, Any, Dict

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import APIRouter, FastAPI, HTTPException, Query
from dotenv import load_dotenv
import requests

# ============================================================
# LOGGING (never log API keys or secrets)
# ============================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("pricing")

# ============================================================
# LOAD ENV & CONFIG
# ============================================================

load_dotenv()

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "").strip(),
    "port": os.getenv("DB_PORT", "5432").strip(),
    "database": os.getenv("DB_NAME", "").strip(),
    "user": os.getenv("DB_USER", "").strip(),
    "password": os.getenv("DB_PASSWORD", "").strip(),
}

# Algorithm version – returned in preview/apply for audit trail
ALGO_VERSION = os.getenv("PRICING_ALGO_VERSION", "v1.0.0").strip() or "v1.0.0"

# Pricing strategy (tweak via env without redeploy)
MAX_UP = float(os.getenv("PRICING_MAX_UP", "0.25"))
MAX_DOWN = float(os.getenv("PRICING_MAX_DOWN", "0.20"))
COMPETITOR_PRICE_MIN = float(os.getenv("PRICING_COMPETITOR_MIN", "10"))
COMPETITOR_PRICE_MAX = float(os.getenv("PRICING_COMPETITOR_MAX", "500000"))
COMPETITOR_CACHE_TTL_SEC = int(os.getenv("PRICING_COMPETITOR_CACHE_TTL", "1800"))  # 30 min
HTTP_RETRIES = int(os.getenv("PRICING_HTTP_RETRIES", "3"))
HTTP_RETRY_BACKOFF = float(os.getenv("PRICING_HTTP_RETRY_BACKOFF", "1.0"))

# Room-type strategic floors (env override: PRICING_FLOOR_SINGLE=1000, etc.)
_ROOM_TYPE_FLOOR_DEFAULTS = {"single": 1000, "double": 2500, "triple": 4500, "kk": 5000}
ROOM_TYPE_FLOOR: Dict[str, float] = {}
for k, v in _ROOM_TYPE_FLOOR_DEFAULTS.items():
    ROOM_TYPE_FLOOR[k] = float(os.getenv(f"PRICING_FLOOR_{k.upper()}", str(v)))

# Live competitor prices
COMPETITOR_PRICES_API_URL = os.getenv("COMPETITOR_PRICES_API_URL", "").strip()
COMPETITOR_PRICES_API_KEY = os.getenv("COMPETITOR_PRICES_API_KEY", "").strip()
COMPETITOR_PRICES_TIMEOUT = int(os.getenv("COMPETITOR_PRICES_TIMEOUT", "10"))
SERPAPI_KEY = os.getenv("SERPAPI_KEY", "").strip()
COMPETITOR_GL = os.getenv("COMPETITOR_GL", "us").strip() or "us"
COMPETITOR_CURRENCY = os.getenv("COMPETITOR_CURRENCY", "USD").strip() or "USD"

# Env validation at startup (fail fast for required DB)
def _validate_env() -> None:
    if not DB_CONFIG["host"] or not DB_CONFIG["database"]:
        raise RuntimeError("DB_HOST and DB_NAME are required. Set in .env.")

_validate_env()

app = FastAPI(title="AI Hotel Dynamic Pricing Engine")
router = APIRouter(tags=["Auto Pricing"])

# In-memory competitor cache: key -> (result, expiry_ts)
_competitor_cache: Dict[str, Tuple[Any, float]] = {}

# ============================================================
# DATABASE CONNECTION
# ============================================================

def db():
    """Single DB connection method. Use as context: with db() as conn, conn.cursor() as cur."""
    try:
        return psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
    except Exception as e:
        logger.exception("Database connection failed")
        raise RuntimeError(f"Database connection failed: {e}") from e

# ============================================================
# HEALTH
# ============================================================

@router.get("/health")
def health():
    return {"status": "RUNNING", "time": datetime.utcnow(), "algo_version": ALGO_VERSION}

# ============================================================
# HOTELS
# ============================================================

@router.get("/api/hotels")
def list_hotels():
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT id, name FROM "Hotel" ORDER BY name')
        return cur.fetchall()

# ============================================================
# OCCUPANCY
# ============================================================

def occupancy_ratio(hotel_id: str) -> float:
    """Occupancy ratio in [0, 1]. Returns 0 if no rooms or null."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT
              COUNT(*) FILTER (WHERE r."frontOfficeStatus"='O')::float
              / NULLIF(COUNT(*),0) AS occ
            FROM "Room" r
            JOIN "HotelFloor" hf ON hf.id = r."floorId"
            WHERE hf."hotelId" = %s
        """, (hotel_id,))
        row = cur.fetchone()
        occ = float(row["occ"] or 0.0)
        return max(0.0, min(1.0, occ))

# ============================================================
# VALIDATION LAYER
# ============================================================

def validate_base_price(base_price: float) -> None:
    """Raise HTTPException if base price invalid (must be > 0)."""
    if not isinstance(base_price, (int, float)) or base_price <= 0:
        raise HTTPException(status_code=400, detail="Base price must be greater than 0")

def validate_occupancy(occ: float) -> float:
    """Clamp occupancy to [0, 1]. Returns valid value."""
    if occ is None or not isinstance(occ, (int, float)):
        return 0.0
    return max(0.0, min(1.0, float(occ)))

def validate_competitor_price(comp: float) -> float:
    """Sanity range for competitor price. Returns 0 if invalid (no exception)."""
    if comp is None or not isinstance(comp, (int, float)) or comp < 0:
        return 0.0
    if comp < COMPETITOR_PRICE_MIN or comp > COMPETITOR_PRICE_MAX:
        return 0.0
    return float(comp)

# ============================================================
# AUDIT LOG (structured log; optional DB table later)
# ============================================================

def log_pricing_audit(
    hotel_id: str,
    room_type: str,
    room_type_id: Optional[str],
    old_price: float,
    new_price: float,
    occupancy: float,
    competitor_price: float,
) -> None:
    """Log every price change for traceability. Never log API keys."""
    logger.info(
        "pricing_event hotel_id=%s room_type=%s room_type_id=%s old_price=%s new_price=%s occupancy=%s competitor=%s algo=%s",
        hotel_id, room_type, room_type_id or "", old_price, new_price, occupancy, competitor_price, ALGO_VERSION,
        extra={
            "event": "pricing_audit",
            "hotel_id": hotel_id,
            "room_type": room_type,
            "old_price": old_price,
            "new_price": new_price,
            "occupancy": occupancy,
            "competitor_price": competitor_price,
            "algo_version": ALGO_VERSION,
            "timestamp": datetime.utcnow().isoformat(),
        },
    )

# ============================================================
# HTTP RETRY WITH BACKOFF
# ============================================================

def _get_with_retry(url: str, params: dict, timeout: int) -> requests.Response:
    """GET with retries and exponential backoff. Prevents blocking on transient failures."""
    last_exc: Optional[Exception] = None
    for attempt in range(HTTP_RETRIES):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            return r
        except Exception as e:
            last_exc = e
            logger.warning("Competitor fetch attempt %s failed: %s", attempt + 1, e)
            if attempt < HTTP_RETRIES - 1:
                time.sleep(HTTP_RETRY_BACKOFF * (2 ** attempt))
    if last_exc:
        logger.exception("Competitor fetch failed after retries")
        raise last_exc
    raise RuntimeError("Competitor fetch failed")

# ============================================================
# LIVE COMPETITOR PRICES (real-time from web/API)
# ============================================================

def _get_hotel_name_for_search(hotel_id: str) -> Optional[str]:
    """Fetch hotel name from DB for use in search-based competitor lookup."""
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute('SELECT name FROM "Hotel" WHERE id = %s', (hotel_id,))
            row = cur.fetchone()
            return row.get("name") if row else None
    except Exception:
        return None


def _fetch_live_competitor_from_api(hotel_id: str) -> Tuple[Optional[float], List[str]]:
    """
    Call external API that returns real-time competitor prices.
    Returns (avg_price, []) - no competitor names from custom API.
    """
    if not COMPETITOR_PRICES_API_URL:
        return None, []
    url = COMPETITOR_PRICES_API_URL.replace("{hotel_id}", str(hotel_id))
    headers = {}
    if COMPETITOR_PRICES_API_KEY:
        headers["X-API-Key"] = COMPETITOR_PRICES_API_KEY
    try:
        r = requests.get(url, headers=headers or None, timeout=COMPETITOR_PRICES_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, (int, float)):
            return float(data), []
        if isinstance(data, dict):
            for key in ("avg", "average", "competitorAvg", "mean_price"):
                if key in data and data[key] is not None:
                    return float(data[key]), []
            if "prices" in data and isinstance(data["prices"], list):
                nums = [float(x) for x in data["prices"] if isinstance(x, (int, float))]
                return (sum(nums) / len(nums) if nums else None), []
            if "data" in data and isinstance(data["data"], dict):
                for key in ("avg", "average", "competitorAvg"):
                    if key in data["data"] and data["data"][key] is not None:
                        return float(data["data"][key]), []
        return None, []
    except Exception:
        logger.exception("Competitor fetch failed for hotel_id=%s", hotel_id)
        return None, []


def _is_own_hotel(name: str, own_hotel_name: Optional[str]) -> bool:
    """True if name is the same as or contained in own hotel (exclude from competitor list)."""
    if not own_hotel_name or not name:
        return False
    own = own_hotel_name.strip().lower()
    n = name.strip().lower()
    return own in n or n in own


def _fetch_google_hotels_by_query(own_name: str, search_query: str) -> Tuple[Optional[float], List[str], List[dict]]:
    """
    Call SerpAPI Google Hotels with a specific query.
    Returns (avg_price, list of competitor hotel names, list of {name, price} for each competitor).
    Excludes own hotel from names. competitorHotels: [{"name": "Taj Hotel", "price": 2300}, ...].
    """
    if not SERPAPI_KEY or not own_name:
        return None, [], []
    today = datetime.utcnow().date()
    check_in = today.strftime("%Y-%m-%d")
    check_out = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        params = {
            "engine": "google_hotels",
            "q": search_query,
            "api_key": SERPAPI_KEY,
            "gl": COMPETITOR_GL,
            "hl": "en",
            "currency": COMPETITOR_CURRENCY,
            "check_in_date": check_in,
            "check_out_date": check_out,
            "adults": 2,
            "children": 0,
        }
        r = _get_with_retry("https://serpapi.com/search", params, COMPETITOR_PRICES_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        numbers = []
        names: List[str] = []
        offerings: List[dict] = []  # [{"name": "Taj Hotel", "price": 2300}, ...]
        for ad in data.get("ads", [])[:15]:
            p = ad.get("extracted_price")
            name = str(ad["name"]).strip() if ad.get("name") else None
            if name and _is_own_hotel(name, own_name):
                continue
            if isinstance(p, (int, float)) and 10 < p < 500000:
                numbers.append(float(p))
                if name:
                    names.append(name)
                    offerings.append({"name": name, "price": round(float(p), 2)})
            elif name:
                names.append(name)
        for prop in data.get("properties", [])[:15]:
            rpn = prop.get("rate_per_night") or {}
            low = rpn.get("extracted_lowest") or prop.get("extracted_lowest")
            name = str(prop["name"]).strip() if prop.get("name") else None
            if name and _is_own_hotel(name, own_name):
                continue
            if isinstance(low, (int, float)) and 10 < low < 500000:
                numbers.append(float(low))
                if name:
                    names.append(name)
                    offerings.append({"name": name, "price": round(float(low), 2)})
            elif name:
                names.append(name)
        avg = sum(numbers) / len(numbers) if numbers else None
        return avg, list(dict.fromkeys(names)), offerings
    except Exception:
        logger.exception("Competitor fetch failed for query=%s", search_query[:50] if search_query else "")
        return None, [], []


def _competitor_avg_for_room_type(offerings: List[dict], our_base_price: float) -> Tuple[float, List[str], List[dict]]:
    """
    From competitor name+price list, pick prices in a range relevant to our room type (e.g. single ~1000 -> use 500–2500).
    Returns (avg, list of names used, list of offerings in range). If no offerings in range, use all.
    """
    if not offerings:
        return 0.0, [], []
    low = max(10, our_base_price * 0.4)
    high = min(500000, our_base_price * 2.5)
    in_range = [o for o in offerings if low <= o.get("price", 0) <= high]
    use = in_range if in_range else offerings
    prices = [o["price"] for o in use if isinstance(o.get("price"), (int, float))]
    names_used = list(dict.fromkeys(o.get("name", "") for o in use if o.get("name")))
    avg = sum(prices) / len(prices) if prices else 0.0
    return avg, names_used, use


def _fetch_live_competitor_google_hotels(hotel_id: str) -> Tuple[Optional[float], List[str]]:
    """
    Fetch real-time competitor prices and names from Google Hotels (one search for the hotel).
    Returns (avg_price, list of competitor hotel names). Excludes own hotel name from names.
    """
    if not SERPAPI_KEY:
        return None, []
    own_name = _get_hotel_name_for_search(hotel_id)
    if not own_name:
        return None, []
    avg, names, _ = _fetch_google_hotels_by_query(own_name, own_name)
    return avg, names


def _fetch_live_competitor_serpapi(hotel_id: str) -> Tuple[Optional[float], List[str]]:
    """
    Fallback: fetch competitor price signals and titles from Google organic search via SerpAPI.
    Returns (avg_price, list of competitor names). Excludes own hotel name from names.
    """
    if not SERPAPI_KEY:
        return None, []
    own_name = _get_hotel_name_for_search(hotel_id)
    if not own_name:
        return None, []
    query = f"{own_name} hotel room price per night"
    try:
        r = requests.get(
            "https://serpapi.com/search",
            params={"q": query, "api_key": SERPAPI_KEY, "engine": "google"},
            timeout=COMPETITOR_PRICES_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        numbers = []
        names: List[str] = []
        for obj in data.get("organic_results", [])[:10]:
            title = (obj.get("title") or "").strip()
            if title and not _is_own_hotel(title, own_name):
                names.append(title)
            snippet = (obj.get("snippet") or "") + " " + title
            for m in re.findall(r"[\d,]+(?:\.\d{2})?\s*(?:USD|€|EUR|PKR|Rs\.?|INR)", snippet, re.I):
                n = float(re.sub(r"[, ]", "", m.replace("Rs.", "").replace("Rs", "").strip()))
                if 100 < n < 500000:
                    numbers.append(n)
        avg = sum(numbers) / len(numbers) if numbers else None
        return avg, list(dict.fromkeys(names))
    except Exception:
        logger.exception("SerpAPI organic competitor fetch failed for hotel_id=%s", hotel_id)
        return None, []


def _fallback_competitor_from_hotel_base(hotel_id: str) -> float:
    """When no API/DB data: use hotel's own average base price so column is not empty."""
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(AVG(rt."basePrice"), 0) AS avg_base
                FROM "RoomType" rt
                JOIN "Room" r ON r."roomTypeId" = rt.id
                JOIN "HotelFloor" hf ON hf.id = r."floorId"
                WHERE hf."hotelId" = %s
            """, (hotel_id,))
            row = cur.fetchone()
            return float(row["avg_base"] or 0.0)
    except Exception:
        logger.warning("Fallback competitor from hotel base failed for hotel_id=%s", hotel_id, exc_info=True)
        return 0.0


def _live_competitor_avg(hotel_id: str) -> Optional[float]:
    """Try live sources first; return None if none configured or all fail. Uses only price."""
    avg, _ = _fetch_live_competitor_from_api(hotel_id)
    if avg is not None and avg > 0:
        return avg
    avg, _ = _fetch_live_competitor_google_hotels(hotel_id)
    if avg is not None and avg > 0:
        return avg
    avg, _ = _fetch_live_competitor_serpapi(hotel_id)
    return avg


def get_competitor_display(hotel_id: str) -> dict:
    """
    Returns competitor avg price and list of competitor hotel names from SerpAPI.
    Use avg for pricing (same as competitor_avg). hotelNames excludes your own hotel.
    """
    avg, names = _fetch_live_competitor_from_api(hotel_id)
    if avg is not None and avg > 0:
        return {"avg": avg, "hotelNames": []}
    avg, names = _fetch_live_competitor_google_hotels(hotel_id)
    if avg is not None and avg > 0:
        return {"avg": avg, "hotelNames": names}
    avg, names = _fetch_live_competitor_serpapi(hotel_id)
    if avg is not None and avg > 0:
        return {"avg": avg, "hotelNames": names}
    fallback = _fallback_competitor_from_hotel_base(hotel_id)
    return {"avg": fallback, "hotelNames": []}


def get_competitor_per_room_type(
    hotel_id: str,
    room_type_names: List[str],
    base_by_name: Optional[dict] = None,
) -> dict:
    """
    Fetch competitor price and hotel names per room type (one SerpAPI search per room type).
    Cached for COMPETITOR_CACHE_TTL (default 30 min). base_by_name: optional dict room_type_name -> base price.
    Returns dict: room_type_name -> {"avg", "hotelNames", "competitorHotels": [{"name", "price"}]}.
    """
    cache_key = f"{hotel_id}:{','.join(sorted(room_type_names))}"
    now = time.time()
    if cache_key in _competitor_cache:
        cached, expiry = _competitor_cache[cache_key]
        if now < expiry:
            return cached
        del _competitor_cache[cache_key]
    hotel_level = get_competitor_display(hotel_id)
    hotel_avg = hotel_level["avg"]
    hotel_names = hotel_level.get("hotelNames") or []
    result = {}
    own_name = _get_hotel_name_for_search(hotel_id)
    for rt in room_type_names:
        if own_name and SERPAPI_KEY:
            search_query = f"{own_name} {rt} room price"
            avg, names, offerings = _fetch_google_hotels_by_query(own_name, search_query)
            if offerings or (avg is not None and avg > 0):
                our_base = (base_by_name or {}).get(rt)
                if our_base and offerings:
                    avg_use, names_use, offerings_use = _competitor_avg_for_room_type(offerings, our_base)
                    result[rt] = {
                        "avg": avg_use if avg_use > 0 else (avg or 0),
                        "hotelNames": names_use or names,
                        "competitorHotels": offerings_use[:10],
                    }
                else:
                    result[rt] = {
                        "avg": avg or 0,
                        "hotelNames": names,
                        "competitorHotels": offerings[:10],
                    }
                continue
        result[rt] = {"avg": hotel_avg, "hotelNames": hotel_names, "competitorHotels": []}
    _competitor_cache[cache_key] = (result, now + COMPETITOR_CACHE_TTL_SEC)
    return result


# ============================================================
# COMPETITOR PRICE (live API / SerpAPI Google Hotels / hotel base fallback only)
# ============================================================

def competitor_avg(hotel_id: str) -> float:
    """Average competitor price for pricing math. Unchanged logic."""
    live = _live_competitor_avg(hotel_id)
    if live is not None and live > 0:
        return live
    return _fallback_competitor_from_hotel_base(hotel_id)

# ============================================================
# AI PRICING ENGINE (REAL MOVEMENT)
# ============================================================

def ai_price(base_price: float, competitor: float, occ: float, room_type: str) -> float:
    """Compute AI price from base, competitor, occupancy and room type. Validates inputs."""
    validate_base_price(base_price)
    occ = validate_occupancy(occ)
    competitor = validate_competitor_price(competitor)
    rt = room_type.lower() if room_type else ""

    strategic_floor = 0.0
    for key, val in ROOM_TYPE_FLOOR.items():
        if key in rt:
            strategic_floor = val
            break

    # DEMAND LOGIC
    if occ < 0.30:
        demand_factor = -0.15
    elif occ < 0.50:
        demand_factor = -0.05
    elif occ < 0.70:
        demand_factor = 0.10
    else:
        demand_factor = 0.25

    price = base_price * (1 + demand_factor)

    # COMPETITOR BLEND
    if competitor > 0:
        price = (price * 0.7) + (competitor * 0.3)

    # MOVEMENT WINDOW
    upper = base_price * (1 + MAX_UP)
    lower = base_price * (1 - MAX_DOWN)

    price = min(price, upper)
    price = max(price, lower)

    # ABSOLUTE FLOOR (allow 20% below strategic floor)
    if strategic_floor > 0:
        price = max(price, strategic_floor * 0.8)

    return round(price, 2)

# ============================================================
# PREVIEW
# ============================================================

@router.get("/api/auto-pricing/preview")
def preview_pricing(
    hotelId: Optional[str] = Query(None),
    hotel_id: Optional[str] = Query(None)
):
    """Preview AI pricing. Competitor price per room type (SerpAPI). Returns algo_version for audit."""
    hid = hotelId or hotel_id
    if not hid:
        raise HTTPException(400, "hotelId is required")

    occ = validate_occupancy(occupancy_ratio(hid))

    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT rt.id, rt.name, rt."basePrice"
            FROM "RoomType" rt
            JOIN "Room" r ON r."roomTypeId" = rt.id
            JOIN "HotelFloor" hf ON hf.id = r."floorId"
            WHERE hf."hotelId" = %s
        """, (hid,))
        rows = cur.fetchall()

    if not rows:
        return {
            "occupancy": round(occ * 100, 2),
            "competitorPrice": None,
            "roomTypes": [],
            "rooms": [],
            "chartData": [],
            "profitComparison": {"totalCurrent": 0, "totalAI": 0, "difference": 0, "direction": "same"},
            "algo_version": ALGO_VERSION,
        }

    for r in rows:
        base = float(r["basePrice"] or 0)
        if base <= 0:
            raise HTTPException(400, f"Room type '{r['name']}' has invalid base price (must be > 0)")

    room_type_names = [r["name"] for r in rows]
    base_by_name = {r["name"]: float(r["basePrice"]) for r in rows}
    comp_per_room = get_competitor_per_room_type(hid, room_type_names, base_by_name=base_by_name)

    room_types = []
    total_current = 0.0
    total_ai = 0.0
    for r in rows:
        base = float(r["basePrice"])
        rt_name = r["name"]
        info = comp_per_room.get(rt_name, {})
        comp = validate_competitor_price(info.get("avg") or 0.0)
        comp_names = info.get("hotelNames") or []
        comp_hotels = info.get("competitorHotels") or []
        ai = ai_price(base, comp, occ, rt_name)
        total_current += base
        total_ai += ai
        comp_label = f"{round(comp, 2)}" if comp > 0 else None
        if comp_label and comp_names:
            comp_label += " (" + ", ".join(comp_names[:5]) + (")" if len(comp_names) <= 5 else ", ...)")
        room_types.append({
            "roomType": rt_name,
            "currentPrice": round(base, 2),
            "competitorPrice": round(comp, 2) if comp > 0 else None,
            "competitorPriceLabel": comp_label,
            "competitorHotelNames": comp_names,
            "competitorHotels": comp_hotels,
            "aiPrice": ai,
            "change": round(ai - base, 2),
            "direction": "up" if ai > base else ("down" if ai < base else "same"),
        })
    profit_diff = round(total_ai - total_current, 2)
    avg_competitor = sum(rt["competitorPrice"] or 0 for rt in room_types) / max(len(room_types), 1)
    chart_data = [
        {"roomType": rt["roomType"], "competitorPrice": rt["competitorPrice"], "ourPrice": rt["aiPrice"]}
        for rt in room_types
    ]
    return {
        "occupancy": round(occ * 100, 2),
        "competitorPrice": round(avg_competitor, 2) if avg_competitor > 0 else None,
        "roomTypes": room_types,
        "rooms": room_types,
        "chartData": chart_data,
        "profitComparison": {
            "totalCurrent": round(total_current, 2),
            "totalAI": round(total_ai, 2),
            "difference": profit_diff,
            "direction": "up" if profit_diff > 0 else ("down" if profit_diff < 0 else "same"),
        },
        "algo_version": ALGO_VERSION,
    }

# ============================================================
# APPLY (LIVE UPDATE)
# ============================================================

@router.post("/api/auto-pricing/apply")
def apply_pricing(
    hotelId: Optional[str] = Query(None),
    hotel_id: Optional[str] = Query(None)
):
    """Apply AI pricing. Logs every price change (audit). Returns algo_version."""
    hid = hotelId or hotel_id
    if not hid:
        raise HTTPException(400, "hotelId is required")

    occ = validate_occupancy(occupancy_ratio(hid))

    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT rt.id, rt.name, rt."basePrice"
            FROM "RoomType" rt
            JOIN "Room" r ON r."roomTypeId" = rt.id
            JOIN "HotelFloor" hf ON hf.id = r."floorId"
            WHERE hf."hotelId" = %s
        """, (hid,))
        rows = cur.fetchall()

    if not rows:
        return {
            "status": "APPLIED",
            "occupancy": round(occ * 100, 2),
            "updatedRoomTypes": 0,
            "profitComparison": {"totalBefore": 0, "totalAfter": 0, "difference": 0},
            "algo_version": ALGO_VERSION,
        }

    for r in rows:
        base = float(r["basePrice"] or 0)
        if base <= 0:
            raise HTTPException(400, f"Room type '{r['name']}' has invalid base price (must be > 0)")

    room_type_names = [r["name"] for r in rows]
    base_by_name = {r["name"]: float(r["basePrice"]) for r in rows}
    comp_per_room = get_competitor_per_room_type(hid, room_type_names, base_by_name=base_by_name)

    updated = 0
    total_before = 0.0
    total_after = 0.0
    with db() as conn, conn.cursor() as cur:
        for r in rows:
            old_price = float(r["basePrice"])
            comp = validate_competitor_price(comp_per_room.get(r["name"], {}).get("avg") or 0.0)
            new_price = ai_price(old_price, comp, occ, r["name"])
            total_before += old_price
            total_after += new_price

            if round(old_price, 2) != round(new_price, 2):
                cur.execute("""
                    UPDATE "RoomType"
                    SET "basePrice" = %s
                    WHERE id = %s
                """, (new_price, r["id"]))
                updated += 1
                log_pricing_audit(
                    hotel_id=hid,
                    room_type=r["name"],
                    room_type_id=str(r["id"]) if r.get("id") else None,
                    old_price=old_price,
                    new_price=new_price,
                    occupancy=occ,
                    competitor_price=comp,
                )

        conn.commit()

    return {
        "status": "APPLIED",
        "occupancy": round(occ * 100, 2),
        "updatedRoomTypes": updated,
        "profitComparison": {
            "totalBefore": round(total_before, 2),
            "totalAfter": round(total_after, 2),
            "difference": round(total_after - total_before, 2),
        },
        "algo_version": ALGO_VERSION,
    }

# ============================================================
# REGISTER ROUTER
# ============================================================

app.include_router(router)

preview = preview_pricing
apply = apply_pricing
competitor_price = competitor_avg
