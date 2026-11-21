import base64, datetime as dt, hashlib, html, json, re, urllib.parse, urllib.request, io
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from PIL import Image
import xml.etree.ElementTree as ET
from pathlib import Path

from argparse import ArgumentParser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import os
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent
BEIJING = dt.timezone(dt.timedelta(hours=8))
NEWS_HOURS = 18
PERSON_HOURS = 168
NOW = dt.datetime.now(dt.timezone.utc)
HISTORY = ROOT / 'published_history.json'


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
    if any(w in title for w in HIGH_RISK_CLAIMS if w != 'ipo') or re.search(r'\bipo\b', title): return False
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
    if re.search(r'(?<![a-z])ai(?![a-z])', low) or any(w.lower() in low for w in ('人工智能','大模型','机器人','芯片','智能体','agent','模型','robot','chip','llm')): points += 5
    if any(w.lower() in source.lower() for w in TRUSTED_SOURCES): points += 9
    if any(w in low for w in MAJOR_ACTORS): points += 12
    if any(w in low for w in SUBSTANTIVE): points += 12
    if any(w in low for w in ('科研突破','重大突破','全球首','新一代','新模型','开源模型','机器人量产','world model','世界模型')): points += 7
    if any(w in low for w in ('金融','股价','股票','融资','财报','贷款','证券','期货')): points -= 10
    if len(title)>85: points -= 8
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
    if desc.casefold() == title.casefold() or desc.casefold() in title.casefold(): return False
    if re.search(r'(?:相关新闻|查看全部报道|View Full Coverage|Read more|阅读更多)', desc, re.I): return False
    if len(re.sub(r'[^A-Za-z\u4e00-\u9fff]', '', desc)) < 22: return False
    return True


def norm(s):
    return hashlib.sha256(re.sub(r'[^\w\u4e00-\u9fff]', '', s.lower()).encode()).hexdigest()[:20]

def search(q, leader=False, international=False):
    params = urllib.parse.urlencode({
        'q': q + (' when:7d' if leader else ' when:1d'),
        'hl': 'en-US' if international else 'zh-CN', 'gl': 'US' if international else 'CN',
        'ceid': 'US:en' if international else 'CN:zh-Hans',
    })
    root = ET.fromstring(fetch('https://news.google.com/rss/search?' + params))
    rows = []
    for item in root.findall('.//item'):
        title = tidy(item.findtext('title'))
        publisher = item.find('source')
        if publisher is None:
            continue
        suffix = ' - ' + tidy(publisher.text)
        title = title.removesuffix(suffix)
        desc = item.findtext('description', '')
        links = [html.unescape(u) for u in re.findall(r"""href=["'](https?://[^"']+)""", desc)]
        publisher_host = host(publisher.get('url', ''))
        link = next((u for u in links if publisher_host and domain_matches(host(u), publisher_host)), '')
        row = candidate(title, link, item.findtext('pubDate'), '', 'Google News')
        if row and in_window(row, PERSON_HOURS if leader else NEWS_HOURS):
            rows.append(row)
    return rows


class ArticleMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        if tag == 'meta':
            values = dict(attrs)
            key = values.get('property') or values.get('name')
            if key and values.get('content'):
                self.meta[key.lower()] = values['content']


def article_meta_summary(url):
    source = publisher_for(url)
    if not source:
        return ''
    try:
        req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0 (compatible; HongbinDaily/1.0)'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            redirect_source = publisher_for(resp.geturl())
            if not redirect_source or redirect_source['name'] != source['name']:
                return ''
            page = resp.read(350_000).decode('utf-8', 'ignore')
        parser = ArticleMetadata()
        parser.feed(page)
        return tidy(parser.meta.get('og:description') or parser.meta.get('description'))[:600]
    except (OSError, ValueError) as exc:
        print('原文摘要获取失败:', source['name'], str(exc)[:65])
    return ''


SOURCE_REGISTRY = json.loads((ROOT / 'news_sources.json').read_text('utf-8'))['sources']
FEEDS = [source for source in SOURCE_REGISTRY if source['type'] in ('rss', 'atom')]

def fetch(url, accept=None, timeout=8):
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; HongbinDaily/1.0)'}
    if accept:
        headers['Accept'] = accept
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
        return response.read(1_500_000)


def host(url):
    try:
        return (urllib.parse.urlsplit(url).hostname or '').lower().removeprefix('www.')
    except (TypeError, ValueError):
        return ''


def domain_matches(actual, expected):
    return actual == expected or actual.endswith('.' + expected)


