"""Audit public RSS/Atom or page metadata without touching poster output."""
import concurrent.futures, datetime, json, re, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT=Path(__file__).resolve().parent
sources=json.loads((ROOT/'news_sources.json').read_text(encoding='utf-8'))['sources']
def probe(s):
    req=urllib.request.Request(s['url'],headers={'User-Agent':'Mozilla/5.0 (compatible; HongbinDailySourceAudit/1.0)','Accept':'application/rss+xml,application/atom+xml,application/xml,text/xml,text/html;q=0.8'})
    out={k:s[k] for k in ('name','region','type','url')}
    try:
        with urllib.request.urlopen(req,timeout=12) as r:
            data=r.read(500000)
            out['http_status']=r.status
            out['content_type']=r.headers.get('Content-Type','')
        if s['type']=='web':
            page=data.decode('utf-8','ignore')
            out['status']='public_page' if re.search(r'<title\\b|property=["\\\']og:title',page,re.I) else 'unrecognized_page'
            out['items']=0
        else:
            root=ET.fromstring(data)
            items=root.findall('.//item') or root.findall('.//{http://www.w3.org/2005/Atom}entry')
            out['items']=len(items)
            out['status']='feed_ok' if items else 'feed_empty'
            if items:
                first=items[0]
                out['sample_title']=(first.findtext('title') or first.findtext('{http://www.w3.org/2005/Atom}title') or '')[:110]
    except Exception as e:
        out.update(status='failed',error=str(e)[:180],items=0)
    return out
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    results=list(pool.map(probe,sources))
out={'tested_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'results':results}
dest=ROOT/'output'/'source_audit.json';dest.parent.mkdir(exist_ok=True)
dest.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
for r in results:
    print(f"{r['status']:15} {r['items']:3} {r['name']}: {r.get('error',r.get('sample_title',''))[:75]}")
print('SUMMARY',json.dumps({k:sum(x['status']==k for x in results) for k in ('feed_ok','feed_empty','public_page','unrecognized_page','failed')}))
