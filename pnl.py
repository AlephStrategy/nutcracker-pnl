'''P&L checker - Browser Console version.
Works with app.py on backend.
Efficiency version. Works by reversing trades to arrive to historical balances and analyze trades. 
Includes balances from lend and margin accounts. Doesn't see balances used as collateral. 
Calculates daily volatility noise available for harvesting. 
Outputs 3-level summary: Buy-and-hold as a floor, actual performance (realized and unrealized), potential ceiling if harvested every intra-day volatility.
Trading fees are accounted for in analyze_trades and balances rewind.
Allows manual entry for missing withdrawal-deposits
 '''

import ccxt
import pandas as pd
import time
from datetime import datetime, timedelta, timezone
import copy
import requests
import warnings
import os
import json
from pathlib import Path
import logging
logging.basicConfig(level=logging.INFO)
logging.info("Backend loaded — markets and modules initialized.")

class BinanceNoSapi(ccxt.binance):
    def fetch_currencies(self, params={}):
        return {}

def init_exchange(exchange_name, api_key, api_secret, api_password=None, load_markets=True):
    exchange_name = exchange_name.lower()

    if exchange_name == "gateio":
        exchange_name = "gate"

    if exchange_name == "huobi":
        exchange_name = "htx"

    if exchange_name == "binance":
        exchange_class = BinanceNoSapi
    else:
        exchange_class = getattr(ccxt, exchange_name)

    params = {
        'enableRateLimit': True,
        'apiKey': api_key,
        'secret': api_secret,
    }
    if api_password:
        params['password'] = api_password

    exchange = exchange_class(params)

    # Only load markets if requested
    if load_markets:
        exchange.load_markets()

    return exchange




CORE_QUOTES = [
    "USDT", "USDC", "BTC", "ETH", "XRP",
    "SOL", "BNB", "BGB",
    "EUR", "USD", "GBP"
]
PRIMARY_QUOTE = "USDC"
SECONDARY_QUOTE = "USDT"
TERTIARY_QUOTES = ["USD", "EUR", "GBP"]



def infer_quotes(exchange, balances, trades):
    markets = exchange.load_markets()
    
    quotes_from_markets = {m.split('/')[1] for m in markets if '/' in m}
    quotes_from_trades = {t['symbol'].split('/')[1] for t in trades if '/' in t['symbol']}
    quotes_from_balances = {asset for asset, bal in balances.items() if bal > 0}

    inferred = quotes_from_markets | quotes_from_trades | quotes_from_balances
    return inferred

def get_all_quotes(exchange, balances=None, trades=None):
    inferred = infer_quotes(exchange, balances or {}, trades or [])
    all_quotes = set(CORE_QUOTES) | inferred
    return sorted(all_quotes)

def get_conversion_rate(asset, quote, prices_map, conversion_rates):
    # Try direct ticker first
    direct = prices_map.get(f"{asset}/{quote}")
    if direct and direct > 0:
        return direct

    inverse = prices_map.get(f"{quote}/{asset}")
    if inverse and inverse > 0:
        return 1.0 / inverse

    # Fallback to full conversion engine
    return convert(1.0, asset, quote, conversion_rates)



def discover_relevant_pairs(exchange, active_assets, spot_markets,
                            fee_currencies=None, flow_assets=None):
    """
    Discovers only the SPOT trading pairs relevant to the user's portfolio.
    Uses asset × quote_priority scanning (fast, safe, Binance-friendly),
    with a universal fallback to recover hidden markets (Bitrue, Bitmart, LBank, Gate, Huobi).
    """
    if fee_currencies is None:
        fee_currencies = set()
    if flow_assets is None:
        flow_assets = set()

    # Assets that matter for trade scanning
    relevant_assets = set(active_assets) | set(fee_currencies) | set(flow_assets)

    # Priority quotes (expanded: XRP added)
    quote_priorities = ["USDC", "USDT", "BTC", "ETH", "BNB", "XRP"]

    discovery = set()

    # --- Primary discovery using normalized spot_markets ---
    for asset in relevant_assets:
        base = asset[2:] if (asset.startswith('LD') and asset != 'LDO' and len(asset) > 3) else asset

        # Skip stablecoins as base
        if base in ["USDC", "USDT", "FDUSD", "TUSD", "DAI"]:
            continue

        for quote in quote_priorities:
            if base == quote:
                continue

            symbol = f"{base}/{quote}"
            if symbol in spot_markets:
                discovery.add(symbol)

    # ---------------------------------------------------------
    # UNIVERSAL FALLBACK: Recover hidden markets from raw CCXT data
    # (Fixes Bitrue, Bitmart, LBank, Gate, Huobi, OKX quirks)
    # ---------------------------------------------------------
    raw_markets = exchange.markets  # raw CCXT market map

    for asset in relevant_assets:
        base = asset[2:] if (asset.startswith('LD') and asset != 'LDO' and len(asset) > 3) else asset

        if base in ["USDC", "USDT", "FDUSD", "TUSD", "DAI"]:
            continue

        for quote in quote_priorities:
            if base == quote:
                continue

            symbol = f"{base}/{quote}"
            market_id = f"{base}{quote}".upper()

            # Skip if already discovered
            if symbol in discovery:
                continue

            # If raw market exists, synthesize the symbol
            if market_id in raw_markets:
                m = raw_markets[market_id]

                # Skip futures/swap markets
                if m.get("type") not in (None, "spot"):
                    continue

                # Skip inactive markets
                if m.get("active") is False:
                    continue

                # Add synthesized spot market
                discovery.add(symbol)

    return list(discovery)

def safe_fetch_tickers(exchange, symbols):
    """
    Universal ticker fetcher that works across all ccxt exchanges.
    - Binance/OKX/KuCoin: supports list → use fetch_tickers(list)
    - Bitget: does NOT support list → fetch_ticker() per symbol
    - Fallback: per-symbol fetch
    """
    try:
        # Exchanges that support list input
        return exchange.fetch_tickers(symbols)
    except Exception:
        pass

    # Bitget and others → fallback to per-symbol
    tickers = {}
    for sym in symbols:
        try:
            tickers[sym] = exchange.fetch_ticker(sym)
        except Exception:
            continue
    return tickers

def fetch_price_history_map(exchange, symbol, days=30):
    try:
        limit = days + 10 
        
        ohlcv = exchange.fetch_ohlcv(symbol, '1d', limit=limit)
        if not ohlcv or len(ohlcv) == 0:
            ohlcv = exchange.fetch_ohlcv(symbol, '4h', limit=limit*6)

        if not ohlcv:
            return {}, []   # return empty price_map AND empty ohlcv

        price_map = {}
        for day in ohlcv:
            ts = datetime.fromtimestamp(day[0] / 1000, tz=timezone.utc)
            date_str = ts.strftime('%Y-%m-%d')
            price_map[date_str] = float(day[4])  # close price

        return price_map, ohlcv

    except Exception as e:
        logging.warning(f"History Fetch Failed ({symbol}): {e}")
        return {}, []

