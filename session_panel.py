"""session-panel: replay a Claude Code session as an animated live-panel page.

Session numbers follow agentplay's Claude Code counts (Apache-2.0, re-implemented); the page is a
compact renderer for live-panel's JSON config format (MIT). Standard library only.

  python session_panel.py fixtures/session.jsonl          # writes session-panel.html
  python session_panel.py --latest                        # newest session under ~/.claude/projects
  python session_panel.py --config panel.json             # render any live-panel config (supported subset)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path


# --- session: Claude Code JSONL -> steps and totals ------------------------------------

KIND = {"Read": "read", "Grep": "search", "Glob": "search", "LS": "search", "Edit": "edit", "Write": "edit",
        "MultiEdit": "edit", "NotebookEdit": "edit", "WebFetch": "web", "WebSearch": "web", "Task": "subagent",
        "Agent": "subagent", "TodoWrite": "plan"}
# Counting rules follow agentplay's builder.py (Apache-2.0): what is a verify step, a user turn, an error, a retry.
VERIFY = re.compile(r"\b(pytest|go test|go vet|npm (?:run )?(?:test|build|lint)|pnpm (?:run )?(?:test|check|lint)"
                    r"|yarn (?:test|build)|tsc\b|ruff|mypy|make (?:test|check)|cargo (?:test|clippy|check)"
                    r"|vitest|jest|just test)")
MULTI = {"git", "gh", "aws", "npm", "pnpm", "yarn", "make", "cargo", "go", "uv", "just", "docker", "kubectl"}
PREFIX = {"cd", "export", "set", "unset", "source", "pushd", "popd"}
RETRY_WINDOW = 8


def kind_of(name, inp):
    if name == "Bash":
        return "verify" if VERIFY.search(inp.get("command", "")) else "run"
    return "mcp" if name.startswith("mcp__") else KIND.get(name, "tool")


def verb(cmd):
    """First command word(s) of a shell line: 'cd x && npm test -- -u' -> 'npm test'.
    shortcut: no quote-aware tokenizing, unlike agentplay's _shell_verb; upgrade if retries look off."""
    for seg in re.split(r"&&|\|\||;|\n", cmd):
        words = [w for w in seg.split() if not re.match(r"[A-Za-z_]\w*=", w)]
        while words and words[0] in {"sudo", "time", "timeout", "nohup", "env"}:
            words = [w for w in words[1:] if not re.fullmatch(r"-?\d+[smh]?|-\w+", w)] if len(words) > 1 else words[:1]
        if words and words[0] not in PREFIX:
            first = words[0].strip("()").replace("\\", "/").rsplit("/", 1)[-1][:32] or "sh"
            sub = next((w for w in words[1:] if not w.startswith("-") and "/" not in w), "")
            return f"{first} {sub}" if first in MULTI and re.fullmatch(r"[a-z][a-z0-9-]{0,24}", sub) else first
    return "sh"


def user_turn(r, text):
    """agentplay's rule: not meta, not a compact summary, not harness text."""
    t = (text or "").lstrip()
    return bool(t) and not (r.get("isMeta") or r.get("isCompactSummary") or t.startswith(("<", "[Request interrupted"))
                            or "[SYSTEM NOTIFICATION" in t)


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def _text(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if b.get("type") == "text")


