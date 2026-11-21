"""Probe AIHOT public API as an optional discovery source; never modify production poster."""
import json, urllib.request, urllib.error, datetime
from pathlib import Path
BASE="https://aihot.news"
PATHS=["/api/v1/items","/api/v1/hot-topics","/api/v1/dailies/latest"]
results=[]
for path in PATHS:
    url=BASE+path
    row={"url":url}
    try:
        req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 (compatible; HongbinDailyAudit/1.0)","Accept":"application/json"})
        with urllib.request.urlopen(req,timeout=15) as r:
            body=r.read(300000)
            row["http_status"]=r.status
            row["content_type"]=r.headers.get("Content-Type","")
        obj=json.loads(body)
        row["status"]="json_ok"
        row["root_type"]=type(obj).__name__
        if isinstance(obj,dict):
            row["keys"]=list(obj.keys())[:25]
            for k,v in obj.items():
                if isinstance(v,list):
                    row["list_field"]=k
                    row["count_in_sample"]=len(v)
                    if v and isinstance(v[0],dict):row["item_fields"]=list(v[0].keys())[:35]
                    if k=="items":
                        row["sample_shapes"]=[{"source_type":type(it.get("source")).__name__,"source_sample":str(it.get("source"))[:120],"links_type":type(it.get("links")).__name__,"links_sample":str(it.get("links"))[:250],"publishedAt":str(it.get("publishedAt"))[:45],"score":it.get("score")} for it in v[:4] if isinstance(it,dict)]
                    break
        elif isinstance(obj,list):
            row["count_in_sample"]=len(obj)
            if obj and isinstance(obj[0],dict):row["item_fields"]=list(obj[0].keys())[:35]
    except Exception as e:row["status"]="failed";row["error"]=str(e)[:220]
    results.append(row)
    print(json.dumps(row,ensure_ascii=False))
out=Path("output/aihot_probe.json");out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps({"tested_at_utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),"results":results},ensure_ascii=False,indent=2),encoding="utf-8")
