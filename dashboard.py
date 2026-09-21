"""Local dashboard for the Hyperliquid arbitrage bot."""

import json
import time
from pathlib import Path

from aiohttp import web

import config


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HypeTrader Control Room</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Space+Grotesk:wght@400;500;600;700&display=swap');
:root{--ink:#f1f4ed;--muted:#8c968b;--line:#26332e;--panel:#111a17;--panel2:#16221e;--bg:#08100e;--mint:#9bf2c4;--amber:#f7c873;--red:#ff7f79;--cyan:#8ed8ed}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 15% 0,#17352d 0,transparent 35%),linear-gradient(135deg,#08100e,#0b1512 55%,#101b17);color:var(--ink);font-family:'Space Grotesk',sans-serif;min-height:100vh}main{max-width:1440px;margin:auto;padding:28px 30px 56px}.top{display:flex;justify-content:space-between;gap:20px;align-items:end;border-bottom:1px solid var(--line);padding-bottom:24px}.kicker,.mono{font-family:'DM Mono',monospace;text-transform:uppercase;letter-spacing:.08em}.kicker{color:var(--mint);font-size:11px}.title{font-size:clamp(30px,4vw,58px);line-height:1;margin:8px 0 0}.sub{color:var(--muted);margin:10px 0 0}.actions{display:flex;gap:10px;flex-wrap:wrap;justify-content:flex-end}button{font:600 13px 'Space Grotesk';border:1px solid var(--line);background:var(--panel2);color:var(--ink);padding:11px 16px;border-radius:5px;cursor:pointer}button:hover{border-color:var(--mint)}button.primary{background:var(--mint);color:#07110d;border-color:var(--mint)}button.danger{color:var(--red);border-color:#663331}.status{display:inline-flex;align-items:center;gap:8px;font-size:12px;color:var(--muted);margin-top:20px}.dot{width:9px;height:9px;border-radius:50%;background:var(--red)}.dot.on{background:var(--mint);box-shadow:0 0 15px var(--mint)}.grid{display:grid;grid-template-columns:1.35fr .65fr;gap:16px;margin-top:20px}.panel{background:rgba(17,26,23,.88);border:1px solid var(--line);border-radius:7px;padding:18px}.panel h2{font-size:14px;margin:0 0 16px}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.metric{background:#0c1512;border:1px solid var(--line);padding:15px;border-radius:5px}.label{font-size:11px;color:var(--muted);text-transform:uppercase}.value{font:500 25px 'DM Mono';margin-top:8px}.positive{color:var(--mint)}.warning{color:var(--amber)}.markets{grid-column:1/-1}.table{width:100%;border-collapse:collapse}.table th{text-align:left;color:var(--muted);font:11px 'DM Mono';text-transform:uppercase;padding:9px 8px;border-bottom:1px solid var(--line)}.table td{padding:14px 8px;border-bottom:1px solid #1a2621;font-size:13px}.table tr:last-child td{border:0}.pair{font-weight:600}.tag{font:11px 'DM Mono';padding:4px 7px;border:1px solid var(--line);border-radius:3px;color:var(--cyan)}.cards{display:grid;grid-template-columns:1fr 1fr;gap:10px}.kv{display:flex;justify-content:space-between;gap:10px;padding:8px 0;border-bottom:1px solid #1a2621;font-size:12px}.kv:last-child{border:0}.kv span:first-child{color:var(--muted)}.positions{min-height:80px;color:var(--muted);font-size:13px}.position{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid #1a2621}.notice{position:fixed;right:22px;bottom:22px;background:var(--panel2);border:1px solid var(--mint);padding:12px 16px;border-radius:5px;display:none}.foot{color:var(--muted);font-size:11px;margin-top:22px}@media(max-width:900px){main{padding:20px 16px}.top{display:block}.actions{justify-content:flex-start;margin-top:18px}.grid{grid-template-columns:1fr}.markets{grid-column:auto}.table{min-width:680px}.panel:has(.table){overflow:auto}.metrics{grid-template-columns:1fr 1fr}.cards{grid-template-columns:1fr}}
</style></head>
<body><main>
<header class="top"><div><div class="kicker">HypeTrader / Control Room</div><h1 class="title">Arbitrage cockpit</h1><p class="sub">Live market distance, account state, and guarded controls.</p><div class="status"><span id="dot" class="dot"></span><span id="status">Bot stopped</span><span>·</span><span id="tradingStatus">Trading disabled</span><span>·</span><span id="updated">Waiting for telemetry</span></div></div><div class="actions"><button id="toggle" class="primary" onclick="toggleBot()">Start bot</button><button id="tradingToggle" onclick="toggleTrading()">Start trading</button><button class="danger" onclick="closePositions()">Close positions at market</button></div></header>
<section class="grid"><div class="panel"><h2>Account pulse</h2><div class="metrics"><div class="metric"><div class="label">Account value</div><div id="balance" class="value">—</div></div><div class="metric"><div class="label">Open positions</div><div id="positionCount" class="value">—</div></div><div class="metric"><div class="label">Exposure</div><div id="exposure" class="value">—</div></div></div></div>
<div class="panel"><h2>Risk state</h2><div id="risk" class="cards"></div></div>
<div class="panel markets"><h2>Market distance <span class="mono" style="color:var(--muted);font-size:10px">mid prices / basis points</span></h2><table class="table"><thead><tr><th>Pair</th><th>Perp mid</th><th>Spot mid</th><th>Distance</th><th>Book age</th><th>State</th></tr></thead><tbody id="markets"></tbody></table></div>
<div class="panel"><h2>Current positions</h2><div id="positions" class="positions">No open positions.</div></div>
<div class="panel"><h2>Configured guardrails</h2><div id="config" class="cards"></div></div></section>
<div class="foot mono">Local control surface · API keys remain server-side · last refresh <span id="refresh">—</span></div><div id="notice" class="notice"></div></main>
<audio id="tradeSound" src="/assets/coin.mp3" preload="auto"></audio><audio id="errorSound" src="/assets/error.mp3" preload="auto"></audio>
<script>
let wasRunning=false,lastEvents=0;
const money=v=>v==null?'—':`$${Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}`;
const num=v=>v==null?'—':Number(v).toLocaleString(undefined,{maximumFractionDigits:8});
function notify(text){const n=document.querySelector('#notice');n.textContent=text;n.style.display='block';setTimeout(()=>n.style.display='none',3500)}
async function command(path){const r=await fetch(path,{method:'POST'});const d=await r.json();notify(d.message||d.error||'Done');await refresh()}
async function toggleBot(){await command(document.querySelector('#toggle').dataset.running==='true'?'/api/stop':'/api/start')}
async function toggleTrading(){await command(document.querySelector('#tradingToggle').dataset.enabled==='true'?'/api/trading/stop':'/api/trading/start')}
async function closePositions(){if(confirm('Send market orders to close all tracked legs?'))await command('/api/close')}
function render(d){document.querySelector('#dot').className='dot '+(d.running?'on':'');document.querySelector('#status').textContent=d.running?'Bot running':'Bot stopped';document.querySelector('#tradingStatus').textContent=d.trading_enabled?'Trading enabled':'Trading disabled';const b=document.querySelector('#toggle');b.dataset.running=d.running;b.textContent=d.running?'Stop bot':'Start bot';const t=document.querySelector('#tradingToggle');t.dataset.enabled=d.trading_enabled;t.disabled=!d.running;t.textContent=d.trading_enabled?'Stop trading':'Start trading';document.querySelector('#balance').textContent=money(d.account.value);document.querySelector('#positionCount').textContent=d.account.positions.length;document.querySelector('#exposure').textContent=money(d.account.positions.reduce((s,p)=>s+Math.abs(p.size*p.entry_price),0));document.querySelector('#updated').textContent='Live';document.querySelector('#refresh').textContent=new Date().toLocaleTimeString();
const r=d.risk||{};document.querySelector('#risk').innerHTML=[['Trading',r.halted?'HALTED':(d.trading_enabled?'ENABLED':'DISABLED'),r.halted?'warning':(d.trading_enabled?'positive':'')],['Daily PnL',money(r.daily_realized_pnl),''],['Peak value',money(r.peak_account_value),'']].map(x=>`<div class="kv"><span>${x[0]}</span><strong class="${x[2]}">${x[1]}</strong></div>`).join('');
document.querySelector('#markets').innerHTML=d.markets.map(m=>`<tr><td class="pair">${m.perp} <span class="tag">/ ${m.spot}</span></td><td class="mono">${num(m.perp_mid)}</td><td class="mono">${num(m.spot_mid)}</td><td class="mono ${Math.abs(m.spread_bps)>=d.config.MIN_SPREAD_BPS?'warning':''}">${m.spread_bps==null?'—':m.spread_bps.toFixed(2)+' bps'}</td><td class="mono">${m.age==null?'—':m.age.toFixed(1)+'s'}</td><td>${m.state}</td></tr>`).join('');document.querySelector('#positions').innerHTML=d.account.positions.length?d.account.positions.map(p=>`<div class="position"><span><strong>${p.asset}</strong> ${p.side} ${num(p.size)} <span class="tag">${p.market}</span></span><span class="mono">${money(p.unrealized_pnl)}</span></div>`).join(''):'No open positions.';document.querySelector('#config').innerHTML=[['Max position',money(d.config.MAX_POSITION_USD)],['Max order',money(d.config.MAX_ORDER_USD)],['Total exposure',money(d.config.MAX_TOTAL_EXPOSURE_USD)],['Min spread',d.config.MIN_SPREAD_BPS+' bps'],['Close spread',d.config.CLOSE_SPREAD_BPS+' bps'],['Spot sells',d.config.ALLOW_SPOT_SELL?'Enabled':'Disabled']].map(x=>`<div class="kv"><span>${x[0]}</span><strong>${x[1]}</strong></div>`).join('');wasRunning=d.running}
async function refresh(){try{const r=await fetch('/api/state');const d=await r.json();render(d);const e=await (await fetch('/api/events?after='+lastEvents)).json();for(const x of e.events){if(x.kind==='trade')document.querySelector('#tradeSound').play().catch(()=>{});if(x.kind==='error')document.querySelector('#errorSound').play().catch(()=>{});lastEvents=Math.max(lastEvents,x.id)}}catch(e){document.querySelector('#status').textContent='Dashboard connection lost'}}
refresh();setInterval(refresh,2000);
</script></body></html>"""


def _public_config() -> dict:
    keys = ("MAX_POSITION_USD", "MAX_ORDER_USD", "MAX_TOTAL_EXPOSURE_USD", "MIN_SPREAD_BPS", "CLOSE_SPREAD_BPS", "ALLOW_SPOT_SELL")
    return {key: getattr(config, key) for key in keys}


class Dashboard:
    def __init__(self, runtime):
        self.runtime = runtime
        self.app = web.Application()
        self.app.router.add_get("/", self.index)
        self.app.router.add_get("/api/state", self.state)
        self.app.router.add_get("/api/events", self.events)
        self.app.router.add_get("/assets/{name}", self.asset)
        self.app.router.add_post("/api/start", self.start)
        self.app.router.add_post("/api/stop", self.stop)
        self.app.router.add_post("/api/trading/start", self.start_trading)
        self.app.router.add_post("/api/trading/stop", self.stop_trading)
        self.app.router.add_post("/api/close", self.close)
        self._event_id = 0

    async def index(self, request):
        return web.Response(text=INDEX_HTML, content_type="text/html")

    async def state(self, request):
        account = await self.runtime.account_snapshot()
        markets = []
        for perp, spot in config.ARB_PAIRS:
            perp_book = self.runtime.feed.get_book(perp, "perp") if self.runtime.feed else None
            spot_book = self.runtime.feed.get_book(spot, "spot") if self.runtime.feed else None
            ages = []
            if perp_book: ages.append(time.time() - perp_book.ts)
            if spot_book: ages.append(time.time() - spot_book.ts)
            markets.append({
                "perp": perp, "spot": spot,
                "perp_mid": perp_book.mid if perp_book else None,
                "spot_mid": spot_book.mid if spot_book else None,
                "spread_bps": self.runtime.feed.get_spread_bps(perp, spot) if self.runtime.feed else None,
                "age": max(ages) if ages else None,
                "state": self.runtime.strategy._positions[perp].state.value if self.runtime.strategy else "stopped",
            })
        risk = account.pop("risk", {})
        strategy_positions = account.get("strategy_positions", [])
        if strategy_positions:
            account["positions"] = [
                {
                    "asset": leg["asset"],
                    "market": leg["market"],
                    "side": leg["side"],
                    "size": leg["size"],
                    "entry_price": leg["price"],
                    "unrealized_pnl": 0.0,
                    "status": leg["status"],
                    "close_status": leg["close_status"],
                }
                for position in strategy_positions
                for leg in position["legs"]
            ]
        return web.json_response({"running": self.runtime.running, "trading_enabled": self.runtime.trading_enabled, "account": account, "risk": risk, "markets": markets, "config": _public_config()})

    async def events(self, request):
        after = int(request.query.get("after", "0"))
        events = []
        log_path = Path(__file__).parent / config.LOG_FILE
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
            for line in lines:
                event_id = abs(hash(line))
                if event_id <= after:
                    continue
                if "Order filled" in line or "Position closed" in line:
                    events.append({"id": event_id, "kind": "trade"})
                elif "ERROR" in line or "CRITICAL" in line:
                    events.append({"id": event_id, "kind": "error"})
        return web.json_response({"events": events})

    async def asset(self, request):
        name = request.match_info["name"]
        if name not in ("coin.mp3", "error.mp3"):
            raise web.HTTPNotFound()
        path = Path(__file__).with_name("dashboard_assets") / name
        if not path.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def start(self, request):
        await self.runtime.start()
        return web.json_response({"message": "Bot started"})

    async def stop(self, request):
        await self.runtime.stop()
        return web.json_response({"message": "Bot stopped"})

    async def start_trading(self, request):
        if not self.runtime.running:
            return web.json_response({"error": "Start the bot before enabling trading"}, status=409)
        self.runtime.set_trading_enabled(True)
        return web.json_response({"message": "Trading enabled"})

    async def stop_trading(self, request):
        self.runtime.set_trading_enabled(False)
        return web.json_response({"message": "Trading disabled"})

    async def close(self, request):
        closed = await self.runtime.close_positions()
        return web.json_response({"message": f"Close requested for {len(closed)} position(s)", "assets": closed})

    async def run(self, host="127.0.0.1", port=8080):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        return runner
