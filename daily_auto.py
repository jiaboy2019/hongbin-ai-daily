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
old = {x['key'] for x in state['items'] if x.get('date') != TODAY.isoformat()}
# 同一天重跑时不自动增加期号，避免测试造成跳号。
last_dates = [x.get('date') for x in state['items']]
SAME_DAY = TODAY.isoformat() in last_dates[-7:]

# 仅调整新闻筛选；原网页模板、图标、画布与栏目布局均保持不变。
EXCLUDE = ('股票', '股价', '炒股', '基金', '投资建议', '科普周', '洽谈会', '峰会', '大会召开', '奖励大会', '书讯', '据称', '传闻', '网传', '出事了', '震惊', '重磅来袭', '十大热词', '培训', '培训机构', '横向测评', '辞职', '利润翻倍', '亏超', '新媒体创作', '课程体系', '技能培训班', '抵押的AI芯片')
SIGNALS = ('发布', '推出', '上线', '开源', '突破', '模型', '机器人', '芯片', '智能体', 'Agent', '研发', '科研', '应用', '功能', '升级', '世界模型', '编程', '自动化', 'AI', 'artificial intelligence', 'robot', 'chip', 'research', 'launch', 'model', 'agent', 'breakthrough')
TRUSTED_SOURCES = ('Reuters','Associated Press','AP News','Bloomberg','Financial Times','新华','人民网','中新网','TechCrunch','The Verge','MIT Technology Review','Ars Technica','IEEE Spectrum','机器之心','量子位','智东西','36氪','钛媒体','Hugging Face','OpenAI','Google DeepMind','Anthropic','NVIDIA','Microsoft','Nature','Science','The Decoder','MIT News')
MAJOR_ACTORS = ('openai','anthropic','deepmind','google','microsoft','nvidia','meta','apple','deepseek','xai','mistral','hugging face','英伟达','谷歌','微软','苹果','阿里','腾讯','华为','字节','百度','宇树','智元')
SUBSTANTIVE = ('重大突破','首次','全球首','新一代','发布','推出','开源','突破','训练','上线','收购','部署','商用','launch','released','unveils','announces','breakthrough','open-source','deploy','introduces','rolls out')
LOW_VALUE = ('昨夜今晨','早报汇总','科技早报','一文看懂','盘点','微电影','签名','保黄','三种可能','股市','美股','体育','沙巴官网','博彩','竞猜','运动助手','心理困扰','关联机制','中介作用','调节效应','三年行动方案','五张清单','地方出台','研讨会','论坛','或将','传闻','rumor','rumour')
# 对极易引发误传的重大事件，默认不使用单一转载标题作为已证实事实。
# 只有在独立核验流程建立后，才考虑恢复这类高风险报道。
HIGH_RISK_CLAIMS = ('回归英伟达','离职一周','第二次机会','出轨','证明黎曼猜想','攻克黎曼猜想','数学界第一神山','上市','ipo','被迫出局','被解雇','辞职','去世','收购完成','宣布破产','宣布倒闭','遭逮捕','涉嫌犯罪','万亿估值落槌')
SUSPICIOUS_AGGREGATORS = ('moomoo','investing.com','sina.com','搜狐','sohu','techgenyz')
def editorial_eligible(row):
    title = row.get('title','').lower()
    source = row.get('source','').lower()
    if any(w in title for w in HIGH_RISK_CLAIMS): return False
    # Explicitly reject unsupported sensational claims, even when the RSS publisher appears established.
    if any(w in title for w in ('huang scandal', 'ouster of ceo jensen huang', 'jensen huang scandal',
                               'chatgpt wallet', 'riemann hypothesis solved',
                               '黎曼猜想已被证明', '黎曼猜想被攻克')): return False
    if any(w in title for w in ('[第一时间]','今日经济 -','昨夜今晨','每日速览','一周回顾')): return False
    if any(w in source for w in SUSPICIOUS_AGGREGATORS) and not any(w in source for w in ('新华','路透')): return False
    return True

def score(row):
    title, source = row['title'], row.get('source','')
    low = title.lower()
    if not editorial_eligible(row) or any(w.lower() in low for w in EXCLUDE + LOW_VALUE): return -100
    if len(title) > 100: return -30
    points = 0
    if any(w.lower() in low for w in ('ai','人工智能','大模型','机器人','芯片','智能体','agent','模型','robot','chip','llm')): points += 5
    if any(w.lower() in source.lower() for w in TRUSTED_SOURCES): points += 9
    if any(w in low for w in MAJOR_ACTORS): points += 12
    if any(w in low for w in SUBSTANTIVE): points += 12
    if any(w in low for w in ('科研突破','重大突破','全球首','新一代','新模型','开源模型','机器人量产','world model','世界模型')): points += 7
    if any(w in low for w in ('金融','股价','股票','融资','财报','贷款','证券','期货')): points -= 10
    if len(title)>65: points -= 8
    return points if points >= 8 else -20