def canonical_url(url):
    if not isinstance(url, str):
        return ''
    hostname = host(url)
    if not hostname or not url.startswith('https://') or '@' in urllib.parse.urlsplit(url).netloc:
        return ''
    parts = urllib.parse.urlsplit(url)
    query = sorted((k, v) for k, v in urllib.parse.parse_qsl(parts.query)
                   if not k.lower().startswith('utm_') and k.lower() not in {'fbclid', 'gclid', 'ref'})
    return urllib.parse.urlunsplit(('https', hostname, parts.path.rstrip('/') or '/',
                                     urllib.parse.urlencode(query), ''))


def publisher_for(url):
    hostname = host(url)
    path = urllib.parse.urlsplit(url).path.lower()
    for source in SOURCE_REGISTRY:
        domain = source.get('domain', host(source['url']))
        prefix = source.get('path_prefix')
        if domain and domain_matches(hostname, domain) and (not prefix or path.startswith(prefix)):
            return source
    return None


def parse_date(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        try:
            result = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    return result.astimezone(dt.timezone.utc) if result.tzinfo else None


def candidate(title, url, published, description, via, heat=0):
    title = tidy(title)
    normalized_url = canonical_url(url)
    when = parse_date(published)
    source = publisher_for(url) if normalized_url else None
    if not (title and source and when):
        return None
    return {
        'title': title, 'source': source['name'], 'key': norm(title),
        'article_url': url, 'canonical_url': normalized_url, 'published_at': when.isoformat(),
        'age_hours': (NOW - when).total_seconds() / 3600,
        'description': tidy(description)[:600], 'discovered_via': via,
        'region': 'CN' if source.get('region') == 'CN' else 'international',
        'official': bool(source.get('official')), 'heat': heat,
    }


def in_window(row, hours):
    return 0 <= row['age_hours'] <= hours


def direct_feed(source):
    ns = '{http://www.w3.org/2005/Atom}'
    root = ET.fromstring(fetch(source['url']))
    rows = []
    entries = root.findall('.//item') or root.findall(f'.//{ns}entry')
    for item in entries:
        link = item.findtext('link')
        if not link:
            link = next((x.get('href', '') for x in item.findall(f'{ns}link')
                         if x.get('rel', 'alternate') == 'alternate'), '')
        date = item.findtext('pubDate') or item.findtext(f'{ns}published')
        # Atom only requires updated. For ordinary articles, an edit is not publication.
        if not date and source.get('use_atom_updated'):
            date = item.findtext(f'{ns}updated')
        description = (item.findtext('description') or item.findtext(f'{ns}summary')
                       or item.findtext(f'{ns}content') or '')
        row = candidate(item.findtext('title') or item.findtext(f'{ns}title'),
                        urllib.parse.urljoin(source['url'], link) if link else '',
                        date, description, 'direct')
        if row and in_window(row, PERSON_HOURS):
            rows.append(row)
    return rows, len(entries)


def parse_aihot_item(item):
    if not isinstance(item, dict) or not isinstance(item.get('links'), dict):
        return None
    title = next((item[k] for k in ('title', 'originalTitle') if isinstance(item.get(k), str) and item[k]), '')
    summary = item.get('summary')
    heat = item.get('score')
    row = candidate(title, item['links'].get('original'), item.get('publishedAt'),
                    summary if isinstance(summary, str) else '', 'AIHOT',
                    min(100, max(0, heat)) if type(heat) is int else 0)
    if row and isinstance(item.get('originalTitle'), str):
        row['original_title'] = tidy(item['originalTitle'])
    return row


def aihot_page_rows(mode, max_pages):
    endpoint = 'https://aihot.news/api/v1/items'
    rows, seen, cursors = [], set(), set()
    counts = Counter()
    errors = []
    cursor = None
    for _ in range(max_pages):
        params = {'mode': mode}
        if cursor:
            params['cursor'] = cursor
        url = endpoint + '?' + urllib.parse.urlencode(params)
        try:
            payload = json.loads(fetch(url, 'application/json', timeout=12))
            items = payload['items']
            if not isinstance(items, list):
                raise ValueError('AIHOT items is not a list')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(str(exc))
            break
        counts['pages'] += 1
        for item in items:
            counts['raw'] += 1
            row = parse_aihot_item(item)
            if not row:
                counts['missing_metadata_or_unverified_publisher'] += 1
            elif row['canonical_url'] not in seen:
                seen.add(row['canonical_url'])
                if in_window(row, NEWS_HOURS):
                    counts['valid_18h'] += 1
                if in_window(row, PERSON_HOURS):
                    rows.append(row)
        page = payload.get('page') if isinstance(payload.get('page'), dict) else {}
        cursor = page.get('nextCursor')
        if not page.get('hasMore') or not isinstance(cursor, str) or cursor in cursors:
            break
        cursors.add(cursor)
    counts['valid_person_window'] = len(rows)
    return rows, {**counts, 'errors': errors}


def aihot_candidates():
    selected, selected_stats = aihot_page_rows('selected', 4)
    general, general_stats = aihot_page_rows('all', 6)
    rows = {row['canonical_url']: row for row in reversed(selected + general)}
    return list(rows.values()), {
        'selected_feed': selected_stats, 'all_feed': general_stats,
        'valid_18h': sum(in_window(row, NEWS_HOURS) for row in rows.values()),
        'valid_person_window': len(rows),
    }


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
    'Ian Buck': 'Ian_Buck', 'Yossi Matias': 'Yossi_Matias',
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
    ('无人出租车', 'Robotaxi'), ('自动驾驶', 'Robotaxi'), ('Robotaxi', 'Robotaxi'),
    ('Waymo', 'Waymo'), ('Thinking Machines', 'Thinking_Machines_Lab'),
]
def fetch_news_image(title, used_pages=None):
    used_pages = used_pages if used_pages is not None else set()
    candidates = [(word, page) for word, page in NEWS_IMAGE_PAGES if word.lower() in title.lower()]
    candidates.sort(key=lambda pair: (pair[1] in used_pages, -len(pair[0])))
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

