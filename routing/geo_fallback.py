def probe_region(exchange):
    probe_symbols = ["BTC/USDT", "BTC/USDC", "ETH/USDT", "ETH/USDC"]

    for sym in probe_symbols:
        try:
            exchange.fetch_ticker(sym)
            return True
        except Exception:
            continue

    return False

def geo_fallback(exchange, us_backend, eu_backend):
    if probe_region(exchange):
        return us_backend
    return eu_backend