def similar(a, b):
    # 使用字符三元组发现换标题的近似重复事件；并非完整语义去重。
    def grams(t):
        t = re.sub(r'[^\w\u4e00-\u9fff]', '', t.lower())
        return {t[i:i+3] for i in range(max(0, len(t)-2))}
    x, y = grams(a), grams(b)
    return bool(x and y and len(x & y) / min(len(x), len(y)) >= 0.78)


def tidy(s):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', '', s or ''))).strip()

def valid_description(row):
    desc = tidy(row.get('description', ''))
    title = tidy(row.get('title', ''))
    if len(desc) < 25: return False
    source = tidy(row.get('source', ''))
    # RSS metadata sometimes repeats only the publisher name, not an article summary.
    normalized_desc = re.sub(r'[^\w\u4e00-\u9fff]', '', desc.lower())
    normalized_source = re.sub(r'[^\w\u4e00-\u9fff]', '', source.lower())
    if normalized_desc == normalized_source or (normalized_source and normalized_desc.startswith(normalized_source) and len(normalized_desc) < len(normalized_source) + 12): return False
    if title.lower() in desc.lower() or desc.lower() in title.lower(): return False
    if re.search(r'(?:相关新闻|查看全部报道|View Full Coverage|Read more|阅读更多)', desc, re.I): return False
    if len(re.sub(r'[^A-Za-z\u4e00-\u9fff]', '', desc)) < 22: return False
    return True


def norm(s):
    return hashlib.sha256(re.sub(r'[^\w\u4e00-\u9fff]', '', s.lower()).encode()).hexdigest()[:20]

def search(q, leader=False, international=False):
    params = urllib.parse.urlencode({'q': q + (' when:2d' if leader else ' when:1d'), 'hl':'en-US' if international else 'zh-CN','gl':'US' if international else 'CN','ceid':'US:en' if international else 'CN:zh-Hans'})
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
            if not (0 <= age <= (48 if leader else 18) * 3600): continue
        except (ValueError, TypeError, AttributeError):
            continue
        raw_description = x.findtext('description', '') or ''
        description = tidy(raw_description)
        publisher_links = [html.unescape(u) for u in re.findall(r'href=["\\\'](https?://[^"\\\']+)', raw_description)]
        publisher_links = [u for u in publisher_links if not (urllib.parse.urlparse(u).hostname or '').endswith(('google.com','google.cn'))]
        # Google News 的描述常为标题/相关新闻列表；仅在有独立文本时使用。
        if description.startswith(title): description = description[len(title):].strip(' -—:：')
        if len(description) < 24 or description.lower().startswith('view full coverage'): description = ''
        row = {'title': title, 'source': source[:30], 'key': k, 'description': description, 'article_url': publisher_links[0] if publisher_links else ''}
        if k not in old and len(title) >= 10 and (leader or score(row) > 0) and not any(similar(title, p['title']) for p in state['items'][-100:]):
            rows.append(row)
    return rows

def article_meta_summary(url):
    # Extract a publisher's own metadata, never fabricate an article summary.
    if not url or not url.startswith(('https://','http://')): return ''
    host = urllib.parse.urlparse(url).hostname or ''
    if host.endswith(('google.com','google.cn','googleusercontent.com')): return ''
    try:
        req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0 (compatible; HongbinDaily/1.0)'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            page = resp.read(350_000).decode('utf-8', 'ignore')
        for tag in re.findall(r'<meta\\b[^>]*>', page, re.I):
            if not re.search(r'(?:og:description|name=["\\x27]description)', tag, re.I): continue
            m = re.search(r'content=["\\x27](.*?)["\\x27]', tag, re.I | re.S)
            if m:
                result = tidy(m.group(1))[:450]
                if len(result) >= 30: return result
    except Exception as exc:
        print('原文摘要获取失败:', host, str(exc)[:65])
    return ''


