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
SIGNALS = ('发布', '推出', '上线', '开源', '突破', '模型', '机器人', '芯片', '智能体', 'Agent', '研发', '科研', '应用', '功能', '升级', '世界模型', '编程', '自动化', 'AI', 'artificial intelligence', 'robot', 'chip', 'research', 'launch', 'model', 'agent', 'breakthrough')
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

def search(q, leader=False, international=False):
    params = urllib.parse.urlencode({'q': q + ' when:1d', 'hl':'en-US' if international else 'zh-CN','gl':'US' if international else 'CN','ceid':'US:en' if international else 'CN:zh-Hans'})
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
        description = tidy(x.findtext('description', ''))
        # Google News 的描述常为标题/相关新闻列表；仅在有独立文本时使用。
        if description.startswith(title): description = description[len(title):].strip(' -—:：')
        if len(description) < 24 or description.lower().startswith('view full coverage'): description = ''
        row = {'title': title, 'source': source[:30], 'key': k, 'description': description}
        if k not in old and len(title) >= 10 and (leader or score(row) > 0) and not any(similar(title, p['title']) for p in state['items'][-100:]):
            rows.append(row)
    return rows

def gather(queries, leader=False, international=False):
    rows, used = [], set()
    for q in queries:
        try:
            for r in search(q, leader=leader, international=international):
                if r['key'] not in used:
                    rows.append(r); used.add(r['key'])
        except Exception as e:
            print('RSS query failed:', q, e)
    return sorted(rows, key=score, reverse=True) if not leader else rows


# 独立 RSS 来源：与 Google News 搜索互补。失败时跳过，不影响日报生成。
DIRECT_FEEDS = [
 ('OpenAI','https://openai.com/news/rss.xml'),
 ('Google DeepMind','https://deepmind.google/blog/rss.xml'),
 ('Google Research','https://research.google/blog/rss/'),
 ('Google AI','https://blog.google/innovation-and-ai/technology/ai/rss/'),
 ('Hugging Face','https://huggingface.co/blog/feed.xml'),
 ('NVIDIA','https://blogs.nvidia.com/feed/'),
 ('TechCrunch','https://techcrunch.com/category/artificial-intelligence/feed/'),
 ('The Verge','https://www.theverge.com/rss/ai-artificial-intelligence/index.xml'),
 ('Ars Technica','https://arstechnica.com/ai/feed/'),
 ('WIRED','https://www.wired.com/feed/tag/ai/latest/rss'),
 ('MIT Technology Review','https://www.technologyreview.com/topic/artificial-intelligence/feed'),
 ('IEEE Spectrum','https://spectrum.ieee.org/feeds/feed.rss'),
 ('MIT News','https://news.mit.edu/rss/topic/artificial-intelligence2'),
 ('The Decoder','https://the-decoder.com/feed/'),
 ('arXiv AI','https://rss.arxiv.org/rss/cs.AI'),
 ('量子位','https://www.qbitai.com/feed'),
 ('机器之心','https://www.jiqizhixin.com/rss'),
 ('36氪','https://36kr.com/feed'),
]
def direct_feed_rows():
    result = []
    ns = {'atom':'http://www.w3.org/2005/Atom'}
    for source, url in DIRECT_FEEDS:
        try:
            req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0 HongbinDaily/1.0'})
            with urllib.request.urlopen(req, timeout=8) as resp:
                root = ET.fromstring(resp.read(1_500_000))
            entries = root.findall('.//item') or root.findall('.//atom:entry', ns)
            count = 0
            for item in entries:
                title = tidy(item.findtext('title') or item.findtext('atom:title', default='', namespaces=ns))
                date_str = item.findtext('pubDate') or item.findtext('atom:published', default='', namespaces=ns) or item.findtext('atom:updated', default='', namespaces=ns)
                try:
                    when = parsedate_to_datetime(date_str) if not date_str.endswith('Z') else dt.datetime.fromisoformat(date_str.replace('Z','+00:00'))
                    if when.tzinfo is None: when = when.replace(tzinfo=dt.timezone.utc)
                    age = (dt.datetime.now(dt.timezone.utc)-when.astimezone(dt.timezone.utc)).total_seconds()
                    if not 0 <= age <= 18*3600: continue
                except Exception: continue
                description = tidy(item.findtext('description') or item.findtext('atom:summary', default='', namespaces=ns))
                if len(description) < 25 or title in description: description = ''
                row = {'title':title,'source':source,'key':norm(title),'description':description}
                if len(title)>=10 and row['key'] not in old and score(row)>0 and not any(similar(title,p['title']) for p in state['items'][-100:]):
                    result.append(row); count+=1
            print('独立源:',source,count)
        except Exception as e:
            print('独立源不可用，已跳过:',source,str(e)[:100])
    return result

