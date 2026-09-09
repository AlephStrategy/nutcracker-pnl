from routing.exchange_identity import detect_exchange
from routing.account_region import detect_account_region
from routing.geo_fallback import geo_fallback

def route(exchange_name, exchange, backends):
    identity = detect_exchange(exchange_name)
    region = detect_account_region(exchange)

    if identity == "binance_eu":
        return backends["eu"]

    if identity == "binance_us":
        return backends["us"]

    if identity == "binance_global":
        if region == "eu":
            return backends["eu"]
        if region == "us":
            return backends["us"]
        return geo_fallback(exchange, backends["us"], backends["eu"])

    return backends["us"]
