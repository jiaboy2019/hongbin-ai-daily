import base64, datetime as dt, hashlib, html, json, re, urllib.parse, urllib.request, io
from email.utils import parsedate_to_datetime
from PIL import Image
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
# 同一天重跑时不自动增加期号，避免测试造成跳号。
last_dates = [x.get('date') for x in state['items']]
SAME_DAY = TODAY.isoformat() in last_dates[-7:]

# 仅调整新闻筛选；原网页模板、图标、画布与栏目布局均保持不变。
EXCLUDE = ('股票', '股价', '炒股', '基金', '投资建议', '科普周', '洽谈会', '峰会', '大会召开', '奖励大会', '书讯', '据称', '传闻', '网传', '出事了', '震惊', '重磅来袭', '十大热词', '培训', '培训机构', '横向测评', '辞职', '利润翻倍', '亏超', '新媒体创作', '课程体系', '技能培训班', '抵押的AI芯片')
SIGNALS = ('发布', '推出', '上线', '开源', '突破', '模型', '机器人', '芯片', '智能体', 'Agent', '研发', '科研', '应用', '功能', '升级', '世界模型', '编程', '自动化')
def score(row):
    title = row['title']
    if any(w in title for w in EXCLUDE): return -100
    matches = sum(3 for w in SIGNALS if w.lower() in title.lower())
    return matches + (1 if len(title) <= 45 else 0) if matches else -20

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

def search(q, leader=False):
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
            if not (0 <= age <= 18 * 3600): continue
        except (ValueError, TypeError, AttributeError):
            continue
        row = {'title': title, 'source': source[:16], 'key': k}
        if k not in old and len(title) >= 10 and (leader or score(row) > 0) and not any(similar(title, p['title']) for p in state['items'][-100:]):
            rows.append(row)
    return rows

def gather(queries, leader=False):
    rows, used = [], set()
    for q in queries:
        try:
            for r in search(q, leader=leader):
                if r['key'] not in used:
                    rows.append(r); used.add(r['key'])
        except Exception as e:
            print('RSS query failed:', q, e)
    return sorted(rows, key=score, reverse=True) if not leader else rows


# 使用百科条目中明确对应人物的肖像，不使用搜索结果中的随机人像。
PERSON_PAGES = {'黄仁勋':'Jensen_Huang','奥尔特曼':'Sam_Altman','苏姿丰':'Lisa_Su','纳德拉':'Satya_Nadella','李飞飞':'Fei-Fei_Li','马斯克':'Elon_Musk','李彦宏':'Robin_Li','吴泳铭':'Eddie_Wu_(businessman)'}

def fetch_portrait(name):
    title = PERSON_PAGES.get(name)
    if not title: return None
    url = 'https://en.wikipedia.org/api/rest_v1/page/summary/' + urllib.parse.quote(title)
    try:
        req = urllib.request.Request(url, headers={'User-Agent':'HongbinDaily/1.0 (contact: github.com/jiaboy2019/hongbin-ai-daily)'})
        with urllib.request.urlopen(req, timeout=12) as response:
            page = json.load(response)
        photo = (page.get('thumbnail') or {}).get('source')
        if not photo: return None
        req = urllib.request.Request(photo, headers={'User-Agent':'HongbinDaily/1.0'})
        with urllib.request.urlopen(req, timeout=12) as response:
            content = response.read(3_000_000)
        im = Image.open(io.BytesIO(content)).convert('RGB')
        im.thumbnail((600,600))
        out = io.BytesIO()
        im.save(out, format='JPEG', quality=82)
        return 'data:image/jpeg;base64,' + base64.b64encode(out.getvalue()).decode('ascii')
    except Exception as exc:
        print('人物照片下载失败:', name, exc)
        return None

def clip(s, n):
    return s if len(s) <= n else s[:n-1] + '…'