def enrich_person_summaries(rows, identify_person):
    # Do not manufacture facts from the headline; only use publisher-provided metadata.
    attempts = 0
    for row in sorted(rows, key=score, reverse=True):
        if attempts >= 35: break
        if not identify_person(row['title'])[0] or not editorial_eligible(row): continue
        if valid_description(row): continue
        url = row.get('article_url', '')
        if not url: continue
        attempts += 1
        summary = article_meta_summary(url)
        if summary:
            candidate = dict(row, description=summary)
            if valid_description(candidate): row['description'] = summary
    print('人物原文摘要补采尝试:', attempts)


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
 # 扩展来源：新增真实媒体/研究机构的公开订阅地址，失效源自动跳过。
 ('钛媒体','https://www.tmtpost.com/rss'),
 ('InfoQ','https://www.infoq.cn/feed'),
 ('IT之家','https://www.ithome.com/rss/'),
 ('雷峰网','https://www.leiphone.com/feed'),
 ('Engadget AI','https://www.engadget.com/category/ai/feed/'),
 ('The Register','https://www.theregister.com/software/ai_ml/headlines.atom'),
 ('Meta News','https://about.fb.com/news/feed/'),
 ('Meta Engineering','https://engineering.fb.com/feed/'),
 ('Google Cloud AI','https://cloudblog.withgoogle.com/products/ai-machine-learning/rss/'),
 ('LangChain','https://blog.langchain.com/rss.xml'),
 ('GitHub AI','https://github.blog/ai-and-ml/feed/'),
 ('Cloudflare AI','https://blog.cloudflare.com/tag/ai/rss/'),
]
def direct_feed_rows(max_age_hours=18):
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
                    if not 0 <= age <= max_age_hours*3600: continue
                except Exception: continue
                description = tidy(item.findtext('description') or item.findtext('{http://purl.org/rss/1.0/modules/content/}encoded') or item.findtext('atom:summary', default='', namespaces=ns) or item.findtext('atom:content', default='', namespaces=ns))
                # 直接媒体源提供的独立摘要优先；仅删除标题重复前缀，不丢弃整段正文。
                if description.startswith(title): description = description[len(title):].strip(' -—:：')
                if len(description) < 25: description = ''
                description = description[:450]
                link = item.findtext('link') or item.findtext('atom:link', default='', namespaces=ns)
                if not link:
                    atom_link = item.find('atom:link', ns)
                    if atom_link is not None: link = atom_link.attrib.get('href','')
                row = {'title':title,'source':source,'key':norm(title),'description':description,'article_url':link,'age_hours':age/3600}
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

# 中英文报道可能使用不同姓名写法；肖像只按已知对应人物匹配。
PERSON_PORTRAIT_ALIASES = {
    '姚顺雨': 'Yao_Shunyu', '梁文锋': 'Liang_Wenfeng',
    'Demis Hassabis': 'Demis_Hassabis', 'Andrej Karpathy': 'Andrej_Karpathy',
    'Jeff Dean': 'Jeff_Dean', 'François Chollet': 'François_Chollet',
    'Dario Amodei': 'Dario_Amodei', 'Sundar Pichai': 'Sundar_Pichai',
}
def fetch_portrait(name):
    page = PERSON_PAGES.get(name) or PERSON_PORTRAIT_ALIASES.get(name)
    return fetch_wiki_thumbnail(page) if page else None

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