def clean_attribution(text):
    """Remove repetitive publisher lead-ins; source is displayed separately."""
    text = tidy(text)
    text = re.sub(r'^(?:[\w\u4e00-\u9fff]{2,22}\s*)?(?:\d{1,2}\s*月\s*\d{1,2}\s*日)?\s*消息[：:,，\s]+', '', text)
    text = re.sub(r'^(?:据|根据|来自)\s*[^，。:：]{1,28}?(?:报道|消息|披露|介绍|称|显示)[，,:：\s]+', '', text)
    text = re.sub(r'^(?:报道称|消息称|媒体报道)[，,:：\s]+', '', text)
    return text.strip()

def complete_excerpt(text, limit):
    """Never output a mechanically clipped sentence or an ellipsis."""
    text = clean_attribution(text).replace('…', '').replace('...', '')
    text = text.strip(' ，,；;：:')
    if len(text) <= limit:
        return text if text and not re.fullmatch(r'(?:\d{1,2}\s*月\s*\d{1,2}\s*日)?\s*消息[。.]?', text) else ''
    for sentence in re.split(r'(?<=[。！？!?])', text):
        if 12 <= len(sentence.strip()) <= limit:
            return sentence.strip()
    # Prefer a complete sentence or a short factual clause.
    for clause in re.split(r'[。！？!?；;，,]', text):
        clause = clause.strip()
        if 8 <= len(clause) <= limit - 1 and not clause.endswith(('将','并','和','与','及','为','在','对','称','表示')):
            return clause + '。'
    return ''

def short_headline(raw, limit=22):
    """Produce a short, complete factual statement, never an unfinished title."""
    title = clean_attribution(to_chinese(raw)).rstrip('。！？!?')
    title = re.split(r'\s+[-–—|｜]\s+', title)[0].strip()
    def fits(text):
        return sum(0.55 if char.isascii() else 1 for char in text) <= limit

    if fits(title) and not title.endswith(('将','并','和','与','及','为','在','对')):
        return title
    clauses = re.split(r'[，,；;。！？!?：:]', title)
    # Prefer the clause containing the actual AI event, not an introductory slogan.
    vague = ('面向先锋者','三年销售目标','从实验室走向工厂','反超','之后','新选择','完美风暴','价格泄露')
    actions = ('发布','推出','上线','开源','研发','落地','应用','训练','升级','部署','成立','合作','突破','进入','宣布','推出','采用')
    topics = ('AI','人工智能','模型','机器人','智能体','芯片','OpenAI','Claude','Gemini','华为','微软','谷歌','英伟达')
    candidates = [c.strip(' “”“‘’') for c in clauses]
    candidates = [c for c in candidates[:1] if len(c) >= 7 and fits(c)
                  and not c.endswith(('将','并','和','与','及','为','在','对','称','表示','之后','之前'))
                  and not any(v in c for v in vague)]
    informative = [c for c in candidates if any(v in c for v in actions)
                   and any(t.lower() in c.lower() for t in topics)]
    if informative: return informative[0]
    if candidates: return candidates[0]
    compressed = re.sub(r'（[^）]*）|\([^)]*\)', '', clauses[0])
    compressed = re.sub(r'(?:重磅|刚刚|首次|正式|最新|全新|重大|惊人|突然|宣布|成功)', '', compressed)
    compressed = re.sub(r'(开源权重模型|开放权重模型)', '开源模型', compressed)
    compressed = re.sub(r'(发布|推出)(?:一款)?(?:可跑在|可运行在|可在)[^，。]{2,14}?的(?=开源模型|大模型|生成模型)', r'\1', compressed)
    compressed = re.sub(r'“[^”]{6,}”', '', compressed).strip(' ，,：:；;')
    if len(compressed) >= 7 and fits(compressed) and not compressed.endswith(('将','并','和','与','及','为','在','对')):
        return compressed
    # Use an intentionally general but complete headline instead of cutting a claim mid-sentence.
    for actor in ('OpenAI','Anthropic','Google','微软','英伟达','Meta','苹果','华为','DeepSeek','阿里','腾讯','百度','宇树','NVIDIA'):
        if actor.lower() in title.lower():
            return actor + ('发布AI新进展' if '发布' in title or '推出' in title else '公布AI相关动态')
    return '人工智能领域出现新进展'

