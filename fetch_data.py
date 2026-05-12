#!/usr/bin/env python3
"""
發財888888 資產儀表板 — 自動抓價腳本
由 GitHub Actions 每小時執行，更新 data.json

數據來源:
  - yfinance       : 美股 + 台股 + USD/TWD（主）
  - TWSE API       : 台股即時報價（備援，無需 API Key）
  - FinMind v4     : 台股除權息資料（需 token，GitHub Secret: FINMIND_TOKEN）
"""

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta

import requests
import yfinance as yf

TZ_TW = timezone(timedelta(hours=8))
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")


# ─────────────────────────────────────────
# 工具函式
# ─────────────────────────────────────────
def now_tw() -> str:
    return datetime.now(TZ_TW).isoformat()

def today_str() -> str:
    return datetime.now(TZ_TW).date().isoformat()

def year_start_str() -> str:
    return datetime.now(TZ_TW).date().replace(month=1, day=1).isoformat()

def load_portfolio() -> dict:
    with open("portfolio.json", "r", encoding="utf-8") as f:
        return json.load(f)


# ─────────────────────────────────────────
# 1. yfinance 批次抓價（台股 + 美股 + 匯率）
# ─────────────────────────────────────────
def fetch_prices_yfinance(symbols: list[str]) -> dict[str, float]:
    """
    回傳 {symbol: price}
    台股用 .TW 後綴（yfinance 支援）
    匯率用 USDTWD=X
    """
    prices: dict[str, float] = {}
    if not symbols:
        return prices

    try:
        raw = yf.download(
            tickers=symbols,
            period="5d",
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=True,
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


# ─────────────────────────────────────────
# 2. TWSE 即時 API 備援（台股，無需 Key）
# ─────────────────────────────────────────
def fetch_prices_twse(tw_tickers: list[str]) -> dict[str, float]:
    """
    直接呼叫 TWSE MIS API（無 CORS 問題，伺服器端）
    先試上市（tse_），再試上櫃（otc_）
    """
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
        ex_ch = "|".join(
            f"{prefix}{t.replace('.TW','').lower()}.tw" for t in tickers
        )
        url = (
            f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
            f"?ex_ch={ex_ch}&json=1&delay=0"
        )
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return _parse(r.json().get("msgArray", []))

    # TSE（上市）
    try:
        prices.update(_fetch("tse_", tw_tickers))
    except Exception as e:
        print(f"[TWSE tse_] error: {e}", file=sys.stderr)

    # OTC（上櫃）for missing
    missing = [t for t in tw_tickers if t not in prices]
    if missing:
        try:
            prices.update(_fetch("otc_", missing))
        except Exception as e:
            print(f"[TWSE otc_] error: {e}", file=sys.stderr)

    return prices


# ─────────────────────────────────────────
# 3. FinMind 配息資料
# ─────────────────────────────────────────
def fetch_finmind_dividends(
    token: str,
    stock_map: dict[str, dict],   # {stock_id: {name, shares}}
) -> tuple[list, list]:
    """
    回傳 (upcoming, ytd)
    upcoming: 除息日 >= 今天
    ytd     : 今年除息日 < 今天
    """
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
                ex_date = d.get("CashExDividendTradingDate", "")
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

            time.sleep(0.2)   # 避免超過 FinMind rate limit

        except Exception as e:
            print(f"[FinMind] {stock_id}: {e}", file=sys.stderr)

    upcoming.sort(key=lambda x: x["exDate"])
    ytd.sort(key=lambda x: x["exDate"])
    return upcoming, ytd


# ─────────────────────────────────────────
# 4. 美股曝險計算（直接持股 + ETF 內含）
# ─────────────────────────────────────────
def calc_us_exposure(portfolio, loan, prices, usd_twd, etf_weights):
    exposure: dict[str, dict] = {}

    def add(sym: str, value_usd: float):
        if sym not in exposure:
            exposure[sym] = {"value_usd": 0.0, "value_twd": 0.0}
        exposure[sym]["value_usd"] += value_usd
        exposure[sym]["value_twd"] += value_usd * usd_twd

    for h in list(portfolio) + list(loan):
        tick = h.get("tick") or ""
        if not tick:
            continue

        etf_id = tick.replace(".TW", "")

        # 直接美股
        if not tick.endswith(".TW") and h["cur"] == "USD":
            price = prices.get(tick, h["price"])
            add(tick, h["shares"] * price)

        # 含美股的台股 ETF
        elif etf_id in etf_weights:
            etf_price = prices.get(tick, h["price"])
            etf_value_twd = h["shares"] * etf_price
            etf_value_usd = etf_value_twd / usd_twd
            for us_sym, weight in etf_weights[etf_id].items():
                add(us_sym, etf_value_usd * weight)

        # 美股 ETF (TOPT etc.)
        elif not tick.endswith(".TW") and h["cur"] == "USD" and etf_id in etf_weights:
            etf_price = prices.get(tick, h["price"])
            etf_value_usd = h["shares"] * etf_price
            for us_sym, weight in etf_weights[etf_id].items():
                add(us_sym, etf_value_usd * weight)

    # 排序
    return dict(
        sorted(exposure.items(), key=lambda x: x[1]["value_twd"], reverse=True)
    )


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
def main():
    logs: dict = {}
    cfg = load_portfolio()
    portfolio = cfg["portfolio"]
    loan_list = cfg["loan"]
    etf_weights = cfg.get("etf_weights", {})

    # ── 收集所有 ticker ──────────────────
    all_ticks = list({
        h["tick"]
        for h in portfolio + loan_list
        if h.get("tick")
    })
    tw_ticks = [t for t in all_ticks if t.endswith(".TW")]
    us_ticks = [t for t in all_ticks if not t.endswith(".TW")]
    yf_symbols = all_ticks + ["USDTWD=X"]

    # ── 抓價（yfinance 主，TWSE 備援台股）───
    prices: dict[str, float] = {}

    try:
        prices = fetch_prices_yfinance(yf_symbols)
        logs["yfinance"] = f"ok ({len(prices)} prices)"
    except Exception as e:
        logs["yfinance"] = f"error: {e}"

    # 任何台股 yfinance 沒抓到 → TWSE 備援
    tw_missing = [t for t in tw_ticks if t not in prices]
    if tw_missing:
        try:
            twse_p = fetch_prices_twse(tw_missing)
            prices.update(twse_p)
            logs["twse_backup"] = f"ok ({len(twse_p)} prices)"
        except Exception as e:
            logs["twse_backup"] = f"error: {e}"

    usd_twd = prices.get("USDTWD=X", 31.5)
    logs["usd_twd"] = round(usd_twd, 4)

    # ── 計算持倉現值 ─────────────────────
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
        portfolio_items.append({
            **h,
            "price": round(p, 4),
            "value_twd": round(v, 2),
            "return_pct": round(ret, 2),
        })

    loan_items = []
    for h in loan_list:
        p = price_for(h)
        v = val_twd(h)
        c = h.get("cost", h["shares"] * h["perSh"])
        pnl = v - c
        loan_items.append({
            **h,
            "price": round(p, 4),
            "value_twd": round(v, 2),
            "unrealized_pnl": round(pnl, 2),
        })

    # ── 匯總 ─────────────────────────────
    cash_like = set(cfg.get("cash_like_cats", []))
    targets = cfg.get("targets", {})
    cat_order = list(targets.keys())

    port_total = sum(h["value_twd"] for h in portfolio_items
                     if h["cat"] not in cash_like)
    port_cost   = sum(h["cost"] for h in portfolio_items
                      if h["cat"] not in cash_like)
    cash_pool   = sum(h["value_twd"] for h in portfolio_items
                      if h["cat"] in cash_like)
    loan_total  = sum(h["value_twd"] for h in loan_items)
    loan_cost   = sum(h.get("cost", h["shares"] * h["perSh"]) for h in loan_list)
    loan_pnl    = loan_total - loan_cost
    avail_cash  = cfg.get("available_cash", 0) + cash_pool
    stock_loan  = cfg.get("stock_loan", 0)
    nav         = port_total + loan_total + avail_cash - stock_loan

    port_ret = (port_total - port_cost) / port_cost * 100 if port_cost > 0 else 0

    # 各分類 breakdown
    cat_breakdown = []
    investable = nav - avail_cash + stock_loan  # for % calculation base
    for cat, tgt in targets.items():
        items = [h for h in portfolio_items if h["cat"] == cat]
        cat_val  = sum(h["value_twd"] for h in items)
        cat_cost = sum(h["cost"] for h in items)
        cat_ret  = (cat_val - cat_cost) / cat_cost * 100 if cat_cost > 0 else 0
        actual_pct = cat_val / nav * 100 if nav > 0 else 0
        target_pct = tgt["pct"] * 100
        diff_pct   = actual_pct - target_pct
        cat_breakdown.append({
            "cat":        cat,
            "value":      round(cat_val, 0),
            "cost":       round(cat_cost, 0),
            "return_pct": round(cat_ret, 2),
            "actual_pct": round(actual_pct, 2),
            "target_pct": round(target_pct, 2),
            "diff_pct":   round(diff_pct, 2),
            "strat":      tgt.get("strat", ""),
        })

    # ── 美股曝險 ─────────────────────────
    us_exposure = calc_us_exposure(
        portfolio_items, loan_items, prices, usd_twd, etf_weights
    )
    us_exp_list = [
        {"sym": k, **v, "value_twd": round(v["value_twd"], 0),
         "value_usd": round(v["value_usd"], 2)}
        for k, v in us_exposure.items()
    ]

    # ── 配息資料 ─────────────────────────
    # 建立 stock_id → {name, shares}
    stock_map = {}
    for h in portfolio + loan_list:
        tick = h.get("tick", "")
        if tick and tick.endswith(".TW"):
            sid = tick.replace(".TW", "")
            if sid not in stock_map:
                stock_map[sid] = {"name": h["name"], "shares": h["shares"]}
            else:
                stock_map[sid]["shares"] = max(
                    stock_map[sid]["shares"], h["shares"]
                )

    div_upcoming, div_ytd = [], []
    if FINMIND_TOKEN:
        try:
            div_upcoming, div_ytd = fetch_finmind_dividends(
                FINMIND_TOKEN, stock_map
            )
            logs["finmind"] = f"ok (upcoming:{len(div_upcoming)}, ytd:{len(div_ytd)})"
        except Exception as e:
            logs["finmind"] = f"error: {e}"
            div_upcoming = cfg.get("dividends_upcoming", [])
            div_ytd      = cfg.get("dividends_ytd", [])
    else:
        logs["finmind"] = "no token — using portfolio.json cache"
        div_upcoming = cfg.get("dividends_upcoming", [])
        div_ytd      = cfg.get("dividends_ytd", [])

    ytd_total = sum(d.get("amount", d.get("perShare", 0) * d.get("shares", 0))
                    for d in div_ytd)
    annual_estimate = ytd_total * (12 / max(datetime.now(TZ_TW).month, 1))

    # ── 輸出 data.json ───────────────────
    output = {
        "updated_at": now_tw(),
        "exchange_rate": round(usd_twd, 4),
        "summary": {
            "nav":                round(nav, 0),
            "portfolio_total":    round(port_total, 0),
            "portfolio_cost":     round(port_cost, 0),
            "portfolio_return_pct": round(port_ret, 2),
            "loan_total":         round(loan_total, 0),
            "loan_pnl":           round(loan_pnl, 0),
            "available_cash":     round(avail_cash, 0),
            "stock_loan":         round(stock_loan, 0),
            "cat_breakdown":      cat_breakdown,
        },
        "portfolio": portfolio_items,
        "loan": loan_items,
        "dividends": {
            "upcoming":        div_upcoming,
            "ytd":             div_ytd,
            "ytd_total":       round(ytd_total, 0),
            "annual_estimate": round(annual_estimate, 0),
        },
        "us_exposure": us_exp_list,
        "logs": logs,
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"[OK] data.json updated at {output['updated_at']}")
    print(f"     NAV={nav:,.0f}  prices={len(prices)}  errors={[k for k,v in logs.items() if 'error' in str(v)]}")


if __name__ == "__main__":
    main()
