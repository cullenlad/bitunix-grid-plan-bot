#!/usr/bin/env python3
import os, sys, json, time, uuid, pathlib, stat, getpass, requests, yaml, re, argparse
from rich.console import Console
from rich.table import Table

HOME=os.path.expanduser("~")
APPDIR=os.path.join(HOME,".bitunix_grid_bot")
SECRETS=os.path.join(APPDIR,"secrets.json")
CONFIG=os.path.join(APPDIR,"config.yaml")
PLAN=os.path.join(APPDIR,"plan.yaml")
BASE="https://fapi.bitunix.com"
console=Console()
UTILIZATION=0.95

def now_ms(): return str(int(time.time()*1000))
def sha256_hex(s): 
    import hashlib; 
    return hashlib.sha256(s.encode()).hexdigest()

class BitunixClient:
    def __init__(self,k,s):
        self.k=k; self.s=s; self.h={"language":"en-US","Content-Type":"application/json","language":"en-US"}
    def _sign(self,method,path,q=None,b=None):
        nonce=uuid.uuid4().hex; ts=now_ms(); qp=""
        if q: qp="".join([f"{k}{v}" for k,v in sorted(q.items())])
        body=json.dumps(b,separators=(',',':')) if b else ""
        digest=sha256_hex(nonce+ts+self.k+qp+body); sign=sha256_hex(digest+self.s)
        h={**self.h,"api-key":self.k,"nonce":nonce,"timestamp":ts,"sign":sign}
        url=BASE+path
        if q: url+="?"+ "&".join([f"{k}={v}" for k,v in sorted(q.items())])
        return url,h,body
    def get(self,p,q=None): u,h,_=self._sign("GET",p,q,None); return requests.get(u,headers=h,timeout=30).json()
    def post(self,p,b=None): u,h,d=self._sign("POST",p,None,b); return requests.post(u,headers=h,data=d,timeout=30).json()
    def change_leverage(self,symbol,lev,coin="USDT"): return self.post("/api/v1/futures/account/change_leverage",{"symbol":symbol,"leverage":int(lev),"marginCoin":coin})
    def change_margin_mode(self,symbol,mode="ISOLATION",coin="USDT"): return self.post("/api/v1/futures/account/change_margin_mode",{"symbol":symbol,"marginMode":mode,"marginCoin":coin})
    def change_position_mode(self,mode="ONE_WAY"): return self.post("/api/v1/futures/account/change_position_mode",{"positionMode":mode})
    def place(self,b): return self.post("/api/v1/futures/trade/place_order",b)
    def pending_orders(self,symbol): return self.post("/api/v1/futures/trade/get_pending_orders",{"symbol":symbol})
    def cancel_orders(self,orderIdList): return self.post("/api/v1/futures/trade/cancel_orders",{"orderIdList":orderIdList})
    def positions(self,symbol=None):
        q={}; 
        if symbol: q["symbol"]=symbol
        return self.get("/api/v1/futures/position/get_pending_positions",q)

def ensure_dirs():
    pathlib.Path(APPDIR).mkdir(parents=True,exist_ok=True)
    if not os.path.exists(SECRETS):
        with open(SECRETS,"w") as f: json.dump({"api_key":"","api_secret":""},f); os.chmod(SECRETS,stat.S_IRUSR|stat.S_IWUSR)
    if not os.path.exists(CONFIG):
        with open(CONFIG,"w") as f: yaml.safe_dump({"symbol":"BTCUSDT","leverage":3,"marginMode":"ISOLATION","positionMode":"ONE_WAY","tif":"GTC","levels":16,"bandPct":3.0,"highestSell":160000,"maxPlacePerTick":12},f)
    if not os.path.exists(PLAN):
        with open(PLAN,"w") as f: yaml.safe_dump({"symbol":"BTCUSDT","levels":[],"meta":{"created":int(time.time())}},f)

def load_secrets(): return json.load(open(SECRETS))
def load_cfg(): return yaml.safe_load(open(CONFIG))
def save_cfg(cfg): yaml.safe_dump(cfg,open(CONFIG,"w"))
def load_plan(): return yaml.safe_load(open(PLAN))
def save_plan(p): yaml.safe_dump(p,open(PLAN,"w"))

def first_row_like(d):
    if isinstance(d,list) and d: return d[0]
    if isinstance(d,dict): return d
    return {}

def get_account(c):
    r=c.get("/api/v1/futures/account",{"marginCoin":"USDT"})
    row=first_row_like(r.get("data"))
    avail=0.0
    for k in ("available","availableBalance","availableMargin","cashBalance","availableCash"):
        if k in row:
            try: avail=float(row[k]); break
            except: pass
    return avail,r

def get_rules(c,symbol):
    r=c.get("/api/v1/futures/market/trading_pairs",{"symbols":symbol})
    row=first_row_like(r.get("data"))
    basePrecision=int(row.get("basePrecision",4)) if row else 4
    minTradeVolume=float(row.get("minTradeVolume","0.0001")) if row else 0.0001
    return basePrecision,minTradeVolume,r