def publishable_title(row, limit=22):
    title = short_headline(row['title'], limit)
    if title in ('人工智能领域出现新进展','OpenAI发布AI新进展','Anthropic发布AI新进展','Google发布AI新进展','Meta发布AI新进展','英伟达公布AI相关动态'):
        return False
    if any(x in title for x in ('公布AI相关动态','发布AI新进展','高考','违规考生','数学问题','数学突破','价格已在网上泄露','完美风暴','预测翻转','开始赚AI的钱','算力供给承压','折叠方向盘','AI领路人','奇点的看法','营收目标')):
        return False
    # Reject slogans and unmatched quotation marks before they reach the poster.
    if any(x in title for x in ('·生态共融','具身智创','AI教母','短板变成爆品','活力和雄心')):
        return False
    if title.count('“') != title.count('”') or title.count('「') != title.count('」'):
        return False
    if title.count('‘') != title.count('’'):
        return False
    if title.startswith(('在“','在「','之后','关于')) or title.endswith(('之后','之前','推出','发布','宣布','称','表示','成功','突破','将','与','的')):
        return False
    return len(title) >= 8

def article_body(row, limit):
    title = to_chinese(row['title'].strip())
    desc = to_chinese(row.get('description', '').strip())
    if desc and len(re.findall('[一-鿿]', desc)) >= 12:
        result = complete_excerpt(desc, limit)
        if result: return result
    result = complete_excerpt(title.rstrip('。！？!?') + '。', limit)
    if result: return result
    return short_headline(title, min(limit-1, 28)) + '。'

def clip(s, n):
    """Keep existing non-news clipping behavior unchanged."""
    return s if len(s) <= n else s[:n-1] + '…'

people = [
    ('黄仁勋', '英伟达联合创始人'), ('奥尔特曼', 'OpenAI CEO'),
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
    ('Satya Nadella', '微软CEO'), ('Jensen Huang', '英伟达联合创始人'),
    ('Sam Altman', 'OpenAI CEO'), ('Fei-Fei Li', 'AI科学家'),
    ('Andrew Ng', 'AI教育与研究者'), ('François Chollet', 'AI研究者'),
    ('Ian Buck', '英伟达高性能计算副总裁'),
    ('Yossi Matias', 'Google Research负责人'),
]


name_roles = dict(people)
def identify_person(title):
    # Ignore SEO keyword suffixes rather than treating them as actual news subjects.
    title = re.split(r'[|｜](?:DeepSeek|AI|OpenAI|英伟达|限额|模型|人工智能|科技|新闻)(?:[|｜]|$)', title, maxsplit=1, flags=re.I)[0]
    title = re.sub(r'_[^\s]*新浪新闻.*$', '', title)
    for name, role in name_roles.items():
        if re.search(r'(?<![A-Za-z])' + re.escape(name) + r'(?![A-Za-z])', title, re.I):
            return name, role
    role_pattern = r'科学家|创始人|CEO|首席执行官|研究员|教授|董事长|负责人|总裁|工程师'
    speech = r'表示|指出|介绍|发布|分享|宣布|展示|：'
    match = re.search(rf'(?P<role>{role_pattern})\s*(?P<name>[\u4e00-\u9fff]{{2,3}})(?={speech})', title)
    if match:
        name = match['name']
        common_surnames = '赵钱孙李周吴郑王冯陈蒋沈韩杨朱秦许何吕张孔曹严华金魏陶姜谢邹苏潘葛范彭袁柳唐薛雷贺罗郝马苗凤方任俞柳史韦贾路江郭林钟徐邱骆高蔡田叶胡刘黄杜梁宋郑夏汪傅顾孟白熊阮邵崔程陆石侯邓洪龚'
        if name[0] in common_surnames:
            return name, match['role']
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
    # The person must be central to a concrete AI event, not finance, entertainment or a roundup.
    low = row['title'].lower()
    if any(x in low for x in ('crypto','xrp','ada ','bitcoin','token','poster','movie','film','marketing','trump','donat','acquisition','buyout','60,000','60000','丑闻','收购','海报','电影','捐款','加密货币','预测翻转')):
        return False
    if not relevant_ai_person_event(row['title']): return False
    if len(row.get('title','').strip()) < 9: return False
    if any(w in row['title'].lower() for w in ('博彩','竞猜','广告推广','sponsored content')): return False
    return True

