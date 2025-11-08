#!/usr/bin/env python3
import os, sys, json, time, hashlib, uuid, pathlib, stat, getpass, requests, yaml, re, argparse, csv, datetime
from rich.console import Console
from rich.table import Table

HOME=os.path.expanduser("~")
APPDIR=os.path.join(HOME,".bitunix_grid_bot")
SECRETS=os.path.join(APPDIR,"secrets.json")
CONFIG=os.path.join(APPDIR,"config.yaml")
PLAN=os.path.join(APPDIR,"plan.yaml")
LOGDIR=os.path.join(APPDIR,"logs")
TICKCSV=os.path.join(LOGDIR,"ticks.csv")
SNAPCSV=os.path.join(LOGDIR,"plan_snapshot.csv")
BASE="https://fapi.bitunix.com"
console=Console()
_last_api_ok=False
UTILIZATION=0.95

def now_ms(): return str(int(time.time()*1000))
def sha256_hex(s):
    import hashlib
    return hashlib.sha256(s.encode()).hexdigest()

class BitunixClient:
    def __init__(self,k,s):
        self.k=k; self.s=s
        self.h={"language":"en-US","Content-Type":"application/json"}
    def _sign(self,method,path,q=None,b=None):
        nonce=uuid.uuid4().hex; ts=now_ms(); qp=""
        if q: qp="".join([f"{k}{v}" for k,v in sorted(q.items())])
        body=json.dumps(b,separators=(',',':')) if b else ""
        digest=sha256_hex(nonce+ts+self.k+qp+body)
        sign=sha256_hex(digest+self.s)
        h={**self.h,"api-key":self.k,"nonce":nonce,"timestamp":ts,"sign":sign}
        url=BASE+path
        if q: url+="?"+ "&".join([f"{k}={v}" for k,v in sorted(q.items())])
        return url,h,body
    def get(self,p,q=None):
        u,h,_=self._sign("GET",p,q,None)
        return requests.get(u,headers=h,timeout=30).json()
    def post(self,p,b=None):
        u,h,d=self._sign("POST",p,None,b)
        return requests.post(u,headers=h,data=d,timeout=30).json()
    def change_leverage(self,symbol,lev,coin="USDT"):
        return self.post("/api/v1/futures/account/change_leverage",{"symbol":symbol,"leverage":int(lev),"marginCoin":coin})
    def change_margin_mode(self,symbol,mode="ISOLATION",coin="USDT"):
        return self.post("/api/v1/futures/account/change_margin_mode",{"symbol":symbol,"marginMode":mode,"marginCoin":coin})
    def change_position_mode(self,mode="ONE_WAY"):
        return self.post("/api/v1/futures/account/change_position_mode",{"positionMode":mode})
    def place(self,b): return self.post("/api/v1/futures/trade/place_order",b)
    def pending_orders(self,symbol): return self.post("/api/v1/futures/trade/get_pending_orders",{"symbol":symbol})
    def cancel_orders(self,orderIdList): return self.post("/api/v1/futures/trade/cancel_orders",{"orderIdList":orderIdList})
    def positions(self,symbol=None):
        q={}
        if symbol: q["symbol"]=symbol
        return self.get("/api/v1/futures/position/get_pending_positions",q)

def ensure_dirs():
    pathlib.Path(APPDIR).mkdir(parents=True,exist_ok=True)
    pathlib.Path(LOGDIR).mkdir(parents=True,exist_ok=True)
    if not os.path.exists(SECRETS):
        with open(SECRETS,"w") as f: json.dump({"api_key":"","api_secret":""},f)
        os.chmod(SECRETS,stat.S_IRUSR|stat.S_IWUSR)
    if not os.path.exists(CONFIG):
        with open(CONFIG,"w") as f: yaml.safe_dump({"symbol":"BTCUSDT","leverage":3,"marginMode":"ISOLATION","positionMode":"ONE_WAY","tif":"GTC","levels":16,"bandPct":3.0,"highestSell":200000,"maxPlacePerTick":12},f)
    if not os.path.exists(PLAN):
        with open(PLAN,"w") as f: yaml.safe_dump({"symbol":"BTCUSDT","levels":[],"meta":{"created":int(time.time())}},f)
    if not os.path.exists(TICKCSV):
        with open(TICKCSV,"w",newline="") as f:
            w=csv.writer(f)
            w.writerow(["ts_iso","symbol","cap","hb","lb","band_pct","levels_total","levels_pending","levels_placed","levels_filled","placed_buys","placed_sells","qty_per_level","available_usdt","leverage","sum_buy_prices"])

def load_secrets(): return json.load(open(SECRETS))
def load_cfg(): return yaml.safe_load(open(CONFIG))
def save_cfg(cfg): yaml.safe_dump(cfg,open(CONFIG,"w"))
def load_plan(): return yaml.safe_load(open(PLAN))
def save_plan(p): yaml.safe_dump(p,open(PLAN,"w"))

