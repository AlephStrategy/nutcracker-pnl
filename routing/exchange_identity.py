def detect_exchange(exchange_name: str) -> str:
    name = exchange_name.lower()

    if name in ["binance", "binance-global"]:
        return "binance_global"

    if name in ["binanceeu", "binance-eu"]:
        return "binance_eu"

    if name in ["binanceus", "binance-us"]:
        return "binance_us"

    if name in ["bitget"]:
        return "bitget"

    if name in ["gate", "gateio"]:
        return "gate"

    if name in ["okx"]:
        return "okx"

    if name in ["kucoin"]:
        return "kucoin"

    return "unknown"