def is_chinese_text(t):
    return len(re.findall('[一-鿿]',to_chinese(t))) >= 5


def person_specific_story(row, name):
    title = row['title']
    # 综合节目/资讯合集即使提到人名，也不是一条可核实的个人动态。
    if any(x in title.lower() for x in ('今日焦点','新闻速览','昨夜今晨','每日简报','一周回顾',
        '新闻联播','今日要闻','热点汇总','weekly roundup','news roundup')): return False
    if any(x in title for x in (' | ','｜','；',';')): return False
    # 姓名必须与该条AI事实在同一主题内，而非仅被顺带提及。
    return name.lower() in title.lower()

# Normalize bilingual names so the same person cannot occupy two slots.
PERSON_ALIASES = {'Jensen Huang':'黄仁勋','Satya Nadella':'纳德拉','Sam Altman':'奥尔特曼',
                  'Dario Amodei':'阿莫迪','Elon Musk':'马斯克','Sundar Pichai':'皮查伊',
                  'Demis Hassabis':'哈萨比斯','Fei-Fei Li':'李飞飞'}
def person_key(name):
    aliases = dict(PERSON_ALIASES, **{
        'Mark Zuckerberg': '扎克伯格', 'Yann LeCun': '杨立昆',
        'Geoffrey Hinton': '辛顿', 'Andrew Ng': '吴恩达',
    })
    return aliases.get(name, name).casefold()


def is_chinese_ready(row):
    t = to_chinese(row['title'])
    return len(re.findall('[一-鿿]', t)) >= 8 and len(re.findall(r'[A-Za-z]', t)) <= len(t)*0.55

# 先筛真正具有独立事实信息的摘要，再按新闻价值评分；Google News 标题列表不能算摘要。

# 英文自动翻译受限时，绝不把未翻译的英文长标题刊登为中文新闻。
# 从完整候选库补齐，兼顾国内、国际、不同媒体和不同事件。
def quality_gate(row):
    """Avoid weak syndicated sources and translation artifacts in published headlines."""
    source = row.get('source', '').lower()
    if any(x in source for x in ('风闻','财富号','金十','tempo.co','techgig','www.itsdw','中华网','万维读者','cryptonews','crypto briefing','biggo finance')):
        return False
    raw = row.get('title','').lower()
    if any(x in raw for x in ('vacation','resort','holiday','ipo','listed','million dollars','billion dollars','investor','fundraising','funding','market nervous','market jitters','math breakthrough','mathematical breakthrough')):
        return False
    if re.search(r'\b(?:stocks?|shares|trading|copper)\b', raw) or any(x in raw for x in ('retail investor',
                               'riemann','mathematical hypothesis','film trailer','movie poster',
                               'price plunge','selloff','financial market')):
        return False
    chinese = row['title']
    if any(x in chinese for x in ('散户投资','人类贸易','股市','股价','铜价','金属市场','黎曼猜想','度假村','度假','上市后','数学突破','特朗普账户','捐款','美元资助','电影海报')):
        return False
    if not any(x in raw for x in ('ai','artificial intelligence','model','robot','agent','chip','openai','anthropic','deepmind','nvidia','gemini','deepseek','人工智能','模型','机器人','芯片','智能体')):
        return False
    return True

def relevant_ai_news(row):
    """普通新闻必须直接讲AI技术、AI产品或AI应用，不能仅因提到AI市场预期入选。"""
    title = row['title'].lower()
    excluded = ('锡价','铜价','铝价','关税','跨境包裹','插混','燃油','混动','显卡驱动',
                'geforce hotfix','game ready','股价','股票','债券','基金','汇率','楼市',
                '房地产','融资轮','估值','价格暴跌','商品期货','金属','抛售','黎曼','数学猜想','riemann','hypothesis','selloff','metal','copper')
    if any(word in title for word in excluded): return False
    direct_ai = ('人工智能','大模型','语言模型','世界模型','智能体','ai应用','ai工具',
                 'ai模型','ai助手','ai芯片','ai机器人','机器人','机器学习',
                 '深度学习','生成式ai','生成式人工智能','deepseek','chatgpt',
                 'openai','anthropic','gemini','grok','claude','llm','agent',
                 'artificial intelligence','machine learning','ai agent',
                 'ai model','robotics','robot')
    return any(word in title for word in direct_ai) or bool(re.search(r'(?<![a-z])ai(?![a-z])',title))



EDITORIAL_BLOCKLIST = (
    '博彩', '竞猜', '投注平台', '在线登陆官网', '在线登录官网', '米乐app',
    'm6米乐', '现金网', '体育投注', 'betting', 'casino', 'gambling',
    '沙巴官网', '推广链接', '点击注册', '邀请码',
)