def input_float(p):
    while True:
        v=input(p).strip()
        try: return float(v)
        except: console.print("[red]Invalid number[/]")

def input_int(p, default=None, minv=2):
    while True:
        v=input(p).strip()
        if v=="" and default is not None: return int(default)
        try:
            iv=int(v)
            if iv>=minv: return iv
            else: console.print(f"[red]Must be >= {minv}[/]")
        except: console.print("[red]Invalid integer[/]")

def input_float_default(p, default):
    v=input(p).strip()
    if v=="": return float(default)
    try: return float(v)
    except: return float(default)

def _first_row_like(d):
    if isinstance(d,list) and d: return d[0]
    if isinstance(d,dict): return d
    return {}

def get_account(c):
    r=c.get("/api/v1/futures/account",{"marginCoin":"USDT"})
    row=_first_row_like(r.get("data"))
    avail=0.0
    for k in ("available","availableBalance","availableMargin","cashBalance","availableCash"):
        if k in row:
            try: avail=float(row[k]); break
            except: pass
    return avail,r

def get_rules(c,symbol):
    r=c.get("/api/v1/futures/market/trading_pairs",{"symbols":symbol})
    row=_first_row_like(r.get("data"))
    basePrecision=int(row.get("basePrecision",4)) if row else 4
    minTradeVolume=float(row.get("minTradeVolume","0.0001")) if row else 0.0001
    return basePrecision,minTradeVolume,r

def round_qty(qty, prec):
    fmt="{:0."+str(prec)+"f}"
    return float(fmt.format(qty))

def detect_buy_cap(c,symbol,probe_price,min_vol):
    r=c.place({"symbol":symbol,"side":"BUY","tradeSide":"OPEN","orderType":"LIMIT","qty":str(min_vol),"price":str(probe_price),"effect":"POST_ONLY","reduceOnly":False,"clientId":f"probe-{uuid.uuid4().hex[:6]}"})
    if r.get("code")==0 and r.get("data") and r["data"].get("orderId"):
        oid=r["data"]["orderId"]; c.cancel_orders([{"orderId":oid,"symbol":symbol}]); return None,r
    m=re.search(r"Max Buy Order Price\s+([0-9]+(?:\.[0-9]+)?)", str(r.get("msg","")))
    if r.get("code")==30014 and m: return float(m.group(1)),r
    return None,r

def cancel_all_symbol(c,symbol):
    pend=c.pending_orders(symbol); ids=[]
    if pend.get("code")==0 and pend.get("data"):
        for o in pend["data"]:
            ids.append({"orderId":o["orderId"],"symbol":symbol})
    if ids: return c.cancel_orders(ids)
    return {"code":0,"msg":"No pending"}

def make_plan(symbol, lowest_buy, highest_buy, highest_sell, total_levels=50, buy_fraction=0.67):
    buy_levels=max(2,int(total_levels*buy_fraction)); sell_levels=max(2,total_levels-buy_levels)
    buys=[round(lowest_buy+i*((highest_buy-lowest_buy)/(buy_levels-1)),2) for i in range(buy_levels)]
    sells=[round(highest_buy+i*((highest_sell-highest_buy)/(sell_levels-1)),2) for i in range(sell_levels)]
    levels=[{"side":"BUY","price":p,"status":"PENDING","orderId":None} for p in buys]+[{"side":"SELL","price":p,"status":"PENDING","orderId":None} for p in sells]
    return {"symbol":symbol,"levels":levels,"meta":{"created":int(time.time()),"lowest_buy":lowest_buy,"highest_buy":highest_buy,"highest_sell":highest_sell,"total":total_levels,"buy_levels":buy_levels,"sell_levels":sell_levels}}

def plan_stats(plan):
    t=len(plan["levels"]); filled=sum(1 for L in plan["levels"] if L["status"]=="FILLED"); placed=sum(1 for L in plan["levels"] if L["status"]=="PLACED"); pending=t-filled-placed
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
                L["status"]="FILLED"
    return plan

