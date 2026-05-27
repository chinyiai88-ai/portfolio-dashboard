#!/usr/bin/env python3
"""
發財888888 資產儀表板 — 自動抓價腳本
由 GitHub Actions 每小時執行，更新 data.json
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
import yfinance as yf

TZ_TW = timezone(timedelta(hours=8))
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")


def now_tw() -> str:
    return datetime.now(TZ_TW).isoformat()

def today_str() -> str:
    return datetime.now(TZ_TW).date().isoformat()

def year_start_str() -> str:
    return datetime.now(TZ_TW).date().replace(month=1, day=1).isoformat()

def load_portfolio() -> dict:
    with open("portfolio.json", "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_prices_yfinance(symbols: list[str]) -> dict[str, float]:
    prices: dict[str, float] = {}
    if not symbols:
        return prices
    try:
        raw = yf.download(
            tickers=symbols, period="1mo", interval="1d",
            progress=False, auto_adjust=True, threads=True,
        )
        close = raw["Close"] if "Close" in raw else raw
        if len(symbols) == 1:
            sym = symbols[0]
            series = close.dropna()
            if not series.empty:
                val = float(series.iloc[-1])
                if val > 0:
                    prices[sym] = val
        else:
            for sym in symbols:
                try:
                    col = close[sym] if sym in close.columns else None
                    if col is not None:
                        series = col.dropna()
                        if not series.empty:
                            val = float(series.iloc[-1])
                            if val > 0:
                                prices[sym] = val
                except Exception:
                    pass
    except Exception as e:
        print(f"[yfinance] error: {e}", file=sys.stderr)
    return prices


def fetch_prices_twse(tw_tickers: list[str]) -> dict[str, float]:
    prices: dict[str, float] = {}

    def _parse(msg_array: list) -> dict:
        out = {}
        for q in msg_array or []:
            sym = q.get("c", "").upper() + ".TW"
            z = q.get("z", "-")
            y = q.get("y", "-")
            raw_p = z if (z and z != "-") else y
            if raw_p and raw_p != "-":
                try:
                    out[sym] = float(raw_p)
                except ValueError:
                    pass
        return out

    def _fetch(prefix: str, tickers: list[str]) -> dict:
        ex_ch = "|".join(f"{prefix}{t.replace('.TW','').lower()}.tw" for t in tickers)
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex_ch}&json=1&delay=0"
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return _parse(r.json().get("msgArray", []))

    try:
        prices.update(_fetch("tse_", tw_tickers))
    except Exception as e:
        print(f"[TWSE tse_] error: {e}", file=sys.stderr)

    missing = [t for t in tw_tickers if t not in prices]
    if missing:
        try:
            prices.update(_fetch("otc_", missing))
        except Exception as e:
            print(f"[TWSE otc_] error: {e}", file=sys.stderr)

    return prices


def fetch_dividends_yfinance(stock_map: dict, today: str, yr_start: str) -> tuple[list, list]:
    """yfinance 股利備援：當無 FinMind Token 時，用 yfinance 抓歷史除息紀錄"""
    upcoming: list[dict] = []
    ytd:      list[dict] = []
    cutoff = str((datetime.now(TZ_TW) + timedelta(days=60)).date())

    for stock_id, info in stock_map.items():
        try:
            tick = stock_id + ".TW"
            divs = yf.Ticker(tick).dividends
            if divs is None or divs.empty:
                continue
            for ts, amount in divs.items():
                amount = round(float(amount), 4)
                if amount <= 0:
                    continue
                try:
                    date_str = ts.tz_convert("Asia/Taipei").strftime("%Y-%m-%d")
                except Exception:
                    date_str = str(ts)[:10]
                base = {
                    "stock":    info["name"],
                    "stock_id": stock_id,
                    "shares":   info["shares"],
                    "perShare": amount,
                    "exDate":   date_str,
                    "payDate":  "",
                    "note":     "yfinance",
                    "amount":   round(amount * info["shares"], 2),
                }
                if yr_start <= date_str < today:
                    ytd.append({**base, "date": date_str, "received": True, "est": False})
                elif today <= date_str <= cutoff:
                    upcoming.append(base)
            time.sleep(0.15)
        except Exception as e:
            print(f"[yf-div] {stock_id}: {e}", file=sys.stderr)

    upcoming.sort(key=lambda x: x["exDate"])
    ytd.sort(key=lambda x: x["exDate"])
    return upcoming, ytd


def fetch_finmind_dividends(token: str, stock_map: dict) -> tuple[list, list]:
    today = today_str()
    yr_start = year_start_str()
    upcoming: list[dict] = []
    ytd: list[dict] = []

    for stock_id, info in stock_map.items():
        try:
            url = "https://api.finmindtrade.com/api/v4/data"
            params = {
                "dataset": "TaiwanStockDividend",
                "data_id": stock_id,
                "start_date": yr_start,
                "token": token,
            }
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            body = r.json()
            if body.get("status") != 200:
                continue
            for d in body.get("data", []):
                cash = (
                    float(d.get("CashEarningsDistribution") or 0)
                    + float(d.get("CashStatutorySurplus") or 0)
                )
                ex_date  = d.get("CashExDividendTradingDate", "")
                pay_date = d.get("CashDividendPaymentDate", "") or ""
                if cash <= 0 or not ex_date:
                    continue
                item = {
                    "stock": info["name"],
                    "stock_id": stock_id,
                    "shares": info["shares"],
                    "perShare": round(cash, 4),
                    "exDate": ex_date,
                    "payDate": pay_date,
                    "note": str(d.get("year", "")),
                    "amount": round(cash * info["shares"], 2),
                }
                if ex_date >= today:
                    upcoming.append(item)
                elif ex_date >= yr_start:
                    item["received"] = bool(pay_date and pay_date <= today)
                    item["est"] = False
                    ytd.append(item)
            time.sleep(0.2)
        except Exception as e:
            print(f"[FinMind] {stock_id}: {e}", file=sys.stderr)

    upcoming.sort(key=lambda x: x["exDate"])
    ytd.sort(key=lambda x: x["exDate"])
    return upcoming, ytd


def calc_us_exposure(portfolio, loan, prices, usd_twd, etf_weights):
    """回傳 {sym: {value_usd, value_twd, direct_usd, etf_usd}}，分開直接持股與 ETF 內含"""
    direct_usd: dict[str, float] = {}
    etf_usd:    dict[str, float] = {}

    def add(d: dict, sym: str, val: float):
        d[sym] = d.get(sym, 0.0) + val

    for h in list(portfolio) + list(loan):
        tick = h.get("tick") or ""
        if not tick:
            continue
        etf_id = tick.replace(".TW", "")

        # 台股 ETF 含美股成分
        if tick.endswith(".TW") and etf_id in etf_weights:
            p = prices.get(tick, h["price"])
            val_usd = h["shares"] * p / usd_twd
            for us_sym, w in etf_weights[etf_id].items():
                add(etf_usd, us_sym, val_usd * w)

        # 美股 ETF（如 TOPT）展開成分
        elif not tick.endswith(".TW") and h["cur"] == "USD" and etf_id in etf_weights:
            p = prices.get(tick, h["price"])
            val_usd = h["shares"] * p
            for us_sym, w in etf_weights[etf_id].items():
                add(etf_usd, us_sym, val_usd * w)

        # 直接持有美股
        elif not tick.endswith(".TW") and h["cur"] == "USD":
            p = prices.get(tick, h["price"])
            add(direct_usd, tick, h["shares"] * p)

    all_syms = set(list(direct_usd.keys()) + list(etf_usd.keys()))
    exposure = {}
    for sym in all_syms:
        d = direct_usd.get(sym, 0.0)
        e = etf_usd.get(sym, 0.0)
        total = d + e
        exposure[sym] = {
            "value_usd":  round(total, 2),
            "value_twd":  round(total * usd_twd, 0),
            "direct_usd": round(d, 2),
            "etf_usd":    round(e, 2),
        }

    return dict(sorted(exposure.items(), key=lambda x: x[1]["value_twd"], reverse=True))


def fetch_twii_realtime_twse() -> tuple[float | None, str | None]:
    """從 TWSE 即時 API 抓大盤收盤指數（tse_t00.tw = 發行量加權股價指數）
    盤後返回當日收盤，盤中返回最新成交指數，無資料或非交易時間返回 (None, None)。
    """
    try:
        url = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
        params = {"ex_ch": "tse_t00.tw", "json": "1", "delay": "0"}
        headers = {
            "Referer": "https://mis.twse.com.tw/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        r = requests.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        body = r.json()
        msg = body.get("msgArray", [])
        if not msg:
            return None, None
        q = msg[0]
        z = q.get("z", "-")   # 當前成交指數（盤中 / 盤後收盤）
        y = q.get("y", "-")   # 昨日收盤
        d = q.get("d", "")    # 日期 YYYYMMDD
        price_str = z if (z and z not in ("-", "")) else y
        if not price_str or price_str in ("-", "") or not d:
            return None, None
        price = float(price_str.replace(",", ""))
        if price <= 0:
            return None, None
        date_str = f"{d[:4]}/{d[4:6]}/{d[6:8]}"
        return price, date_str
    except Exception as e:
        print(f"[TWII-TWSE] {e}", file=sys.stderr)
        return None, None


def fetch_twii_market(cfg: dict, nav: float,
                      existing_ath: float = 0, existing_ath_date: str = "") -> dict | None:
    """抓台股加權指數，計算移動平均線與進場觸發條件。
    最新收盤優先用 TWSE 即時 API；yfinance 2y 歷史僅用於計算 MA。
    """
    try:
        ticker = yf.Ticker('^TWII')
        hist = ticker.history(period='2y')
        if hist.empty:
            return None

        close_s = hist['Close'].dropna()
        # ── 優先使用 TWSE 即時 API 取得最新收盤（修正 yfinance 延遲問題）──
        rt_price, rt_date = fetch_twii_realtime_twse()
        yf_date = hist.index[-1].strftime('%Y/%m/%d')
        if rt_price and rt_date and rt_date >= yf_date:
            close      = rt_price
            close_date = rt_date
            print(f"[TWII] TWSE即時: {close:,.2f} ({close_date})", file=sys.stderr)
        else:
            close      = round(float(close_s.iloc[-1]), 2)
            close_date = yf_date
            print(f"[TWII] yfinance fallback: {close:,.2f} ({close_date})", file=sys.stderr)

        hist_max      = round(float(close_s.max()), 2)
        hist_max_date = close_s.idxmax().strftime('%Y/%m/%d')
        if hist_max >= existing_ath:
            ath, ath_date = hist_max, hist_max_date
        else:
            ath, ath_date = existing_ath, existing_ath_date

        drop_pct = round((close - ath) / ath * 100, 2) if ath > 0 else 0
        n        = len(close_s)
        ma20  = round(float(close_s.rolling(20).mean().iloc[-1]),  2) if n >= 20  else None
        ma60  = round(float(close_s.rolling(60).mean().iloc[-1]),  2) if n >= 60  else None
        ma240 = round(float(close_s.rolling(240).mean().iloc[-1]), 2) if n >= 240 else None

        triggers_cfg = cfg.get("twii_triggers", {})
        reserve_pct  = triggers_cfg.get("reserve_pct", 0.20)
        reserve      = nav * reserve_pct
        scenarios    = triggers_cfg.get("scenarios", [])

        triggers: list[dict] = []
        for sc in scenarios:
            if sc.get("use_ma60") and ma60:
                level = round(ma60, 2)
            elif "pct_drop" in sc and sc["pct_drop"] > 0:
                level = round(ath * (1 - sc["pct_drop"] / 100), 2)
            elif "fixed_trigger" in sc:
                level = float(sc["fixed_trigger"])
            else:
                continue
            amount    = round(reserve * sc["invest_pct"] / 100, 0)
            gap       = round(close - level, 0)
            triggered = close <= level
            triggers.append({"name": sc["name"], "trigger": level,
                              "amount": amount, "gap": gap, "triggered": triggered})

        triggers.sort(key=lambda x: -x["trigger"])
        return {"close": close, "close_date": close_date,
                "ath": ath, "ath_date": ath_date, "drop_pct": drop_pct,
                "ma20": ma20, "ma60": ma60, "ma240": ma240, "triggers": triggers}
    except Exception as e:
        print(f"[TWII] error: {e}", file=sys.stderr)
        return None


def main():
    logs: dict = {}
    cfg         = load_portfolio()
    portfolio   = cfg["portfolio"]
    loan_list   = cfg["loan"]
    etf_weights = cfg.get("etf_weights", {})
    thresholds  = cfg.get("thresholds",      {"high": 30, "midH": 15, "midL": -10, "low": -20})
    pos2_thr    = cfg.get("pos2_thresholds", {"high": 50, "midH": 20, "midL": -20, "low": -40})

    all_ticks  = list({h["tick"] for h in portfolio + loan_list if h.get("tick")})
    tw_ticks   = [t for t in all_ticks if t.endswith(".TW")]
    yf_symbols = all_ticks + ["USDTWD=X"]

    prices: dict[str, float] = {}
    try:
        prices = fetch_prices_yfinance(yf_symbols)
        logs["yfinance"] = f"ok ({len(prices)} prices)"
    except Exception as e:
        logs["yfinance"] = f"error: {e}"

    tw_missing = [t for t in tw_ticks if t not in prices]
    if tw_missing:
        try:
            twse_p = fetch_prices_twse(tw_missing)
            prices.update(twse_p)
            logs["twse_backup"] = f"ok ({len(twse_p)} prices)"
        except Exception as e:
            logs["twse_backup"] = f"error: {e}"

    # 個別 fallback：針對仍然缺漏的 .TW ticker（如債券 ETF）逐一抓取
    still_missing = [t for t in tw_ticks if t not in prices]
    if still_missing:
        recovered = []
        for sym in still_missing:
            try:
                hist = yf.Ticker(sym).history(period="1mo")
                if not hist.empty:
                    series = hist["Close"].dropna()
                    if not series.empty:
                        val = float(series.iloc[-1])
                        if val > 0:
                            prices[sym] = val
                            recovered.append(sym)
            except Exception:
                pass
        if recovered:
            logs["yf_individual"] = f"ok ({','.join(recovered)})"

    usd_twd = prices.get("USDTWD=X", 31.5)
    logs["usd_twd"] = round(usd_twd, 4)

    def val_twd(h) -> float:
        p = prices.get(h["tick"], h["price"]) if h.get("tick") else h["price"]
        return h["shares"] * p * usd_twd if h["cur"] == "USD" else h["shares"] * p

    def price_for(h) -> float:
        return prices.get(h["tick"], h["price"]) if h.get("tick") else h["price"]

    portfolio_items = []
    for h in portfolio:
        p = price_for(h)
        v = val_twd(h)
        c = h["cost"]
        ret = (v - c) / c * 100 if c > 0 else 0
        portfolio_items.append({**h, "price": round(p, 4), "value_twd": round(v, 2), "return_pct": round(ret, 2)})

    loan_items = []
    for h in loan_list:
        p = price_for(h)
        v = val_twd(h)
        c = h.get("cost", h["shares"] * h["perSh"])
        loan_items.append({**h, "price": round(p, 4), "value_twd": round(v, 2), "unrealized_pnl": round(v - c, 2)})

    cash_like  = set(cfg.get("cash_like_cats", []))
    targets    = cfg.get("targets", {})

    port_total = sum(h["value_twd"] for h in portfolio_items if h["cat"] not in cash_like)
    port_cost  = sum(h["cost"]      for h in portfolio_items if h["cat"] not in cash_like)
    cash_pool  = sum(h["value_twd"] for h in portfolio_items if h["cat"] in cash_like)
    loan_total = sum(h["value_twd"] for h in loan_items)
    loan_cost  = sum(h.get("cost", h["shares"] * h["perSh"]) for h in loan_list)
    loan_pnl   = loan_total - loan_cost
    avail_cash = cfg.get("available_cash", 0) + cash_pool
    stock_loan = cfg.get("stock_loan", 0)
    nav        = port_total + loan_total + avail_cash - stock_loan
    port_ret   = (port_total - port_cost) / port_cost * 100 if port_cost > 0 else 0

    # 系統操作建議（整體組合）
    if port_ret >= thresholds["high"]:
        signal, signal_level = "減倉", "high"
    elif port_ret >= thresholds["midH"]:
        signal, signal_level = "收割", "midH"
    elif port_ret >= thresholds["midL"]:
        signal, signal_level = "持倉", "neutral"
    elif port_ret >= thresholds["low"]:
        signal, signal_level = "加碼", "midL"
    else:
        signal, signal_level = "跌深", "low"

    # 正2導航燈號（00631L 個別報酬率，使用獨立門檻）
    pos2_ret, pos2_signal, pos2_level = 0.0, "持倉", "neutral"
    pos2 = next((h for h in portfolio_items if h.get("tick") == "00631L.TW"), None)
    if pos2:
        c2 = pos2.get("perSh", 0)
        p2 = pos2.get("price", 0)
        pos2_ret = (p2 - c2) / c2 * 100 if c2 > 0 else 0
        if pos2_ret >= pos2_thr["high"]:   pos2_signal, pos2_level = "減倉", "high"
        elif pos2_ret >= pos2_thr["midH"]: pos2_signal, pos2_level = "收割", "midH"
        elif pos2_ret >= pos2_thr["midL"]: pos2_signal, pos2_level = "持倉", "neutral"
        elif pos2_ret >= pos2_thr["low"]:  pos2_signal, pos2_level = "加碼", "midL"
        else:                              pos2_signal, pos2_level = "跌深", "low"

    # 分類 breakdown
    cat_breakdown = []
    for cat, tgt in targets.items():
        items     = [h for h in portfolio_items if h["cat"] == cat]
        cat_val   = sum(h["value_twd"] for h in items)
        cat_cost  = sum(h["cost"]      for h in items)
        cat_ret   = (cat_val - cat_cost) / cat_cost * 100 if cat_cost > 0 else 0
        actual_pct  = cat_val / nav * 100 if nav > 0 else 0
        target_pct  = tgt["pct"] * 100
        target_amt  = nav * tgt["pct"]
        cat_breakdown.append({
            "cat":           cat,
            "value":         round(cat_val, 0),
            "cost":          round(cat_cost, 0),
            "return_pct":    round(cat_ret, 2),
            "actual_pct":    round(actual_pct, 2),
            "target_pct":    round(target_pct, 2),
            "diff_pct":      round(actual_pct - target_pct, 2),
            "target_amount": round(target_amt, 0),
            "diff_amount":   round(cat_val - target_amt, 0),
            "strat":         tgt.get("strat", ""),
        })

    # 美股曝險
    us_exp_list = [
        {"sym": k, **v}
        for k, v in calc_us_exposure(portfolio_items, loan_items, prices, usd_twd, etf_weights).items()
    ]

    # 配息
    stock_map = {}
    for h in portfolio + loan_list:
        tick = h.get("tick", "")
        if tick and tick.endswith(".TW"):
            sid = tick.replace(".TW", "")
            if sid not in stock_map:
                stock_map[sid] = {"name": h["name"], "shares": h["shares"]}
            else:
                stock_map[sid]["shares"] = max(stock_map[sid]["shares"], h["shares"])

    static_upcoming = cfg.get("dividends_upcoming", [])
    static_ytd      = cfg.get("dividends_ytd", [])

    today    = today_str()
    yr_start = year_start_str()

    # 動態修正靜態 YTD 的已入帳狀態，並補齊 amount / exDate 欄位
    for d in static_ytd:
        pay = d.get("payDate") or d.get("date") or ""
        d["received"] = bool(pay and pay <= today)
        # 補算 amount（靜態資料未預先計算）
        if not d.get("amount"):
            ps = float(d.get("perShare") or 0)
            sh = float(d.get("shares") or 0)
            d["amount"] = round(ps * sh, 2)
        # 補 exDate = date（前端 key 生成需要 exDate）
        if not d.get("exDate") and d.get("date"):
            d["exDate"] = d["date"]

    # 過濾已過期的靜態 upcoming（避免舊日期殘留在除息預告區塊）
    static_upcoming_valid = [d for d in static_upcoming if d.get("exDate", "") >= today]

    div_upcoming, div_ytd = [], []
    if FINMIND_TOKEN:
        try:
            div_upcoming, div_ytd = fetch_finmind_dividends(FINMIND_TOKEN, stock_map)
            logs["finmind"] = f"ok (upcoming:{len(div_upcoming)}, ytd:{len(div_ytd)})"
        except Exception as e:
            logs["finmind"] = f"error: {e}"

    # yfinance 備援：FinMind 失敗或無 token 時，改用 yfinance 抓真實歷史除息資料
    if not div_upcoming and not div_ytd:
        try:
            div_upcoming, div_ytd = fetch_dividends_yfinance(stock_map, today, yr_start)
            logs["yf_dividends"] = f"ok (upcoming:{len(div_upcoming)}, ytd:{len(div_ytd)})"
        except Exception as e:
            logs["yf_dividends"] = f"error: {e}"

    if not FINMIND_TOKEN and "yf_dividends" not in logs:
        logs["finmind"] = "no token — using portfolio.json cache"

    # 合併靜態資料（補 API 沒抓到的標的，例如基金、無代碼持股）
    def _div_key(d: dict) -> str:
        return (d.get("stock", "") or d.get("stock_id", "")) + (d.get("exDate") or d.get("date", ""))

    def _merge_divs(api_list: list, static_list: list) -> list:
        seen = {_div_key(d) for d in api_list}
        result = list(api_list)
        for d in static_list:
            if _div_key(d) not in seen:
                result.append(d)
        return result

    div_upcoming = _merge_divs(div_upcoming, static_upcoming_valid)
    div_ytd      = _merge_divs(div_ytd, static_ytd)
    div_upcoming.sort(key=lambda x: x.get("exDate", ""))
    div_ytd.sort(key=lambda x: x.get("exDate") or x.get("date", ""))

    ytd_total       = sum(d.get("amount", d.get("perShare", 0) * d.get("shares", 0)) for d in div_ytd)
    annual_estimate = ytd_total * (12 / max(datetime.now(TZ_TW).month, 1))
    ytd_yield       = ytd_total / nav * 100 if nav > 0 else 0
    thirty_days_later = str((datetime.now(TZ_TW) + timedelta(days=30)).date())
    next30d = sum(
        d.get("amount", d.get("perShare", 0) * d.get("shares", 0))
        for d in div_upcoming
        if d.get("exDate", "") <= thirty_days_later
    )

    # 台股大盤進場觸發狀況
    existing_ath, existing_ath_date = 0.0, ""
    try:
        with open("data.json", "r", encoding="utf-8") as _f:
            _prev = json.load(_f)
            _mkt = _prev.get("twii_market") or {}
            existing_ath      = float(_mkt.get("ath", 0) or 0)
            existing_ath_date = _mkt.get("ath_date", "")
    except Exception:
        pass
    twii_market = fetch_twii_market(cfg, nav, existing_ath, existing_ath_date)
    if twii_market:
        logs["twii"] = f"ok ({twii_market['close']:,.0f} | ATH:{twii_market['ath']:,.0f})"
    else:
        logs["twii"] = "error: failed to fetch"

    output = {
        "updated_at":    now_tw(),
        "exchange_rate": round(usd_twd, 4),
        "summary": {
            "nav":                  round(nav, 0),
            "portfolio_total":      round(port_total, 0),
            "portfolio_cost":       round(port_cost, 0),
            "portfolio_return_pct": round(port_ret, 2),
            "loan_total":           round(loan_total, 0),
            "loan_pnl":             round(loan_pnl, 0),
            "available_cash":       round(avail_cash, 0),
            "stock_loan":           round(stock_loan, 0),
            "signal":               signal,
            "signal_level":         signal_level,
            "pos2_signal":          pos2_signal,
            "pos2_signal_level":    pos2_level,
            "pos2_return_pct":      round(pos2_ret, 2),
            "thresholds":           thresholds,
            "cat_breakdown":        cat_breakdown,
        },
        "portfolio": portfolio_items,
        "loan":      loan_items,
        "dividends": {
            "upcoming":         div_upcoming,
            "ytd":              div_ytd,
            "ytd_total":        round(ytd_total, 0),
            "annual_estimate":  round(annual_estimate, 0),
            "next30d_estimate": round(next30d, 0),
            "ytd_yield_pct":    round(ytd_yield, 2),
        },
        "us_exposure": us_exp_list,
        "targets": {
            cat: {"pct": float(tgt["pct"]), "strat": tgt.get("strat", "")}
            for cat, tgt in targets.items()
        },
        "cash_like_cats": list(cfg.get("cash_like_cats", [])),
        "etf_weights":    etf_weights,
        "twii_market":    twii_market,
        "logs": logs,
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=True, indent=2)

    print(f"[OK] data.json updated at {output['updated_at']}")
    print(f"     NAV={nav:,.0f}  prices={len(prices)}  errors={[k for k,v in logs.items() if 'error' in str(v)]}")


if __name__ == "__main__":
    main()
