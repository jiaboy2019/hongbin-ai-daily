import base64, datetime as dt, hashlib, html, json, re, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'output'
OUT.mkdir(exist_ok=True)
TODAY = dt.datetime.now(ZoneInfo('Asia/Shanghai')).date()
HISTORY = ROOT / 'published_history.json'
state = json.loads(HISTORY.read_text('utf-8')) if HISTORY.exists() else {'last_issue': 2, 'items': []}
old = {x['key'] for x in state['items']}

def tidy(s):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', '', s or ''))).strip()

def norm(s):
    return hashlib.sha256(re.sub(r'[^\w\u4e00-\u9fff]', '', s.lower()).encode()).hexdigest()[:20]

def search(q):
    params = urllib.parse.urlencode({'q': q + ' when:1d', 'hl':'zh-CN','gl':'CN','ceid':'CN:zh-Hans'})
    req = urllib.request.Request('https://news.google.com/rss/search?' + params, headers={'User-Agent':'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=25) as f:
        root = ET.fromstring(f.read())
    rows = []
    for x in root.findall('.//item'):
        t = tidy(x.findtext('title', ''))
        title, sep, source = t.rpartition(' - ')
        if not sep:
            title, source = t, '新闻媒体'
        k = norm(title)
        if k not in old and len(title) >= 10:
            rows.append({'title': title, 'source': source[:16], 'key': k})
    return rows

def gather(queries):
    rows, used = [], set()
    for q in queries:
        try:
            for r in search(q):
                if r['key'] not in used:
                    rows.append(r); used.add(r['key'])
        except Exception as e:
            print('RSS query failed:', q, e)
    return rows

def clip(s, n):
    return s if len(s) <= n else s[:n-1] + '…'

people = [
    ('黄仁勋', '英伟达CEO'), ('奥尔特曼', 'OpenAI CEO'),
    ('苏姿丰', 'AMD CEO'), ('纳德拉', '微软CEO'),
    ('李飞飞', 'AI科学家'), ('马斯克', 'xAI创始人'),
    ('李彦宏', '百度创始人'), ('吴泳铭', '阿里巴巴CEO'),
]
leaders, selected = [], []
for name, role in people:
    found = [r for r in gather([name + ' 人工智能']) if name in r['title']]
    if found:
        r = found[0]
        leaders.append({'name': name + '｜' + role, 'title': clip(r['title'], 23), 'body': clip(r['title'], 45), 'source': r['source']})
        selected.append(r)
    if len(leaders) == 3:
        break

used = {r['key'] for r in selected}
news = [r for r in gather(['人工智能 最新 技术', 'AI 大模型 应用', 'AI 芯片 科技']) if r['key'] not in used]
if len(news) < 4 or len(leaders) < 3:
    raise RuntimeError('今日新闻不足以填满固定栏目，停止生成，避免发布占位内容')
news = news[:4]
issue = str(int(state.get('last_issue', 2)) + 1).zfill(3)
data = {
    'issue': issue, 'date': TODAY.isoformat(),
    'headline': {'title': clip(news[0]['title'], 25), 'body': clip(news[0]['title'], 55), 'source': news[0]['source']},
    'updates': [{'tag': tag, 'title': clip(r['title'], 21), 'body': clip(r['title'], 53), 'source': r['source']} for tag, r in zip(['AI动态', '产品应用', '前沿科技'], news[1:])],
    'leaders': leaders,
}
(OUT / 'daily.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), 'utf-8')
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1100, 'height': 1900})
    page.add_init_script('localStorage.setItem("hongbin_v12", ' + json.dumps(json.dumps(data, ensure_ascii=False)) + ')')
        import functools, http.server, threading
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler,
        directory=str(ROOT)
    )
    server = http.server.ThreadingHTTPServer(
        ('127.0.0.1', 0), handler
    )
    threading.Thread(
        target=server.serve_forever, daemon=True
    ).start()
    page.goto(
        f'http://127.0.0.1:{server.server_port}/index.html',
        wait_until='load'
    )

    page.evaluate('document.fonts.ready')
    page.wait_for_timeout(800)
    error = page.locator('#errors').inner_text()
    if error.strip():
        raise RuntimeError('模板文字溢出：' + error)
    png = page.locator('#poster').evaluate('(c) => c.toDataURL("image/png").split(",")[1]')
    (OUT / '洪滨每日AI早报.png').write_bytes(base64.b64decode(png))
    browser.close()
state['last_issue'] = int(issue)
state['items'] = (state['items'] + [{'key': r['key'], 'title': r['title'], 'date': TODAY.isoformat()} for r in news + selected])[-500:]
HISTORY.write_text(json.dumps(state, ensure_ascii=False, indent=2), 'utf-8')
print('已生成', OUT / '洪滨每日AI早报.png')