def tick_execute(c, cfg, plan, band_pct=None, max_place=None):
    base_prec, min_vol, _=get_rules(c, cfg["symbol"])
    avail,_=get_account(c)
    cap,_=detect_buy_cap(c, cfg["symbol"], 10**9, min_vol)
    if cap is None:
        console.print("[red]Cap unavailable[/]")
        return plan, {"cap":None,"hb":None,"lb":None,"qty":None,"placed_buys":0,"placed_sells":0}
    hb=round(cap*0.999,2); lb=round(hb*(1-(band_pct or cfg.get("bandPct",3.0))/100.0),2)
    buys_window=[L for L in plan["levels"] if L["side"]=="BUY" and L["status"]=="PENDING" and lb<=L["price"]<=hb]
    buys_window=sorted(buys_window, key=lambda x: x["price"])
    if max_place is None: max_place=cfg.get("maxPlacePerTick",12)
    buys_window=buys_window[:max_place]
    qty=compute_qty(avail, cfg["leverage"], [L["price"] for L in buys_window], base_prec, min_vol)
    placed_buys=0; placed_sells=0
    for L in buys_window:
        b={"symbol":cfg["symbol"],"side":"BUY","tradeSide":"OPEN","orderType":"LIMIT","qty":str(qty),"price":str(L["price"]),"effect":cfg["tif"],"reduceOnly":False,"clientId":f"pbuy-{uuid.uuid4().hex[:8]}"}
        r=c.place(b)
        if r.get("code")==20003:
            qty=max(min_vol, round_qty(qty*0.85, base_prec))
            r=c.place({**b,"qty":str(qty),"clientId":f"pbuy-{uuid.uuid4().hex[:8]}"})
        if r.get("code")==0 and r.get("data"):
            L["status"]="PLACED"; L["orderId"]=r["data"]["orderId"]; placed_buys+=1
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
                L["status"]="PLACED"; L["orderId"]=r["data"]["orderId"]; placed_sells+=1
    return plan, {"cap":cap,"hb":hb,"lb":lb,"qty":qty,"placed_buys":placed_buys,"placed_sells":placed_sells,"available":avail,"sum_buy_prices":sum([L["price"] for L in buys_window])}

def log_tick(cfg, plan, meta):
    st=plan_stats(plan)
    ts=datetime.datetime.now(datetime.UTC).isoformat()
    with open(TICKCSV,"a",newline="") as f:
        w=csv.writer(f)
        w.writerow([ts,cfg["symbol"],meta.get("cap"),meta.get("hb"),meta.get("lb"),cfg.get("bandPct",3.0),st["total"],st["pending"],st["placed"],st["filled"],meta.get("placed_buys"),meta.get("placed_sells"),meta.get("qty"),meta.get("available"),cfg.get("leverage"),meta.get("sum_buy_prices")])

def export_snapshot(plan):
    with open(SNAPCSV,"w",newline="") as f:
        w=csv.writer(f)
        w.writerow(["ts_iso","symbol","idx","side","price","status","orderId"])
        ts=datetime.datetime.now(datetime.UTC).isoformat()
        for i,L in enumerate(plan["levels"]):
            w.writerow([ts,plan["symbol"],i,L["side"],L["price"],L["status"],L["orderId"] if L["orderId"] else ""])
    return SNAPCSV

def test_api():
    global _last_api_ok
    sec=load_secrets()
    if not sec["api_key"] or not sec["api_secret"]:
        console.print("[red]No API keys saved yet[/]"); _last_api_ok=False; return False
    c=BitunixClient(sec["api_key"],sec["api_secret"])
    r1=c.get("/api/v1/futures/account/get_leverage_margin_mode",{"symbol":"BTCUSDT","marginCoin":"USDT"})
    r2=c.get("/api/v1/futures/account",{"marginCoin":"USDT"})
    ok=(r1.get("code")==0 and r2.get("code")==0)
    _last_api_ok=ok
    if ok: console.print("[green]API connection successful![/]")
    else: console.print("[red]API connection failed[/]")
    console.print({"leverage_margin_mode":r1,"single_account":r2})
    return ok

def need_api_ok():
    if not _last_api_ok:
        console.print("[yellow]Testing API first...[/]")
        if not test_api():
            console.print("[red]Cannot continue until API test passes[/]")
            return False
    return True

