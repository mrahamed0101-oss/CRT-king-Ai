#!/usr/bin/env python3
"""CRT KING AI - OKX 15m rule-based scanner. READ ONLY; never places orders."""
import os, time, logging, math
from datetime import datetime, timezone
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("CRT_KING")
TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
CHAT=os.getenv("TELEGRAM_CHAT_ID","").strip()
INTERVAL=max(60,int(os.getenv("SCAN_INTERVAL_SECONDS","180")))
MIN_SCORE=max(3,int(os.getenv("MIN_SIGNAL_SCORE","5")))
BASE="https://www.okx.com"; TF="15m"; TOP_N=20; COOLDOWN=3600
S=requests.Session(); S.headers["User-Agent"]="CRT-KING-AI/2.0"
last_alert={}; update_id=0; scan_status="Starting"; scan_time="Not scanned"; scan_count=0

def api(path,params=None):
    r=S.get(BASE+path,params=params,timeout=15); r.raise_for_status()
    j=r.json()
    if j.get("code")!="0": raise RuntimeError(j.get("msg","OKX API error"))
    return j.get("data",[])

def send(msg,chat=None):
    target=str(chat or CHAT)
    r=S.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
             json={"chat_id":target,"text":msg,"disable_web_page_preview":True},timeout=15)
    if not r.ok: log.error("Telegram error: %s",r.text[:250]); return False
    log.info("Telegram message sent to %s",target); return True

def commands():
    global update_id
    r=S.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates",
            params={"offset":update_id+1,"timeout":1},timeout=5)
    if not r.ok: return
    for u in r.json().get("result",[]):
        update_id=max(update_id,u["update_id"])
        m=u.get("message") or {}; chat=str(m.get("chat",{}).get("id",""))
        cmd=(m.get("text") or "").split()
        if not cmd: continue
        cmd=cmd[0].lower()
        if cmd in ("/start","/help"):
            send("ð CRT KING AI\n/start - Start\n/help - Help\n/status - Scanner status\n"
                 "OKX USDT swaps, 15m. CRT/SMC are rule-based approximations. READ-ONLY; no trades.",chat)
        elif cmd=="/status":
            send(f"ð CRT KING AI STATUS\n{scan_status}\nLast scan: {scan_time}\nAlerts last scan: {scan_count}\nREAD-ONLY",chat)

def ema(v,n):
    if len(v)<n:return None
    x=sum(v[:n])/n; k=2/(n+1)
    for z in v[n:]: x=z*k+x*(1-k)
    return x

def atr(c,n=14):
    tr=[max(c[i]["h"]-c[i]["l"],abs(c[i]["h"]-c[i-1]["c"]),abs(c[i]["l"]-c[i-1]["c"])) for i in range(1,len(c))]
    return sum(tr[-n:])/n if len(tr)>=n else None

def pivots(c,left=3,right=3):
    hi=[]; lo=[]
    for i in range(left,len(c)-right):
        if all(c[i]["h"]>c[j]["h"] for j in range(i-left,i+right+1) if j!=i):hi.append((i,c[i]["h"]))
        if all(c[i]["l"]<c[j]["l"] for j in range(i-left,i+right+1) if j!=i):lo.append((i,c[i]["l"]))
    return hi,lo

def candles(inst):
    rows=api("/api/v5/market/candles",{"instId":inst,"bar":TF,"limit":"220"})
    out=[]
    for x in reversed(rows):
        try: out.append(dict(ts=int(x[0]),o=float(x[1]),h=float(x[2]),l=float(x[3]),c=float(x[4]),confirm=x[8]))
        except Exception: pass
    if out and out[-1]["confirm"]=="0":out.pop()
    return out

def universe():
    ts=api("/api/v5/market/tickers",{"instType":"SWAP"}); a=[]
    for t in ts:
        if t.get("instId","").endswith("-USDT-SWAP"):
            try:a.append((float(t.get("volCcy24h") or 0),t["instId"]))
            except Exception:pass
    return [x[1] for x in sorted(a,reverse=True)[:TOP_N]]

