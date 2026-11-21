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

def fetch_wiki_thumbnail(title):
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
        print('百科配图下载失败:', title, exc)
        return None

def fetch_portrait(name):
    return fetch_wiki_thumbnail(PERSON_PAGES.get(name))

# 只使用与新闻标题中明确出现的机构/产品对应的百科图片。
# 这些是相关主体的示意配图，并非新闻现场照片；找不到时不以随机图片代替。
NEWS_IMAGE_PAGES = [
    ('OpenAI', 'OpenAI'), ('ChatGPT', 'ChatGPT'),
    ('Gemini', 'Gemini_(chatbot)'), ('Google', 'Google'),
    ('Meta', 'Meta_Platforms'), ('微软', 'Microsoft'),
    ('Microsoft', 'Microsoft'), ('苹果', 'Apple_Inc.'),
    ('Apple', 'Apple_Inc.'), ('英伟达', 'Nvidia'),
    ('Nvidia', 'Nvidia'), ('AMD', 'Advanced_Micro_Devices'),
    ('华为', 'Huawei'), ('特斯拉', 'Tesla,_Inc.'),
    ('Tesla', 'Tesla,_Inc.'), ('百度', 'Baidu'),
    ('阿里', 'Alibaba_Group'), ('腾讯', 'Tencent'),
    ('小米', 'Xiaomi'), ('DeepSeek', 'DeepSeek'),
    ('宇树', 'Unitree_Robotics'), ('Anthropic', 'Anthropic'),
]
def fetch_news_image(title):
    for keyword, page in NEWS_IMAGE_PAGES:
        if keyword.lower() in title.lower():
            uri = fetch_wiki_thumbnail(page)
            if uri: return uri, keyword
    return None, None

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
# 人物动态必须有可归属的科技信息；媒体标题仅提到姓名不足以入选。
LEADER_BLOCK = ('离职', '离任', '辞任', '辞职', '去世', '逝世', '遇刺', '被捕', '间谍', '战争', '导弹', '政治通牒', '爆料', '首谈', '传闻', '谣言', '阴谋', '曝', '被曝', '震惊', '特朗普', '股价', '股市', '投资机会', '官司', '起诉', '拟募资', '再创业', '神话破裂', '行业又要变天', '或将', '据传', '网曝', '疑似')
LEADER_TECH = ('人工智能', 'AI', '模型', '机器人', '芯片', '科研', '研究', '论文', '技术', '智能体', '算法', '开源', '发布', '产品', '编程', '算力', '计算', '自动驾驶', '世界模型', '具身智能', 'agent')
LEADER_BAD_SOURCES = ('禁闻', '搜狐', 'sohu', '驱动之家', '东方财富', '股吧', '自媒体')
def leader_valid(row):
    title = row['title']; source = row['source'].lower()
    return (not any(w in title for w in LEADER_BLOCK)
            and any(w.lower() in title.lower() for w in LEADER_TECH)
            and source not in ('x', 'twitter', 'x.com', 'twitter.com')
            and not any(w.lower() in source for w in LEADER_BAD_SOURCES))

leaders, selected, leader_names = [], [], []
for name, role in people:
    if len(leaders) == 3: break
    found = [r for r in gather(['"' + name + '" (AI OR 人工智能 OR 机器人 OR 科研)', '"' + name + '" (演讲 OR 发表 OR 发布 OR 研究)'], leader=True) if name.lower() in r['title'].lower() and leader_valid(r)]
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
    # 仅填充原模板现有新闻图片槽位，不改变版式；每张图与标题中的实体相符。
    news_image_count = 0
    for key, row in zip(['headline', 'u0', 'u1', 'u2'], news):
        uri, matched = fetch_news_image(row['title'])
        if uri:
            page.evaluate('''async ([key, src]) => { const im = new Image(); im.src = src; await im.decode(); images[key] = im; render(); }''', [key, uri])
            print('新闻配图:', key, matched)
            news_image_count += 1
    print('成功加载新闻相关配图:', news_image_count, '/4')
    if news_image_count != 4:
        raise RuntimeError('新闻相关配图不足4张，停止导出，避免占位图或无关图片')
    # 只向原模板的既有图片槽位注入真实人物照片；不更改布局和图标。
    portrait_count = 0
    for i, person in enumerate(leader_names):
        uri = fetch_portrait(person)
        if uri:
            page.evaluate('''async ([key, src]) => { const im = new Image(); im.src = src; await im.decode(); images[key] = im; render(); }''', ['l' + str(i), uri])
            portrait_count += 1
    print('成功加载人物照片:', portrait_count, '/', len(leader_names))
    if portrait_count != 3:
        raise RuntimeError('人物照片未全部加载，停止导出，避免生成占位图')
    page.wait_for_timeout(800)
    error = page.locator('#errors').inner_text()
    if not page.evaluate('globe.complete && globe.naturalWidth > 0'):
        raise RuntimeError('原版地球图标加载失败，停止导出')
    if error.strip():
        raise RuntimeError('模板文字溢出：' + error)
    png = page.locator('#poster').evaluate('(c) => c.toDataURL("image/png").split(",")[1]')
    (OUT / 'daily.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), 'utf-8')
    (OUT / '洪滨每日AI早报.png').write_bytes(base64.b64decode(png))
    browser.close()
    server.shutdown()
state['last_issue'] = int(issue)
state['items'] = (state['items'] + [{'key': r['key'], 'title': r['title'], 'date': TODAY.isoformat()} for r in news + selected])[-500:]
HISTORY.write_text(json.dumps(state, ensure_ascii=False, indent=2), 'utf-8')
print('已生成', OUT / '洪滨每日AI早报.png')