def render_png(data, news, leader_names):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=os.environ.get('HONG_BIN_CHROMIUM'))
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
            raise ValueError(error)
        png = page.locator('#poster').evaluate('(c) => c.toDataURL("image/png").split(",")[1]')
        browser.close()
        server.shutdown()
        server.server_close()
        return base64.b64decode(png)


EVENT_TERMS = (
    ('release', r'发布|推出|上线|开放|开源|\breleas\w*|\blaunch\w*|\bunveil\w*|\bintroduc\w*'),
    ('research', r'研究|论文|实验|突破|评测|基准|\bresearch\b|\bbenchmark\b|\bstudy\b'),
    ('deployment', r'部署|接入|落地|应用于|用于|投入使用|\bdeploy\w*|\bintegrat\w*'),
    ('hardware', r'机器人|芯片|加速器|量产|\brobot\w*|\bchips?\b|\bgpus?\b'),
)
MODEL_TOKEN = re.compile(r'(?<![a-z0-9])(?:gpt|gemini|claude|qwen|deepseek|grok|kimi|glm|llama|inkling)'
                         r'[ -]?(?:[a-z]+[ -]+)?\d+(?:\.\d+)*[a-z]?(?![a-z0-9])', re.I)
LOW_SUBSTANCE = re.compile(r'论坛|双店|门店|开业|展位|亮相.{0,10}(?:展|会)|参展|签约仪式|座谈|'
                           r'推广|软文|赞助内容|抽奖|福利券|\bsponsored\b|\bgiveaway\b', re.I)
PERSON_ACTION = re.compile(r'发布|展示|证实|解释|指出|宣布|表示|提出|开源|分享|介绍|'
                           r'\b(?:announc|publish|show|demonstrat|discuss|explain|say|said|present|launch|introduc|reveal)\w*', re.I)
UNCONFIRMED = re.compile(r'未经证实|尚无证据|尚待验证|有传言|疑似|消息人士|爆料|泄露|援引招聘信息|'
                         r'\b(?:unverified|unconfirmed|rumou?r|allegedly|leak|ouster)\w*', re.I)
BRIEFING_ROLES = {'AI科学家', 'AI研究者', 'AI企业家', 'AI教育与研究者', '机器人研究者', '计算机科学家'}
ROLE_CONTEXT = r'科学家|创始人|CEO|首席|教授|研究员|工程师|董事长|负责人|总裁|researcher|professor|founder|chief|engineer'


def event_profile(title):
    return {label for label, pattern in EVENT_TERMS if re.search(pattern, title, re.I)}


def model_tokens(title):
    return {re.sub(r'[\s-]', '', m.group().lower()) for m in MODEL_TOKEN.finditer(title)}


def duplicate_event(row, previous):
    previous_url = previous.get('canonical_url') or canonical_url(previous.get('article_url'))
    if row['key'] == previous.get('key') or row['canonical_url'] == previous_url:
        return True
    earlier = previous.get('title', '')
    current = row['title']
    models = model_tokens(current)
    earlier_models = model_tokens(earlier)
    current_events, earlier_events = event_profile(current), event_profile(earlier)
    if similar(current, earlier):
        # A distinct model version or a confirmed deployment following a release can advance a story.
        if models and earlier_models and models.isdisjoint(earlier_models):
            return False
        if row['official'] and 'deployment' in current_events and 'deployment' not in earlier_events:
            return False
        return True
    return bool(models & earlier_models and current_events & earlier_events & {'release', 'deployment'})


def blocked(row):
    text = ' '.join(row.get(k, '') for k in ('title', 'original_title', 'description', 'source', 'body'))
    lowered = text.lower()
    return (any(term in lowered for term in EDITORIAL_BLOCKLIST)
            or bool(LOW_SUBSTANCE.search(text) or UNCONFIRMED.search(text))
            or not editorial_eligible(row))


def editorial_rank(row):
    events = event_profile(row['title'])
    return (score(row) + 10 * row['official'] + 5 * len(events)
            + min(6, row['heat'] / 16) - min(12, row['age_hours'] / 16))


def short_body(row, limit):
    if not valid_description(row):
        return ''
    text = to_chinese(row['description'])
    title = to_chinese(row['title'])
    for sentence in re.split(r'(?<=[。！？])|(?<=[.!?])\s+', text):
        body = complete_excerpt(sentence, limit)
        if (body and len(re.findall(r'[\u4e00-\u9fff]', body)) >= 8 and not similar(body, title)
                and not body.startswith(('据', '随着', '为了', '除了', '当时', '在此背景下', '其中'))):
            return body
    return ''