def analyze(inst,c):
    if len(c)<105:return None
    v=[x["c"] for x in c]; z=c[-1]; p=c[-2]; a=atr(c)
    if not a or a<=0:return None
    es={n:ema(v,n) for n in (9,20,50,100,200)}
    hi,lo=pivots(c[:-1]); sh=hi[-1][1] if hi else max(x["h"] for x in c[-30:-1])
    slw=lo[-1][1] if lo else min(x["l"] for x in c[-30:-1])
    prevhi=max(x["h"] for x in c[-21:-1]); prevlo=min(x["l"] for x in c[-21:-1])
    # CRT rolling four-candle range reclaim proxy
    rh=max(x["h"] for x in c[-5:-1]); rl=min(x["l"] for x in c[-5:-1])
    crtL=p["c"]<rl<z["c"] and z["c"]>z["o"]
    crtS=p["c"]>rh>z["c"] and z["c"]<z["o"]
    sweepL=z["l"]<prevlo and z["c"]>prevlo
    sweepS=z["h"]>prevhi and z["c"]<prevhi
    bull=z["c"]>sh and p["c"]<=sh; bear=z["c"]<slw and p["c"]>=slw
    up=es[9]>es[20]>es[50]; down=es[9]<es[20]<es[50]
    chochL=bull and down; chochS=bear and up
    bosL=bull and up; bosS=bear and down
    fvgL=z["l"]>c[-3]["h"]; fvgS=z["h"]<c[-3]["l"]
    # OB proxy: last opposite candle before confirmed break
    obL=obS=None
    if bull:
        for x in reversed(c[-10:-1]):
            if x["c"]<x["o"]:obL=(x["l"],x["h"]);break
    if bear:
        for x in reversed(c[-10:-1]):
            if x["c"]>x["o"]:obS=(x["l"],x["h"]);break
    dh=max(x["h"] for x in c[-50:-1]); dl=min(x["l"] for x in c[-50:-1]); mid=(dh+dl)/2
    discount=z["c"]<mid; premium=z["c"]>mid
    L=[]; Q=[]
    def add(ok,pts,label,target):
        if ok:target[0]+=pts;target[1].append(label)
    ls=[0,[]]; ss=[0,[]]
    add(up,1,"EMA trend",ls);add(down,1,"EMA trend",ss)
    add(es[100] is not None and es[50]>es[100],1,"EMA100",ls);add(es[100] is not None and es[50]<es[100],1,"EMA100",ss)
    add(es[200] is not None and z["c"]>es[200],1,"EMA200",ls);add(es[200] is not None and z["c"]<es[200],1,"EMA200",ss)
    add(sweepL,2,"Sell-side sweep",ls);add(sweepS,2,"Buy-side sweep",ss)
    add(bull,2,"BOS/CHoCH",ls);add(bear,2,"BOS/CHoCH",ss)
    add(fvgL,1,"FVG",ls);add(fvgS,1,"FVG",ss)
    add(obL is not None,1,"Order Block",ls);add(obS is not None,1,"Order Block",ss)
    add(discount,1,"Discount",ls);add(premium,1,"Premium",ss)
    add(crtL,2,"CRT reclaim",ls);add(crtS,2,"CRT reclaim",ss)
    add(z["c"]>z["o"],1,"Bull candle",ls);add(z["c"]<z["o"],1,"Bear candle",ss)
    direction="LONG" if ls[0]>=MIN_SCORE and ls[0]>ss[0] else "SHORT" if ss[0]>=MIN_SCORE and ss[0]>ls[0] else "WAIT"
    if direction=="WAIT":return {"direction":"WAIT"}
    if direction=="LONG":
        stop=min(slw,obL[0] if obL else slw)-a*.15; risk=z["c"]-stop; t=[z["c"]+risk*i for i in (1,2,3)]; tags=ls[1]
    else:
        stop=max(sh,obS[1] if obS else sh)+a*.15; risk=stop-z["c"]; t=[z["c"]-risk*i for i in (1,2,3)]; tags=ss[1]
    if risk<=0:return None
    return dict(inst=inst,d=direction,direction=direction,entry=z["c"],sl=stop,tp=t,ls=ls[0],ss=ss[0],tags=tags,
      ema=es,range=(rl,rh),swings=(slw,sh),bos="Bullish" if bosL else "Bearish" if bosS else "None",
      choch="Bullish" if chochL else "Bearish" if chochS else "None",sweep="Sell-side" if sweepL else "Buy-side" if sweepS else "None",
      zone="Discount" if discount else "Premium" if premium else "Equilibrium",ob=(obL,obS),
      fvg=("Bullish" if fvgL else "Bearish" if fvgS else "None"),demand=(slw,slw+a*.35),supply=(sh-a*.35,sh))

def fmt(x):return f"{x:.8g}"
def message(s):
    e=s["ema"]
    return (f"ð CRT KING AI ALERT\n\nCoin: {s['inst'].replace('-','/')}\nSignal: {s['d']}\nTimeframe: {TF}\n"
      f"Entry: {fmt(s['entry'])}\nStop Loss: {fmt(s['sl'])}\nTP1: {fmt(s['tp'][0])}\nTP2: {fmt(s['tp'][1])}\nTP3: {fmt(s['tp'][2])}\n"
      f"LONG Score: {s['ls']} | SHORT Score: {s['ss']}\n"
      f"EMA 9/20/50/100/200: {' / '.join(fmt(e[n]) if e[n] else 'N/A' for n in (9,20,50,100,200))}\n"
      f"CRT Range Low/High: {fmt(s['range'][0])} / {fmt(s['range'][1])}\n"
      f"Swing Low/High: {fmt(s['swings'][0])} / {fmt(s['swings'][1])}\n"
      f"BOS: {s['bos']} | CHoCH: {s['choch']} | Liquidity sweep: {s['sweep']}\n"
      f"Premium/Discount: {s['zone']}\nOB (bull/bear): {s['ob']}\nFVG: {s['fvg']}\n"
      f"Demand: {s['demand']} | Supply: {s['supply']}\nConfirmations: {', '.join(s['tags'])}\n\n"
      "â ï¸ Rule-based estimates, not guaranteed. Verify on chart. READ-ONLY: no orders placed.")

def main():
    global scan_status,scan_time,scan_count
    if not TOKEN or not CHAT:raise RuntimeError("Railway Variables missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    send("ð CRT KING AI is online. OKX scanner started. READ-ONLY.")
    while True:
        try:
            commands(); scan_status="Scanning"; count=0; symbols=universe()
            for inst in symbols:
                try:
                    commands(); s=analyze(inst,candles(inst))
                    if not s or s.get("direction", s.get("d", "WAIT"))=="WAIT":continue
                    key=(inst,s["d"]); now=time.time()
                    if now-last_alert.get(key,0)<COOLDOWN:continue
                    if send(message(s)):last_alert[key]=now;count+=1
                    time.sleep(.15)
                except Exception as ex:log.warning("%s: %s",inst,ex)
            scan_count=count;scan_time=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC");scan_status="Running"
            log.info("Scan complete: %d symbols, %d alerts",len(symbols),count)
        except Exception as ex:scan_status=f"Error: {ex}";log.exception("Scan cycle failed")
        for _ in range(max(1,INTERVAL//5)):
            try:commands()
            except Exception:log.exception("Telegram polling failed")
            time.sleep(5)

if __name__=="__main__":main()