people = [
    ('黄仁勋', '英伟达CEO'), ('奥尔特曼', 'OpenAI CEO'),
    ('苏姿丰', 'AMD CEO'), ('纳德拉', '微软CEO'),
    ('李飞飞', 'AI科学家'), ('马斯克', 'xAI创始人'),
    ('李彦宏', '百度创始人'), ('吴泳铭', '阿里巴巴CEO'),
    ('扎克伯格', 'Meta CEO'), ('皮查伊', '谷歌CEO'),
    ('库克', '苹果CEO'), ('阿莫迪', 'Anthropic CEO'),
    ('杨立昆', 'AI科学家'), ('辛顿', 'AI科学家'),
    ('梁文锋', 'DeepSeek创始人'), ('周鸿祎', '360创始人'),
    ('雷军', '小米创始人'), ('王小川', '百川智能创始人'),
    ('李开复', 'AI企业家'), ('朱松纯', 'AI科学家'), ('姚期智', '计算机科学家'),
    ('周志华', '机器学习科学家'), ('唐杰', 'AI研究者'), ('何恺明', 'AI科学家'),
    ('王兴兴', '宇树科技创始人'), ('彭志辉', '智元机器人联合创始人'),
    ('王鹤', '机器人研究者'), ('杨植麟', '月之暗面创始人'),
    ('闫俊杰', 'MiniMax创始人'), ('刘知远', 'AI研究者'),
    ('李沐', 'AI研究者'), ('陈天奇', 'AI研究者'),
    ('Andrej Karpathy', 'AI研究者'), ('Demis Hassabis', 'Google DeepMind CEO'),
    ('Dario Amodei', 'Anthropic CEO'), ('Yann LeCun', 'AI科学家'),
    ('Geoffrey Hinton', 'AI科学家'), ('Jeff Dean', 'Google首席科学家'),
    ('Sundar Pichai', 'Alphabet CEO'), ('Mark Zuckerberg', 'Meta CEO'),
    ('Satya Nadella', '微软CEO'), ('Jensen Huang', '英伟达CEO'),
    ('Sam Altman', 'OpenAI CEO'), ('Fei-Fei Li', 'AI科学家'),
    ('Andrew Ng', 'AI教育与研究者'), ('François Chollet', 'AI研究者'),
]
leaders, selected, leader_names = [], [], []
for name, role in people:
    if len(leaders) == 3: break
    found = [r for r in gather(['"' + name + '" (AI OR 人工智能 OR 机器人 OR 科研)', '"' + name + '" (演讲 OR 发表 OR 发布 OR 研究)'], leader=True) if name.lower() in r['title'].lower() and r['source'].lower() not in ('x', 'twitter', '搜狐', 'sohu') and not any(w in r['title'] for w in ('离职传闻','被捕','据称','传闻','网传','最新消息：','政治通牒','被搞了','说白了','震惊','爆料'))]
    if found and not any(name in n or n in name for n in leader_names):
        r = sorted(found, key=lambda item: (score(item), len(item['title']) <= 65), reverse=True)[0]
        leaders.append({'name': name + '｜' + role, 'title': clip(r['title'], 23), 'body': clip(r['title'], 45), 'source': r['source']})
        selected.append(r)
        leader_names.append(name)
    if len(leaders) == 3:
        break

used = {r['key'] for r in selected}
news = [r for r in gather(['人工智能', 'AI 模型', 'AI 新产品', '机器人', 'AI 芯片', '智能体', 'AI 科研突破', '大模型 发布', 'AI 应用 上线']) if r['key'] not in used]
print('候选统计：普通新闻', len(news), '人物', len(leaders), '人物名单', leader_names)
if len(news) < 4 or len(leaders) < 3:
    raise RuntimeError(f'18小时窗口候选不足：新闻{len(news)}/4，人物{len(leaders)}/3。保持原模板和新闻标准，停止出图。')
news = sorted(news, key=score, reverse=True)[:4]
issue = str(int(state.get('last_issue', 2)) + (0 if SAME_DAY else 1)).zfill(3)
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
    # 只向原模板的既有图片槽位注入真实人物照片；不更改布局和图标。
    portrait_count = 0
    for i, person in enumerate(leader_names):
        uri = fetch_portrait(person)
        if uri:
            page.evaluate('''async ([key, src]) => { const im = new Image(); im.src = src; await im.decode(); images[key] = im; render(); }''', ['l' + str(i), uri])
            portrait_count += 1
    print('成功加载人物照片:', portrait_count, '/', len(leader_names))
    page.wait_for_timeout(800)
    error = page.locator('#errors').inner_text()
    if error.strip():
        raise RuntimeError('模板文字溢出：' + error)
    png = page.locator('#poster').evaluate('(c) => c.toDataURL("image/png").split(",")[1]')
    (OUT / '洪滨每日AI早报.png').write_bytes(base64.b64decode(png))
    browser.close()
    server.shutdown()
state['last_issue'] = int(issue)
state['items'] = (state['items'] + [{'key': r['key'], 'title': r['title'], 'date': TODAY.isoformat()} for r in news + selected])[-500:]
HISTORY.write_text(json.dumps(state, ensure_ascii=False, indent=2), 'utf-8')
print('已生成', OUT / '洪滨每日AI早报.png')