def normalize_pair(pair):
    return pair.replace(":", "/").replace("-", "/").upper()


# Add this above main()
def prefetch_all_price_histories(exchange, currency_pairs, days):
    all_histories = {}
    logging.info(f"Pre-fetching price history for {len(currency_pairs)} pairs...")
    
    for i, symbol in enumerate(currency_pairs):
        try:
            price_map, ohlcv = fetch_price_history_map(exchange, symbol, days)

            all_histories[symbol] = {
                "price_map": price_map,
                "ohlcv": ohlcv
            }
            
            time.sleep(1.5)
            
            if (i + 1) % 10 == 0:
                logging.info(f"  ...fetched {i + 1}/{len(currency_pairs)} pairs")
                
        except Exception as e:
            logging.warning(f"Could not fetch history for {symbol}: {e}")
            
    return all_histories


# Function to fetch filled orders for a specific currency symbol
def fetch_filled_orders(exchange, symbol, since):
    """
    Optimized for Bitrue: Single fetch with recent-history fallback 
    to prevent excessive rate-limiting delays.
    """
    try:
        # Step 1: Attempt to fetch from 'since'
        trades = exchange.fetch_my_trades(symbol, since=since)
        
        # Step 2: Fallback - If empty, Bitrue might be ignoring 'since' 
        # or it's a very active pair. Fetch the last 200.
        if not trades:
            trades = exchange.fetch_my_trades(symbol, limit=200)
            
        # Filter and Deduplicate
        valid_trades = [t for t in trades if t['timestamp'] >= since]
        # Remove potential duplicates by ID
        unique_trades = {t['id']: t for t in valid_trades}
        
        return sorted(unique_trades.values(), key=lambda x: x['timestamp'])

    except Exception as e:
        # Silence 'Not Found' but log others
        if "not found" not in str(e).lower():
            logging.error(f"Trade Error ({symbol}): {e}")
        return []

    except Exception as e:
        if "not found" not in str(e).lower():
            logging.error(f"Error fetching trades for {symbol}: {e}")
        return []

def convert(amount, from_asset, to_asset, conversion_rates):
    # Stablecoin 1:1 fallback
    stables = {"USDT", "USDC", "FDUSD", "TUSD", "DAI", "USDD", "USDJ"}
    if from_asset in stables and to_asset in stables:
        return amount

    if from_asset == to_asset:
        return amount

    # direct
    direct = conversion_rates.get(f"{from_asset}/{to_asset}")
    if direct:
        return amount * direct

    # inverse
    inverse = conversion_rates.get(f"{to_asset}/{from_asset}")
    if inverse:
        return amount / inverse

    # multi-hop via BTC
    if conversion_rates.get(f"{from_asset}/BTC") and conversion_rates.get(f"BTC/{to_asset}"):
        return amount * conversion_rates[f"{from_asset}/BTC"] * conversion_rates[f"BTC/{to_asset}"]

    # multi-hop via ETH
    if conversion_rates.get(f"{from_asset}/ETH") and conversion_rates.get(f"ETH/{to_asset}"):
        return amount * conversion_rates[f"{from_asset}/ETH"] * conversion_rates[f"ETH/{to_asset}"]

    # NEW: multi-hop via USDT
    if conversion_rates.get(f"{from_asset}/USDT") and conversion_rates.get(f"USDT/{to_asset}"):
        return amount * conversion_rates[f"{from_asset}/USDT"] * conversion_rates[f"USDT/{to_asset}"]

    return None




# Function to analyze trade data
def analyze_trades(trades, conversion_rates, base_quote="USDC"):
    """
    Hybrid V3: Precision Matcher with Friction (Net Fees).
    Tail trimming and fee logic preserved exactly.
    Output is in base_quote (USDC by default).
    """
    if not trades:
        return 0.0, 0.0, 0.0, 0.0, 0

    symbol = trades[0]['symbol']
    base, quote = symbol.split('/')

    # 1. Sort newest first (LIFO-ish)
    sorted_trades = sorted(trades, key=lambda x: x['timestamp'], reverse=True)

    buys = []
    sells = []

    # 2. Pre-process net impact (fee-aware)
    for t in sorted_trades:
        side  = t['side']
        amount = float(t.get('amount', 0))
        cost   = float(t.get('cost', 0))
        price  = float(t.get('price', 0))

        fee = t.get('fee', {}) or {}
        f_cost = float(fee.get('cost', 0)) if fee.get('cost') is not None else 0.0
        f_curr = fee.get('currency')

        if side == 'buy':
            # Net qty received
            net_qty = amount - (f_cost if f_curr == base else 0)

            # Net cost paid (in quote)
            net_val = cost + (f_cost if f_curr == quote else 0)

            # Third-currency fee
            if f_curr and f_curr not in [base, quote]:
                rate = conversion_rates.get(f"{f_curr}/{quote}") or \
                       (1.0 / conversion_rates.get(f"{quote}/{f_curr}", 1.0))
                net_val += f_cost * rate

            buys.append({'qty': net_qty, 'val': net_val, 'price': price})

        else:  # sell
            # Net qty paid
            net_qty = amount + (f_cost if f_curr == base else 0)

            # Net revenue received (in quote)
            net_val = cost - (f_cost if f_curr == quote else 0)

            # Third-currency fee
            if f_curr and f_curr not in [base, quote]:
                rate = conversion_rates.get(f"{f_curr}/{quote}") or \
                       (1.0 / conversion_rates.get(f"{quote}/{f_curr}", 1.0))
                net_val -= f_cost * rate

            sells.append({'qty': net_qty, 'val': net_val, 'price': price})

    # 3. Determine matching volume
    total_buy_vol  = sum(b['qty'] for b in buys)
    total_sell_vol = sum(s['qty'] for s in sells)
    match_vol = min(total_buy_vol, total_sell_vol)

    if match_vol <= 0:
        return 0.0, 0.0, 0.0, 0.0, len(trades)

    # 4. Tail trimming (unchanged)
    def trim_to_target(trade_list, target_vol):
        accum_qty = 0.0
        accum_val = 0.0

        for t in trade_list:
            if accum_qty >= target_vol:
                break

            remaining = target_vol - accum_qty
            take_qty = min(t['qty'], remaining)

            fraction = take_qty / t['qty'] if t['qty'] > 0 else 0
            accum_val += t['val'] * fraction
            accum_qty += take_qty

        return accum_val, accum_qty

    matched_buy_val,  matched_buy_qty  = trim_to_target(buys,  match_vol)
    matched_sell_val, matched_sell_qty = trim_to_target(sells, match_vol)

    # 5. Convert matched values (in quote) → USDC
    #    This is the ONLY part that changed.
    rate_quote_to_usdc = convert(1.0, quote, base_quote, conversion_rates) or 1.0


    final_buy_size_usdc  = matched_buy_val  * rate_quote_to_usdc
    final_sell_size_usdc = matched_sell_val * rate_quote_to_usdc


    return matched_buy_qty, final_buy_size_usdc, matched_sell_qty, final_sell_size_usdc, len(trades)