def enrich(row):
    if valid_description(row):
        return row
    summary = article_meta_summary(row['article_url'])
    return {**row, 'description': summary} if summary else row


def choose_people(rows, history):
    people_cards, selected, names = [], [], []
    for row in rows:
        name, role = identify_person(row['title'])
        story = row
        if not name or not PERSON_ACTION.search(row['title']):
            lead = re.split(r'(?<=[。！？])|(?<=[.!?])\s+', row['description'], maxsplit=1)[0]
            lead_name, lead_role = identify_person(lead)
            if lead_name in name_roles and PERSON_ACTION.search(lead):
                name, role = lead_name, lead_role
                story = {**row, 'title': lead}
        action = PERSON_ACTION.search(story['title'])
        subject = story['title'].casefold().find(name.casefold()) if name else -1
        if (not name or not action or subject < 0 or not 0 < action.start() - subject <= len(name) + 20
                or person_key(name) in {person_key(n) for n in names}
                or blocked(row) or not eligible_person_report(story)
                or not person_specific_story(story, name) or not PERSON_ACTION.search(story['title'])
                or any(duplicate_event(row, other) for other in history + selected)):
            continue
        row = enrich(row)
        if blocked(row):
            continue
        if role in BRIEFING_ROLES and not re.search(ROLE_CONTEXT, row['title'] + row['description'], re.I):
            continue
        if not is_chinese_text(story['title']) or not publishable_title(story, 23):
            continue
        body = short_body(row, 54)
        headline = short_headline(story['title'], 23)
        if similar(headline, body):
            continue
        if not body:
            continue
        stamp = parse_date(row['published_at']).astimezone(BEIJING).strftime('%m-%d %H:%M')
        people_cards.append({
            'name': name + '｜' + role, 'title': headline, 'body': body,
            'source': row['source'] + ' · ' + stamp, 'published_at': row['published_at'],
            'url': row['article_url'], 'discovered_via': row['discovered_via'],
        })
        selected.append(row)
        names.append(name)
        if len(selected) == 3:
            break
    return people_cards, selected, names


def choose_news(rows, history, people_rows):
    selected, cards = [], []
    eligible = [r for r in rows if in_window(r, NEWS_HOURS) and not blocked(r)
                and relevant_ai_news(r) and quality_gate(r) and event_profile(r['title'])]
    # Reserve one slot for each region when publishable, then prefer different publishers.
    for region, distinct in [('international', True), ('CN', True)] + [(None, True)] * 4 + [(None, False)] * 4:
        if len(selected) == 4:
            break
        for row in eligible:
            if region and row['region'] != region:
                continue
            if distinct and row['source'] in {item['source'] for item in selected}:
                continue
            if any(duplicate_event(row, other) for other in history + people_rows + selected):
                continue
            title_limit, body_limit = (32, 73) if not selected else (22, 48)
            if not is_chinese_ready(row) or not publishable_title(row, title_limit):
                continue
            headline = short_headline(row['title'], title_limit)
            # Abbreviating a long headline must retain the technology and the event.
            if not relevant_ai_news({'title': headline}) or not event_profile(headline):
                continue
            row = enrich(row)
            if blocked(row):
                continue
            body = short_body(row, body_limit)
            if not body or similar(headline, body):
                continue
            cards.append({
                'title': headline, 'body': body,
                'source': row['source'], 'url': row['article_url'],
                'published_at': row['published_at'], 'discovered_via': row['discovered_via'],
            })
            selected.append(row)
            break
    return cards, selected


def collect():
    queries = [
        ('AI 模型 发布 机器人 芯片', False, False),
        ('AI robotics models research chip launch', False, True),
        ('AI 创始人 科学家 发布 机器人', True, False),
        ('AI CEO scientist announces robot models', True, True),
        ('黄仁勋 奥尔特曼 李飞飞 梁文锋 杨植麟 人工智能', True, False),
    ]
    for source in SOURCE_REGISTRY:
        if source['type'] == 'web' and source.get('official'):
            domain = source.get('domain', host(source['url']))
            queries.append((f'site:{domain} AI', True, source['region'] != 'CN'))
    stats, rows = {}, []
    with ThreadPoolExecutor(max_workers=12) as pool:
        aihot = pool.submit(aihot_candidates)
        feeds = [(source['name'], pool.submit(direct_feed, source)) for source in FEEDS]
        searches = [(q[0], pool.submit(search, *q)) for q in queries]
        for name, future in feeds + searches:
            try:
                value = future.result()
                gathered, raw = value if isinstance(value, tuple) else (value, None)
                stats[name] = {'raw_feed_entries': raw, 'valid_18h': sum(in_window(r, NEWS_HOURS) for r in gathered),
                               'valid_person_window': len(gathered)}
                rows.extend(gathered)
            except (OSError, ValueError, ET.ParseError) as exc:
                stats[name] = {'error': str(exc)[:200]}
        aihot_rows, aihot_stats = aihot.result()
    rows.extend(aihot_rows)
    stats['AIHOT'] = aihot_stats
    ranked = sorted(rows, key=editorial_rank, reverse=True)
    by_url = {}
    for row in ranked:
        by_url.setdefault(row['canonical_url'], row)
    return sorted(by_url.values(), key=editorial_rank, reverse=True), stats


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def final_problems(news_cards, people_cards):
    problems = []
    if len(news_cards) != 4 or len(people_cards) != 3:
        problems.append(f'栏目数量不足：新闻 {len(news_cards)}/4，人物 {len(people_cards)}/3')
    for card in news_cards + people_cards:
        if not all(card.get(key) for key in ('title', 'body', 'source', 'url', 'published_at')):
            problems.append(f'缺少标题、正文或溯源字段：{card.get("title")}')
        if not publisher_for(card['url']) or blocked(card):
            problems.append(f'来源或正文未通过检查：{card["title"]}')
    return problems