def detect_buy_cap(c,symbol,probe_price,min_vol):
    r=c.place({"symbol":symbol,"side":"BUY","tradeSide":"OPEN","orderType":"LIMIT","qty":str(min_vol),"price":str(probe_price),"effect":"POST_ONLY","reduceOnly":False,"clientId":f"probe-{uuid.uuid4().hex[:6]}"})
    if r.get("code")==0 and r.get("data") and r["data"].get("orderId"):
        oid=r["data"]["orderId"]; c.cancel_orders([{"orderId":oid,"symbol":symbol}]); return None,r
    m=re.search(r"Max Buy Order Price\s+([0-9]+(?:\.[0-9]+)?)", str(r.get("msg","")))
    if r.get("code")==30014 and m: return float(m.group(1)),r
    return None,r

def round_qty(qty, prec):
    fmt="{:0."+str(prec)+"f}"; return float(fmt.format(qty))

def make_plan(symbol, lowest_buy, highest_buy, highest_sell, total_levels=50, buy_fraction=0.67):
    buy_levels=max(2,int(total_levels*buy_fraction)); sell_levels=max(2,total_levels-buy_levels)
    buys=[round(lowest_buy+i*((highest_buy-lowest_buy)/(buy_levels-1)),2) for i in range(buy_levels)]
    sells=[round(highest_buy+i*((highest_sell-highest_buy)/(sell_levels-1)),2) for i in range(sell_levels)]
    levels=[{"side":"BUY","price":p,"status":"PENDING","orderId":None} for p in buys]+[{"side":"SELL","price":p,"status":"PENDING","orderId":None} for p in sells]
    return {"symbol":symbol,"levels":levels,"meta":{"created":int(time.time()),"lowest_buy":lowest_buy,"highest_buy":highest_buy,"highest_sell":highest_sell,"total":total_levels,"buy_levels":buy_levels,"sell_levels":sell_levels}}

def plan_stats(plan):
    t=len(plan["levels"]); filled=sum(1 for L in plan["levels"] if L["status"]=="FILLED"); placed=sum(1 for L in plan["levels"] if L["status"]=="PLACED")
    pending=t-filled-placed
    return {"total":t,"placed":placed,"filled":filled,"pending":pending}

def status_table(plan):
    st=plan_stats(plan)
    tbl=Table(title=f"Plan {plan['symbol']} | total {st['total']} placed {st['placed']} filled {st['filled']} pending {st['pending']}")
    tbl.add_column("Idx"); tbl.add_column("Side"); tbl.add_column("Price"); tbl.add_column("Status"); tbl.add_column("OrderId")
    for i,L in enumerate(plan["levels"]):
        tbl.add_row(str(i),L["side"],str(L["price"]),L["status"],str(L["orderId"]))
    console.print(tbl)

def compute_qty(available, lev, buy_prices, base_prec, min_vol):
    if not buy_prices: return min_vol
    sum_prices=sum(buy_prices)
    qty=(available*lev*UTILIZATION)/sum_prices
    qty=round_qty(qty, base_prec)
    if qty<min_vol: qty=min_vol
    return qty

def reconcile_fills_with_pending(c,symbol,plan):
    pend=c.pending_orders(symbol)
    pend_ids=set()
    if pend.get("code")==0 and pend.get("data"):
        for o in pend["data"]:
            pend_ids.add(o["orderId"])
    for L in plan["levels"]:
        if L["orderId"] and L["status"] in ("PLACED","PENDING"):
            if L["orderId"] not in pend_ids:
                if L["side"]=="BUY": L["status"]="FILLED"
                elif L["side"]=="SELL": L["status"]="FILLED"
    return plan