def unwrap_balance(value):
    try:
        # logging.info(f"Unwrapping balance value: {value}")
        if isinstance(value, dict):
            unwrapped = float(value.get("total") or value.get("free") or 0.0)
            # logging.info(f"Unwrapped dict balance: {unwrapped}")
            return unwrapped
        unwrapped = float(value or 0.0)
        #logging.debug(f"Unwrapped raw balance: {unwrapped}")
        return unwrapped
    except (ValueError, TypeError):
        logging.warning(f"Invalid balance value encountered: {value}. Defaulting to 0.")
        return 0.0


def fetch_total_aggregated_balance(exchange):
    """
    Standardized balance fetcher. Gracefully skips Margin/Earn if unavailable.
    """
    total_balances = {}
    
    # 1. Spot balances
    try:
        spot = exchange.fetch_balance()
        for asset, total in spot.get('total', {}).items():
            val = float(total) if total is not None else 0.0
            if val > 0:
                total_balances[asset] = total_balances.get(asset, 0) + val
    except Exception as e:
        logging.error(f"Critical Error: Could not fetch Spot balances: {e}")
        return {}

    # 2. Margin balances (if supported)
    try:
        if hasattr(exchange, 'fetch_margin_balance'):
            margin = exchange.fetch_margin_balance()
            for asset, total in margin.get('total', {}).items():
                val = float(total) if total is not None else 0.0
                if val > 0:
                    total_balances[asset] = total_balances.get(asset, 0) + val
    except Exception:
        logging.debug("Margin info not available for this exchange.")

    # 3. Binance Earn (Simple Earn Flexible)
    if exchange.id == 'binance':
        try:
            flex = exchange.sapi_get_simple_earn_flexible_position()
            for pos in flex.get('rows', []):
                total_balances[f"LD{pos['asset']}"] = float(pos['totalAmount'])
        except Exception:
            pass
        
    # Remove dust
    return {k: v for k, v in total_balances.items() if v > 0.00000001}

    
def get_best_price(history_map, target_date, fallback_price=0.0):
    """Finds the closest price in history <= target_date."""
    if not history_map:
        return fallback_price

    # Sort dates to find the nearest predecessor
    available_dates = sorted(history_map.keys())
    price = fallback_price
    for d in available_dates:
        if d <= target_date:
            price = history_map[d]
        else:
            break
    return price if price > 0 else fallback_price


def calculate_portfolio_value_usdc(balances, prices_map, conversion_rates, log_warnings=False):
    """
    Value each asset in USDC using date-specific prices_map first,
    then falling back to the universal conversion engine.
    """
    total_value_usdc = 0.0

    for asset, amount_raw in balances.items():
        amount = unwrap_balance(amount_raw)
        if amount == 0:
            continue

        if asset == "USDC":
            price_usdc = 1.0
        else:
            price_usdc = get_conversion_rate(asset, "USDC", prices_map, conversion_rates)

        if price_usdc is not None and price_usdc > 0:
            total_value_usdc += amount * price_usdc
        elif log_warnings and amount > 0.01:
            logging.warning(f"Price Discovery Failed: No USDC price for {asset} (Amt: {amount})")

    return total_value_usdc



def fetch_portfolio_flows(exchange, since, active_assets=None):
    """
    Universal flow fetcher for ALL exchanges.
    Tries bulk first, then per-asset, then fallback patterns.
    Returns: { date: [ {type, asset, amount}, ... ] }
    """
    active_assets = active_assets or []

    start_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"[{start_ts}] Starting flow fetch for exchange: {exchange.id}")
    logging.info("This process may take up to several minutes depending on the exchange API speed.")

    all_flows = []

    # -----------------------------
    # 1. Try bulk fetch (fast path)
    # -----------------------------
    logging.info("Attempting bulk flow fetch (fast mode)...")

    try:
        deposits = exchange.fetch_deposits(since=since)
        withdrawals = exchange.fetch_withdrawals(since=since)

        if deposits or withdrawals:
            logging.info(f"Bulk fetch succeeded: {len(deposits or [])} deposits, {len(withdrawals or [])} withdrawals.")
            all_flows.extend(deposits or [])
            all_flows.extend(withdrawals or [])
        else:
            logging.info("Bulk fetch returned no results.")
    except Exception as e:
        logging.info(f"Bulk fetch failed: {str(e)}")
        deposits, withdrawals = [], []

    # If bulk returned something, skip per-asset
    if all_flows:
        logging.info("Bulk mode successful — skipping per-asset fallback.")
        return normalize_flows(all_flows)

    # -----------------------------
    # 2. Per-asset fallback (slow)
    # -----------------------------
    logging.info(f"Bulk flow fetch empty — falling back to per-asset mode for {exchange.id}.")
    logging.info(f"Scanning {len(active_assets)} assets individually...")

    for idx, asset in enumerate(active_assets, start=1):
        logging.info(f"[{idx}/{len(active_assets)}] Checking flows for asset: {asset}")

        variants = set([
            asset,
            asset.upper(),
            asset.lower(),
            asset[2:] if asset.startswith("LD") else asset
        ])

        for code in variants:
            try:
                d = exchange.fetch_deposits(code=code, since=since)
                w = exchange.fetch_withdrawals(code=code, since=since)

                if d or w:
                    logging.info(f"  Found {len(d or [])} deposits and {len(w or [])} withdrawals for {code}.")
                    all_flows.extend(d or [])
                    all_flows.extend(w or [])
                time.sleep(exchange.rateLimit / 1000)
            except Exception:
                continue

    # -----------------------------
    # 3. Ledger fallback (MEXC/OKX)
    # -----------------------------
    if not all_flows and hasattr(exchange, "fetch_ledger"):
        logging.info("Per-asset mode returned no results — trying fetch_ledger() fallback...")

        try:
            ledger = exchange.fetch_ledger(since=since)
            count = 0
            for entry in ledger:
                if entry.get("type") in ["deposit", "withdrawal"]:
                    all_flows.append(entry)
                    count += 1
            logging.info(f"fetch_ledger() found {count} flow entries.")
        except Exception as e:
            logging.info(f"fetch_ledger() failed: {str(e)}")

    # -----------------------------
    # 4. Final summary
    # -----------------------------
    end_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"[{end_ts}] Flow fetch complete. Total flow entries found: {len(all_flows)}")

    return normalize_flows(all_flows)


def normalize_flows(all_flows):
    flow_map = {}

    for f in all_flows:
        try:
            ts = f.get('timestamp') or f.get('updated') or f.get('created_at')
            if not ts:
                continue

            date = datetime.fromtimestamp(ts / 1000).strftime('%Y-%m-%d')

            # Detect type
            raw_type = (f.get('type') or '').lower()
            info_str = str(f.get('info', {})).lower()

            if 'deposit' in raw_type or 'deposit' in info_str:
                ftype = 'deposit'
            elif 'withdraw' in raw_type or 'withdraw' in info_str:
                ftype = 'withdrawal'
            else:
                # Unknown → infer from amount sign
                amt = float(f.get('amount', 0))
                ftype = 'deposit' if amt > 0 else 'withdrawal'

            # Detect asset
            asset = f.get('currency') or f.get('code')
            if not asset:
                continue

            amount = abs(float(f.get('amount', 0)))

            flow_map.setdefault(date, []).append({
                'type': ftype,
                'asset': asset.upper(),
                'amount': amount
            })

        except Exception:
            continue

    return flow_map