def menu():
    ensure_dirs()
    while True:
        console.print("\n[bold cyan]BitUnix Grid Bot — Plan Mode[/]")
        console.print("1) Test API connection")
        console.print("2) Set API keys")
        console.print("3) Configure defaults")
        console.print("4) Create/Update WIDE PLAN (write plan.yaml)")
        console.print("5) Run PLAN tick now (place allowed slice)")
        console.print("6) Show PLAN status")
        console.print("7) Show positions and pending orders")
        console.print("8) Cancel all pending orders on symbol")
        console.print("9) Export PLAN snapshot CSV")
        console.print("0) Exit")
        choice=input("> ").strip()
        if choice=="1":
            test_api()
        elif choice=="2":
            k=input("API Key: ").strip()
            s=getpass.getpass("API Secret: ").strip()
            with open(SECRETS,"w") as f: json.dump({"api_key":k,"api_secret":s},f)
            os.chmod(SECRETS,stat.S_IRUSR|stat.S_IWUSR)
            console.print("[green]Saved[/]"); test_api()
        elif choice=="3":
            if not need_api_ok(): continue
            cfg=load_cfg()
            sym=input(f"Symbol [{cfg['symbol']}]: ").strip() or cfg["symbol"]
            lev=input(f"Leverage [{cfg.get('leverage',3)}]: ").strip() or str(cfg.get("leverage",3))
            mm=input(f"MarginMode ISOLATION/CROSS [{cfg.get('marginMode','ISOLATION')}]: ").strip() or cfg.get("marginMode","ISOLATION")
            pm=input(f"PositionMode ONE_WAY/HEDGE [{cfg.get('positionMode','ONE_WAY')}]: ").strip() or cfg.get("positionMode","ONE_WAY")
            tif=input(f"TIF GTC/IOC/FOK/POST_ONLY [{cfg.get('tif','GTC')}]: ").strip() or cfg.get("tif","GTC")
            levels=input_int(f"Default Levels [{cfg.get('levels',16)}]: ", default=cfg.get("levels",16), minv=4)
            bandPct=input_float_default(f"Default Buy band % below cap [{cfg.get('bandPct',3.0)}]: ", cfg.get("bandPct",3.0))
            hs=input_float_default(f"Default Highest SELL level [{cfg.get('highestSell',200000)}]: ", cfg.get("highestSell",200000))
            mpt=input_int(f"Max new orders per tick [{cfg.get('maxPlacePerTick',12)}]: ", default=cfg.get("maxPlacePerTick",12), minv=1)
            cfg.update({"symbol":sym,"leverage":int(lev),"marginMode":mm,"positionMode":pm,"tif":tif,"levels":levels,"bandPct":bandPct,"highestSell":hs,"maxPlacePerTick":mpt})
            save_cfg(cfg)
            sec=load_secrets(); c=BitunixClient(sec["api_key"],sec["api_secret"])
            console.print(c.change_leverage(sym,int(lev))); console.print(c.change_margin_mode(sym,mm)); console.print(c.change_position_mode(pm))
        elif choice=="4":
            if not need_api_ok(): continue
            cfg=load_cfg()
            lb=input_float("Plan Lowest BUY: ")
            hb=input_float("Plan Highest BUY: ")
            hs=input_float(f"Plan Highest SELL [{cfg.get('highestSell',200000)}]: ") or cfg.get("highestSell",200000)
            lv=input_int("Plan total levels [50]: ", default=50, minv=4)
            bf=input_float_default("Plan buy fraction (0.67=67%) [0.67]: ", 0.67)
            plan=make_plan(cfg["symbol"], float(lb), float(hb), float(hs), int(lv), float(bf))
            save_plan(plan)
            console.print({"plan":"saved","stats":plan_stats(plan)})
        elif choice=="5":
            if not need_api_ok(): continue
            cfg=load_cfg(); sec=load_secrets(); c=BitunixClient(sec["api_key"],sec["api_secret"])
            plan=load_plan()
            band=input_float_default(f"Band % for this tick [{cfg.get('bandPct',3.0)}]: ", cfg.get("bandPct",3.0))
            mplace=input_int(f"Max new orders this tick [{cfg.get('maxPlacePerTick',12)}]: ", default=cfg.get("maxPlacePerTick",12), minv=1)
            plan,meta=tick_execute(c,cfg,plan,band_pct=band,max_place=mplace); save_plan(plan); log_tick(cfg,plan,meta); console.print({"tick":meta}); status_table(plan)
        elif choice=="6":
            plan=load_plan(); status_table(plan)
        elif choice=="7":
            if not need_api_ok(): continue
            cfg=load_cfg(); sec=load_secrets(); c=BitunixClient(sec["api_key"],sec["api_secret"])
            console.print({"positions":c.positions(cfg["symbol"])})
            console.print({"pending_orders":c.pending_orders(cfg["symbol"])})
        elif choice=="8":
            if not need_api_ok(): continue
            cfg=load_cfg(); sec=load_secrets(); c=BitunixClient(sec["api_key"],sec["api_secret"])
            console.print(cancel_all_symbol(c,cfg["symbol"]))
        elif choice=="9":
            plan=load_plan(); path=export_snapshot(plan); console.print({"snapshot_csv":path})
        elif choice=="0":
            sys.exit(0)
        else:
            console.print("[red]Invalid choice[/]")

def main():
    ensure_dirs()
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan-tick",action="store_true")
    args=ap.parse_args()
    if args.plan_tick:
        if not test_api(): sys.exit(1)
        cfg=load_cfg(); sec=load_secrets(); c=BitunixClient(sec["api_key"],sec["api_secret"])
        plan=load_plan()
        plan,meta=tick_execute(c,cfg,plan,band_pct=cfg.get("bandPct",3.0),max_place=cfg.get("maxPlacePerTick",12))
        save_plan(plan); log_tick(cfg,plan,meta)
        st=plan_stats(plan); console.print({"tick_done":True,"meta":meta,"stats":st})
        sys.exit(0)
    menu()

if __name__=="__main__":
    main()
