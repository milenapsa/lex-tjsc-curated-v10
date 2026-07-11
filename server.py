
from __future__ import annotations
import json, os, re, time, urllib.request, urllib.parse, uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import defaultdict, deque

PORT=int(os.getenv("PORT","8080"))
UPSTREAM=os.getenv("LEX_UPSTREAM","http://homosapiens-lex-search-aggregator-v09:8080")
VERSION="0.10.0-tjsc-curated"
TIMEOUT=15
TTL=1800
UA="Lex-HomoSapiens/0.10"
PAGES=[
 {"id":"tjsc_sumulas","name":"TJSC — Súmulas","url":"https://www.tjsc.jus.br/web/jurisprudencia/sumulas-do-tjsc","type":"sumula_tjsc"},
 {"id":"tjsc_enunciados","name":"TJSC — Enunciados","url":"https://www.tjsc.jus.br/web/jurisprudencia/enunciados-do-tjsc","type":"enunciado_tjsc"},
]
PORTAL="https://www.tjsc.jus.br/web/jurisprudencia"
_cache={}

def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

class TextBlocks(HTMLParser):
    tags={"p","li","h1","h2","h3","h4","td"}
    def __init__(self):
        super().__init__(); self.active=0; self.buf=[]; self.blocks=[]
    def handle_starttag(self, tag, attrs):
        if tag in self.tags: self.active += 1
    def handle_endtag(self, tag):
        if tag in self.tags and self.active:
            self.active -= 1
            if self.active==0:
                s=re.sub(r"\s+"," ","".join(self.buf)).strip()
                self.buf=[]
                if 30 <= len(s) <= 1800 and s not in self.blocks: self.blocks.append(s)
    def handle_data(self,data):
        if self.active: self.buf.append(data)

def fetch_text(page):
    hit=_cache.get(page["id"])
    if hit and time.time()-hit[0] < TTL: return hit[1]
    req=urllib.request.Request(page["url"],headers={"User-Agent":UA,"Accept":"text/html"})
    with urllib.request.urlopen(req,timeout=TIMEOUT) as r:
        raw=r.read(2_000_000).decode("utf-8","replace")
    p=TextBlocks(); p.feed(raw)
    _cache[page["id"]]=(time.time(),p.blocks)
    return p.blocks

STOP={"de","da","do","das","dos","e","a","o","em","para","por","com","um","uma","no","na","nos","nas","lei","art"}
def tokens(q):
    return [x for x in re.findall(r"[a-z0-9áéíóúâêôãõç]+",q.lower()) if len(x)>2 and x not in STOP]

def tjsc_search(query,limit):
    toks=tokens(query)
    results=[]; evidence=[]
    for page in PAGES:
        try:
            blocks=fetch_text(page)
            scored=[]
            for text in blocks:
                low=text.lower()
                score=sum(1 for t in toks if t in low)
                if score and (len(toks)<=1 or score>=min(2,len(toks))):
                    scored.append((score,text))
            scored.sort(key=lambda x:(-x[0],len(x[1])))
            count=0
            for score,text in scored[:limit]:
                title=text[:140] + ("…" if len(text)>140 else "")
                results.append({
                    "id":f'{page["id"]}:{abs(hash(text))}',
                    "title":title,
                    "summary":text,
                    "type":page["type"],
                    "date":"",
                    "organization":"Tribunal de Justiça de Santa Catarina",
                    "source":page["id"],
                    "source_label":page["name"],
                    "source_url":page["url"],
                    "official_url":page["url"],
                    "portal_url":PORTAL,
                    "is_official":True,
                    "is_synthetic":False,
                    "retrieved_at":now(),
                    "match_score":score
                }); count+=1
            evidence.append({"source":page["id"],"status":"ok","count":count,"request_url":page["url"],"cache_ttl_seconds":TTL})
        except Exception as exc:
            evidence.append({"source":page["id"],"status":"error","error_type":exc.__class__.__name__,"message":str(exc)[:200]})
    return results,evidence

def fetch_json(url,method="GET",payload=None):
    body=None if payload is None else json.dumps(payload,ensure_ascii=False).encode()
    headers={"User-Agent":UA,"Accept":"application/json"}
    if body is not None: headers["Content-Type"]="application/json"
    req=urllib.request.Request(url,data=body,headers=headers,method=method)
    with urllib.request.urlopen(req,timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())

def interleave(items,limit):
    groups=defaultdict(deque); order=[]
    for x in items:
        s=x.get("source","unknown")
        if s not in groups: order.append(s)
        groups[s].append(x)
    out=[]
    while len(out)<limit and any(groups[s] for s in order):
        for s in order:
            if groups[s] and len(out)<limit: out.append(groups[s].popleft())
    return out

