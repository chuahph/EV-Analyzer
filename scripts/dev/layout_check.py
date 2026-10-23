"""Landscape Glance layout check for every phone size, inside each phone's
safe areas.

Every Glance page (Live parked, driving, readings late, dimmed, and the
eight other tabs) on 12 phones, each as a home-screen app (notch and
home-bar insets) and in Safari (its bars take ~52 px of height). Reports
any text outside the safe area, spilling or cut off by its box, a bottom
row that wraps, side boxes that overlap or run over the title bar.

Run a local server on demo data, then:
    DATABASE_URL=sqlite:///./layout.db APP_PASSCODE="" python run.py serve --port 8000
    python scripts/dev/layout_check.py http://127.0.0.1:8000 /tmp
An empty report (only "page errors": []) is a pass. Needs playwright; set
CHROME_PATH to use a preinstalled Chromium.
"""
import httpx, datetime, time, sys, json, re
from playwright.sync_api import sync_playwright
import os
CHROME=os.environ.get("CHROME_PATH") or None
B=sys.argv[1] if len(sys.argv)>1 else "http://127.0.0.1:8000"
OUT=sys.argv[2] if len(sys.argv)>2 else "."
MI=1/1.609344
ONLY=sys.argv[3] if len(sys.argv)>3 else None
def iso(t): return datetime.datetime.fromtimestamp(t,datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
# landscape CSS size, left/right/bottom safe-area insets (standalone web app)
DEV=[("SE 2/3",667,375,0,0,0),("12/13 mini",812,375,50,50,21),("11/XR",896,414,48,48,21),
     ("12-14",844,390,47,47,21),("14 Plus",926,428,47,47,21),("14 Pro-16",852,393,59,59,21),
     ("16 Pro",874,402,62,62,21),("15 Pro Max",932,430,59,59,21),("16 Pro Max",956,440,62,62,21),
     ("Pixel 7",915,412,24,0,0),("Android S",800,360,0,0,0),("SE 1",568,320,0,0,0)]
CHK=r"""([L,R,Bo])=>{
 const out=[]; const W=innerWidth,H=innerHeight;
 const root=document.querySelector('.glance'); if(!root||getComputedStyle(root).display==='none') return ['glance hidden'];
 const BOX='.gl-rt,.gl-mini,.gl-odo,.gl-trip,.gl-power,.gl-battbar,.gl-trace,.gl-chip,.gl-live,.gl-foot,.gl-head,.gl-box,.gl-tip,.gl-pod,.gl-panel,.gl-tab,.gl-legend,.gl-split,.gl-side,.gl-main';
 const vis=e=>{for(let x=e;x&&x!==document.body;x=x.parentElement){const c=getComputedStyle(x);if(c.display==='none'||c.visibility==='hidden'||+c.opacity===0)return false;}return true;};
 const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
 for(let n;(n=walker.nextNode());){
   if(!n.textContent.trim()) continue; const e=n.parentElement;
   if(e.closest('.gl-frame')||e.closest('svg')) continue; if(!vis(e)) continue;
   const rg=document.createRange(); rg.selectNodeContents(n); const r=rg.getBoundingClientRect(); if(!r.width) continue;
   const t=n.textContent.trim().slice(0,28);
   if(r.left<L-0.5||r.right>W-R+0.5||r.bottom>H-Bo+0.5||r.top<-0.5) out.push(`outside safe area: "${t}"`);
   const box=e.closest(BOX); if(box){const p=box.getBoundingClientRect();const cs=getComputedStyle(box);
     const pl=box.classList.contains('gl-main')?0:parseFloat(cs.borderLeftWidth)||0, pr=box.classList.contains('gl-main')?0:parseFloat(cs.borderRightWidth)||0;
     if(r.right>p.right-pr+0.6||r.left<p.left+pl-0.6) out.push(`spills ${box.className.split(' ')[0]}: "${t}" by ${Math.max(r.right-p.right+pr,p.left+pl-r.left).toFixed(1)}`);
     if(r.bottom>p.bottom+0.6||r.top<p.top-0.6) out.push(`spills vertically ${box.className.split(' ')[0]}: "${t}"`);}
   for(let x=e;x&&x!==root;x=x.parentElement){const c=getComputedStyle(x);
     if((c.overflow==='hidden'||c.overflowX==='hidden')&&(x.scrollWidth>x.clientWidth+0.5)&&x.contains(n)){
       const xr=x.getBoundingClientRect(); if(r.right>xr.right+0.5||r.left<xr.left-0.5){out.push(`cut off by ${x.className.split(' ')[0]}: "${t}"`);break;}}}
 }
 const foot=root.querySelector('.gl-foot'); if(foot&&vis(foot)){const kids=[...foot.children].filter(vis).map(k=>k.getBoundingClientRect().top);
   if(kids.length&&Math.max(...kids)-Math.min(...kids)>4) out.push('bottom row wraps');}
 // boxes overlapping each other inside the side columns
 for(const side of root.querySelectorAll('.gl-side')){const ks=[...side.children].filter(vis).map(k=>k.getBoundingClientRect());
   for(let i=1;i<ks.length;i++) if(ks[i].top<ks[i-1].bottom-1) out.push('side boxes overlap');}
 for(const c of root.querySelectorAll('.gl-side,.gl-pod,.gl-page,.gl-body')){ if(!vis(c)) continue;
   const cr=c.getBoundingClientRect();
   for(const k of c.querySelectorAll(':scope > *')){ if(!vis(k)) continue; const r=k.getBoundingClientRect(); if(!r.height) continue;
     if(r.top<cr.top-6||r.bottom>cr.bottom+6) {out.push('box overflows '+String(c.getAttribute('class')).split(' ')[0]+': '+String(k.getAttribute('class')||k.tagName).split(' ')[0]+' by '+Math.max(cr.top-r.top,r.bottom-cr.bottom).toFixed(0)); }}}
 const hd=root.querySelector('.gl-head'); if(hd){const hb=hd.getBoundingClientRect().bottom; for(const k of root.querySelectorAll('.gl-side > *, .gl-pod > *')){ if(vis(k)&&k.getBoundingClientRect().top<hb-1) {out.push('box covers title bar: '+String(k.getAttribute('class')||k.tagName).split(' ')[0]); break;}}}
 const page=root.querySelector('.gl-body'), f=root.querySelector('.gl-foot');
 if(page&&f&&vis(f)){ if(page.getBoundingClientRect().bottom>f.getBoundingClientRect().top+1) out.push('page overlaps bottom row'); }
 return [...new Set(out)];
}"""
def send(vin,t,f):
    d=[]
    for k,v in f.items():
        d.append({"key":k,"value":{"shiftStateValue":v}} if k=="Gear" else ({"key":k,"value":{"stringValue":v}} if isinstance(v,str) else {"key":k,"value":{"doubleValue":v}}))
    httpx.post(B+"/api/telemetry/live",json={"records":[{"vin":vin,"createdAt":iso(t),"data":d}]})
res={}; shots=[]
with sync_playwright() as p:
    b=p.chromium.launch(executable_path=CHROME)
    ctx=b.new_context(viewport={"width":393,"height":852},is_mobile=True,has_touch=True,service_workers="block")
    pg=ctx.new_page(); errs=[]; pg.on("pageerror",lambda e:errs.append(str(e)))
    pg.add_init_script("localStorage.setItem('orientHintOff','1')")
    def summ(r):
        resp=r.fetch(); j=resp.json()
        j["driving"]["recent_trips"]=[{"id":267,"start_time":"2026-10-15T07:31","end_time":"2026-10-15T07:49","distance_km":11.2,"duration_min":18,"avg_speed_kmh":37,"wh_per_km":134,"efficiency_band":"typical"}]+(j["driving"].get("recent_trips") or [])
        r.fulfill(status=200,content_type="application/json",body=json.dumps(j))
    pg.route(re.compile(r"/api/summary"),summ)
    pg.goto(B+"/"); pg.wait_for_timeout(1500)
    pg.locator("#home-cars .car-card").first.click(); pg.wait_for_timeout(3000)
    vin=pg.evaluate("lastData.vehicle.vin")
    style=None
    def states():
        # (name, setup) for the live page
        return ["parked","driving","stale","dimmed"]
    def setdev(name,W,H,L,R,Bo,mode):
        h=H if mode=="app" else H-52
        bo=Bo if mode=="app" else 0
        pg.set_viewport_size({"width":W,"height":h})
        pad=f"max(8px,0px) max(14px,{R}px) max(8px,{bo}px) max(14px,{L}px)"
        pg.evaluate("(css)=>{let s=document.getElementById('safe-emu');if(!s){s=document.createElement('style');s.id='safe-emu';document.head.appendChild(s);} s.textContent=css;}", f".glance{{padding:{pad} !important}}")
        pg.wait_for_timeout(500)
        return f"{name} {W}x{h} {mode}", bo
    combos=[(d,m) for d in DEV for m in ("app","safari") if not ONLY or ONLY in d[0]]
    # pass 1: parked (no drive open), every tab
    send(vin,time.time(),{"Gear":"ShiftStateP","VehicleSpeed":0.0,"Odometer":28770.4*MI,"BatteryLevel":52.7,"RatedRange":219*MI,"OutsideTemp":20.0})
    for (name,W,H,L,R,Bo),mode in combos:
        key,bo=setdev(name,W,H,L,R,Bo,mode)
        for tab in ["live","drive","eff","battery","trips","charge","parked","cost","tips"]:
            pg.click(f"[data-gl-tab={tab}]"); pg.wait_for_timeout(1200 if tab=="live" else 450)
            o=pg.evaluate(CHK,[L,R,bo])
            if o: res[f"{key} | {tab}{'-parked' if tab=='live' else ''}"]=o
            if tab=="live" and mode=="app": pg.screenshot(path=f"{OUT}/ss-{W}-parked.png")
            if tab=="live":
                for sk in (1,2,3,4,5,6,7,8,9,10,11,12,13,14):          # the swipeable designs, parked
                    pg.evaluate(f"glSkinSet({sk})"); pg.wait_for_timeout(300)
                    o=pg.evaluate(CHK,[L,R,bo])
                    if o: res[f"{key} | live-parked-skin{sk}"]=o
                pg.evaluate("glSkinSet(0)"); pg.wait_for_timeout(200)
    # pass 2: a drive in progress: fresh, readings late, dimmed
    pg.click("[data-gl-tab=live]")
    t0=time.time()-40; odo=28770.4
    for i in range(40):
        odo+=0.025
        send(vin,t0+i,{"Gear":"ShiftStateD","VehicleSpeed":(118 if i>30 else 90)*MI,"Odometer":odo*MI,"EnergyRemaining":40-i*0.004,"PackVoltage":372.0,"PackCurrent":60.0,"OutsideTemp":32.0,"HvacPower":"On","BatteryLevel":52.7,"RatedRange":219*MI})
    for (name,W,H,L,R,Bo),mode in combos:
        key,bo=setdev(name,W,H,L,R,Bo,mode)
        odo+=0.03; send(vin,time.time(),{"Gear":"ShiftStateD","VehicleSpeed":118*MI,"Odometer":odo*MI,"PackVoltage":372.0,"PackCurrent":60.0})
        pg.wait_for_timeout(1500)
        o=pg.evaluate(CHK,[L,R,bo]);  res.update({f"{key} | live-drive":o} if o else {})
        if mode=="app": pg.screenshot(path=f"{OUT}/ss-{W}-drive.png")
        pg.evaluate("glLiveState.data.now.age_sec=30; glLiveState.recvAt=Date.now()-1000"); pg.wait_for_timeout(1300)
        o=pg.evaluate(CHK,[L,R,bo]);  res.update({f"{key} | live-stale":o} if o else {})
        if mode=="app": pg.screenshot(path=f"{OUT}/ss-{W}-stale.png")
        for sk in (1,2,3,4,5,6,7,8,9,10,11,12,13,14):                  # the swipeable designs, driving, bright then dimmed
            pg.evaluate(f"glSkinSet({sk})"); pg.wait_for_timeout(300)
            o=pg.evaluate(CHK,[L,R,bo])
            if o: res[f"{key} | live-drive-skin{sk}"]=o
            pg.evaluate("document.body.classList.add('gl-dim')"); pg.wait_for_timeout(200)
            o=pg.evaluate(CHK,[L,R,bo])
            if o: res[f"{key} | live-dim-skin{sk}"]=o
            pg.evaluate("document.body.classList.remove('gl-dim')")
        pg.evaluate("glSkinSet(0)"); pg.wait_for_timeout(200)
        pg.evaluate("document.body.classList.add('gl-dim')"); pg.wait_for_timeout(300)
        o=pg.evaluate(CHK,[L,R,bo]);  res.update({f"{key} | live-dim":o} if o else {})
        pg.evaluate("document.body.classList.remove('gl-dim')")
    res["page errors"]=errs
    b.close()
print(json.dumps(res,indent=1))
print("checked:",len(DEV)*2,"device/mode combinations x 12 views")
