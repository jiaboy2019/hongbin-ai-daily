import base64, datetime as dt, hashlib, html, json, re, urllib.parse, urllib.request
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

# 自动生成程序：保持 index.html 与 globe.png 固定母版不变
ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'output'
OUT.mkdir(exist_ok=True)
TODAY = dt.datetime.now(ZoneInfo('Asia/Shanghai')).date()
HISTORY = ROOT / 'published_history.json'
state = json.loads(HISTORY.read_text('utf-8')) if HISTORY.exists() else {'last_issue': 2, 'items': []}
old = {x['key'] for x in state['items']}

# 仅调整新闻筛选；原网页模板、图标、画布与栏目布局均保持不变。
EXCLUDE = ('股票', '股价', '炒股', '基金', '投资建议', '科普周', '洽谈会', '峰会', '大会召开', '奖励大会', '书讯', '据称', '传闻', '网传', '出事了', '震惊', '重磅来袭', '十大热词')
SIGNALS = ('发布', '推出', '上线', '开源', '突破', '模型', '机器人', '芯片', '智能体', 'Agent', '研发', '科研', '应用', '功能', '升级', '世界模型', '编程', '自动化')
def score(row):
    title = row['title']
    if any(w in title for w in EXCLUDE): return -100
    return sum(3 for w in SIGNALS if w.lower() in title.lower()) + (1 if len(title) <= 45 else 0)

def similar(a, b):
    # 使用字符三元组发现换标题的近似重复事件；并非完整语义去重。
    def grams(t):
        t = re.sub(r'[^\w\u4e00-\u9fff]', '', t.lower())
        return {t[i:i+3] for i in range(max(0, len(t)-2))}
    x, y = grams(a), grams(b)
    return bool(x and y and len(x & y) / min(len(x), len(y)) >= 0.78)


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
        try:
            published = parsedate_to_datetime(x.findtext('pubDate', '')).astimezone(ZoneInfo('Asia/Shanghai'))
            age = (dt.datetime.now(ZoneInfo('Asia/Shanghai')) - published).total_seconds()
            if not (0 <= age <= 36 * 3600): continue
        except (ValueError, TypeError, AttributeError):
            continue
        row = {'title': title, 'source': source[:16], 'key': k}
        if k not in old and len(title) >= 10 and score(row) > 0 and not any(similar(title, p['title']) for p in state['items'][-100:]):
            rows.append(row)
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
    return sorted(rows, key=score, reverse=True)

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
        r = sorted(found, key=score, reverse=True)[0]
        leaders.append({'name': name + '｜' + role, 'title': clip(r['title'], 23), 'body': clip('据' + r['source'] + '报道：' + r['title'], 45), 'source': r['source']})
        selected.append(r)
    if len(leaders) == 3:
        break

used = {r['key'] for r in selected}
news = [r for r in gather(['人工智能 最新 技术', 'AI 大模型 应用', 'AI 芯片 科技']) if r['key'] not in used]
if len(news) < 4 or len(leaders) < 3:
    raise RuntimeError('今日新闻不足以填满固定栏目，停止生成，避免发布占位内容')
news = sorted(news, key=score, reverse=True)[:4]
issue = str(int(state.get('last_issue', 2)) + 1).zfill(3)
data = {
    'issue': issue, 'date': TODAY.isoformat(),
    'headline': {'title': clip(news[0]['title'], 25), 'body': clip('据' + news[0]['source'] + '报道：' + news[0]['title'], 55), 'source': news[0]['source']},
    'updates': [{'tag': tag, 'title': clip(r['title'], 21), 'body': clip('据' + r['source'] + '报道：' + r['title'], 53), 'source': r['source']} for tag, r in zip(['AI动态', '产品应用', '前沿科技'], news[1:])],
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
