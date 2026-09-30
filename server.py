import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote
import requests
from flask import Flask, jsonify, request

app=Flask(__name__)
SEARCH="https://archive.org/advancedsearch.php"
META="https://archive.org/metadata/{}"
S=requests.Session()
S.headers.update({"User-Agent":"Layz-BitChord-Addon/1.0"})

def num(v):
    if v in (None,""): return None
    m=re.search(r"\d+(?:\.\d+)?",str(v))
    if not m: return None
    n=float(m.group()); return int(n) if n.is_integer() else n

def pick(d,*keys):
    for k in keys:
        if d.get(k) not in (None,""): return d[k]
    return None

def file_url(i,n):
    return "https://archive.org/download/"+quote(i,safe="")+"/"+quote(n,safe="")

def streaminfo(u):
    try:
        r=S.get(u,headers={"Range":"bytes=0-63"},timeout=(5,12),stream=True)
        r.raise_for_status(); d=r.raw.read(64); r.close()
        if len(d)<42 or d[:4]!=b"fLaC" or (d[4]&127)!=0: return None,None
        p=int.from_bytes(d[18:28],"big")
        sr=p>>44; bd=((p>>36)&31)+1
        return (sr if 1000<=sr<=768000 else None),(bd if 4<=bd<=32 else None)
    except requests.RequestException: return None,None

def inspect(i,item):
    n=str(item.get("name","")); u=file_url(i,n)
    sr=num(pick(item,"sample_rate","samplerate","sampleRate"))
    bd=num(pick(item,"bit_depth","bitdepth","bitDepth"))
    if sr is None or bd is None:
        a,b=streaminfo(u); sr=sr if sr is not None else a; bd=bd if bd is not None else b
    return {"url":u,"filename":n,"sampleRate":sr,"bitDepth":bd,
            "length":num(item.get("length")),"size":num(item.get("size")),
            "bitrate":num(pick(item,"bitrate","bit_rate","bitRate"))}

def best_flac(i):
    try:
        r=S.get(META.format(quote(i,safe="")),timeout=(5,20)); r.raise_for_status(); meta=r.json()
    except (requests.RequestException,ValueError): return None
    c=[x for x in meta.get("files",[]) if str(x.get("name","")).lower().endswith(".flac")]
    if not c: return None
    c.sort(key=lambda x:num(x.get("size")) or 0,reverse=True)
    out=[]
    with ThreadPoolExecutor(max_workers=4) as pool:
        fs=[pool.submit(inspect,i,x) for x in c[:10]]
        for f in as_completed(fs):
            try: out.append(f.result())
            except Exception: pass
    if not out: return None
    out.sort(key=lambda x:(x.get("bitDepth") or 0,x.get("sampleRate") or 0,x.get("size") or 0),reverse=True)
    return out[0]

def creator(v):
    if isinstance(v,list): return str(v[0]) if v else "Internet Archive"
    return str(v) if v else "Internet Archive"

@app.get("/manifest.json")
def manifest():
    return jsonify({"id":"layzxz.bitchord-flac","name":"Layz Add On","version":"1.0.0","resources":["search","stream"],
      "settings":[{"key":"quality","type":"select","default":"lossless","options":[{"label":"Lossless","value":"lossless"},{"label":"High","value":"high"},{"label":"Low","value":"low"}]}]})

@app.get("/health")
def health(): return jsonify({"ok":True})

@app.get("/search")
def search():
    q=(request.args.get("q") or "").strip()
    if not q:
        return jsonify({"tracks":[]})
    try:
        r=S.get(
            SEARCH,
            params={
                "q":f"mediatype:audio AND ({q})",
                "fl[]":["identifier","title","creator","album","length"],
                "rows":20,
                "output":"json"
            },
            timeout=(5,10)
        )
        r.raise_for_status()
        docs=r.json().get("response",{}).get("docs",[])
    except (requests.RequestException,ValueError) as e:
        return jsonify({"tracks":[],"error":str(e)}),502

    tracks=[]
    for d in docs:
        if not d.get("identifier"):
            continue
        i=str(d["identifier"])
        tracks.append({
            "id":i,
            "title":str(d.get("title") or i),
            "artist":creator(d.get("creator")),
            "album":str(d.get("album") or ""),
            "duration":num(d.get("length")),
            "artworkURL":"https://archive.org/services/img/"+quote(i,safe=""),
            "format":"flac",
            "audioQuality":"LOSSLESS"
        })
    return jsonify({"tracks":tracks[:20]})

@app.get("/stream/<path:track_id>")
def stream(track_id):
    x=best_flac(track_id.strip())
    if not x: return jsonify({"error":"track not found"}),404
    sr=x.get("sampleRate"); bd=x.get("bitDepth")
    detail=[]
    if bd: detail.append(f"{bd}-bit")
    if sr: detail.append(f"{sr/1000:g} kHz")
    return jsonify({"url":x["url"],"format":"flac","quality":"Lossless"+((" · "+" / ".join(detail)) if detail else ""),
      "codec":"flac","container":"flac","manifest":"none","encrypted":False,
      "sampleRate":sr,"bitDepth":bd,"bitrate":x.get("bitrate")})

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","8080")))