def main():
    parser = ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--test', action='store_true', help='isolated output, read-only published history (default)')
    mode.add_argument('--formal', action='store_true', help='write validated output and update history')
    parser.add_argument('--as-of', help='timezone-aware reference time (test mode only)')
    args = parser.parse_args()
    test_mode = not args.formal
    global NOW
    if args.as_of:
        when = parse_date(args.as_of)
        if not test_mode or not when:
            parser.error('--as-of requires test mode and a timezone-aware timestamp')
        NOW = when
    today = NOW.astimezone(BEIJING).date().isoformat()
    state = json.loads(HISTORY.read_text('utf-8')) if HISTORY.exists() else {'last_issue': 2, 'items': []}
    same_day = any(item.get('date') == today for item in state['items'][-7:])
    issue = str(int(state.get('last_issue', 2)) + (0 if same_day else 1)).zfill(3)
    history = [item for item in state['items'] if item.get('date', '') < today]
    out = ROOT / ('test-output' if test_mode else 'output')
    out.mkdir(exist_ok=True)
    rows, stats = collect()
    eligible_history = [row for row in rows if not any(duplicate_event(row, old) for old in history)]
    people_cards, people_rows, names = choose_people(eligible_history, [])
    news_cards, news_rows = choose_news(eligible_history, [], people_rows)
    problems = final_problems(news_cards, people_cards)
    selected = news_rows + people_rows
    stats['AIHOT']['selected_news'] = sum(row['discovered_via'] == 'AIHOT' for row in news_rows)
    stats['AIHOT']['selected_people'] = sum(row['discovered_via'] == 'AIHOT' for row in people_rows)
    report = {
        'date': today, 'as_of_utc': NOW.isoformat(), 'issue_preview': issue,
        'sources': stats, 'unique_candidates': len(rows), 'history_rejected': len(rows) - len(eligible_history),
        'news_selected': news_cards, 'people_selected': people_cards,
        'candidates': eligible_history[:40], 'problems': problems,
    }
    print(json.dumps({key: report[key] for key in ('date', 'unique_candidates', 'history_rejected', 'problems')},
                     ensure_ascii=False))
    print('AIHOT:', json.dumps(stats['AIHOT'], ensure_ascii=False))
    if not problems:
        data = {
            'issue': issue, 'date': today, 'headline': news_cards[0],
            'updates': [dict(card, tag=tag) for tag, card in zip(['AI动态', '产品应用', '前沿科技'], news_cards[1:])],
            'leaders': people_cards,
        }
        try:
            png = render_png(data, news_rows, names)
        except Exception as exc:
            problems.append(f'固定模板出图失败：{exc}')
    report['status'] = 'needs_editorial_review' if problems else 'ready'
    if test_mode or problems:
        write_json(out / 'editorial_review.json', report)
    if problems:
        print('待编辑审核：' + '；'.join(problems))
        return 2
    with TemporaryDirectory(dir=out) as directory:
        stage = Path(directory)
        write_json(stage / 'daily.json', data)
        (stage / '洪滨每日AI早报.png').write_bytes(png)
        if not test_mode:
            state['last_issue'] = int(issue)
            state['items'] = (state['items'] + [
                {key: row[key] for key in ('key', 'title', 'article_url', 'canonical_url')} | {'date': today}
                for row in selected if not any(old.get('key') == row['key'] and old.get('date') == today
                                               for old in state['items'])
            ])[-500:]
            write_json(stage / 'published_history.json', state)
        for name in ('daily.json', '洪滨每日AI早报.png'):
            (stage / name).replace(out / name)
        if not test_mode:
            (stage / 'published_history.json').replace(HISTORY)
    print('已生成', out / '洪滨每日AI早报.png')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