def parse_manual_flows(text):
    flow_map = {}
    today = datetime.now().strftime('%Y-%m-%d')

    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            asset, amt, ftype = [x.strip() for x in line.split(',')]
            amt = float(amt)
            flow_map.setdefault(today, []).append({
                'type': ftype.lower(),
                'asset': asset.upper(),
                'amount': amt
            })
        except:
            continue

    return flow_map



def get_net_flow_valuation(flows_by_date, all_histories, conversion_rates):
    total = 0.0

    for date_str, flows in flows_by_date.items():
        # Build daily conversion map
        daily_prices = {
            sym: get_best_price(hist["price_map"], date_str)
            for sym, hist in all_histories.items()
        }

        # Build daily conversion_rates (direct + inverse)
        daily_conv = {}
        for sym, price in daily_prices.items():
            if price:
                base, quote = sym.split('/')
                daily_conv[f"{base}/{quote}"] = price
                daily_conv[f"{quote}/{base}"] = 1.0 / price if price > 0 else None

        # Merge with global conversion graph (BTC/USDC, ETH/USDC, etc.)
        daily_conv.update(conversion_rates)

        for flow in flows:
            asset = flow["asset"]
            amount = flow["amount"]

            # Convert using the universal helper
            price_usdc = convert(1.0, asset, "USDC", daily_conv)
            if price_usdc is None:
                continue

            value = amount * price_usdc

            if flow["type"] == "deposit":
                total += value
            else:
                total -= value

    return total



# Master function for trades unwind
def calculate_all_start_balances(balances_end, symbol_trades_dict, flow_map, analysis_start_ts):
    balances_start = copy.deepcopy(balances_end)

    # 1. Reverse Trades (newest → oldest)
    for symbol, trades in symbol_trades_dict.items():
        if '/' not in symbol:
            continue

        base, quote = symbol.split('/')
        sorted_trades = sorted(trades, key=lambda x: x['timestamp'], reverse=True)

        for t in sorted_trades:
            if t['timestamp'] < analysis_start_ts:
                continue  # do NOT rewind trades before analysis window

            side = t['side']
            amount = float(t.get('amount', 0))
            cost   = float(t.get('cost', 0))

            fee = t.get('fee', {}) or {}
            f_cost = float(fee.get('cost', 0)) if fee.get('cost') is not None else 0.0
            f_curr = fee.get('currency')

            # Net wallet deltas
            if side == 'buy':
                base_delta  = amount - (f_cost if f_curr == base else 0)
                quote_delta = -(cost + (f_cost if f_curr == quote else 0))
            else:
                base_delta  = -(amount + (f_cost if f_curr == base else 0))
                quote_delta = cost - (f_cost if f_curr == quote else 0)

            # Reverse
            balances_start[base]  -= base_delta
            balances_start[quote] -= quote_delta

            # Third-currency fee unwind
            if f_curr and f_curr not in [base, quote]:
                balances_start[f_curr] += f_cost 

    # 2. Reverse Flows (newest → oldest)
    for date_str, flows in flow_map.items():
        for f in flows:
            ts = f.get("timestamp")
            if ts is None or ts < analysis_start_ts:
                continue  # do NOT rewind old flows

            asset  = f['asset']
            amount = f['amount']

            if f['type'] == 'deposit':
                balances_start[asset] -= amount
            else:
                balances_start[asset] += amount

    return balances_start


