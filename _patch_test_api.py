from rich.console import Console
console = Console()
from bitunix_grid_bot import BitunixClient, load_secrets
def test_api():
    sec = load_secrets()
    c = BitunixClient(sec["api_key"], sec["api_secret"])
    r1 = c.get("/api/v1/futures/account/get_leverage_margin_mode", {"symbol":"BTCUSDT","marginCoin":"USDT"})
    ok1 = (r1.get("code")==0)
    r2 = c.get("/api/v1/cp/asset/query")
    ok2 = (r2.get("code")==0)
    return ok1 and ok2, {"get_leverage_margin_mode": r1, "asset_query": r2}