def parse(path):
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [r for r in rows if not r.get("isSidechain")]
    cwd = next((r["cwd"] for r in rows if r.get("cwd")), "")
    times = [t for t in (_ts(r.get("timestamp")) for r in rows if r.get("type") in ("user", "assistant")) if t]
    usage, prompts, events, by_id, notes, last_text, signals = {}, [], [], {}, [], "", 0
    for r in rows:
        msg = r.get("message") or {}
        content = msg.get("content")
        if r.get("type") == "system":  # API errors and refusal fallbacks are errors too
            sub = str(r.get("subtype") or "")
            signals += sub == "model_refusal_fallback" or "error" in sub
        elif r.get("type") == "attachment":
            signals += (r.get("attachment") or {}).get("type") in ("hook_blocking_error", "hook_non_blocking_error")
        elif r.get("type") == "assistant":
            if r.get("isApiErrorMessage"):
                signals += 1
                continue
            if msg.get("id") and msg.get("usage"):
                usage[msg["id"]] = msg["usage"]  # one message is split over several lines: count it once
            for b in content if isinstance(content, list) else []:
                if b.get("type") == "text" and b.get("text", "").strip():
                    last_text = b["text"].strip()
                    notes.append((len(events), last_text))
                elif b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    ev = {"seq": len(events), "tool": b["name"], "kind": kind_of(b["name"], inp), "input": inp,
                          "err": None, "out": ""}
                    by_id[b.get("id")] = ev
                    events.append(ev)
        elif r.get("type") == "user":
            blocks = content if isinstance(content, list) else []
            if any(b.get("type") == "tool_result" for b in blocks):
                for b in blocks:
                    ev = by_id.get(b.get("tool_use_id"))
                    if b.get("type") == "tool_result" and ev:
                        ev["err"] = bool(b.get("is_error"))
                        ev["out"] = b["content"] if isinstance(b.get("content"), str) else _text(b.get("content"))
                        status = str((r.get("toolUseResult") or {}).get("status") or "") \
                            if isinstance(r.get("toolUseResult"), dict) else ""
                        ev["err"] |= ev["kind"] == "subagent" and status in ("failed", "error", "killed", "cancelled")
                continue
            text = content if isinstance(content, str) else " ".join(b["text"] for b in blocks if b.get("type") == "text")
            if text.lstrip().startswith("[Request interrupted") and not r.get("isMeta"):
                signals += 1
            elif user_turn(r, text):
                prompts.append(text.strip())
    for ev in events:  # a call that never got a result was killed or interrupted
        ev["err"] = True if ev["err"] is None else ev["err"]
    window = []
    for ev in events:  # a retry repeats a recent call (same tool, verb and target) that failed
        i = ev["input"]
        ev["key"] = (ev["tool"], verb(i.get("command", "")) if ev["tool"] == "Bash" else None,
                     i.get("file_path") or i.get("path") or i.get("notebook_path") or i.get("url"))
        prev = next((p for p in reversed(window) if p["key"] == ev["key"]), None)
        if prev and prev["err"]:
            ev["retryOf"] = prev["seq"]
        window = (window + [ev])[-RETRY_WINDOW:]
    tok = lambda u: sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                                 "cache_read_input_tokens", "output_tokens"))
    totals = {"calls": len(events), "errors": sum(e["err"] for e in events) + signals,
              "retries": sum("retryOf" in e for e in events), "requests": len(usage),
              "tokens": sum(map(tok, usage.values())), "outputTokens": sum(u.get("output_tokens") or 0 for u in usage.values()),
              "durationMs": int((max(times) - min(times)).total_seconds() * 1000) if times else 0,
              "userTurns": len(prompts)}
    return {"cwd": cwd, "start": min(times).strftime("%H:%M:%S") if times else "00:00:00", "prompts": prompts,
            "events": events, "notes": notes, "result": last_text, "totals": totals}


def _rel(p, cwd):
    return p[len(cwd):].lstrip("/\\") if cwd and p.startswith(cwd) else p


def step_line(ev, cwd):
    i, out = ev["input"], (ev["out"] or "").strip().splitlines()
    target = i.get("file_path") or i.get("path") or i.get("notebook_path") or ""
    what = {"read": f"Read `{_rel(target, cwd)}`", "edit": f"Edited `{_rel(target, cwd)}`",
            "search": f"Searched for `{i.get('pattern', '')}` in `{_rel(target, cwd) or '.'}`",
            "verify": f"Checked with `{i.get('command', '')}`", "run": f"Ran `{i.get('command', '')}`",
            "web": f"Fetched {i.get('url') or i.get('query', '')}", "subagent": f"Asked a subagent: {i.get('description', '')}"
            }.get(ev["kind"], f"Called {ev['tool']}")
    if "retryOf" in ev:
        what += " again"
    if ev["err"]:
        detail = next((x for pat in (r"error|not defined|exception|denied", r"fail") for x in out if re.search(pat, x, re.I)),
                      out[0] if out else "")
        return f"FAILED: {what}: {detail.strip()}."
    return f"{what}{(': ' + out[-1].strip()) if ev['kind'] in ('verify', 'run') and out else ''}."