def tick_execute(c, cfg, plan, band_pct=None, max_place=None):
    base_prec, min_vol, _=get_rules(c, cfg["symbol"])
    avail,_=get_account(c)
    cap,_=detect_buy_cap(c, cfg["symbol"], 10**9, min_vol)
    if cap is None: 
        console.print("[red]Cap unavailable[/]"); 
        return plan
    hb=round(cap*0.999,2); lb=round(hb*(1-(band_pct or cfg.get("bandPct",3.0))/100.0),2)
    buys_window=[L for L in plan["levels"] if L["side"]=="BUY" and L["status"]=="PENDING" and lb<=L["price"]<=hb]
    sells_all=[L for L in plan["levels"] if L["side"]=="SELL" and L["status"]=="PENDING"]
    buys_window=sorted(buys_window, key=lambda x: x["price"])
    if max_place is None: max_place=cfg.get("maxPlacePerTick",12)
    buys_window=buys_window[:max_place]
    qty=compute_qty(avail, cfg["leverage"], [L["price"] for L in buys_window], base_prec, min_vol)
    for L in buys_window:
        b={"symbol":cfg["symbol"],"side":"BUY","tradeSide":"OPEN","orderType":"LIMIT","qty":str(qty),"price":str(L["price"]),"effect":cfg["tif"],"reduceOnly":False,"clientId":f"pbuy-{uuid.uuid4().hex[:8]}"}
        r=c.place(b)
        if r.get("code")==20003:
            qty=max(min_vol, round_qty(qty*0.85, base_prec))
            r=c.place({**b,"qty":str(qty),"clientId":f"pbuy-{uuid.uuid4().hex[:8]}"})
        if r.get("code")==0 and r.get("data"):
            L["status"]="PLACED"; L["orderId"]=r["data"]["orderId"]
    plan=reconcile_fills_with_pending(c,cfg["symbol"],plan)
    pos=c.positions(cfg["symbol"])
    has_long=False; pos_id=None
    if pos.get("code")==0 and pos.get("data"):
        for p in pos["data"]:
            if p.get("side")=="LONG" and float(p.get("openQty","0"))>0:
                has_long=True; pos_id=p.get("positionId"); break
    if has_long:
        pending_sells=[L for L in plan["levels"] if L["side"]=="SELL" and L["status"]=="PENDING"]
        take=sorted(pending_sells, key=lambda x: x["price"])[:max_place]
        for L in take:
            b={"symbol":cfg["symbol"],"side":"SELL","tradeSide":"CLOSE","orderType":"LIMIT","qty":str(qty),"price":str(L["price"]),"effect":cfg["tif"],"reduceOnly":True,"clientId":f"psell-{uuid.uuid4().hex[:8]}","positionId":pos_id}
            r=c.place(b)
            if r.get("code")==0 and r.get("data"):
                L["status"]="PLACED"; L["orderId"]=r["data"]["orderId"]
    return plan

def cancel_all_symbol(c,symbol):
    pend=c.pending_orders(symbol); ids=[]
    if pend.get("code")==0 and pend.get("data"):
        for o in pend["data"]:
            ids.append({"orderId":o["orderId"],"symbol":symbol})
    if ids: return c.cancel_orders(ids)
    return {"code":0,"msg":"No pending"}

def cmd_make_plan(args):
    ensure_dirs()
    sec=load_secrets(); cfg=load_cfg()
    c=BitunixClient(sec["api_key"],sec["api_secret"])
    p=make_plan(cfg["symbol"], args.lowest_buy, args.highest_buy, args.highest_sell, args.levels, args.buy_fraction)
    save_plan(p); console.print({"plan":"created","stats":plan_stats(p)}); status_table(p)

def cmd_status(args):
    ensure_dirs(); p=load_plan(); status_table(p)

def cmd_tick(args):
    ensure_dirs(); sec=load_secrets(); cfg=load_cfg(); p=load_plan()
    c=BitunixClient(sec["api_key"],sec["api_secret"])
    p=tick_execute(c,cfg,p,band_pct=args.band_pct,max_place=args.max_place); save_plan(p); status_table(p)

def cmd_loop(args):
    ensure_dirs(); sec=load_secrets(); cfg=load_cfg()
    c=BitunixClient(sec["api_key"],sec["api_secret"])
    while True:
        p=load_plan()
        p=tick_execute(c,cfg,p,band_pct=args.band_pct,max_place=args.max_place)
        save_plan(p)
        st=plan_stats(p)
        console.print({"loop_tick":"done","stats":st})
        time.sleep(args.interval)

def cmd_cancel(args):
    ensure_dirs(); sec=load_secrets(); cfg=load_cfg()
    c=BitunixClient(sec["api_key"],sec["api_secret"])
    console.print(cancel_all_symbol(c,cfg["symbol"]))

def main():
    ensure_dirs()
    ap=argparse.ArgumentParser()
    sub=ap.add_subparsers(dest="cmd",required=True)
    ap_make=sub.add_parser("make-plan")
    ap_make.add_argument("--lowest-buy",type=float,required=True)
    ap_make.add_argument("--highest-buy",type=float,required=True)
    ap_make.add_argument("--highest-sell",type=float,required=True)
    ap_make.add_argument("--levels",type=int,default=50)
    ap_make.add_argument("--buy-fraction",type=float,default=0.67)
    ap_make.set_defaults(func=cmd_make_plan)
    ap_status=sub.add_parser("status"); ap_status.set_defaults(func=cmd_status)
    ap_tick=sub.add_parser("tick")
    ap_tick.add_argument("--band-pct",type=float,default=None)
    ap_tick.add_argument("--max-place",type=int,default=None)
    ap_tick.set_defaults(func=cmd_tick)
    ap_loop=sub.add_parser("loop")
    ap_loop.add_argument("--band-pct",type=float,default=None)
    ap_loop.add_argument("--max-place",type=int,default=None)
    ap_loop.add_argument("--interval",type=int,default=3600)
    ap_loop.set_defaults(func=cmd_loop)
    ap_cancel=sub.add_parser("cancel"); ap_cancel.set_defaults(func=cmd_cancel)
    args=ap.parse_args(); args.func(args)

if __name__=="__main__": main()
