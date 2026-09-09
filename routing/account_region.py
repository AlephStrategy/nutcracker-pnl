def detect_account_region(exchange) -> str:
    try:
        info = exchange.fetch_account()
    except Exception:
        return "global"

    region = info.get("region") or info.get("country") or info.get("accountType")

    if not region:
        return "global"

    region = region.lower()

    if "eu" in region:
        return "eu"

    if "us" in region:
        return "us"

    return "global"
