import os
import json
import secrets
from math import isfinite
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from threading import RLock
import psycopg
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from decimal import Decimal
from pydantic import BaseModel, Field
from backtesting import Candle, compare_sma_parameters, convert_candles_to_eur, run_sma_backtest

A={'mode':'PAPER','phase':'BOOTSTRAP','equity':Decimal('200'),'protected':Decimal('0'),'trading':Decimal('200'),'max_position':Decimal('50'),'risk_profile':'AGGRESSIVE_BOOTSTRAP','phase_transition_pending':False}
CAPITAL_STATE_LOCK = RLock()
SUPPORTED_CURRENCIES = {'EUR', 'USD', 'GBP', 'CHF', 'JPY', 'CAD', 'AUD'}

def database_connection():
    return psycopg.connect(
        host=os.getenv('POSTGRES_HOST','postgres'),
        dbname=os.getenv('POSTGRES_DB','trading'),
        user=os.getenv('POSTGRES_USER','trading'),
        password=os.getenv('POSTGRES_PASSWORD','trading_dev_password'),
    )

def initialize_capital_storage():
    with database_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS capital_state (
                id SMALLINT PRIMARY KEY CHECK (id = 1),
                mode TEXT NOT NULL,
                phase TEXT NOT NULL,
                equity NUMERIC(14,2) NOT NULL CHECK (equity > 0),
                protected NUMERIC(14,2) NOT NULL,
                trading NUMERIC(14,2) NOT NULL,
                max_position NUMERIC(14,2) NOT NULL,
                risk_profile TEXT NOT NULL,
                phase_transition_pending BOOLEAN NOT NULL
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS backtest_runs (
                id BIGSERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                symbol TEXT NOT NULL,
                run_type TEXT NOT NULL CHECK (run_type IN ('single', 'comparison')),
                price_currency TEXT NOT NULL,
                result_json JSONB NOT NULL
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS backtest_runs_created_at_idx ON backtest_runs (created_at DESC)')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS market_data_cache (
                cache_key TEXT PRIMARY KEY,
                fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                payload_json JSONB NOT NULL
            )
        ''')
        conn.execute('''
            INSERT INTO capital_state
                (id, mode, phase, equity, protected, trading, max_position, risk_profile, phase_transition_pending)
            VALUES (1, 'PAPER', 'BOOTSTRAP', 200, 0, 200, 50, 'AGGRESSIVE_BOOTSTRAP', FALSE)
            ON CONFLICT (id) DO NOTHING
        ''')
        row=conn.execute('''
            SELECT mode, phase, equity, protected, trading, max_position, risk_profile, phase_transition_pending
            FROM capital_state WHERE id=1
        ''').fetchone()
    A.update(mode=row[0],phase=row[1],equity=Decimal(row[2]),protected=Decimal(row[3]),trading=Decimal(row[4]),max_position=Decimal(row[5]),risk_profile=row[6],phase_transition_pending=row[7])
    recalc(A['equity'])
    persist_capital_state()

def persist_capital_state():
    with database_connection() as conn:
        conn.execute('''
            UPDATE capital_state
            SET mode=%s, phase=%s, equity=%s, protected=%s, trading=%s,
                max_position=%s, risk_profile=%s, phase_transition_pending=%s
            WHERE id=1
        ''',(A['mode'],A['phase'],A['equity'],A['protected'],A['trading'],A['max_position'],A['risk_profile'],A['phase_transition_pending']))

def persist_backtest_run(result: dict, run_type: str):
    with database_connection() as conn:
        conn.execute('''
            INSERT INTO backtest_runs (symbol, run_type, price_currency, result_json)
            VALUES (%s, %s, %s, %s::jsonb)
        ''', (result['symbol'], run_type, result['price_currency'], json.dumps(result)))

def get_backtest_runs(limit: int) -> list[dict]:
    with database_connection() as conn:
        rows = conn.execute('''
            SELECT id, created_at, symbol, run_type, result_json
            FROM backtest_runs ORDER BY created_at DESC, id DESC LIMIT %s
        ''', (limit,)).fetchall()
    return [
        {
            'id': row[0],
            'created_at': row[1].isoformat(),
            'symbol': row[2],
            'run_type': row[3],
            'result': row[4],
        }
        for row in rows
    ]

def fetch_alpha_payload(cache_key: str, params: dict, expected_key: str, ttl_seconds: int):
    try:
        with database_connection() as conn:
            row = conn.execute('''
                SELECT payload_json FROM market_data_cache
                WHERE cache_key=%s AND fetched_at >= NOW() - (%s * INTERVAL '1 second')
            ''', (cache_key, ttl_seconds)).fetchone()
        if row and isinstance(row[0], dict) and row[0].get(expected_key):
            return row[0], True
    except Exception:
        # A cache read outage should not prevent retrieving fresh provider data.
        pass

    api_key = os.getenv("ALPHA_VANTAGE_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="Alpha Vantage API key is not configured")
    request_params = {**params, "apikey": api_key}
    try:
        with urlopen(f"https://www.alphavantage.co/query?{urlencode(request_params)}", timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=502, detail="Could not retrieve market data")
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="Market data has an unexpected format")
    if isinstance(data, dict) and data.get(expected_key):
        try:
            with database_connection() as conn:
                conn.execute('''
                    INSERT INTO market_data_cache (cache_key, payload_json, fetched_at)
                    VALUES (%s, %s::jsonb, NOW())
                    ON CONFLICT (cache_key) DO UPDATE
                    SET payload_json=EXCLUDED.payload_json, fetched_at=EXCLUDED.fetched_at
                ''', (cache_key, json.dumps(data)))
        except Exception:
            # Fresh market data remains usable if cache persistence temporarily fails.
            pass
    return data, False

@asynccontextmanager
async def lifespan(app):
    initialize_capital_storage()
    yield

app=FastAPI(title='Bechthold Industries',version='0.1.0',lifespan=lifespan)
app.mount('/static', StaticFiles(directory=Path(__file__).parent/'static'), name='static')

class U(BaseModel): equity: Decimal=Field(gt=0,allow_inf_nan=False)
class PhaseConfirmation(BaseModel): confirm: bool

def require_phase_confirmation_token(confirmation_token: str | None):
    expected_token=os.getenv('PHASE_CONFIRMATION_TOKEN')
    if not expected_token:
        raise HTTPException(status_code=503,detail='Manual confirmation is not configured')
    if not isinstance(confirmation_token, str) or not secrets.compare_digest(confirmation_token,expected_token):
        raise HTTPException(status_code=403,detail='Invalid manual confirmation token')

def recalc(e):
    p=A['phase']
    pending=p=='BOOTSTRAP' and (A['phase_transition_pending'] or e>=Decimal('350'))
    if p=='BOOTSTRAP':
        pr=min(Decimal('200'),e) if pending else Decimal('0')
        t=max(Decimal('0'),e-pr)
        m=Decimal('50')
        r='AGGRESSIVE_BOOTSTRAP'
    else:
        pr=min(Decimal('200'),e)
        t=max(Decimal('0'),e-pr)
        m=Decimal('50') if e<Decimal('1000') else min(Decimal('100'),t*Decimal('.10'))
        r='PROFIT_ONLY' if e<Decimal('1000') else 'CONSERVATIVE'
    A.update(equity=e,protected=pr,trading=t,max_position=m,risk_profile=r,phase_transition_pending=pending)

def out():
    with CAPITAL_STATE_LOCK:
        return {k:float(v.quantize(Decimal('.01'))) if isinstance(v,Decimal) else v for k,v in {'mode':A['mode'],'phase':A['phase'],'phase_transition_pending':A['phase_transition_pending'],'total_equity':A['equity'],'protected_capital':A['protected'],'trading_capital':A['trading'],'max_position_value':A['max_position'],'risk_profile':A['risk_profile']}.items()}

def persist_capital_change_or_rollback(previous_state: dict):
    try:
        persist_capital_state()
    except Exception as exc:
        A.update(previous_state)
        raise HTTPException(status_code=503, detail='Could not save capital state') from exc
@app.get('/')
def root(): return FileResponse(Path(__file__).parent/'static'/'index.html')
@app.get('/health')

def health(): return {'status':'ok'}

@app.get('/health/ready')
def readiness():
    try:
        with database_connection() as conn:
            conn.execute('SELECT 1')
    except Exception:
        raise HTTPException(status_code=503, detail='Database is not ready')
    return {'status':'ready'}

@app.get('/api/capital')

def capital(): return out()
@app.post('/api/capital/simulate')
def simulate(u:U, confirmation_token:str|None=Header(default=None,alias='X-Phase-Confirmation-Token')):
    with CAPITAL_STATE_LOCK:
        require_phase_confirmation_token(confirmation_token)
        previous_state = A.copy()
        recalc(u.equity)
        persist_capital_change_or_rollback(previous_state)
        return out()
@app.post('/api/capital/phase/confirm')
def confirm_phase(u:PhaseConfirmation, confirmation_token:str|None=Header(default=None,alias='X-Phase-Confirmation-Token')):
    with CAPITAL_STATE_LOCK:
        if not u.confirm:
            raise HTTPException(status_code=400,detail='Manual confirmation is required')
        require_phase_confirmation_token(confirmation_token)
        if A['phase']!='BOOTSTRAP' or not A['phase_transition_pending']:
            raise HTTPException(status_code=409,detail='No BOOTSTRAP to PROFIT_ONLY transition is pending')
        previous_state = A.copy()
        A['phase']='PROFIT_ONLY'
        recalc(A['equity'])
        persist_capital_change_or_rollback(previous_state)
        return out()

@app.get('/api/risk/check')

def risk_check(position_value:Decimal=Query(default=Decimal('50'),gt=0)):
    if not position_value.is_finite() or position_value<=0:
        raise HTTPException(status_code=422,detail='position_value must be greater than zero')
    with CAPITAL_STATE_LOCK:
        return {'allowed':position_value<=A['max_position'] and position_value<=A['trading'],'requested':float(position_value),'max_position':float(A['max_position']),'trading_capital':float(A['trading'])}

def get_alpha_vantage_quote(symbol: str, currency: str = 'USD'):
    api_key = os.getenv("ALPHA_VANTAGE_API_KEY")

    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="Alpha Vantage API key is not configured"
        )

    symbol = symbol.strip().upper()
    currency = currency.upper()
    if currency not in SUPPORTED_CURRENCIES:
        raise HTTPException(status_code=422, detail='Unsupported quote currency')

    if not symbol or len(symbol) > 12 or not all(
        c.isalnum() or c in ".-" for c in symbol
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid stock symbol"
        )

    data, _ = fetch_alpha_payload(
        f"quote:{symbol}", {"function": "GLOBAL_QUOTE", "symbol": symbol}, "Global Quote", 300
    )

    quote = data.get("Global Quote", {})

    if not quote or not quote.get("05. price"):
        raise HTTPException(
            status_code=502,
            detail=(
                data.get("Note")
                or data.get("Information")
                or data.get("Error Message")
                or "No quote data returned"
            )
        )

    try:
        price = float(quote["05. price"])
        change = float(quote.get("09. change", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=502, detail="Quote data has an unexpected format")
    if not isfinite(price) or price <= 0:
        raise HTTPException(status_code=502, detail="Quote data has an invalid price")
    if not isfinite(change):
        change = 0.0
    change_percent = quote.get("10. change percent")
    try:
        parsed_change_percent = float(str(change_percent).replace('%', '').strip()) if change_percent is not None else None
    except ValueError:
        parsed_change_percent = None
    if parsed_change_percent is not None and not isfinite(parsed_change_percent):
        change_percent = None

    return {
        "symbol": quote.get("01. symbol", symbol),
        "price": price,
        "currency": currency,
        "change": change,
        "change_percent": change_percent,
        "latest_trading_day": quote.get("07. latest trading day"),
        "source": "Alpha Vantage"
    }


@app.get("/api/market/quote")
def market_quote(symbol: str = "IBM", currency: str = "USD"):
    return get_alpha_vantage_quote(symbol, currency)


def get_alpha_vantage_daily(
    symbol: str, price_currency: str = "USD", *, convert_to_eur: bool = True
) -> list[Candle]:
    api_key = os.getenv("ALPHA_VANTAGE_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="Alpha Vantage API key is not configured")
    symbol = symbol.strip().upper()
    price_currency = price_currency.upper()
    if price_currency not in SUPPORTED_CURRENCIES:
        raise HTTPException(status_code=422, detail="Unsupported price currency")
    if not symbol or len(symbol) > 12 or not all(c.isalnum() or c in ".-" for c in symbol):
        raise HTTPException(status_code=400, detail="Invalid stock symbol")
    data, _ = fetch_alpha_payload(
        f"daily:{symbol}",
        {"function": "TIME_SERIES_DAILY", "symbol": symbol, "outputsize": "compact"},
        "Time Series (Daily)", 3600,
    )
    series = data.get("Time Series (Daily)")
    if not isinstance(series, dict) or not series:
        # Do not return provider payloads, which can contain operational details.
        raise HTTPException(status_code=502, detail="Daily market data unavailable; check provider limits and API configuration")
    try:
        candles = [Candle(date=day, open=float(values["1. open"]), close=float(values["4. close"])) for day, values in series.items()]
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=502, detail="Daily market data has an unexpected format")
    if any(
        not isfinite(price) or price <= 0
        for candle in candles
        for price in (candle.open, candle.close)
    ):
        raise HTTPException(status_code=502, detail="Daily market data contains invalid prices")
    if price_currency == "EUR" or not convert_to_eur:
        return candles
    fx_key = f"Time Series FX (Daily)"
    fx_data, _ = fetch_alpha_payload(
        f"fx-daily:{price_currency}:EUR",
        {"function": "FX_DAILY", "from_symbol": price_currency, "to_symbol": "EUR", "outputsize": "compact"},
        fx_key, 3600,
    )
    fx_series = fx_data.get("Time Series FX (Daily)")
    if not isinstance(fx_series, dict) or not fx_series:
        raise HTTPException(status_code=502, detail="Historical exchange rates unavailable; check provider limits and API configuration")
    try:
        rates = {day: (float(values["1. open"]), float(values["4. close"])) for day, values in fx_series.items()}
        return convert_candles_to_eur(candles, rates)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Could not safely convert market history to EUR") from exc


@app.get("/api/market/history")
def market_history(
    symbol: str = Query(default="NVDA", min_length=1, max_length=12),
    limit: int = Query(default=100, ge=20, le=100),
):
    """Return cached, native-currency daily closes for the stock chart."""
    candles = sorted(
        get_alpha_vantage_daily(symbol, "USD", convert_to_eur=False),
        key=lambda candle: candle.date,
    )[-limit:]
    if len(candles) < 2:
        raise HTTPException(status_code=502, detail="At least two daily prices are required for the chart")
    latest, previous = candles[-1], candles[-2]
    change = latest.close - previous.close
    change_percent = change / previous.close * 100
    if not isfinite(change) or not isfinite(change_percent):
        raise HTTPException(status_code=502, detail="Daily market data contains an invalid price change")
    return {
        "symbol": symbol.strip().upper(),
        "currency": "USD",
        "latest_close": latest.close,
        "change": change,
        "change_percent": change_percent,
        "latest_trading_day": latest.date,
        "frequency": "daily",
        "source": "Alpha Vantage",
        "data_adjustment": "raw_unadjusted",
        "points": [{"date": candle.date, "close": candle.close} for candle in candles],
    }


@app.get("/api/backtest/run")
def backtest_run(
    symbol: str = Query(default="IBM", min_length=1, max_length=12),
    currency: str = "USD",
    fast: int = Query(default=5, ge=1, le=40),
    slow: int = Query(default=20, ge=2, le=100),
):
    if fast >= slow:
        raise HTTPException(status_code=422, detail="fast must be smaller than slow")
    if currency.upper() not in SUPPORTED_CURRENCIES:
        raise HTTPException(status_code=422, detail="Unsupported price currency")
    candles = get_alpha_vantage_daily(symbol, currency)
    with CAPITAL_STATE_LOCK:
        trading_capital = float(A['trading'])
        max_position = float(A['max_position'])
    try:
        result = run_sma_backtest(
            candles,
            initial_capital=trading_capital,
            max_position=max_position,
            fast_window=fast,
            slow_window=slow,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    result.update({"symbol": symbol.strip().upper(), "strategy": "SMA crossover", "fast_window": fast, "slow_window": slow, "bars": len(candles), "price_currency": currency.upper(), "base_currency": "EUR", "data_adjustment": "raw_unadjusted", "max_position_value": max_position, "mode": "PAPER"})
    try:
        persist_backtest_run(result, 'single')
    except Exception:
        raise HTTPException(status_code=503, detail="Could not save backtest history")
    return result


@app.get("/api/backtest/compare")
def backtest_compare(
    symbol: str = Query(default="IBM", min_length=1, max_length=12),
    currency: str = Query(default="USD", min_length=3, max_length=3),
):
    if currency.upper() not in SUPPORTED_CURRENCIES:
        raise HTTPException(status_code=422, detail="Unsupported price currency")
    candles = get_alpha_vantage_daily(symbol, currency)
    with CAPITAL_STATE_LOCK:
        trading_capital = float(A['trading'])
        max_position = float(A['max_position'])
    try:
        result = compare_sma_parameters(
            candles, initial_capital=trading_capital, max_position=max_position
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    result.update({"symbol": symbol.strip().upper(), "bars": len(candles), "price_currency": currency.upper(), "base_currency": "EUR", "data_adjustment": "raw_unadjusted", "mode": "PAPER"})
    try:
        persist_backtest_run(result, 'comparison')
    except Exception:
        raise HTTPException(status_code=503, detail="Could not save comparison history")
    return result


@app.get("/api/backtest/history")
def backtest_history(limit: int = Query(default=10, ge=1, le=50)):
    try:
        return {"runs": get_backtest_runs(limit)}
    except Exception:
        raise HTTPException(status_code=503, detail="Backtest history is unavailable")