# 使用百科条目中明确对应人物的肖像，不使用搜索结果中的随机人像。
PERSON_PAGES = {'黄仁勋':'Jensen_Huang','奥尔特曼':'Sam_Altman','苏姿丰':'Lisa_Su','纳德拉':'Satya_Nadella','李飞飞':'Fei-Fei_Li','马斯克':'Elon_Musk','李彦宏':'Robin_Li','吴泳铭':'Eddie_Wu_(businessman)', '库克':'Tim_Cook', '扎克伯格':'Mark_Zuckerberg', '皮查伊':'Sundar_Pichai', '阿莫迪':'Dario_Amodei', '杨立昆':'Yann_LeCun', '辛顿':'Geoffrey_Hinton', '雷军':'Lei_Jun', '李开复':'Kai-Fu_Lee', '吴恩达':'Andrew_Ng', '王兴兴':'Wang_Xingxing', 'Sam Altman':'Sam_Altman','Jensen Huang':'Jensen_Huang','Demis Hassabis':'Demis_Hassabis','Dario Amodei':'Dario_Amodei','Sundar Pichai':'Sundar_Pichai','Satya Nadella':'Satya_Nadella','Mark Zuckerberg':'Mark_Zuckerberg','Fei-Fei Li':'Fei-Fei_Li','Geoffrey Hinton':'Geoffrey_Hinton','Yann LeCun':'Yann_LeCun','Andrew Ng':'Andrew_Ng'}

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
    ('机器人', 'Robot'), ('人工智能', 'Artificial_intelligence'),
    ('大模型', 'Large_language_model'), ('智能体', 'Intelligent_agent'),
    ('芯片', 'Integrated_circuit'), ('AI', 'Artificial_intelligence'),
]
def fetch_news_image(title, used_pages=None):
    used_pages = used_pages if used_pages is not None else set()
    candidates = [(word, page) for word, page in NEWS_IMAGE_PAGES if word.lower() in title.lower()]
    # 先用明确品牌、产品或技术实体，避免泛用的“机器人”“AI”反复出现同一张图。
    candidates.sort(key=lambda pair: (pair[1] in used_pages, pair[0] in ('AI','机器人','人工智能')))
    for keyword, page in candidates:
        if page in used_pages: continue
        uri = fetch_wiki_thumbnail(page)
        if uri: used_pages.add(page); return uri, keyword
    return None, None

