import os
import sys
import logging
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Optional

_main_dir = os.path.dirname(os.path.abspath(__file__))
if _main_dir not in sys.path:
    sys.path.insert(0, _main_dir)
try:
    os.chdir(_main_dir)
except Exception:
    pass

from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

# Load .env from common locations
for env_path in [
    Path.cwd() / ".env",
    Path(__file__).resolve().parent / ".env",
    Path(__file__).resolve().parent.parent / ".env",
]:
    if env_path.exists():
        try:
            load_dotenv(dotenv_path=env_path, override=False)
        except Exception:
            pass
load_dotenv(override=False)

_main_logger = logging.getLogger("main")

# Optional auto_pricing backend
try:
    import auto_pricing as _auto_pricing
    AUTO_PRICING_MODULE_AVAILABLE = True
except ImportError as e:
    _main_logger.warning("auto_pricing module not available: %s", e)
    _auto_pricing = None
    AUTO_PRICING_MODULE_AVAILABLE = False

# Database config
DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT"),
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
}
_DB_ENV_KEY_MAP = {
    "host": "DB_HOST",
    "port": "DB_PORT",
    "dbname": "DB_NAME",
    "user": "DB_USER",
    "password": "DB_PASSWORD",
}

# Auto pricing constants
AUTO_PRICING_MODEL_PATH = Path(__file__).resolve().parent / "pricing_model.pkl"
AUTO_PRICING_MIN_PRICE = Decimal("500.00")
AUTO_PRICING_MAX_PRICE = Decimal("50000.00")


def get_db_connection():
    """Create database connection."""
    missing = [env_name for key, env_name in _DB_ENV_KEY_MAP.items() if not DB_CONFIG.get(key)]
    if missing:
        raise HTTPException(
            status_code=500,
            detail="Database configuration incomplete. Missing: " + ", ".join(sorted(missing)),
        )
    try:
        return psycopg2.connect(**DB_CONFIG)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database connection failed: {str(e)}")


def auto_pricing_to_decimal(value) -> Decimal:
    """Safe conversion to Decimal. Returns Decimal('0.00') if invalid."""
    if value is None:
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