# --- live-panel config from a session -------------------------------------------------

def _wrap(text, width, rows):
    lines = [""]
    for w in text.split():
        if len(lines[-1]) + len(w) + 1 > width:
            if len(lines) == rows:
                lines[-1] = lines[-1][: width - 3] + "..."
                break
            lines.append("")
        lines[-1] = (lines[-1] + " " + w).strip()
    return lines


BOXES = [("files", ("read", "edit"), "gr"), ("search", ("search",), "cy"), ("tests", ("verify",), "ye"),
         ("shell", ("run",), "pu"), ("web", ("web",), "bl"), ("other", ("subagent", "mcp", "plan", "tool"), "dim")]


def panel_config(s, period=1.5):
    t, ev, W = s["totals"], s["events"], 1200
    kinds = {k: sum(e["kind"] == k for e in ev) for k in {e["kind"] for e in ev}}
    boxes = [b for b in BOXES if any(kinds.get(k) for k in b[1])]
    prompt = s["prompts"][0] if s["prompts"] else "(no prompt)"
    color = {k: c for _, ks, c in BOXES for k in ks}
    values = [{"t": e["kind"], "m": step_line(e, s["cwd"]).replace("`", "")[:58], "g": "fail" if e["err"] else "ok",
               "who": e["kind"], "c": color[e["kind"]]} for e in ev] or [{"t": "idle", "m": "no tool calls", "g": "ok"}]
    cfg = {"meta": {"title": "session replay", "lang": "en"},
           "canvas": {"width": W, "height": 1000, "duration": period * len(values)},
           "theme": {"preset": "terminal-dark"},
           "clock": {"start": s["start"], "rate": t["durationMs"] / 1000 / (period * len(values)) or 1},
           "titlebar": {"text": "~/session-panel - " + (re.split(r"[/\\]", s["cwd"].rstrip("/\\"))[-1] or "session")},
           "credit": {"text": f"replayed from the session transcript: {t['calls']} calls, {t['errors']} failed, "
                              f"{t['retries']} retried, {t['requests']} model requests, {t['tokens']:,} tokens"},
           "machines": {"steps": {"type": "cycle", "period": period, "values": values, "log": {"who": "agent", "c": "gr", "once": True}}},
           "elements": [
               {"type": "text", "x": 0, "w": W, "y": 80, "align": "center",
                "runs": [{"t": "AGENT SESSION REPLAY", "c": "fg", "b": 1}]},
               {"type": "box", "x": 40, "y": 120, "w": 520, "h": 160, "color": "cy", "pad": [12, 14], "align": "left",
                "lines": [{"t": "you", "c": "cy", "b": 1}] + [{"t": x, "c": "fg"} for x in _wrap(prompt, 38, 3)]},
               {"type": "box", "x": 640, "y": 120, "w": 520, "h": 160, "color": "gr", "pad": [12, 14], "align": "left",
                "lines": [{"t": "agent", "c": "gr", "b": 1}, {"runs": ["now: ", {"v": "steps", "c": "ye"}]},
                          {"t": f"{t['requests']} requests, {t['outputTokens']:,} output tokens", "c": "dim"},
                          {"t": f"{t['durationMs'] / 1000:.0f} s wall clock", "c": "dim"}]},
               {"type": "flow", "path": [[560, 200], [640, 200]], "period": 1.2, "offsets": [0, 0.5], "color": "cy"}]}
    bw = (W - 80 - 20 * (len(boxes) - 1)) // max(len(boxes), 1)
    for i, (name, ks, c) in enumerate(boxes):
        x = 40 + i * (bw + 20)
        n = sum(kinds.get(k, 0) for k in ks)
        bad = sum(e["err"] for e in ev if e["kind"] in ks)
        detail = (f"{kinds.get('read', 0)} read, {kinds.get('edit', 0)} edited" if name == "files"
                  else f"{n} call{'s' * (n != 1)}" + (f", {bad} failed" if bad else ""))
        cfg["elements"] += [
            {"type": "box", "x": x, "y": 380, "w": bw, "h": 110, "color": c, "pad": [12, 14], "align": "left",
             "lines": [{"t": name, "c": c, "b": 1}, {"t": detail, "c": "fail" if bad else "fg"}]},
            {"type": "flow", "path": [[900, 280], [900, 330], [x + bw // 2, 330], [x + bw // 2, 380]],
             "period": 1.0 + 0.25 * i, "offsets": [0.2 * i], "color": c}]
    cfg["elements"].append({"type": "log", "x": 40, "y": 540, "w": W - 80, "rows": 10, "title": "steps",
                            "cols": [{"key": "time", "x": 0}, {"key": "who", "x": 130}, {"key": "m", "x": 260},
                                     {"key": "g", "x": 1000}]})
    return cfg


# --- compact live-panel renderer: text, box, line, flow, log; counter + lane machines ----

THEME = {"bg": "#14171c", "bar": "#262a32", "line": "#4a5367", "dim": "#6f788c", "fg": "#e4e8f0", "wh": "#ffffff",
         "cy": "#86e1e6", "bl": "#8fb4f6", "gr": "#7fdba8", "pu": "#b9a2f2", "ye": "#e3c07a", "ok": "#7fdba8",
         "warn": "#e3c07a", "bad": "#f08080", "fail": "#f08080"}  # live-panel terminal-dark colours

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>live-panel</title><style>
body{margin:0;background:var(--bg);overflow:hidden}#st{position:relative;transform-origin:0 0;font:21px/30px "JetBrains Mono",Consolas,monospace;color:var(--fg)}
#st div{position:absolute;white-space:pre;overflow:hidden}.bx{border:2px solid}.tb{background:var(--bar);font-size:14px;text-align:center}
.tb i{position:absolute;width:14px;height:14px;border-radius:7px}svg{position:absolute;left:0;top:0}
</style></head><body><div id="st"></div><!--LIVE_CONFIG--><script>
var C=JSON.parse(document.getElementById('live-config').textContent),TH=__THEME__,cv=C.canvas||{},P={'4:5':[1200,1500],'3:4':[1080,1440],'1:1':[1080,1080]}[cv.preset]||[1200,1500],
W=cv.width||P[0],H=cv.height||P[1],D=cv.duration||30,PRE=cv.preroll||0,ST=document.getElementById('st'),M=C.machines||{},U=[],FL=[],LOG=null,NS='http://www.w3.org/2000/svg';
for(var k in TH)document.body.style.setProperty('--'+k,TH[k]);ST.style.width=W+'px';ST.style.height=H+'px';document.title=(C.meta||{}).title||'live-panel';
function col(c){return TH[c]||c||TH.fg}
function el(x,y,w,h,cls){var d=document.createElement('div');d.style.cssText='left:'+x+'px;top:'+y+'px;'+(w?'width:'+w+'px;':'')+(h?'height:'+h+'px;':'');if(cls)d.className=cls;ST.appendChild(d);return d}
function lane(m,t){var u=((t-(m.phase||0))%m.period+m.period)%m.period,b=u>=m.off&&u<m.off+m.run,n=Math.floor((t-(m.phase||0)-m.off-m.run)/m.period);
 return b?['|/-\\\\'[Math.floor(t*8)%4]+' '+m.busy,'ye']:['\\u2713 '+m.done[((n%m.done.length)+m.done.length)%m.done.length],'gr']}
function cyc(m,t){var n=m.values.length,j=Math.floor((t-(m.t0||0))/m.period),v=m.values[((j%n)+n)%n];return typeof v=='object'?v:{t:v}}
function val(id,t){var f=id.split('.'),m=M[f[0]];if(!m)return['{'+id+'}'];if(m.type=='cycle'){var v=cyc(m,t);return[String(f[1]?v[f[1]]:v.t!=null?v.t:(v.m||''))]}if(m.type=='counter'){var v=Math.floor(m.start+m.rate*t);return[(m.prefix||'')+(m.format=='comma'?v.toLocaleString('en-US'):v)+(m.suffix||'')]}
 if(m.type=='lane')return lane(m,t);return['{'+id+'}']}
function runs(d,rs){(Array.isArray(rs)?rs:[rs]).forEach(function(r){var s=document.createElement('span');if(typeof r=='string')r={t:r};if(r.b)s.style.fontWeight=700;
 if(r.v)U.push(function(t){var o=val(r.v,t);s.textContent=o[0];s.style.color=col(r.c||o[1])});else{s.textContent=r.t;s.style.color=col(r.c)}d.appendChild(s)})}
var B={text:function(e){var d=el(e.x||0,e.y-15,e.w,30);d.style.textAlign=e.align||'left';if(e.size)d.style.fontSize=e.size+'px';runs(d,e.runs||{t:e.t||e.text,c:e.c,b:e.b})},
 box:function(e){var d=el(e.x,e.y,e.w,e.h,'bx'),p=e.pad||[12,14];d.style.textAlign=e.align||'center';d.style.borderColor=col(e.color);d.style.boxSizing='border-box';d.style.padding=p[0]+'px '+(p[2]||p[1])+'px 0 '+p[1]+'px';
  if(e.fill)d.style.background=col(e.fill);(e.lines||[]).forEach(function(l){var r=document.createElement('div');r.style.cssText='position:static';
  runs(r,typeof l=='string'?l:(l.runs||{t:l.t,c:l.c,b:l.b}));d.appendChild(r)})},
 line:function(e){var x=Math.min(e.from[0],e.to[0]),y=Math.min(e.from[1],e.to[1]);el(x-1,y-1,Math.abs(e.to[0]-e.from[0])+2,Math.abs(e.to[1]-e.from[1])+2).style.background=col(e.color||'line')},
 flow:function(e){var s=document.createElementNS(NS,'svg'),pl=document.createElementNS(NS,'polyline');s.setAttribute('width',W);s.setAttribute('height',H);
  pl.setAttribute('points',e.path.join(' '));pl.setAttribute('fill','none');pl.setAttribute('stroke',TH.line);pl.setAttribute('stroke-width',2);s.appendChild(pl);
  var segs=[],L=0;for(var i=1;i<e.path.length;i++){var a=e.path[i-1],b=e.path[i],l=Math.hypot(b[0]-a[0],b[1]-a[1]);segs.push([a,b,l]);L+=l}
  var dots=e.offsets.map(function(){var c=document.createElementNS(NS,'circle');c.setAttribute('r',6);c.setAttribute('fill',col(e.color));s.appendChild(c);return c});
  ST.appendChild(s);FL.push(e);U.push(function(t){dots.forEach(function(c,j){var f=(((t+PRE)/e.period+e.offsets[j])%1)*L;
   for(var i=0;i<segs.length;i++){if(f<=segs[i][2]||i==segs.length-1){var q=Math.min(f/segs[i][2],1),a=segs[i][0],b=segs[i][1];c.setAttribute('cx',a[0]+(b[0]-a[0])*q);c.setAttribute('cy',a[1]+(b[1]-a[1])*q);break}f-=segs[i][2]}})})},
 log:function(e){var d=el(e.x,e.y,e.w,(e.rows+1)*30+20,'bx');d.style.borderColor=TH.line;var h=document.createElement('div');h.style.cssText='position:static;color:'+TH.dim;h.textContent=' '+(e.title||'log');d.appendChild(h);
  var rows=[];for(var i=0;i<e.rows;i++){var r=el(0,30+i*30,e.w,30);d.appendChild(r);rows.push(r)}LOG={e:e,rows:rows}}};
function events(t){var ev=[];for(var id in M){var m=M[id];if(m.type=='cycle'&&m.log){for(var j=Math.max(m.log.once?0:-1e9,Math.floor((t-(m.t0||0))/m.period)-20);(m.t0||0)+j*m.period<=t;j++){var v=cyc(m,(m.t0||0)+j*m.period+1e-6);
 ev.push([(m.t0||0)+j*m.period,v.who||m.log.who,v.m!=null?v.m:(v.t||''),v.g||'',v.c||m.log.c])}continue}if(m.type!='lane'||!m.log)continue;var t0=(m.phase||0)+m.off+m.run;
 for(var k=-20;t0+k*m.period<=t;k++){var g=m.log.msgs[((k%m.log.msgs.length)+m.log.msgs.length)%m.log.msgs.length];ev.push([t0+k*m.period,m.log.who,g[0],g[1],m.log.c])}}
 return ev.sort(function(a,b){return a[0]-b[0]})}
var ck=(C.clock||{}),c0=(ck.start||'00:00:00').split(':').map(Number);
function clock(s){var x=Math.floor(c0[0]*3600+c0[1]*60+c0[2]+s*(ck.rate||1));x=(x%86400+86400)%86400;return[x/3600|0,(x/60|0)%60,x%60].map(function(n){return('0'+n).slice(-2)}).join(':')}
if(C.titlebar){var tb=el(0,0,W,38,'tb');tb.style.lineHeight='38px';[['#f2645a',13],['#f1bd3c',35],['#55c552',57]].forEach(function(o){var i=document.createElement('i');i.style.cssText='left:'+o[1]+'px;top:12px;background:'+o[0];tb.appendChild(i)});tb.appendChild(document.createTextNode(C.titlebar.text||''))}
(C.elements||[]).forEach(function(e){if(B[e.type])B[e.type](e);else console.warn('live-panel subset: skipping '+e.type)});
if(C.credit)B.text(Object.assign({x:0,w:W,align:'center',y:H-14,size:12,c:'dim'},C.credit));
function fit(){var s=Math.min(innerWidth/W,innerHeight/H);ST.style.transform='scale('+s+')'}fit();addEventListener('resize',fit);
window.seek=function(t){U.forEach(function(f){f(t)});if(LOG){var ev=events(t).slice(-LOG.e.rows),cs=LOG.e.cols;LOG.rows.forEach(function(r,i){var v=ev[i];r.innerHTML='';if(!v)return;
 var cell={time:clock(v[0]),who:v[1],m:v[2],g:v[3]};cs.forEach(function(c){var s=el(c.x+20,0);s.style.position='absolute';s.textContent=cell[c.key];s.style.color=col(c.key=='who'?v[4]:c.key=='g'?v[3]:c.key=='time'?'dim':'fg');r.appendChild(s)})})}};
window.__ready=true;var T0=performance.now()-1000*(+(/[?&]t=([0-9.]+)/.exec(location.search)||[0,0])[1]);(function loop(n){window.seek(((n-T0)/1000)%D);if(!/[?&]manual/.test(location.search))requestAnimationFrame(loop)})(performance.now());
</script></body></html>"""


def render(cfg) -> str:
    blob = json.dumps(cfg, ensure_ascii=False).replace("</", "<\\/")
    page = PAGE.replace("__THEME__", json.dumps({**THEME, **(cfg.get("theme") or {}).get("colors", {})}))
    return page.replace("<!--LIVE_CONFIG-->", f'<script id="live-config" type="application/json">{blob}</script>')


def latest(home=Path.home() / ".claude" / "projects"):
    files = list(home.glob("*/*.jsonl"))
    if not files:
        sys.exit(f"no sessions under {home}")
    return max(files, key=lambda p: p.stat().st_mtime)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Replay a Claude Code session as an animated live-panel page.")
    ap.add_argument("session", nargs="?", help="Claude Code session .jsonl")
    ap.add_argument("--latest", action="store_true", help="newest session under ~/.claude/projects")
    ap.add_argument("--config", help="render this live-panel config instead")
    ap.add_argument("--period", type=float, default=1.5, help="seconds per step")
    ap.add_argument("--out", default="session-panel.html")
    a = ap.parse_args(argv)
    if a.config:
        cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    else:
        path = latest() if a.latest else Path(a.session) if a.session else ap.error("give a session file, --latest or --config")
        s = parse(path)
        cfg = panel_config(s, a.period)
        t = s["totals"]
        print(f"{t['calls']} calls, {t['errors']} failed, {t['retries']} retried, "
              f"{t['requests']} requests, {t['tokens']:,} tokens")
    Path(a.out).write_text(render(cfg), encoding="utf-8")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