SOURCES=[
 {"id":"tjsc_sumulas","name":"TJSC — Súmulas","status":"online","coverage":["sumulas","entendimentos_consolidados"],"official":True,"requires_secret":False},
 {"id":"tjsc_enunciados","name":"TJSC — Enunciados","status":"online","coverage":["enunciados","orientacoes_jurisprudenciais"],"official":True,"requires_secret":False},
 {"id":"tjsc_portal_jurisprudencia","name":"TJSC — Portal da Jurisprudência eproc","status":"manual_official_portal","coverage":["acordaos","decisoes_monocraticas","inteiro_teor"],"official":True,"requires_secret":False,"url":PORTAL,
  "automation_note":"Portal público oficial protegido contra automação; consulta manual preservada."}
]

def run_search(path,payload):
    started=time.monotonic()
    q=str(payload.get("query") or payload.get("q") or "").strip()
    limit=max(1,min(int(payload.get("limit",10)),20))
    base=fetch_json(UPSTREAM+("/v1/search" if path=="/v1/search" else path),"POST",payload)
    results=list(base.get("results") or []); evidence=list(base.get("evidence") or [])
    found,proof=tjsc_search(q,limit)
    results.extend(found); evidence.extend(proof)
    seen=set(); dedup=[]
    for x in results:
        k=(x.get("source"),x.get("id"),x.get("title"))
        if k in seen: continue
        seen.add(k); dedup.append(x)
    final=interleave(dedup,limit)
    return {
      "status":"ok","service":"lex-search-aggregator","version":VERSION,"generated_at":now(),
      "trace_id":str(uuid.uuid4()),"query":q,"scope":base.get("scope","all"),
      "result_count":len(final),"results":final,"evidence":evidence,
      "sources_used":sorted({x.get("source") for x in final if x.get("source")}),
      "integrity":{"official":sum(1 for x in final if x.get("is_official")),
                   "synthetic":sum(1 for x in final if x.get("is_synthetic")),
                   "source_urls_present":sum(1 for x in final if x.get("source_url"))},
      "warnings":list(base.get("warnings") or []),
      "human_review_required":True,"no_invention_policy":True,
      "duration_ms":int((time.monotonic()-started)*1000)
    }

class H(BaseHTTPRequestHandler):
    def sendj(self,status,obj):
        data=json.dumps(obj,ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(data))); self.send_header("Cache-Control","no-store")
        self.end_headers(); self.wfile.write(data)
    def body(self):
        n=int(self.headers.get("Content-Length","0") or 0)
        if n>64000: raise ValueError("payload_too_large")
        return json.loads((self.rfile.read(n) if n else b"{}").decode())
    def do_GET(self):
        p=urllib.parse.urlparse(self.path).path
        online=["camara_proposicoes","senado_processos","senado_legislacao","tse_ckan","tjsc_sumulas","tjsc_enunciados"]
        if p in {"/health","/v1/health"}:
            return self.sendj(200,{"status":"ok","service":"lex-search-aggregator","version":VERSION,"generated_at":now(),"real_sources_online":online,"human_review_required":True,"no_invention_policy":True})
        if p in {"/ready","/v1/readiness"}:
            return self.sendj(200,{"status":"ready","version":VERSION,"online_sources":online,"generated_at":now()})
        if p in {"/v1/sources","/v1/sources/registry"}:
            base=fetch_json(UPSTREAM+"/v1/sources")
            return self.sendj(200,{"status":"ok","service":"lex-search-aggregator","version":VERSION,"generated_at":now(),"sources":list(base.get("sources") or [])+SOURCES,"human_review_required":True,"no_invention_policy":True})
        self.sendj(404,{"error":"not_found"})
    def do_POST(self):
        p=urllib.parse.urlparse(self.path).path
        if p not in {"/v1/search","/v1/search/global","/v1/search/legislacao","/v1/search/datasets"}:
            return self.sendj(404,{"error":"not_found"})
        try:
            payload=self.body()
            if not str(payload.get("query") or payload.get("q") or "").strip():
                return self.sendj(422,{"error":"query_required"})
            self.sendj(200,run_search(p,payload))
        except Exception as exc:
            self.sendj(500,{"error":"tjsc_curated_connector_error","detail":exc.__class__.__name__})
    def log_message(self,*args): pass

ThreadingHTTPServer(("0.0.0.0",PORT),H).serve_forever()