class AutoPricingEngine:
    """Rule-based pricing engine. 2.5% increase by default."""

    def __init__(self, rate: Decimal = Decimal("0.025")):
        self.rate = rate

    def calculate(self, base_price: Decimal) -> Decimal:
        if base_price is None or base_price <= 0:
            return Decimal("0.00")
        multiplier = Decimal("1.0") + self.rate
        return (base_price * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


auto_pricing_engine = AutoPricingEngine()

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Auto Pricing API",
    description="Hotel room auto-pricing and profit endpoints",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def read_root():
    return {
        "message": "Auto Pricing API",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
    }


# ---------------------------------------------------------------------------
# Auto Pricing endpoints
# ---------------------------------------------------------------------------

@app.get("/api/db-test")
def auto_pricing_db_test():
    """Database connectivity test."""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.close()
        conn.close()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/hotels")
def auto_pricing_list_hotels():
    """List all hotels (id, name)."""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT id, name FROM "Hotel" ORDER BY name')
        rows = cur.fetchall()
        return {"count": len(rows), "data": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.get("/api/frontoffice/hotel-rooms")
def auto_pricing_frontoffice_hotel_rooms(
    hotelId: Optional[str] = Query(None, description="Hotel ID"),
    hotel_id: Optional[str] = Query(None, description="Hotel ID (alternative)"),
):
    """List rooms for a hotel (front office). Current prices from DB."""
    hid = hotelId or hotel_id
    if not hid:
        raise HTTPException(400, "hotelId or hotel_id is required")
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT
                r.id,
                r."roomNumber" AS "roomNumber",
                rt.name AS "roomType",
                rt."basePrice" AS "basePrice"
            FROM "Room" r
            JOIN "RoomType" rt ON rt.id = r."roomTypeId"
            JOIN "HotelFloor" hf ON hf.id = r."floorId"
            WHERE hf."hotelId" = %s
            ORDER BY CAST(NULLIF(REGEXP_REPLACE(r."roomNumber", '[^0-9]', '', 'g'), '') AS INTEGER), r."roomNumber"
        """, (hid,))
        rows = cur.fetchall()
        return {"rooms": rows}
    except Exception as e:
        _main_logger.exception("frontoffice hotel-rooms failed")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.get("/api/hotels/{hotel_id}/rooms")
def auto_pricing_list_rooms(hotel_id: str):
    """List rooms for a hotel with room type and base price."""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT
                r.id AS room_id,
                r."roomNumber" AS room_number,
                rt.name AS room_type,
                rt."basePrice" AS base_price
            FROM "Room" r
            JOIN "HotelFloor" hf ON hf.id = r."floorId"
            JOIN "Hotel" h ON h.id = hf."hotelId"
            JOIN "RoomType" rt ON rt.id = r."roomTypeId"
            WHERE h.id = %s
            ORDER BY r."roomNumber"::int
        """, (hotel_id,))
        rows = cur.fetchall()
        return {"count": len(rows), "rooms": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.get("/api/prices")
def auto_pricing_get_prices():
    """Get current room prices (base prices)."""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT
                r."roomNumber" AS room_number,
                rt.name AS room_type,
                rt."basePrice" AS price
            FROM "Room" r
            JOIN "RoomType" rt ON rt.id = r."roomTypeId"
            ORDER BY CAST(NULLIF(REGEXP_REPLACE(r."roomNumber", '[^0-9]', '', 'g'), '') AS INTEGER), r."roomNumber"
        """)
        rows = cur.fetchall()
        return {"mode": "base_price", "count": len(rows), "data": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.get("/api/auto-pricing/preview")
def auto_pricing_preview_pricing(
    hotel_id: Optional[str] = Query(None, description="Hotel ID"),
    hotelId: Optional[str] = Query(None, description="Hotel ID (alternative)"),
):
    """Preview AI pricing. Uses auto_pricing module when available, else fallback engine."""
    hid = hotel_id or hotelId
    if not hid:
        raise HTTPException(400, "hotel_id or hotelId is required")
    if AUTO_PRICING_MODULE_AVAILABLE and _auto_pricing is not None:
        return _auto_pricing.preview(hid)
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT DISTINCT rt.id, rt.name, rt."basePrice"
            FROM "RoomType" rt
            JOIN "Room" r ON r."roomTypeId" = rt.id
            JOIN "HotelFloor" hf ON hf.id = r."floorId"
            WHERE hf."hotelId" = %s
        """, (hid,))
        rows = cur.fetchall()
        room_types = []
        total_current = 0.0
        total_ai = 0.0
        for r in rows:
            base = float(r["basePrice"])
            new_price = float(auto_pricing_engine.calculate(auto_pricing_to_decimal(base)))
            total_current += base
            total_ai += new_price
            room_types.append({
                "roomType": r["name"],
                "currentPrice": round(base, 2),
                "competitorPrice": None,
                "aiPrice": round(new_price, 2),
                "change": round(new_price - base, 2),
                "direction": "up" if new_price > base else ("down" if new_price < base else "same"),
            })
        profit_diff = round(total_ai - total_current, 2)
        chart_data = [
            {"roomType": rt["roomType"], "competitorPrice": rt.get("competitorPrice"), "ourPrice": rt["aiPrice"]}
            for rt in room_types
        ]
        return {
            "occupancy": 0.0,
            "competitorPrice": None,
            "roomTypes": room_types,
            "rooms": room_types,
            "chartData": chart_data,
            "profitComparison": {
                "totalCurrent": round(total_current, 2),
                "totalAI": round(total_ai, 2),
                "difference": profit_diff,
                "direction": "up" if profit_diff > 0 else ("down" if profit_diff < 0 else "same"),
            },
            "algo_version": getattr(_auto_pricing, "ALGO_VERSION", "fallback") if _auto_pricing else "fallback",
        }
    except Exception as e:
        _main_logger.exception("auto-pricing preview fallback failed")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.post("/api/auto-pricing/apply")
def auto_pricing_apply_pricing(
    hotel_id: Optional[str] = Query(None, description="Hotel ID"),
    hotelId: Optional[str] = Query(None, description="Hotel ID (alternative)"),
):
    """Apply AI pricing. Requires auto_pricing module when available."""
    hid = hotel_id or hotelId
    if not hid:
        raise HTTPException(400, "hotel_id or hotelId is required")
    if AUTO_PRICING_MODULE_AVAILABLE and _auto_pricing is not None:
        return _auto_pricing.apply(hid)
    raise HTTPException(
        status_code=503,
        detail="Auto pricing module not available. Apply requires the auto_pricing hybrid engine.",
    )


@app.get("/api/profit/yesterday-vs-today")
def auto_pricing_profit(hotel_id: Optional[str] = Query(None, description="Hotel ID (optional, for room prices)")):
    """Profit comparison: yesterday vs today from incomeexpense; optional current room prices."""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cur.execute("""
                SELECT report_date AS date, total_revenue, total_expense
                FROM incomeexpense
                WHERE report_date >= CURRENT_DATE - INTERVAL '2 days'
                ORDER BY report_date DESC
                LIMIT 2
            """)
            rows = cur.fetchall()
        except Exception:
            rows = []
        today_val = 0.0
        yesterday_val = 0.0
        today_revenue = 0.0
        today_expense = 0.0
        yesterday_revenue = 0.0
        yesterday_expense = 0.0
        if rows:
            r0 = rows[0]
            today_revenue = float(r0.get("total_revenue") or 0)
            today_expense = float(r0.get("total_expense") or 0)
            today_val = today_revenue - today_expense
            if len(rows) >= 2:
                r1 = rows[1]
                yesterday_revenue = float(r1.get("total_revenue") or 0)
                yesterday_expense = float(r1.get("total_expense") or 0)
                yesterday_val = yesterday_revenue - yesterday_expense
        difference = round(today_val - yesterday_val, 2)
        trend = "up" if difference > 0 else ("down" if difference < 0 else "flat")
        out = {
            "status": "ok",
            "today": round(today_val, 2),
            "yesterday": round(yesterday_val, 2),
            "difference": difference,
            "trend": trend,
            "prices": {
                "today_revenue": round(today_revenue, 2),
                "today_expense": round(today_expense, 2),
                "yesterday_revenue": round(yesterday_revenue, 2),
                "yesterday_expense": round(yesterday_expense, 2),
            },
        }
        if hotel_id:
            try:
                cur.execute("""
                    SELECT rt.name AS room_type, rt."basePrice" AS price
                    FROM "RoomType" rt
                    JOIN "Room" r ON r."roomTypeId" = rt.id
                    JOIN "HotelFloor" hf ON hf.id = r."floorId"
                    WHERE hf."hotelId" = %s
                    GROUP BY rt.id, rt.name, rt."basePrice"
                    ORDER BY rt.name
                """, (hotel_id,))
                room_prices = cur.fetchall()
                out["room_prices"] = [{"roomType": r["room_type"], "price": float(r["price"])} for r in room_prices]
            except Exception:
                out["room_prices"] = []
        return out
    except HTTPException:
        raise
    except Exception as e:
        _main_logger.exception("profit yesterday-vs-today failed")
        return {
            "status": "error",
            "note": str(e),
            "today": 0,
            "yesterday": 0,
            "difference": 0,
            "trend": "flat",
            "prices": {"today_revenue": 0, "today_expense": 0, "yesterday_revenue": 0, "yesterday_expense": 0},
        }
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def _ensure_auto_pricing_available():
    if not AUTO_PRICING_MODULE_AVAILABLE or _auto_pricing is None:
        raise HTTPException(
            status_code=503,
            detail="auto_pricing module not available on this deployment.",
        )


@app.get("/api/auto-pricing/competitors/{hotel_id}")
def auto_pricing_competitors(hotel_id: str):
    if AUTO_PRICING_MODULE_AVAILABLE and _auto_pricing is not None:
        get_display = getattr(_auto_pricing, "get_competitor_display", None)
        if get_display is not None:
            d = get_display(hotel_id)
            avg = d.get("avg", 0) or 0
            return {"competitorAvg": round(avg, 2) if avg else None}
        competitor_avg = getattr(_auto_pricing, "competitor_avg", None)
        if competitor_avg is not None:
            return {"competitorAvg": competitor_avg(hotel_id)}
        return {"competitorAvg": None}
    raise HTTPException(503, detail="auto_pricing module not available.")


@app.get("/api/price-history/{room_id}")
def auto_pricing_price_history(room_id: str):
    raise HTTPException(501, detail="Price history not implemented in auto_pricing module.")


@app.post("/api/ml/train")
def auto_pricing_ml_train():
    raise HTTPException(501, detail="ML train not implemented in auto_pricing module.")


@app.get("/api/ml/status")
def auto_pricing_ml_status():
    raise HTTPException(501, detail="ML status not implemented in auto_pricing module.")


@app.get("/api/auto-pricing/preview/{hotel_id}")
def auto_pricing_preview_by_path(hotel_id: str):
    _ensure_auto_pricing_available()
    return _auto_pricing.preview(hotel_id)


@app.post("/api/auto-pricing/simulate/{hotel_id}")
def auto_pricing_simulate_by_path(hotel_id: str):
    _ensure_auto_pricing_available()
    return _auto_pricing.preview(hotel_id)


@app.post("/api/auto-pricing/apply/{hotel_id}")
def auto_pricing_apply_by_path(hotel_id: str):
    _ensure_auto_pricing_available()
    return _auto_pricing.apply(hotel_id)


@app.post("/api/jobs/run-auto-pricing/{hotel_id}")
def auto_pricing_run_job(hotel_id: str):
    _ensure_auto_pricing_available()
    return _auto_pricing.apply(hotel_id)


@app.get("/api/analytics/price-graph")
def auto_pricing_price_graph(hotelId: str = Query(..., description="Hotel ID")):
    if AUTO_PRICING_MODULE_AVAILABLE and _auto_pricing is not None:
        price_graph = getattr(_auto_pricing, "price_graph", None)
        if price_graph is not None:
            return price_graph(hotelId)
        raise HTTPException(501, detail="price_graph not implemented in auto_pricing module.")
    raise HTTPException(503, detail="auto_pricing module not available.")


@app.get("/api/analytics/profit")
def auto_pricing_profit_analytics(hotelId: str = Query(..., description="Hotel ID")):
    if AUTO_PRICING_MODULE_AVAILABLE and _auto_pricing is not None:
        profit = getattr(_auto_pricing, "profit", None)
        if profit is not None:
            return profit(hotelId)
        raise HTTPException(501, detail="profit not implemented in auto_pricing module.")
    raise HTTPException(503, detail="auto_pricing module not available.")


@app.get("/health")
def health_check():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