def to_chinese(text):
    if not text or len(re.findall(r'[A-Za-z]', text)) < max(12, len(text)//3): return text
    try:
        query = urllib.parse.urlencode({'client':'gtx','sl':'auto','tl':'zh-CN','dt':'t','q':text})
        req = urllib.request.Request('https://translate.googleapis.com/translate_a/single?'+query,headers={'User-Agent':'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            obj = json.load(resp)
        translated = ''.join(x[0] for x in obj[0] if x and x[0])
        return translated if translated else text
    except Exception as exc:
        print('翻译不可用，保留原文:',str(exc)[:80]); return text

def article_body(row, limit):
    title = to_chinese(row['title'].strip())
    desc = to_chinese(row.get('description', '').strip())
    if desc and desc.lower() not in title.lower() and title.lower() not in desc.lower():
        return clip(desc, limit)
    # RSS 未提供独立摘要时，不捏造新闻事实。以报道来源和原始标题补足信息。
    return clip('据'+row['source']+'报道：'+title+'。', limit)

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
# 开放式人物候选：从新闻报道本身识别人名；上面的名单仅作补充，不是准入限制。
leaders, selected, leader_names = [], [], []
leader_queries = ['AI 科技 人物 表示', '人工智能 科学家 表示', '科技公司 CEO AI', '机器人 创始人 表示', 'AI 研究者 发布', '科技人物 人工智能']
leader_rows = gather(leader_queries, leader=True)
# 补充人物定向搜索；人物名单只用于拓宽搜索，不是入选门槛。
leader_rows += gather(['黄仁勋 AI', '马斯克 xAI', '奥尔特曼 OpenAI', '苏姿丰 AI', '李飞飞 AI', '库克 Apple AI', 'Demis Hassabis AI', 'Sam Altman AI'], leader=True, international=True)
# 把已有名单中的名字作为识别提示，同时识别新闻标题中的中文姓名和英文姓名。
name_roles = dict(people)
def identify_person(title):
    for name, role in name_roles.items():
        if re.search(r'(?<![A-Za-z])'+re.escape(name)+r'(?![A-Za-z])',title,re.I): return name, role
    m = re.search(r'(?:科学家|创始人|CEO|首席执行官|研究员|教授|董事长|负责人|总裁)[：\\s]*([\\u4e00-\\u9fff]{2,4})(?:[：，称说表示认为谈]|\\s)', title)
    if m: return m.group(1), '科技人物'
    m = re.search(r'^([\\u4e00-\\u9fff]{2,4})[：称说表示认为谈]', title)
    if m: return m.group(1), '科技人物'
    return None, None
for r in sorted(leader_rows, key=score, reverse=True):
    name, role = identify_person(r['title'])
    if not name or name in leader_names: continue
    if any(w in r['title'].lower() for w in ('stock','stocks','shares','market selloff','股价','股票','道歉','apology','jeff bezos','didn’t','didn\u2019t')): continue
    if not any(w in r['title'].lower() for w in ('ai','artificial intelligence','model','robot','chip','agent','人工智能','模型','机器人','芯片','智能体','科研','发布','技术')): continue
    leaders.append({'name': name + '｜' + role, 'title': clip(to_chinese(r['title']), 23), 'body': article_body(r, 60), 'source': r['source']})
    selected.append(r); leader_names.append(name)
    if len(leaders) == 3: break
# 绝不把普通科技新闻冒充人物动态；人物不足时继续拓展搜索。
if len(leaders) < 3:
    extra = gather(['AI 创始人 表示', 'AI 科学家 发布', 'AI 公司 CEO 称', '人工智能 研究者 表示'], leader=True)
    for r in extra:
        name, role = identify_person(r['title'])
        if not name or name in leader_names: continue
        leaders.append({'name':name+'｜'+role,'title':clip(r['title'],23),'body':article_body(r,60),'source':r['source']})
        selected.append(r); leader_names.append(name)
        if len(leaders)==3: break

used = {r['key'] for r in selected}
news = [r for r in gather(['人工智能', 'AI 模型', 'AI 新产品', '机器人', 'AI 芯片', '智能体', 'AI 科研突破', '大模型 发布', 'AI 应用 上线']) if r['key'] not in used]
# 国际媒体独立采集，不能只使用中国区中文搜索。
international_news = gather(['artificial intelligence breakthrough', 'OpenAI Google DeepMind Anthropic AI', 'AI robotics chips research', 'AI agents product launch', 'AI science technology'], international=True)
news.extend(r for r in international_news if r['key'] not in {n['key'] for n in news} and r['key'] not in used)
direct_news = direct_feed_rows()
news.extend(r for r in direct_news if r['key'] not in {n['key'] for n in news} and r['key'] not in used)
print('候选统计：普通新闻', len(news), '人物', len(leaders), '人物名单', leader_names)
if len(news) < 4:
    raise RuntimeError(f'18小时窗口候选不足：新闻{len(news)}/4。保持原模板和新闻标准，停止出图。')
# 新闻不再受预设配图词表限制，按媒体报道候选选取。
news = sorted(news, key=score, reverse=True)
# 优先兼顾国际媒体和中文媒体，并避免四条都来自同一来源。
intl = [r for r in news if r in international_news or (r in direct_news and r['source'] not in ('量子位','机器之心','36氪'))]
domestic = [r for r in news if r not in international_news]
chosen = []
for pool in (intl, domestic, intl, domestic, news):
    for r in pool:
        if r not in chosen and not any(similar(r['title'], x['title']) for x in chosen) and (r['source'] not in {x['source'] for x in chosen} or len(chosen) >= 3):
            chosen.append(r); break
    if len(chosen) == 4: break
news = chosen if len(chosen) == 4 else news[:4]
# 翻译服务可能限流。优先选取已有中文标题的新闻，避免英文截断出现在朋友圈。
def is_chinese_ready(row):
    t = to_chinese(row['title'])
    return len(re.findall(r'[\\u4e00-\\u9fff]', t)) >= 6
if not all(is_chinese_ready(r) for r in news):
    ready = [r for r in news if is_chinese_ready(r)]
    ready += [r for r in sorted(domestic+direct_news, key=score, reverse=True) if r not in ready and is_chinese_ready(r)]
    news = ready[:4] if len(ready)>=4 else news

issue = str(int(state.get('last_issue', 2)) + (0 if SAME_DAY else 1)).zfill(3)
data = {
    'issue': issue, 'date': TODAY.isoformat(),
    'headline': {'title': clip(to_chinese(news[0]['title']), 32), 'body': article_body(news[0], 88), 'source': news[0]['source']},
    'updates': [{'tag': tag, 'title': clip(to_chinese(r['title']), 26), 'body': article_body(r, 75), 'source': r['source']} for tag, r in zip(['AI动态', '产品应用', '前沿科技'], news[1:])],
    'leaders': leaders + [{'name':'科技人物动态｜待补充','title':'暂无可确认的人物报道','body':'继续关注国内外科技人物的最新公开动态。','source':'—'} for _ in range(3-len(leaders))],
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
    used_image_pages = set()
    for key, row in zip(['headline', 'u0', 'u1', 'u2'], news):
        uri, matched = fetch_news_image(row['title'], used_image_pages)
        if uri:
            page.evaluate('''async ([key, src]) => { const im = new Image(); im.src = src; await im.decode(); images[key] = im; render(); }''', [key, uri])
            print('新闻配图:', key, matched)
            news_image_count += 1
    print('成功加载新闻相关配图:', news_image_count, '/4')
    # 缺图不阻断出图，沿用原模板的图片占位设计。
    # 只向原模板的既有图片槽位注入真实人物照片；不更改布局和图标。
    portrait_count = 0
    for i, person in enumerate(leader_names):
        uri = fetch_portrait(person)
        if uri:
            page.evaluate('''async ([key, src]) => { const im = new Image(); im.src = src; await im.decode(); images[key] = im; render(); }''', ['l' + str(i), uri])
            portrait_count += 1
    print('成功加载人物照片:', portrait_count, '/', len(leader_names))
    # 无肖像的新闻人物使用原模板占位，不能因此中断出图。
    page.wait_for_timeout(800)
    error = page.locator('#errors').inner_text()
    if not page.evaluate('globe.complete && globe.naturalWidth > 0'):
        raise RuntimeError('原版地球图标加载失败，停止导出')
    if error.strip():
        print('模板文字提醒:', error)
    png = page.locator('#poster').evaluate('(c) => c.toDataURL("image/png").split(",")[1]')
    (OUT / 'daily.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), 'utf-8')
    (OUT / '洪滨每日AI早报.png').write_bytes(base64.b64decode(png))
    browser.close()
    server.shutdown()
state['last_issue'] = int(issue)
state['items'] = (state['items'] + [{'key': r['key'], 'title': r['title'], 'date': TODAY.isoformat()} for r in news + selected])[-500:]
HISTORY.write_text(json.dumps(state, ensure_ascii=False, indent=2), 'utf-8')
print('已生成', OUT / '洪滨每日AI早报.png')