def get_daily_equity_curve_robust(
    balances_end, symbol_trades_dict, all_histories, days,
    flows_by_date, conversion_rates, log_warnings=False
):

    """
    Reconstructs the daily equity curve by rewinding balances day-by-day.
    All valuation is done in USDC using calculate_portfolio_value_usdc().
    """
    daily_curve = []
    current_balances = copy.deepcopy(balances_end)

    now_utc = datetime.now(timezone.utc)
    dates = [(now_utc - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(days + 1)]

    # Index trades by date
    trades_by_date = {}
    for symbol, trades in symbol_trades_dict.items():
        for trade in trades:
            date_str = datetime.fromtimestamp(trade['timestamp'] / 1000).strftime('%Y-%m-%d')
            trades_by_date.setdefault(date_str, []).append(trade)

    for date_str in dates:
        # 1. Build daily price map
        daily_prices = {
            sym: get_best_price(hist["price_map"], date_str)
            for sym, hist in all_histories.items()
        }

        # 2. Value portfolio in USDC
        daily_val = calculate_portfolio_value_usdc(
            current_balances,
            daily_prices,
            conversion_rates,
            log_warnings=log_warnings
        )
        daily_curve.append({'date': date_str, 'value': daily_val})

        # 3. Rewind trades
        todays_trades = trades_by_date.get(date_str, [])
        for trade in todays_trades:
            base, quote = trade['symbol'].split('/')
            current_balances.setdefault(base, 0.0)
            current_balances.setdefault(quote, 0.0)

            if trade['side'] == 'buy':
                current_balances[base] -= trade['amount']
                current_balances[quote] += trade['cost']
            else:
                current_balances[base] += trade['amount']
                current_balances[quote] -= trade['cost']

        # 4. Rewind flows
        todays_flows = flows_by_date.get(date_str, [])
        for flow in todays_flows:
            asset = flow['asset']
            current_balances.setdefault(asset, 0.0)
            if flow['type'] == 'deposit':
                current_balances[asset] -= flow['amount']
            else:
                current_balances[asset] += flow['amount']

    return daily_curve[::-1]


def compute_daily_atr_percent(ohlcv):
    """
    Given OHLCV history for a pair, compute daily ATR% values.
    Returns a list of ATR% floats (e.g., 0.042 for 4.2%).
    """
    atr_percents = []
    prev_close = None

    for candle in ohlcv:
        # Some exchanges return extra fields; we only need the first 6
        ts, open_, high, low, close, volume = candle[:6]

        # Convert to floats (Bitrue often returns strings)
        try:
            open_ = float(open_)
            high = float(high)
            low = float(low)
            close = float(close)
        except Exception as e:
            # Skip malformed candles
            continue

        if prev_close is None:
            prev_close = close
            continue

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

        atr_percent = tr / close if close > 0 else 0
        atr_percents.append(atr_percent)

        prev_close = close

    return atr_percents


def compute_atr_ceiling(start_aum_xrp, atr_percents):
    """
    Compounds ATR% daily to compute the theoretical ATR ceiling.
    """
    ceiling = start_aum_xrp
    for atrp in atr_percents:
        ceiling *= (1 + atrp)
    return ceiling

def compute_atr_opportunity(start_aum, atr_percents):
    """
    Computes the total nominal ATR opportunity:
    Sum of (ATR% * start AUM) for each day.
    """
    return sum(start_aum * p for p in atr_percents)



def run_efficiency_audit(
    equity_curve,          # was curve
    balances_start,
    flows_by_date,
    start_date_prices,
    current_prices_map,
    usdc_alt_start,        # price of 1 USDC in ALT at start
    usdc_alt_now,          # price of 1 USDC in ALT now
    actual_pnl_usdc,       # net PnL in USDC
    actual_pnl_alt,        # net PnL in ALT
    start_aum_usdc,        # flow‑adjusted AUM in USDC
    start_aum_alt,         # flow‑adjusted AUM in ALT
    atr_percents,
    turnover,
    conversion_rates,
    secondary_reporting="BTC"
):
    SECONDARY_REPORTING = secondary_reporting
    """
    Alpha audit with BOTH pure and flow-adjusted Buy & Hold floors,
    in USDC (primary) and ALT (secondary reporting currency).
    """
    if not equity_curve:
        return

    # -----------------------------
    # 1. PURE BUY & HOLD (USDC)
    # -----------------------------
    bh_start = calculate_portfolio_value_usdc(
        balances_start, start_date_prices, conversion_rates, log_warnings=False
    )
    bh_end = calculate_portfolio_value_usdc(
        balances_start, current_prices_map, conversion_rates, log_warnings=False
    )

    market_floor_pnl = bh_end - bh_start

    # -----------------------------------------
    # 2. FLOW-ADJUSTED BUY & HOLD (USDC)
    # -----------------------------------------
    balances_start_flow_adj = copy.deepcopy(balances_start)
    for date_str, flows in flows_by_date.items():
        for f in flows:
            asset = f['asset']
            amt   = f['amount']
            if f['type'] == 'deposit':
                balances_start_flow_adj[asset] = balances_start_flow_adj.get(asset, 0) + amt
            else:
                balances_start_flow_adj[asset] = balances_start_flow_adj.get(asset, 0) - amt

    bh_start_flow_adj = calculate_portfolio_value_usdc(
        balances_start_flow_adj, start_date_prices, conversion_rates, log_warnings=False
    )
    bh_end_flow_adj = calculate_portfolio_value_usdc(
        balances_start_flow_adj, current_prices_map, conversion_rates, log_warnings=False
    )

    market_floor_pnl_flow_adj = bh_end_flow_adj - bh_start_flow_adj

    # -----------------------------
    # 3. Alpha Potential (Noise) in USDC
    # -----------------------------
    alpha_potential = sum(
        abs(equity_curve[i]['value'] - equity_curve[i-1]['value'])
        for i in range(1, len(equity_curve))
    )

    # -----------------------------
    # 4. ATR Opportunity & Efficiency
    # -----------------------------
    atr_opportunity_usdc = compute_atr_opportunity(start_aum_usdc, atr_percents)
    atr_efficiency_usdc  = (actual_pnl_usdc / atr_opportunity_usdc * 100) if atr_opportunity_usdc > 0 else 0

    # ALT opportunity is just USDC opportunity converted at current rate
    atr_opportunity_alt = atr_opportunity_usdc * usdc_alt_now if usdc_alt_now else 0
    atr_efficiency_alt  = (actual_pnl_alt / atr_opportunity_alt * 100) if atr_opportunity_alt > 0 else 0

    # -----------------------------
    # 5. Alpha Harvested (USDC)
    # -----------------------------
    alpha_harvested          = actual_pnl_usdc - market_floor_pnl
    alpha_harvested_flow_adj = actual_pnl_usdc - market_floor_pnl_flow_adj

    eff_pure     = (alpha_harvested / alpha_potential * 100) if alpha_potential > 0 else 0
    eff_flow_adj = (alpha_harvested_flow_adj / alpha_potential * 100) if alpha_potential > 0 else 0

    # -----------------------------
    # 6. ALT Floors (via conversion)
    # -----------------------------
    # Convert USDC floors into ALT using start/now prices
    bh_start_alt         = bh_start * usdc_alt_start if usdc_alt_start else 0
    bh_end_alt           = bh_end * usdc_alt_now   if usdc_alt_now   else 0
    market_floor_pnl_alt = bh_end_alt - bh_start_alt

    bh_start_flow_adj_alt         = bh_start_flow_adj * usdc_alt_start if usdc_alt_start else 0
    bh_end_flow_adj_alt           = bh_end_flow_adj   * usdc_alt_now   if usdc_alt_now   else 0
    market_floor_pnl_flow_adj_alt = bh_end_flow_adj_alt - bh_start_flow_adj_alt

    # -----------------------------
    # 7. Alpha Harvested (ALT)
    # -----------------------------
    # Scale alpha_potential into ALT using current USDC→ALT rate
    alpha_potential_alt = alpha_potential * usdc_alt_now if usdc_alt_now else 0

    alpha_harvested_alt          = actual_pnl_alt - market_floor_pnl_alt
    alpha_harvested_flow_adj_alt = actual_pnl_alt - market_floor_pnl_flow_adj_alt

    eff_pure_alt = (
        alpha_harvested_alt / alpha_potential_alt * 100
        if alpha_potential_alt > 0 else 0
    )
    eff_flow_adj_alt = (
        alpha_harvested_flow_adj_alt / alpha_potential_alt * 100
        if alpha_potential_alt > 0 else 0
    )

    # -----------------------------
    # 8. Capital-usage scaling for ATR
    # -----------------------------
    capital_usage_factor = turnover / 365 if turnover > 0 else 0
    atr_opportunity_usdc_realistic = atr_opportunity_usdc * capital_usage_factor if capital_usage_factor > 0 else 0
    atr_efficiency_usdc_realistic  = (
        (actual_pnl_usdc / atr_opportunity_usdc_realistic * 100)
        if atr_opportunity_usdc_realistic > 0 else 0
    )

    atr_opportunity_alt_realistic = atr_opportunity_alt * capital_usage_factor if capital_usage_factor > 0 else 0
    atr_efficiency_alt_realistic  = (
        (actual_pnl_alt / atr_opportunity_alt_realistic * 100)
        if atr_opportunity_alt_realistic > 0 else 0
    )

    # -----------------------------
    # 9. Output
    # -----------------------------
    logging.info("\n----------- Strategy's Alpha Audit (USDC):")

    logging.info(f"FLOW-ADJ Buy & Hold Floor:       {market_floor_pnl_flow_adj:>10.2f} USDC")
    logging.info(f"Actual PnL:                      {actual_pnl_usdc:>10.2f} USDC")
    logging.info(f"ATR Opportunity (Theoretical):   {atr_opportunity_usdc:>10.2f} USDC")
    logging.info(f"ATR Opportunity (Realistic):     {atr_opportunity_usdc_realistic:.2f} USDC")
    logging.info(f"ATR Efficiency (Realistic):      {atr_efficiency_usdc_realistic:.2f}%")
    logging.info("---------------------------Equity Curve USDC")
    logging.info(f"Alpha Potential (Noise):         {alpha_potential:>10.2f} USDC")
    logging.info(f"Alpha Harvested (Pure):          {alpha_harvested:>10.2f} USDC {eff_pure:>9.2f}%")
    logging.info(f"Alpha Harvested (Flow-Adj):      {alpha_harvested_flow_adj:>10.2f} USDC {eff_flow_adj:>9.2f}%")

    logging.info(f"\n----------- Strategy's Alpha Audit ({SECONDARY_REPORTING}):")
    logging.info(f"FLOW-ADJ Buy & Hold Floor:       {market_floor_pnl_flow_adj_alt:>10.8f} {SECONDARY_REPORTING}")
    logging.info(f"Actual PnL:                      {actual_pnl_alt:>10.8f} {SECONDARY_REPORTING}")
    logging.info(f"ATR Opportunity (Theoretical):   {atr_opportunity_alt:>10.8f} {SECONDARY_REPORTING}")
    logging.info(f"ATR Opportunity (Realistic):     {atr_opportunity_alt_realistic:.8f} {SECONDARY_REPORTING}")
    logging.info(f"ATR Efficiency (Realistic):      {atr_efficiency_alt_realistic:.2f}%")
    logging.info(f"---------------------------Equity Curve {SECONDARY_REPORTING}")
    logging.info(f"Alpha Potential (Noise):         {alpha_potential_alt:>10.8f} {SECONDARY_REPORTING}")
    logging.info(f"Alpha Harvested (Pure):          {alpha_harvested_alt:>10.8f} {SECONDARY_REPORTING} {eff_pure_alt:>9.2f}%")
    logging.info(f"Alpha Harvested (Flow-Adj):      {alpha_harvested_flow_adj_alt:>10.8f} {SECONDARY_REPORTING} {eff_flow_adj_alt:>9.2f}%")
    return {
        "usdc": {
            "market_floor_pnl": market_floor_pnl,
            "market_floor_pnl_flow_adj": market_floor_pnl_flow_adj,
            "actual_pnl": actual_pnl_usdc,                     
            "alpha_potential": alpha_potential,
            "alpha_harvested": alpha_harvested,
            "alpha_harvested_flow_adj": alpha_harvested_flow_adj,
            "efficiency_pure_pct": eff_pure,
            "efficiency_flow_adj_pct": eff_flow_adj,
            "atr_opportunity": atr_opportunity_usdc,
            "atr_opportunity_realistic": atr_opportunity_usdc_realistic,
            "atr_efficiency_realistic_pct": atr_efficiency_usdc_realistic,
        },
        "alt": {
            "secondary_reporting": SECONDARY_REPORTING,
            "market_floor_pnl": market_floor_pnl_alt,
            "market_floor_pnl_flow_adj": market_floor_pnl_flow_adj_alt,
            "actual_pnl_alt": actual_pnl_alt,   
            "alpha_potential": alpha_potential_alt,                   
            "alpha_harvested": alpha_harvested_alt,
            "alpha_harvested_flow_adj": alpha_harvested_flow_adj_alt,
            "efficiency_pure_pct": eff_pure_alt,
            "efficiency_flow_adj_pct": eff_flow_adj_alt,
            "atr_opportunity": atr_opportunity_alt,
            "atr_opportunity_realistic": atr_opportunity_alt_realistic,
            "atr_efficiency_realistic_pct": atr_efficiency_alt_realistic,
        }
    }



def run_pnl_analysis(exchange, lookback_days=30, secondary_reporting="BTC", pre_fetched_flows=None):


    # 0. Test the connection by fetching the server's timestamp
    try:
        server_time = exchange.milliseconds()
        logging.info(f"Connection successful. Server time: {server_time}")
    except ccxt.NetworkError as e:
        logging.error(f"A network error occurred: {e}")
        return {"error": str(e)}
    except ccxt.ExchangeError as e:
        logging.error(f"The exchange reported an error: {e}")
        return {"error": str(e)}
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
        return {"error": str(e)}

    logging.info("Cleaning Digital Soil: Fetching wholesome balances...")
    
    # 1. Validate lookback window
    try:
        days = int(lookback_days)
    except Exception:
        days = 30

    if days < 2:
        days = 2
    elif days > 365:
        days = 365

    logging.info(f"Lookback days: {days}")

    # 2. Compute date range
    today = datetime.now().date()
    lookback_date = today - timedelta(days=days)

    # For CCXT timestamps (milliseconds)
    end_time = datetime.now()
    since = int((end_time - timedelta(days=days)).timestamp() * 1000)
    analysis_start_ts = since

    # For human-readable logs
    lookback_date_str = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%d')

    logging.info(f"Fetching history since {lookback_date_str} (timestamp: {since})")
    SECONDARY_REPORTING = secondary_reporting

    #  Fetch balances
    raw_balances_end = fetch_total_aggregated_balance(exchange)

    # 2a. Normalize LD balances into underlying assets
    normalized_balances_end = {}
    for asset, amount in raw_balances_end.items():
        base = asset[2:] if (asset.startswith('LD') and asset != 'LDO' and len(asset) > 3) else asset
        normalized_balances_end[base] = normalized_balances_end.get(base, 0) + amount

    balances_end = normalized_balances_end
    active_assets = set(balances_end.keys())
    logging.info(f"Active Portfolio: {len(active_assets)} assets identified.")


    # 3. Initialize fee + flow sets
    fee_currencies = set()
    assets_from_flows = set()
    assets_from_trades = set()

    # 4. Fetch flows
    if pre_fetched_flows is not None:
        logging.info("Using pre-fetched flows from UI (skipping exchange flow fetch).")
        portfolio_flows = pre_fetched_flows
    else:
        logging.info("Fetching flows inside PnL engine...")
        portfolio_flows = fetch_portfolio_flows(exchange, since, active_assets or [])



    # Extract assets from flows (flow_map is dict: date → list of flows)
    assets_from_flows = {
        f["asset"]
        for flows in portfolio_flows.values()
        for f in flows
    }

    # flows_by_date is already structured correctly by the module
    flows_by_date = portfolio_flows
    # Already merged in UI

    # 5. Build SPOT markets
    spot_markets = {
        s: m for s, m in exchange.markets.items()
        if m.get('spot') is True
    }

    symbol_trades_dict = {}


    # 6. NOW discover relevant pairs
    potential_pairs = discover_relevant_pairs(
        exchange,
        active_assets,
        spot_markets,
        fee_currencies=fee_currencies,
        flow_assets=assets_from_flows
    )

    logging.info(f"Scanning {len(potential_pairs)} potential pairs for trades...")

    # 7. Fetch trades ONLY for relevant pairs
    for symbol in potential_pairs:
        if symbol not in symbol_trades_dict:
            trades = fetch_filled_orders(exchange, symbol, since)
            if trades:
                symbol_trades_dict[symbol] = trades
                base, quote = symbol.split('/')
                assets_from_trades.update([base, quote])
                for t in trades:
                    f_curr = t.get('fee', {}).get('currency')
                    if f_curr:
                        fee_currencies.add(f_curr)


    # 8. Price Discovery (USDC-centric)
    valuation_pairs = set()
    assets_to_value = active_assets | fee_currencies | assets_from_trades | assets_from_flows

    for asset in assets_to_value:
        base = asset[2:] if (asset.startswith('LD') and asset != 'LDO' and len(asset) > 3) else asset
        if base in ['USDT', 'USDC']:
            continue
        
        if f"{base}/{PRIMARY_QUOTE}" in spot_markets:
            valuation_pairs.add(f"{base}/{PRIMARY_QUOTE}")

        if f"{base}/{SECONDARY_QUOTE}" in spot_markets:
            valuation_pairs.add(f"{base}/{SECONDARY_QUOTE}")

    # build history set
    pairs_to_fetch_history = set(symbol_trades_dict.keys()) | valuation_pairs

    # 9. Fetch historical prices for all pairs
    all_histories = prefetch_all_price_histories(exchange, list(pairs_to_fetch_history), days)

    if "BTC/USDC" not in all_histories:
        logging.warning("BTC/USDC missing from all_histories!")
    else:
        ohlcv_len = len(all_histories["BTC/USDC"].get("ohlcv", []))
        logging.info(f"BTC/USDC OHLCV length: {ohlcv_len}")


    logging.info(f"Optimized History: Fetching data for {len(pairs_to_fetch_history)} pairs...")
    

    
    # 10. PRICE SETUP
    

    # 10.a. Build start-date price map from historical OHLCV
    start_date_prices = {
        s: get_best_price(h["price_map"], lookback_date_str)
        for s, h in all_histories.items()
    }

    # 10.b. Fetch current tickers
    all_tickers = safe_fetch_tickers(exchange, list(pairs_to_fetch_history))

    current_prices_map = {s: t['last'] for s, t in all_tickers.items()}

   
    # Add stablecoin 1:1 backbone to BOTH maps
   
    stables = ["USDC", "USDT", "FDUSD", "TUSD", "DAI"]

    # Add to start_date_prices
    for a in stables:
        for b in stables:
            if a != b:
                start_date_prices[f"{a}/{b}"] = 1.0

    # Add to current_prices_map
    for a in stables:
        for b in stables:
            if a != b:
                current_prices_map[f"{a}/{b}"] = 1.0

    
    # 10.c. Build conversion_rates (current graph)
    
    conversion_rates = {}

    # 1) Add all current tickers
    for symbol, ticker in all_tickers.items():
        base, quote = symbol.split('/')
        price = ticker.get('last')
        if price:
            conversion_rates[f"{base}/{quote}"] = price
            conversion_rates[f"{quote}/{base}"] = 1.0 / price

    # 2) Add stablecoin backbone (1:1)
    for a in stables:
        for b in stables:
            if a != b:
                conversion_rates[f"{a}/{b}"] = 1.0
                conversion_rates[f"{b}/{a}"] = 1.0

    # 3) Add BTC/USDC and ETH/USDC if available
    for pair in ["BTC/USDC", "ETH/USDC"]:
        if pair in current_prices_map:
            price = current_prices_map[pair]
            base, quote = pair.split('/')
            conversion_rates[pair] = price
            conversion_rates[f"{quote}/{base}"] = 1.0 / price


    # 11. Net capital flows (valued in USDC now)
    net_flow_value_usdc = get_net_flow_valuation(flows_by_date, all_histories, conversion_rates)

    # 12. Wholesome Portfolio Analysis – balances at start
    logging.info("Calculating wholesome portfolio values (in USDC)...")
    balances_start = calculate_all_start_balances(balances_end, symbol_trades_dict, flows_by_date, analysis_start_ts)

    
    portfolio_value_end_usdc = calculate_portfolio_value_usdc(
        balances_end,
        current_prices_map,
        conversion_rates,
        log_warnings=True
    )

    portfolio_value_start_usdc = calculate_portfolio_value_usdc(
        balances_start,
        start_date_prices,
        conversion_rates,
        log_warnings=True
    )

    start_aum_pre_flows = portfolio_value_start_usdc
    start_aum_adjusted = start_aum_pre_flows + net_flow_value_usdc

    # 13. Trade Analysis
    combined_total_buy_size = 0
    combined_total_sell_size = 0
    combined_total_pnl_nominal = 0
    symbol_trade_stats = {}

    for symbol, trades in symbol_trades_dict.items():
        _, buy_size, _, sell_size, num_trades = analyze_trades(
            trades, conversion_rates, base_quote=PRIMARY_QUOTE
        )
        nominal_pl = sell_size - buy_size
        combined_total_buy_size += buy_size
        combined_total_sell_size += sell_size
        combined_total_pnl_nominal += nominal_pl

        pct_pl = ((sell_size / buy_size) - 1) * 100 if buy_size > 0 else 0

        symbol_trade_stats[symbol] = {
            "num_trades": num_trades,
            "buy_size": buy_size,
            "sell_size": sell_size,
            "nominal_pl": nominal_pl,
            "pct_pl": pct_pl,
        }
        logging.info(f"\n--- {symbol} ---")
        logging.info(f"Num Trades: {num_trades}")
        logging.info(f"Buy Size:   {buy_size:.8f} {PRIMARY_QUOTE}")
        logging.info(f"Sell Size:  {sell_size:.8f} {PRIMARY_QUOTE}")
        logging.info(f"Nominal PnL:{nominal_pl:.8f} {PRIMARY_QUOTE} ({pct_pl:.2f}%)")
        
    total_pnl_percentage = (combined_total_pnl_nominal / combined_total_buy_size) * 100 if combined_total_buy_size else 0
    annualized_trade_return = ((1 + total_pnl_percentage / 100) ** (365 / days) - 1) * 100
    
    df = pd.DataFrame({
        'Symbol': balances_start.keys(),
        'Balance_Start': [f"{unwrap_balance(v):.8f}" for v in balances_start.values()],
        'Balance_End': [f"{unwrap_balance(balances_end.get(k, 0)):.8f}" for k in balances_start]
    })
    logging.info("\nSymbol Balances at Start (actual) and End Dates:")
    logging.info(df.to_string(index=False))

    # 14. Final Metrics (Adjusted)
    portfolio_nominal_pl = portfolio_value_end_usdc - start_aum_adjusted
    portfolio_pct_pl = (portfolio_nominal_pl / start_aum_adjusted) * 100 if start_aum_adjusted else 0
    portfolio_annualized_pct = ((1 + portfolio_pct_pl / 100) ** (365 / days) - 1) * 100

    if start_aum_adjusted:
        roic = ((combined_total_pnl_nominal / days * 365) / start_aum_adjusted) * 100
        turnover = (combined_total_sell_size / days * 365) / start_aum_adjusted
    else:
        roic, turnover = 0, 0

    # 15. Alternative currency reporting layer
    usdc_alt_now = get_conversion_rate(PRIMARY_QUOTE, SECONDARY_REPORTING, current_prices_map, conversion_rates)
    usdc_alt_start = get_conversion_rate(PRIMARY_QUOTE, SECONDARY_REPORTING, start_date_prices, conversion_rates)


    start_aum_alt = start_aum_adjusted * usdc_alt_start if usdc_alt_start else 0
    end_aum_alt   = portfolio_value_end_usdc * usdc_alt_now if usdc_alt_now else 0

    pnl_alt = end_aum_alt - start_aum_alt
    pct_pnl_alt = (pnl_alt / start_aum_alt * 100) if start_aum_alt else 0
    portfolio_annualized_pct_alt = ((1 + pct_pnl_alt / 100) ** (365 / days) - 1) * 100

    # 16. ATR Series Source (USDC‑centric)
    primary_ohlcv = None

    # 1) Try BTC/USDC explicitly
    if "BTC/USDC" in all_histories:
        ohlcv = all_histories["BTC/USDC"].get("ohlcv", [])
        if ohlcv:
            primary_ohlcv = ohlcv
            logging.info("ATR source: BTC/USDC")

    # 2) If empty or missing, try any BTC/* pair with OHLCV
    if primary_ohlcv is None:
        for sym, hist in all_histories.items():
            if sym.startswith("BTC/") and hist.get("ohlcv"):
                primary_ohlcv = hist["ohlcv"]
                logging.info(f"ATR fallback source: {sym}")
                break

    # 3) If still nothing, try */BTC pairs
    if primary_ohlcv is None:
        for sym, hist in all_histories.items():
            if sym.endswith("/BTC") and hist.get("ohlcv"):
                primary_ohlcv = hist["ohlcv"]
                logging.info(f"ATR fallback source: {sym}")
                break

    # 4) If still nothing, ATR = 0
    if primary_ohlcv is None:
        logging.warning("No suitable ATR pair found; ATR metrics will be zeroed.")
        atr_percents = []
    else:
        atr_percents = compute_daily_atr_percent(primary_ohlcv)


    logging.info(f"\n----------Aggregated Totals:")
    logging.info(f"Total Buy Size: {combined_total_buy_size:.2f} {PRIMARY_QUOTE} | Total Sell Size: {combined_total_sell_size:.2f} {PRIMARY_QUOTE}")
    logging.info(f"Total (Realized) Nominal P&L: {combined_total_pnl_nominal:.2f} {PRIMARY_QUOTE} | Percentage: {total_pnl_percentage:.2f}%")
    logging.info(f"Annualized Trade Return: {annualized_trade_return:.2f}%")

    logging.info("\n----------- Wholesome Portfolio Analysis:")
    logging.info(f"Start AUM: {portfolio_value_start_usdc:.2f} USDC")
    logging.info(f"Net Capital Flow Adjustment: {net_flow_value_usdc:.2f} USDC")
    logging.info(f"Start AUM (flow adjusted): {start_aum_adjusted:.2f} USDC | End AUM: {portfolio_value_end_usdc:.2f} USDC")
    logging.info(f"Net actual PnL: {portfolio_nominal_pl:.2f} USDC ({portfolio_pct_pl:.2f}%)")
    logging.info(f"True Annualized Return: {portfolio_annualized_pct:.2f}% | ROIC: {roic:.2f}%")
    logging.info(f"Assets Turnover Rate: {turnover:.2f}")

    logging.info(f"\n----------- Wholesome Portfolio Analysis ({SECONDARY_REPORTING}):")
    logging.info(f"Start AUM (flow adjusted): {start_aum_alt:.8f} {SECONDARY_REPORTING}")
    logging.info(f"End AUM: {end_aum_alt:.8f} {SECONDARY_REPORTING}")
    logging.info(f"Net actual PnL: {pnl_alt:.8f} {SECONDARY_REPORTING} ({pct_pnl_alt:.2f}%)")

    # 17. Efficiency Audit
    curve = get_daily_equity_curve_robust(
        balances_end,
        symbol_trades_dict,
        all_histories,
        days,
        flows_by_date,
        conversion_rates,
        log_warnings=False
    )

    # Realized PnL comes from executed trades
    realized_pnl_usdc = combined_total_pnl_nominal

    # Unrealized PnL is the remainder
    unrealized_pnl_usdc = portfolio_nominal_pl - realized_pnl_usdc



    alpha_audit = run_efficiency_audit(
        curve,
        balances_start,
        flows_by_date,
        start_date_prices,
        current_prices_map,
        usdc_alt_start,
        usdc_alt_now,
        portfolio_nominal_pl,
        pnl_alt,
        start_aum_adjusted,
        start_aum_alt,
        atr_percents,
        turnover, 
        conversion_rates,
        secondary_reporting
    )


    return {
        "portfolio": {
            "start_value_usdc": portfolio_value_start_usdc,
            "start_value_usdc_flow_adj": start_aum_adjusted,
            "end_value_usdc": portfolio_value_end_usdc,
            "net_flow_value_usdc": net_flow_value_usdc,
            "nominal_pnl_usdc": portfolio_nominal_pl,
            "realized_pnl_usdc": realized_pnl_usdc,
            "unrealized_pnl_usdc": unrealized_pnl_usdc,
            "pct_pnl_usdc": portfolio_pct_pl,
            "annualized_return_pct": portfolio_annualized_pct,
            "roic_pct": roic,
            "turnover": turnover,
            "secondary_reporting": SECONDARY_REPORTING,
            "start_value_alt": start_aum_alt,
            "end_value_alt": end_aum_alt,
            "nominal_pnl_alt": pnl_alt,
            "pct_pnl_alt": pct_pnl_alt,
            "annualized_return_pct_alt": portfolio_annualized_pct_alt,
        },

        "balances": {
            "start": balances_start,
            "end": balances_end,
        },

        "flows": {
            "by_date": flows_by_date,
            "net_flow_value_usdc": net_flow_value_usdc,
        },

        "trades": {
            "trade_analysis": symbol_trade_stats,
            "per_symbol": symbol_trades_dict,
            "total_buy_size": combined_total_buy_size,
            "total_sell_size": combined_total_sell_size,
            "total_nominal_pnl": combined_total_pnl_nominal,
            "total_pnl_pct": total_pnl_percentage,
            "annualized_trade_return_pct": annualized_trade_return,
        },

        "valuation": {
            "start_date_prices": start_date_prices,
            "current_prices_map": current_prices_map,
            "conversion_rates": conversion_rates,
        },

        "equity_curve": curve,

        "atr": {
            "atr_percents": atr_percents
        },

        "alpha_audit": alpha_audit,

        "raw": {
            "all_histories": all_histories,
            "symbol_trades_dict": symbol_trades_dict,
            "assets_to_value": list(assets_to_value),
            "valuation_pairs": list(valuation_pairs),
            "pairs_to_fetch_history": list(pairs_to_fetch_history),
        }
    }