translation_cache = {}
translation_unavailable = False
fallback_translation_attempts = 0
def to_chinese(text):
    global translation_unavailable, fallback_translation_attempts
    if text in translation_cache: return translation_cache[text]
    if not text or len(re.findall(r'[A-Za-z]', text)) < max(12, len(text)//3): return text
    # Primary translator; if rate-limited, try a second independent provider.
    if not translation_unavailable:
        try:
            query = urllib.parse.urlencode({'client':'gtx','sl':'auto','tl':'zh-CN','dt':'t','q':text})
            req = urllib.request.Request('https://translate.googleapis.com/translate_a/single?'+query,headers={'User-Agent':'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=8) as resp:
                obj = json.load(resp)
            translated = ''.join(x[0] for x in obj[0] if x and x[0])
            if translated:
                translation_cache[text] = translated
                return translated
        except Exception as exc:
            if '429' in str(exc): translation_unavailable = True
            print('主翻译源不可用:',str(exc)[:80])
    if fallback_translation_attempts >= 16: return text
    fallback_translation_attempts += 1
    try:
        # Fallback is only for translation, not for inventing missing article facts.
        query = urllib.parse.urlencode({'q':text[:480], 'langpair':'en|zh-CN'})
        req = urllib.request.Request('https://api.mymemory.translated.net/get?'+query, headers={'User-Agent':'HongbinDaily/1.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            obj = json.load(resp)
        translated = html.unescape(obj.get('responseData',{}).get('translatedText',''))
        if obj.get('responseStatus') == 200 and len(re.findall('[一-鿿]',translated)) >= 5:
            translation_cache[text] = translated
            return translated
    except Exception as exc:
        print('备用翻译源不可用:',str(exc)[:80])
    return text

def article_body(row, limit):
    title = to_chinese(row['title'].strip())
    desc = to_chinese(row.get('description', '').strip())
    # 未翻译的英文摘要不能直接印在中文版早报上。
    # 翻译源限流时，只保留已经中文化的事实，不发布英文整段。
    if desc and len(desc) >= 25 and len(re.findall('[一-鿿]', desc)) >= 12:
        return clip(desc, limit)
    # RSS 未提供独立摘要时，不捏造新闻事实。以报道来源和原始标题补足信息。
    # 如翻译失败，不把英文标题重复印成正文。
    if len(re.findall('[一-鿿]', title)) < 8:
        return '原文摘要暂未完成中文转换，建议编辑核对报道后发布。'
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
    ('李沐', 'AI研究者'), ('陈天奇', 'AI研究者'), ('姚顺雨', '腾讯混元首席AI科学家'),
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
leader_queries = ['人工智能 教师 分享', 'AI 医生 应用 表示', 'AI 创业者 介绍', 'AI 工程师 研发', '人工智能 学者 观点', 'AI 设计师 创作', '机器人 创始人 表示', '大模型 产品负责人 介绍']
# 直接 RSS 只抓取一次48小时窗口，再按新闻18小时过滤，避免重复请求30个源。
leader_direct_news = direct_feed_rows(max_age_hours=48)
direct_news = [r for r in leader_direct_news if r.get('age_hours', 999) <= 18]
leader_rows = gather(leader_queries, leader=True)
# 直接媒体 RSS 自带事实摘要，优先补充其中真正涉及具体人物的报道。
leader_rows += [r for r in leader_direct_news if valid_description(r)]
# 补充人物定向搜索；人物名单只用于拓宽搜索，不是入选门槛。
leader_rows += gather(['黄仁勋 AI', '马斯克 xAI', '奥尔特曼 OpenAI', '苏姿丰 AI', '李飞飞 AI', '库克 Apple AI', 'Demis Hassabis AI', 'Sam Altman AI'], leader=True, international=True)
# 直接以中文检索具体人物，减少英文翻译服务限流造成的遗漏。
# 人物栏目补充实际应用者与从业者；48小时内真实媒体报道，不预设必须是科技巨头。
leader_rows += gather([
    'AI 教师 教学 分享', '人工智能 医生 临床 介绍',
    'AI 设计师 创作 分享', '人工智能 工程师 研发 表示',
    'AI 创业者 应用 发布', '机器人 研究员 介绍',
    '大模型 产品经理 表示', 'AI 学者 研究 发现',
    'AI 导演 影视 制作', 'AI 程序员 开发者 分享',
    '人工智能 科研团队 教授 介绍', '智能体 创始人 访谈',
], leader=True)
leader_rows += gather(['奥尔特曼 人工智能 发布', '黄仁勋 人工智能 芯片', '马斯克 xAI 模型', '李飞飞 世界模型', '苏姿丰 AMD AI', '皮查伊 谷歌 Gemini', '纳德拉 微软 AI', '梁文锋 DeepSeek', '王兴兴 宇树 机器人', '阿莫迪 Anthropic', '扎克伯格 Meta AI', '杨立昆 AI', '辛顿 人工智能'], leader=True)
# 对人物栏目增加媒体定向检索，避免通用“科技人物”搜索被无关资讯淹没。
leader_rows += gather([
    'AI CEO site:ithome.com', '人工智能 创始人 site:qbitai.com',
    'AI 科学家 site:tmtpost.com', 'AI 创始人 site:techcrunch.com',
    '机器人 CEO site:36kr.com', '大模型 负责人 site:ithome.com',
    'AI 首席科学家 site:chinanews.com.cn',
], leader=True)
leader_rows += gather([
    'Sam Altman AI announcement', 'Jensen Huang AI statement',
    'Dario Amodei Anthropic', 'Demis Hassabis DeepMind',
    'Sundar Pichai Gemini', 'Satya Nadella AI',
    'Mark Zuckerberg AI', 'Elon Musk xAI',
    'Fei-Fei Li world model', 'Andrew Ng artificial intelligence',
], leader=True, international=True)
# 把已有名单中的名字作为识别提示，同时识别新闻标题中的中文姓名和英文姓名。
name_roles = dict(people)
def identify_person(title):
    # Ignore SEO keyword suffixes rather than treating them as actual news subjects.
    title = re.split(r'[|｜](?:DeepSeek|AI|OpenAI|英伟达|限额|模型|人工智能|科技|新闻)(?:[|｜]|$)', title, maxsplit=1, flags=re.I)[0]
    title = re.sub(r'_[^\s]*新浪新闻.*$', '', title)
    for name, role in name_roles.items():
        if re.search(r'(?<![A-Za-z])' + re.escape(name) + r'(?![A-Za-z])', title, re.I):
            return name, role
    m = re.search(r'(?:科学家|创始人|CEO|首席执行官|研究员|教授|董事长|负责人|总裁|工程师|教师|医生|设计师|创业者|开发者|学者|导演|作者|产品经理)[：\\s]*([\\u4e00-\\u9fff]{2,4})(?:[：，称说表示认为谈]|\\s)', title)
    # “CEO预测”“科学家表示”等标题不是姓名；拒绝把普通动词当人名。
    if m and m.group(1) not in ('预测','表示','认为','透露','宣布','发布','警告','指出','建议','回应','分析','称赞','谈论','介绍','发现','研究','专家','团队','未来','人工','智能','最新','全球','公司','行业'):
        return m.group(1), '科技人物'
    # 允许新闻中“姓名+职务”的人物，职务作为识别依据，不猜测没有身份信息的姓名。
    m2 = re.search(r'([\\u4e00-\\u9fff]{2,3})(?:教授|研究员|工程师|教师|医生|设计师|创业者|开发者|学者|CEO|创始人|负责人|总裁|董事长)[：，称说表示认为谈\\s]', title)
    if m2 and m2.group(1) not in ('人工','智能','公司','行业','科技','全球','预测','专家','研究','产品','应用','未来','技术','最新'):
        return m2.group(1), 'AI相关从业者'
    # 具名“身份 + 姓名”写法：例如“AI教师张三表示”。
    m3 = re.search(r'(?:AI|人工智能|机器人|大模型)?(?:教师|医生|工程师|设计师|研究员|教授|创业者|开发者|学者|创始人|负责人)([\\u4e00-\\u9fff]{2,3})(?:表示|称|说|认为|介绍|分享|发布|指出|：|，)', title, re.I)
    if m3 and m3.group(1) not in ('预测','表示','认为','发布','研究','专家','团队','公司','行业','科技','人工','智能','技术','应用','产品'):
        return m3.group(1), 'AI相关从业者'
    # 只凭标题开头的两个汉字无法可靠判断是人名，容易把“拷问”等动词当人物。
    # 未列入明确人物名单的名字，需要具备职务及姓名的上下文。
    return None, None
    return None, None
def relevant_ai_person_event(title):
    """人物本人参与的AI事件；不把纯融资、债券或股市报道当人物动态。"""
    low = title.lower()
    finance_only = ('债券','债务','融资','募资','股价','股市','股票','财报','估值',
                    'bond','debt','financing','fundraising','valuation','shares','stock')
    ai_terms = ('人工智能','大模型','语言模型','世界模型','机器人','智能体','芯片',
                'gemini','chatgpt','deepseek','grok','claude','agent',
                'artificial intelligence','machine learning','robot','llm','gpu')
    # "AI" 作为独立词匹配，避免把普通英文单词里的 ai 误判为人工智能。
    has_ai = any(term in low for term in ai_terms) or bool(re.search(r'(?<![a-z])ai(?![a-z])',low))
    if not has_ai: return False
    if any(term in low for term in finance_only) and not any(term in low for term in
        ('模型','机器人','智能体','科研','芯片','发布','开源','训练','产品',
         'model','robot','agent','chip','research','launch','release')):
        return False
    return True

def eligible_person_report(row):
    # 正规媒体报道直接作为候选，不再要求独立摘要、动词模板或重大突破评分。
    # 保留明确的高风险标题排除及AI相关性，避免虚构、广告和无关新闻。
    if not editorial_eligible(row): return False
    if not relevant_ai_person_event(row['title'] + ' ' + row.get('description','')): return False
    if len(row.get('title','').strip()) < 9: return False
    if any(w in row['title'].lower() for w in ('博彩','竞猜','广告推广','sponsored content')): return False
    return True

def is_chinese_text(t):
    return len(re.findall('[一-鿿]',to_chinese(t))) >= 5
enrich_person_summaries(leader_rows, identify_person)
# 记录人物候选在哪一道门槛被排除，便于定位真正瓶颈；不输出假新闻。
person_rejections = {'no_name':0,'invalid_report':0,'not_chinese':0,'eligible':0}
person_samples = []
for candidate in leader_rows:
    candidate_name, _ = identify_person(candidate['title'])
    if not candidate_name:
        reason='no_name'
    elif not eligible_person_report(candidate):
        reason='invalid_report'
    elif not is_chinese_text(candidate['title']):
        reason='not_chinese'
    else:
        reason='eligible'
    person_rejections[reason] += 1
    if reason == 'invalid_report' and len(person_samples) < 12:
        person_samples.append({'name':candidate_name,'title':candidate['title'][:85],
            'source':candidate.get('source',''), 'has_description':valid_description(candidate),
            'score':score(candidate)})
print('人物合格候选明细:', json.dumps([
    {'name':identify_person(r['title'])[0], 'title':r['title'][:110],
     'source':r.get('source',''), 'description_ok':valid_description(r),
     'excluded_word':next((w for w in ('stock','stocks','shares','market selloff','股价','股票','道歉','apology','jeff bezos','didn’t','didn\\u2019t','科技早报','或将','传闻') if w in r['title'].lower()), ''),
     'has_ai':relevant_ai_person_event(r['title']+' '+r.get('description',''))}
    for r in leader_rows if identify_person(r['title'])[0] and eligible_person_report(r)
    and is_chinese_text(r['title'])
][:25], ensure_ascii=False))
print('人物候选诊断:', json.dumps(person_rejections, ensure_ascii=False))
print('人物被排除样本:', json.dumps(person_samples, ensure_ascii=False))
def person_specific_story(row, name):
    title = row['title']
    # 综合节目/资讯合集即使提到人名，也不是一条可核实的个人动态。
    if any(x in title.lower() for x in ('今日焦点','新闻速览','昨夜今晨','每日简报','一周回顾',
        '新闻联播','今日要闻','热点汇总','weekly roundup','news roundup')): return False
    if any(x in title for x in (' | ','｜','；',';')): return False
    # 姓名必须与该条AI事实在同一主题内，而非仅被顺带提及。
    return name.lower() in title.lower()

for r in sorted(leader_rows, key=score, reverse=True):
    name, role = identify_person(r['title'])
    if not name or name in leader_names: continue
    if not eligible_person_report(r) or not person_specific_story(r, name): continue
    if any(w in r['title'].lower() for w in ('stock','stocks','shares','market selloff','股价','股票','道歉','apology','jeff bezos','didn’t','didn\u2019t','科技早报','或将','传闻')): continue
    if not relevant_ai_person_event(r['title'] + ' ' + r.get('description','')): continue
    if not is_chinese_text(r['title']): continue
    # 仅标题可核验时，以标题事实形成简讯，不冒充原文独立摘要。
    if not valid_description(r):
        r = dict(r, headline_only_person=True)
    leaders.append({'name': name + '｜' + role, 'title': clip(to_chinese(r['title']), 23), 'body': article_body(r, 60), 'source': r['source']})
    selected.append(r); leader_names.append(name)
    if len(leaders) == 3: break
# 绝不把普通科技新闻冒充人物动态；人物不足时继续拓展搜索。
if len(leaders) < 3:
    extra = gather(['AI 创始人 表示', 'AI 科学家 发布', 'AI 公司 CEO 称', '人工智能 研究者 表示', 'AI 教师 表示', 'AI 医生 介绍', 'AI 工程师 研发', '人工智能 教授 指出', '机器人 创业者 分享', 'AI 设计师 应用'], leader=True)
    for r in extra:
        name, role = identify_person(r['title'])
        if not name or name in leader_names or not is_chinese_text(r['title']) or not eligible_person_report(r) or not person_specific_story(r, name): continue
        if not relevant_ai_person_event(r['title'] + ' ' + r.get('description','')): continue
        if not valid_description(r):
            r = dict(r, headline_only_person=True)
        leaders.append({'name':name+'｜'+role,'title':clip(r['title'],23),'body':article_body(r,60),'source':r['source']})
        selected.append(r); leader_names.append(name)
        if len(leaders)==3: break

used = {r['key'] for r in selected}
news = [r for r in gather(['人工智能', 'AI 模型', 'AI 新产品', '机器人', 'AI 芯片', '智能体', 'AI 科研突破', '大模型 发布', 'AI 应用 上线']) if r['key'] not in used]
# 国际媒体独立采集，不能只使用中国区中文搜索。
international_news = gather(['artificial intelligence breakthrough', 'OpenAI Google DeepMind Anthropic AI', 'AI robotics chips research', 'AI agents product launch', 'AI science technology'], international=True)
news.extend(r for r in international_news if r['key'] not in {n['key'] for n in news} and r['key'] not in used)
# 不提供可靠公开 RSS 的权威媒体，按站点定向检索，仍严格执行18小时窗口。
# 这只是补充发现渠道，不是限制新闻发布媒体的白名单。
site_queries_cn = [
    '人工智能 site:news.cn', 'AI site:people.com.cn',
    '人工智能 site:chinanews.com.cn', 'AI site:huanqiu.com',
    'AI site:jiqizhixin.com', '人工智能 site:qbitai.com',
    '机器人 site:zhidx.com', 'AI site:36kr.com',
]
site_queries_en = [
    'AI site:reuters.com/technology', 'AI site:bloomberg.com',
    'artificial intelligence site:ft.com', 'AI site:techcrunch.com',
    'AI site:technologyreview.com', 'AI site:venturebeat.com',
    'AI site:spectrum.ieee.org',
]
site_news = gather(site_queries_cn) + gather(site_queries_en, international=True)
news.extend(r for r in site_news if r['key'] not in {n['key'] for n in news} and r['key'] not in used)
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
    return len(re.findall('[一-鿿]', t)) >= 8 and len(re.findall(r'[A-Za-z]', t)) <= len(t)*0.55

# 先筛真正具有独立事实信息的摘要，再按新闻价值评分；Google News 标题列表不能算摘要。

# 英文自动翻译受限时，绝不把未翻译的英文长标题刊登为中文新闻。
# 从完整候选库补齐，兼顾国内、国际、不同媒体和不同事件。
def relevant_ai_news(row):
    """普通新闻必须直接讲AI技术、AI产品或AI应用，不能仅因提到AI市场预期入选。"""
    title = row['title'].lower()
    excluded = ('锡价','铜价','铝价','关税','跨境包裹','插混','燃油','混动','显卡驱动',
                'geforce hotfix','game ready','股价','股票','债券','基金','汇率','楼市',
                '房地产','融资轮','估值','价格暴跌','商品期货')
    if any(word in title for word in excluded): return False
    direct_ai = ('人工智能','大模型','语言模型','世界模型','智能体','ai应用','ai工具',
                 'ai模型','ai助手','ai芯片','ai机器人','机器人','机器学习',
                 '深度学习','生成式ai','生成式人工智能','deepseek','chatgpt',
                 'openai','anthropic','gemini','grok','claude','llm','agent',
                 'artificial intelligence','machine learning','ai agent',
                 'ai model','robotics','robot')
    return any(word in title for word in direct_ai) or bool(re.search(r'(?<![a-z])ai(?![a-z])',title))

ready_pool = sorted([r for r in news + domestic + direct_news + international_news + site_news if relevant_ai_news(r) and is_chinese_ready(r) and score(r)>0 and editorial_eligible(r)],key=lambda r: (valid_description(r), score(r)),reverse=True)
new_selection = []
for r in ready_pool:
    if r['key'] in {x['key'] for x in new_selection}: continue
    if any(similar(r['title'],x['title']) for x in new_selection): continue
    if len(new_selection) < 3 and r['source'] in {x['source'] for x in new_selection}: continue
    new_selection.append(r)
    if len(new_selection)==4: break
if len(new_selection)<4:
    for r in ready_pool:
        if r['key'] not in {x['key'] for x in new_selection} and not any(similar(r['title'],x['title']) for x in new_selection): new_selection.append(r)
        if len(new_selection)==4: break
if len(new_selection)<4:
    report = {
        'date': TODAY.isoformat(), 'status': 'needs_editorial_review',
        'reason': f'具备独立摘要的中文新闻仅{len(new_selection)}/4条',
        'diagnostics': {
            'translation_service_rate_limited': translation_unavailable,
            'all_news_candidates': len(news + domestic + direct_news + international_news),
            'independent_summary_candidates': sum(valid_description(r) for r in news + domestic + direct_news + international_news),
            'chinese_with_independent_summary': len(ready_pool),
            'distinct_news_selected': len(new_selection),
        },
        'news_candidates': [
            {'title': r['title'], 'source': r['source'],
             'description': tidy(r.get('description',''))[:240],
             'has_independent_summary': valid_description(r), 'score': score(r)}
            for r in sorted(news + domestic + direct_news + international_news, key=score, reverse=True)[:40]
        ],
        'note': '待人工审阅，不得直接作为正式新闻发布。'
    }
    (OUT / 'editorial_review.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    raise RuntimeError('独立摘要中文新闻不足4条：已保存候选报告，停止出图')
news = [dict(r, headline_only_news=not valid_description(r)) for r in new_selection]

# 成品发布前的最低质量门槛：禁止空栏目、重复标题摘要、可疑模型型号。
# 未达标时工作流失败，不覆盖已发布的正式成品。
UNVERIFIED_MODEL_PATTERNS = (r'GPT-6\.2', r'GPT-7(?:\.\d+)?', r'Claude\s+6(?:\.\d+)?')
def valid_summary(row):
    if valid_description(row): return True
    if row.get('headline_only_news') and relevant_ai_news(row) and len(row['title'].strip()) >= 10: return True
    # 标题简讯只用于人物栏目：明确标记、真实姓名、中文事实与AI相关性。
    # 不把重复标题伪装成独立摘要，也不放宽普通新闻标准。
    return bool(row.get('headline_only_person')
                and identify_person(row['title'])[0]
                and eligible_person_report(row)
                and len(row['title'].strip()) >= 9)

if len(leaders) != 3:
    # 失败时保留可审阅的候选报告，供人工补选真实人物报道。
    report = {
        'date': TODAY.isoformat(), 'status': 'needs_editorial_review',
        'reason': f'科技人物仅{len(leaders)}/3条；有效摘要新闻{len(ready_pool)}条',
        'person_filter_diagnostics': {
            'candidate_count': len(leader_rows),
            'recognized_names': len([r for r in leader_rows if identify_person(r['title'])[0]]),
            'recognized_with_independent_summary': len([r for r in leader_rows if identify_person(r['title'])[0] and valid_description(r)]),
            'recognized_with_summary_and_eligible_source': len([r for r in leader_rows if identify_person(r['title'])[0] and valid_description(r) and editorial_eligible(r) and score(r)>=8]),
            'recognized_with_chinese_title': len([r for r in leader_rows if identify_person(r['title'])[0] and valid_description(r) and editorial_eligible(r) and score(r)>=8 and is_chinese_text(r['title'])]),
            'translation_rate_limited': translation_unavailable,
        },
        'recognized_person_details': [
            {'title': r['title'], 'source': r['source'], 'name': identify_person(r['title'])[0],
             'summary_ok': valid_description(r), 'eligible': editorial_eligible(r),
             'score': score(r), 'chinese_title': is_chinese_text(r['title'])}
            for r in leader_rows if identify_person(r['title'])[0]
        ][:40],
        'selected_person_candidates_unverified': leaders,
        'person_search_candidates': [
            {'title': r['title'], 'source': r['source'],
             'description': tidy(r.get('description',''))[:240],
             'score': score(r), 'recognized_person': identify_person(r['title'])[0]}
            for r in sorted(leader_rows, key=score, reverse=True)[:35]
        ],
        'news_candidates': [
            {'title': r['title'], 'source': r['source'],
             'description': tidy(r.get('description',''))[:240], 'score': score(r)}
            for r in ready_pool[:30]
        ],
        'note': '仅供编辑审阅，候选未完成独立事实核验，不能直接发布。'
    }
    (OUT / 'editorial_review.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    raise RuntimeError(f'人物动态仅{len(leaders)}/3条，已输出待审候选报告，拒绝用占位新闻出图')
# 出图前再检查人物标签，不能把动词、抽象词当作人名。
INVALID_PERSON_NAMES = {'预测','表示','认为','透露','宣布','发布','警告','指出','建议','回应','分析','介绍','研究','专家','团队','未来'}
if any(p['name'].split('｜')[0] in INVALID_PERSON_NAMES for p in leaders):
    raise RuntimeError('人物栏目识别到非人名，拒绝出图')
if any(not valid_summary(r) for r in news + selected):
    invalid = [r for r in news + selected if not valid_summary(r)]
    report = {
        'date': TODAY.isoformat(), 'status': 'needs_editorial_review',
        'reason': f'{len(invalid)}条入选内容缺少独立摘要',
        'invalid_selected_items': [
            {'title': r['title'], 'source': r['source'],
             'description': tidy(r.get('description', ''))[:250],
             'person': identify_person(r['title'])[0]}
            for r in invalid
        ],
        'selected_news': [
            {'title': r['title'], 'source': r['source'],
             'description': tidy(r.get('description', ''))[:250]}
            for r in news
        ],
        'selected_people': [
            {'title': r['title'], 'source': r['source'],
             'description': tidy(r.get('description', ''))[:250],
             'person': identify_person(r['title'])[0]}
            for r in selected
        ],
        'note': '仅用于编辑诊断；禁止将标题改写当作独立事实摘要。'
    }
    (OUT / 'editorial_review.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    raise RuntimeError(f'{len(invalid)}条入选内容缺少独立摘要，已输出待审报告，拒绝出图')
if any(re.search(pattern, r['title'], re.I) for r in news + selected for pattern in UNVERIFIED_MODEL_PATTERNS):
    raise RuntimeError('发现未经独立确认的模型版本号，停止出图')

issue = str(int(state.get('last_issue', 2)) + (0 if SAME_DAY else 1)).zfill(3)
data = {
    'issue': issue, 'date': TODAY.isoformat(),
    'headline': {'title': clip(to_chinese(news[0]['title']), 32), 'body': article_body(news[0], 88), 'source': news[0]['source']},
    'updates': [{'tag': tag, 'title': clip(to_chinese(r['title']), 22), 'body': article_body(r, 48), 'source': r['source']} for tag, r in zip(['AI动态', '产品应用', '前沿科技'], news[1:])],
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
