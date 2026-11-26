import base64, datetime as dt, hashlib, html, json, re, urllib.parse, urllib.request, io
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from PIL import Image
import xml.etree.ElementTree as ET
from pathlib import Path

from argparse import ArgumentParser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from itertools import combinations
import os
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent
BEIJING = dt.timezone(dt.timedelta(hours=8))
NEWS_HOURS = 18
PERSON_HOURS = 48
PERSON_FALLBACK_HOURS = 96
ISSUE_START_DATE = dt.date(2026, 11, 23)
NOW = dt.datetime.now(dt.timezone.utc)
HISTORY = ROOT / 'published_history.json'


# 仅调整新闻筛选；原网页模板、图标、画布与栏目布局均保持不变。
EXCLUDE = ('股票', '股价', '炒股', '基金', '投资建议', '科普周', '洽谈会', '峰会', '大会召开', '奖励大会', '书讯', '据称', '传闻', '网传', '出事了', '震惊', '重磅来袭', '十大热词', '培训', '培训机构', '横向测评', '辞职', '利润翻倍', '亏超', '新媒体创作', '课程体系', '技能培训班', '抵押的AI芯片')
SIGNALS = ('发布', '推出', '上线', '开源', '突破', '模型', '机器人', '芯片', '智能体', 'Agent', '研发', '科研', '应用', '功能', '升级', '世界模型', '编程', '自动化', 'AI', 'artificial intelligence', 'robot', 'chip', 'research', 'launch', 'model', 'agent', 'breakthrough')
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
        'q': q + (' when:2d' if leader else ' when:1d'),
        'hl': 'en-US' if international else 'zh-CN', 'gl': 'US' if international else 'CN',
        'ceid': 'US:en' if international else 'CN:zh-Hans',
    })
    root = ET.fromstring(fetch('https://news.google.com/rss/search?' + params))
    rows = []
    entries = root.findall('.//item')
    for item in entries[:8]:
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
        meta = publisher_article(link) if link else None
        row = candidate(meta[0], link, meta[1], meta[2], 'Google News') if meta else None
        if row and in_window(row, PERSON_FALLBACK_HOURS if leader else NEWS_HOURS):
            rows.append(row)
    return rows, len(entries)


class ArticleMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta = {}
        self.links = []
        self.paragraphs = []
        self.article_paragraphs = []
        self._article_depth = 0
        self._paragraph = None

    def handle_starttag(self, tag, attrs):
        if tag == 'article':
            self._article_depth += 1
        if tag == 'p':
            self._paragraph = []
        if tag == 'meta':
            values = dict(attrs)
            key = values.get('property') or values.get('name')
            if key and values.get('content'):
                self.meta[key.lower()] = values['content']
        elif tag == 'a':
            url = dict(attrs).get('href')
            if url:
                self.links.append(url)

    def handle_data(self, data):
        if self._paragraph is not None:
            self._paragraph.append(data)

    def handle_endtag(self, tag):
        if tag == 'p' and self._paragraph is not None:
            value = tidy(''.join(self._paragraph))
            if len(value) >= 18:
                self.paragraphs.append(value)
                if self._article_depth:
                    self.article_paragraphs.append(value)
            self._paragraph = None
        elif tag == 'article' and self._article_depth:
            self._article_depth -= 1


def article_meta_summary(url):
    article = publisher_article(url)
    return article[2] if article else ''


@lru_cache(maxsize=256)
def publisher_article(url):
    source = publisher_for(url)
    if not source:
        return None
    try:
        req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0 (compatible; HongbinDaily/1.0)'})
        with urllib.request.urlopen(req, timeout=4) as resp:
            redirect_source = publisher_for(resp.geturl())
            if not redirect_source or redirect_source['name'] != source['name']:
                return None
            page = resp.read(950_000).decode(resp.headers.get_content_charset('utf-8'), 'replace')
        parser = ArticleMetadata()
        parser.feed(page)
        meta = parser.meta
        title = tidy(meta.get('og:title') or meta.get('twitter:title') or meta.get('title'))
        date = next((meta[k] for k in ('article:published_time', 'og:published_time', 'pubdate',
                                      'publishdate', 'datepublished', 'dc.date.issued') if meta.get(k)), '')
        if not date:
            match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', page)
            date = match[1] if match else ''
        if date and not parse_date(date) and source.get('region') == 'CN':
            # Domestic publishers sometimes omit the zone from a full local timestamp.
            if re.fullmatch(r'\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d)?', date):
                date += '+08:00'
        description = tidy(meta.get('og:description') or meta.get('description'))[:600]
        paragraphs = parser.article_paragraphs or parser.paragraphs
        return title, date, description, tuple(paragraphs[:14])
    except (OSError, ValueError, LookupError):
        return None


SOURCE_CONFIG = json.loads((ROOT / 'news_sources.json').read_text('utf-8'))
SOURCE_REGISTRY = SOURCE_CONFIG['sources']
DISCOVERY = {source['name']: source for source in SOURCE_REGISTRY if source['category'] == 'discovery'}
PUBLISHERS = [source for source in SOURCE_REGISTRY if source['category'] != 'discovery']

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
    for source in PUBLISHERS:
        domain = source.get('domain', host(source['url']))
        prefix = source.get('path_prefix')
        domains = [domain, *source.get('domain_aliases', [])]
        if any(expected and domain_matches(hostname, expected) for expected in domains) and (not prefix or path.startswith(prefix)):
            return source
    if approved := reviewed_person_event(url):
        return {'name': approved[1]['source'], 'category': 'official'}
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


def candidate(title, url, published, description, via):
    title = tidy(title)
    normalized_url = canonical_url(url)
    when = parse_date(published)
    source = publisher_for(url) if normalized_url else None
    if not (title and source and when):
        return None
    return {
        'title': title, 'source': source['name'], 'key': norm(title),
        'article_url': url, 'canonical_url': normalized_url, 'published_at': when.isoformat(),
        'published_at_beijing': when.astimezone(BEIJING).isoformat(),
        'age_hours': (NOW - when).total_seconds() / 3600,
        'description': tidy(description)[:600], 'discovered_via': via,
        'region': 'CN' if source.get('region') == 'CN' else 'international',
        'category': source['category'],
    }


def in_window(row, hours):
    published = parse_date(row.get('published_at'))
    if published is None:
        return False
    local_age = NOW.astimezone(BEIJING) - published.astimezone(BEIJING)
    return dt.timedelta() <= local_age <= dt.timedelta(hours=hours)


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
        description = (item.findtext('description') or item.findtext(f'{ns}summary')
                       or item.findtext(f'{ns}content') or '')
        row = candidate(item.findtext('title') or item.findtext(f'{ns}title'),
                        urllib.parse.urljoin(source['url'], link) if link else '',
                        date, description, 'direct')
        if row and in_window(row, PERSON_FALLBACK_HOURS):
            rows.append(row)
    return rows, len(entries)


ARTICLE_LINK = re.compile(
    r'/\d{4}/\d\d/|/20\d\d-\d\d-|/(?:posts?|articles?|news|research|blog|blogs|story|stories|updates|a)/[^/?#]{5,}'
    r'|/\d{5,}\.(?:s?html)|/\d/\d{3}/\d{3}\.htm|/article-[^/]+|/(?:p|detail)/\d{5,}', re.I)


def web_source(source, url=None):
    listing = url or source['url']
    parser = ArticleMetadata()
    parser.feed(fetch(listing, timeout=6).decode('utf-8', 'replace'))
    links = {}
    article_pattern = re.compile(source['article_path'], re.I) if source.get('article_path') else ARTICLE_LINK
    for href in parser.links:
        link = urllib.parse.urljoin(listing, href)
        normalized = canonical_url(link)
        if normalized and publisher_for(link) == source and article_pattern.search(urllib.parse.urlsplit(link).path):
            if normalized != canonical_url(listing):
                links.setdefault(normalized, link)
    if not links:
        return [], 0
    rows = []
    for position, link in enumerate(list(links.values())[:6], start=1):
        metadata = publisher_article(link)
        if metadata:
            title, date, description = metadata[:3]
            row = candidate(title, link, date, description, 'web')
            if row and in_window(row, PERSON_FALLBACK_HOURS):
                row['publisher_position'] = position
                rows.append(row)
    return rows, len(links)


def hacker_news():
    ids = json.loads(fetch(DISCOVERY['Hacker News']['url']))
    item_base = 'https://hacker-news.firebaseio.com/v0/item/'
    rows = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(fetch, f'{item_base}{identifier}.json', timeout=4)
                   for identifier in ids[:18] if type(identifier) is int]
        for future in futures:
            try:
                item = json.loads(future.result()) or {}
                link = item.get('url', '')
                meta = publisher_article(link) if link and publisher_for(link) else None
                row = candidate(meta[0], link, meta[1], meta[2], 'Hacker News') if meta else None
                if row and in_window(row, PERSON_FALLBACK_HOURS):
                    rows.append(row)
            except (OSError, ValueError):
                continue
    return rows, len(futures)


def github_trending():
    parser = ArticleMetadata()
    parser.feed(fetch(DISCOVERY['GitHub Trending']['url']).decode('utf-8', 'replace'))
    repositories = {href.lower() for href in parser.links if re.fullmatch(r'/[\w.-]+/[\w.-]+', href)}
    rows = []
    for source in PUBLISHERS:
        if source['type'] != 'atom' or host(source['url']) != 'github.com':
            continue
        repo = urllib.parse.urlsplit(source['url']).path.removesuffix('/releases.atom').lower()
        if repo in repositories:
            discovered, _ = direct_feed(source)
            rows.extend(dict(row, discovered_via='GitHub Trending') for row in discovered)
    return rows, len(repositories)


def parse_aihot_item(item):
    if not isinstance(item, dict) or not isinstance(item.get('links'), dict):
        return None
    title = next((item[k] for k in ('title', 'originalTitle') if isinstance(item.get(k), str) and item[k]), '')
    summary = item.get('summary')
    row = candidate(title, item['links'].get('original'), item.get('publishedAt'),
                    summary if isinstance(summary, str) else '', 'AIHOT')
    if row and isinstance(item.get('originalTitle'), str):
        row['original_title'] = tidy(item['originalTitle'])
    return row


def aihot_page_rows(mode, max_pages):
    endpoint = DISCOVERY['AIHOT']['url']
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
                if in_window(row, PERSON_FALLBACK_HOURS):
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


def aihot_hot_topics():
    endpoint = DISCOVERY['AIHOT']['url']
    try:
        items = json.loads(fetch(urllib.parse.urljoin(endpoint, '/api/v1/hot-topics'))).get('items', [])
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(items, list):
        return {}
    topics = [item for item in items[:10] if isinstance(item, dict) and type(item.get('rank')) is int
              and 1 <= item['rank'] <= 10 and (when := parse_date(item.get('latestAt'))) and when <= NOW]

    def topic_links(topic):
        links = [topic.get('links') or {}]
        public_id = topic.get('storyPublicId', '')
        if isinstance(public_id, str) and re.fullmatch(r'[0-9a-f-]{36}', public_id):
            url = urllib.parse.urljoin(endpoint, '/api/v1/stories/' + public_id)
            try:
                story = json.loads(fetch(url, timeout=5))['story']
                links.extend(report.get('links') or {} for report in story.get('reports', [])
                             if isinstance(report, dict))
            except (OSError, ValueError, KeyError, AttributeError, TypeError):
                pass
        return topic, {canonical_url(link.get('original')) for link in links if isinstance(link, dict)} - {''}

    result = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        for topic, links in pool.map(topic_links, sorted(topics, key=lambda item: item['rank'])):
            for link in links:
                result.setdefault(link, {key: topic[key] for key in ('rank', 'participantCount', 'sourceCount')
                                         if type(topic.get(key)) is int and topic[key] >= 0})
    return result



MEDIA = json.loads((ROOT / 'editorial_media.json').read_text('utf-8'))
GENERIC_IMAGE = re.compile(r'(?:^|[/_.-])(?:logo|placeholder|default|avatar|favicon|site-icon)(?:[/_.-]|$)', re.I)
BUSY_COVER = re.compile(r'benchmark|chart\b|bar.graph|headquarters|company.building|评测图|柱状图|总部大楼', re.I)


def reviewed_person_event(url):
    target = canonical_url(url)
    if target:
        for profile in MEDIA['people']:
            for event in profile.get('verified_events', []):
                if canonical_url(event['url']) == target:
                    return profile, event
    return None


def verified_person(name):
    target = person_key(name)
    today = NOW.astimezone(BEIJING).date()
    for profile in MEDIA['people']:
        if target not in {person_key(n) for n in [profile['name'], *profile.get('aliases', [])]}:
            continue
        checked = dt.date.fromisoformat(profile['verified_on'])
        if abs((today - checked).days) <= 180:
            return profile
    return None


@lru_cache(maxsize=80)
def download_image(url, contain=False):
    if not url.startswith('https://'):
        return None
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; HongbinDaily/1.0)'})
        with urllib.request.urlopen(req, timeout=7) as response:
            if not response.geturl().startswith('https://'):
                return None
            blob = response.read(8_000_001)
        if len(blob) > 8_000_000:
            return None
        with Image.open(io.BytesIO(blob)) as source:
            if min(source.size) < 150 or max(source.size) > 8000:
                return None
            picture = source.convert('RGBA')
        if contain:
            picture.thumbnail((684, 410), Image.Resampling.LANCZOS)
            canvas = Image.new('RGB', (720, 478), 'white')
            offset = ((canvas.width - picture.width) // 2, (canvas.height - picture.height) // 2)
            canvas.paste(picture, offset, picture)
            picture = canvas
        else:
            picture = picture.convert('RGB')
            picture.thumbnail((720, 720))
        buffer = io.BytesIO()
        picture.save(buffer, format='JPEG', quality=88)
        bytes_out = buffer.getvalue()
        return 'data:image/jpeg;base64,' + base64.b64encode(bytes_out).decode('ascii'), hashlib.sha256(bytes_out).hexdigest()
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


@lru_cache(maxsize=40)
def article_images(url):
    """Use media attached to the verified original article, never a company-stock image lookup."""
    source = publisher_for(url)
    if not source:
        return []
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; HongbinDaily/1.0)'})
        with urllib.request.urlopen(request, timeout=6) as response:
            if publisher_for(response.geturl()) != source:
                return []
            final_url = response.geturl()
            page = response.read(950_000).decode(response.headers.get_content_charset('utf-8'), 'replace')
        parser = ArticleMetadata()
        parser.feed(page)
        if parser.meta.get('og:type', '').lower() not in ('article', 'news.article') and not publisher_article(url):
            return []
        alt = parser.meta.get('og:image:alt', '')
        return [
            {'url': urllib.parse.urljoin(final_url, value), 'evidence': url}
            for key in ('og:image:secure_url', 'og:image', 'twitter:image')
            if (value := parser.meta.get(key)) and not GENERIC_IMAGE.search(urllib.parse.urlsplit(value).path)
            and not BUSY_COVER.search(urllib.parse.urlsplit(value).path + ' ' + alt)
        ]
    except (OSError, ValueError, LookupError):
        return []


def media_candidates(card, person):
    portraits = []
    if person:
        profile = verified_person(person)
        if profile:
            portraits = [dict(image, kind='portrait') for image in profile.get('images', [])]
    url = card.get('article_url') or card.get('url', '')
    if not url:
        return portraits
    for entry in MEDIA['articles']:
        if canonical_url(entry['article']) == canonical_url(url):
            return portraits + [dict(image, kind='article_image') for image in entry['images'] + article_images(url)]
    return portraits + [dict(image, kind='article_image') for image in article_images(url)]


def prepare_images(news, leaders):
    slots = list(zip(('headline', 'u0', 'u1', 'u2'), news, [''] * 4))
    slots += [(f'l{i}', card, card['person']) for i, card in enumerate(leaders)]
    ready, evidence, hashes = {}, {}, set()
    with ThreadPoolExecutor(max_workers=7) as pool:
        references = [(slot, card, person, pool.submit(media_candidates, card, person)) for slot, card, person in slots]
        used_urls = set()
        for slot, card, person, future in references:
            attempted = []
            for candidate in future.result():
                url = candidate['url']
                if url in used_urls or url in attempted:
                    continue
                attempted.append(url)
                image = download_image(url, contain=candidate.get('fit') == 'contain')
                if image and image[1] not in hashes:
                    ready[slot] = image[0]
                    hashes.add(image[1])
                    used_urls.add(url)
                    evidence[slot] = {**candidate, 'status': 'ready', 'attempts': attempted}
                    break
            if slot not in ready:
                evidence[slot] = {'status': 'pending', 'candidate': person or card.get('title'),
                                  'article': card.get('article_url') or card.get('url'),
                                  'attempts': attempted}
    return ready, {'downloaded': len(ready), 'selected': len(slots), 'required': 7, 'slots': evidence}


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

def text_width(text):
    return sum(0.55 if char.isascii() else 1 for char in text)


def complete_excerpt(text, limit):
    """Never output a mechanically clipped sentence or an ellipsis."""
    text = clean_attribution(text).replace('…', '').replace('...', '')
    text = text.strip(' ，,；;：:')
    if text_width(text) <= limit:
        return text if text and not re.fullmatch(r'(?:\d{1,2}\s*月\s*\d{1,2}\s*日)?\s*消息[。.]?', text) else ''
    for sentence in re.split(r'(?<=[。！？!?])', text):
        if len(sentence.strip()) >= 12 and text_width(sentence.strip()) <= limit:
            return sentence.strip()
    # Prefer a complete sentence or a short factual clause.
    for clause in re.split(r'[。！？!?；;，,]', text):
        clause = clause.strip()
        if len(clause) >= 8 and text_width(clause) <= limit - 1 and not clause.endswith(('将','并','和','与','及','为','在','对','称','表示')):
            return clause + '。'
    return ''

def short_headline(raw, limit=22):
    """Produce a short, complete factual statement, never an unfinished title."""
    title = clean_attribution(to_chinese(raw)).rstrip('。！？!?')
    title = re.split(r'\s+[-–—|｜]\s+', title)[0].strip()
    title = re.sub(r'\bGoogle\s+Gemini\b', 'Gemini', title, flags=re.I)
    title = re.sub(r'\bComputer Use\b', '电脑操作', title, flags=re.I)
    if text_width(title) <= limit and not title.endswith(('将','并','和','与','及','为','在','对')):
        return title
    clauses = re.split(r'[，,；;。！？!?：:]|并(?=推出|发布|宣布|开放)', title)
    # Prefer the clause containing the actual AI event, not an introductory slogan.
    vague = ('面向先锋者','三年销售目标','从实验室走向工厂','反超','之后','新选择','完美风暴','价格泄露')
    actions = ('发布','推出','上线','开源','研发','落地','应用','训练','升级','部署','成立','合作','突破','进入','宣布','推出','采用')
    topics = ('AI','人工智能','模型','机器人','智能体','芯片','OpenAI','Claude','Gemini','华为','微软','谷歌','英伟达')
    candidates = [c.strip(' “”“‘’') for c in clauses]
    candidates = [c for c in candidates[:1] if len(c) >= 7 and text_width(c) <= limit
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
    if len(compressed) >= 7 and text_width(compressed) <= limit and not compressed.endswith(('将','并','和','与','及','为','在','对')):
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
    ('Robby Stein', 'Google搜索产品副总裁'),
    ('Thibault Sottiaux', 'OpenAI产品负责人'),
]


name_roles = dict(people)
name_roles.update({person['name']: person['role'] for person in SOURCE_CONFIG['person_watchlist']})
name_roles.update({name: item['role'] for item in MEDIA['people']
                   for name in [item['name'], *item.get('aliases', [])]})
def identify_person(title):
    # Ignore SEO keyword suffixes rather than treating them as actual news subjects.
    title = re.split(r'[|｜](?:DeepSeek|AI|OpenAI|英伟达|限额|模型|人工智能|科技|新闻)(?:[|｜]|$)', title, maxsplit=1, flags=re.I)[0]
    title = re.sub(r'_[^\s]*新浪新闻.*$', '', title)
    role_pattern = r'首席科学家|科学家|创始人|CEO|首席执行官|研究员|教授|董事长|负责人|总裁|工程师'
    english_role = r'CEO|chief executive(?: officer)?|chief scientist|(?:co-)?founder|professor|researcher|engineer'
    for name in name_roles:
        match = re.search(r'(?<![A-Za-z])' + re.escape(name) + r'(?![A-Za-z])', title, re.I)
        if match:
            prefix = title[:match.start()]
            role = re.search(rf'(?:{role_pattern}|{english_role})\s*$', prefix, re.I)
            return name, role.group().strip() if role else ''
    speech = r'表示|指出|介绍|发布|分享|宣布|展示|谈|回应|获任|离任|[:：，、,。]|$'
    match = re.search(rf'(?P<role>{role_pattern})\s*(?P<name>[\u4e00-\u9fff]{{2,3}})(?=\s*(?:{speech}))', title)
    if match:
        name = match['name']
        common_surnames = '赵钱孙李周吴郑王冯陈蒋沈韩杨朱秦许何吕张孔曹严华金魏陶姜谢邹苏潘葛范彭袁柳唐薛雷贺罗郝马苗凤方任俞柳史韦贾路江郭林钟徐邱骆高蔡田叶胡刘黄杜梁宋郑夏汪傅顾孟白熊阮邵崔程陆石侯邓洪龚'
        if name[0] in common_surnames:
            return name, match['role']
    match = re.search(rf'\b(?P<role>(?i:{english_role}))\s+(?P<name>[A-Z][a-z]+(?:[ -][A-Z][a-z]+){{1,2}})\b', title)
    if match:
        return match['name'], match['role']
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
    if not any(x in raw for x in ('ai','artificial intelligence','model','robot','agent','chip','openai','anthropic','deepmind','nvidia','gemini','deepseek','llm','qwen','混元','人工智能','模型','机器人','芯片','智能体')):
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


def render_png(data, assets):
    required = {'headline', 'u0', 'u1', 'u2', 'l0', 'l1', 'l2'}
    if set(assets) != required:
        raise ValueError('图片待处理：' + ', '.join(sorted(required - set(assets))))
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
        try:
            page.goto(f'http://127.0.0.1:{server.server_port}/index.html', wait_until='load')


            page.evaluate('document.fonts.ready')
            filled = page.evaluate("""async (assets) => {
                for (const [key, src] of Object.entries(assets)) {
                    const im = new Image();
                    im.src = src;
                    await im.decode();
                    images[key] = im;
                }
                render();
                return Object.keys(assets).filter(key => images[key]?.naturalWidth > 0).length;
            }""", assets)
            if filled != len(required):
                raise ValueError('画布未加载全部配图，停止导出')
            if not page.evaluate('globe.complete && globe.naturalWidth > 0'):
                raise RuntimeError('原版地球图标加载失败，停止导出')
            error = page.locator('#errors').inner_text()
            if error.strip():
                raise ValueError(error)
            png = page.locator('#poster').evaluate('(c) => c.toDataURL("image/png").split(",")[1]')
            return base64.b64decode(png)
        finally:
            browser.close()
            server.shutdown()
            server.server_close()


EVENT_TERMS = (
    ('release', r'发布|推出|上线|登陆|开放|开源|\breleas\w*|\blaunch\w*|\bunveil\w*|\bintroduc\w*'),
    ('research', r'研究|论文|实验|突破|评测|基准|\bresearch\b|\bbenchmark\b|\bstudy\b'),
    ('deployment', r'部署|接入|集成|落地|应用于|用于|投入使用|\bdeploy\w*|\bintegrat\w*'),
    ('hardware', r'机器人|芯片|加速器|量产|\brobot\w*|\bchips?\b|\bgpus?\b'),
    ('impact', r'请愿|就业|无障碍|课堂|消防|\bpetition\w*|\bfirefight\w*|\baccessibility\b'),
)
MODEL_TOKEN = re.compile(r'(?<![a-z0-9])(?:gpt|gemini|claude|qwen|deepseek|grok|kimi|glm|llama|inkling)'
                         r'[ -]?(?:[a-z]+[ -]+)?\d+(?:\.\d+)*[a-z]?(?![a-z0-9])', re.I)
LOW_SUBSTANCE = re.compile(r'论坛|双店|门店|开业|展位|亮相.{0,10}(?:展|会)|参展|签约仪式|座谈|'
                           r'推广|软文|赞助内容|抽奖|福利券|\bsponsored\b|\bgiveaway\b', re.I)
PERSON_ACTION = re.compile(r'发布|展示|证实|解释|指出|宣布|表示|提出|开源|分享|介绍|'
                           r'重申|邀请|透露|\b(?:announc|publish|show|demonstrat|discuss|explain|say|said|present|launch|introduc|reveal|reiterat)\w*', re.I)
UNCONFIRMED = re.compile(r'未经证实|尚无证据|尚待验证|有传言|疑似|消息人士|爆料|泄露|援引招聘信息|'
                         r'\b(?:unverified|unconfirmed|rumou?r|allegedly|leak|ouster)\w*', re.I)
MARKET_ONLY = re.compile(r'股价|股市|股票.{0,5}抛售|投资者|万亿估值|\bI\.?P\.?O\.?\b|'
                         r'\b(?:shares|stocks?)\b.{0,30}\b(?:fall|drop|jump|soar|sell|trading)\w*', re.I)
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
        if row.get('category') == 'official' and 'deployment' in current_events and 'deployment' not in earlier_events:
            return False
        return True
    return bool(models & earlier_models and current_events & earlier_events & {'release', 'deployment'})


def blocked(row):
    text = ' '.join(row.get(k, '') for k in ('title', 'original_title', 'description', 'source', 'body'))
    lowered = text.lower()
    return (any(term in lowered for term in EDITORIAL_BLOCKLIST)
            or bool(LOW_SUBSTANCE.search(text) or UNCONFIRMED.search(text))
            or bool(MARKET_ONLY.search(row.get('title', '')))
            or bool(re.search(r'黎曼猜想|riemann hypothesis', text, re.I))
            or not editorial_eligible(row))


PRACTICAL_IMPACT = re.compile(r'工作|教育|教学|就业|辅助|无障碍|医疗|普通用户|办公|开源|本地运行|'
                              r'\b(?:workflows?|education|jobs?|accessibility|healthcare|open.weights)\b', re.I)


def editorial_rank(row):
    text = f"{row['title']} {row['description']}"
    return (score(row) + 5 * len(event_profile(row['title']))
            + 6 * bool(PRACTICAL_IMPACT.search(text)) + 4 * valid_description(row)
            - row['age_hours'] / 4)


SUMMARY_DETAIL = re.compile(r'\d|支持|实现|新增|配备|开放|试用|部署|处理|可用|适用|选择|转录|训练|'
                            r'输入|输出|包括|覆盖|允许|上线|用户|开发者|人员|团队|目前|首批')
SUMMARY_USE = re.compile(r'用户|开发者|研究人员|消防|学生|医生|可以|便于|可用|用于|部署|试用|接入|'
                         r'缩短|减少|能够|通过|无需')
SUMMARY_NOISE = re.compile(r'注册领取|接受全部|隐私政策|订阅|广告合作|电子邮件地址|阅读原文|点击查看|'
                           r'版权所有|最新资讯|责任编辑|上一篇|下一篇|^相关阅读|^来源[:：]|^作者[:：]', re.I)


def summary_parts(paragraphs, titles, limit):
    parts = []
    translated = 0
    for paragraph in paragraphs:
        if SUMMARY_NOISE.search(paragraph):
            continue
        if len(paragraph) > 470:
            ends = list(re.finditer(r'[。！？]|[.!?](?=\s|$)', paragraph[:470]))
            if not ends:
                continue
            paragraph = paragraph[:ends[-1].end()]
        if not re.search(r'[\u4e00-\u9fff]{2}', paragraph):
            if translated >= 3:
                continue
            translated += 1
        text = to_chinese(paragraph)
        for raw in re.split(r'(?<=[。！？])|(?<=[.!?])\s+', text):
            clauses = [raw, *re.split(r'[；;]', raw)]
            if len(raw) >= limit and not re.search(r'如果|除非|只要|一旦|仅当|前提', raw):
                clauses.extend(re.split(r'[，,](?=开发者|用户|研究人员|该工具|该模型|模型|团队|权重)', raw))
            for clause in clauses:
                fact = clean_attribution(clause).strip(' ，,；;：:。')
                if (not 12 <= len(fact) < limit or len(re.findall(r'[\u4e00-\u9fff]', fact)) < 8
                        or '…' in fact or '...' in fact
                        or not SUMMARY_DETAIL.search(fact)
                        or fact.startswith(('据', '随着', '为了', '除了', '当时', '在此背景下', '同时', '其中'))
                        or fact.endswith(('将', '并', '与', '及', '和', '为', '在', '对', '包括', '从', '表示'))
                        or any(similar(fact, other) for other in titles)
                        or SUMMARY_NOISE.search(fact) or blocked({'body': fact})):
                    continue
                sentence = fact if fact.endswith(('！', '？', '!', '?')) else fact + '。'
                if sentence not in parts:
                    parts.append(sentence)
                    if len(parts) == 14:
                        return parts
    return parts


def select_summary(parts, target, limit):
    best, best_rank = '', ()
    for count in range(1, min(3, len(parts)) + 1):
        for block in combinations(parts, count):
            body = ''.join(block)
            if len(body) > limit or any(similar(a, b) for a, b in combinations(block, 2)):
                continue
            rank = (len(body) >= target, min(2, count), bool(SUMMARY_USE.search(body)),
                    bool(re.search(r'\d', body)), len(body))
            if rank > best_rank:
                best, best_rank = body, rank
    return best


def news_summary(row, minimum, maximum, headline=''):
    titles = (to_chinese(row['title']), headline)
    description = tidy(row.get('description', ''))
    feed_parts = summary_parts([description], titles, maximum)
    body = select_summary(feed_parts, minimum, maximum)
    if len(body) >= minimum and body.count('。') >= 2:
        return body
    article = publisher_article(row.get('article_url', ''))
    if article:
        extra = summary_parts([article[2], *article[3]], titles, maximum)
        return select_summary(list(dict.fromkeys(feed_parts[:5] + extra))[:14], minimum, maximum)
    return body


def enrich(row):
    if valid_description(row):
        return row
    summary = article_meta_summary(row['article_url'])
    return {**row, 'description': summary} if summary else row


def person_action_event(title, description):
    event = title + ' ' + description
    # A specific launch, decision, trial or named public event must accompany the attribution.
    return bool(re.search(r'发布|推出|上线|邀请.{0,25}(?:测试|试用)|启用|公布|签署|开源|'
                          r'(?:纳斯达克|开幕|主题演讲|发布会).{0,40}(?:承诺|宣布|重申)|'
                          r'\b(?:released?|rolling out|trial|announced|nasdaq opening bell)\b', event, re.I))


def classify_news(card):
    text = card['title'] + ' ' + card.get('body', card.get('description', ''))
    practical = re.search(r'消防|无线电|医疗配送|无人出租车|课堂|无障碍|医护|仓储|工厂|患者|wildfire|firefighter', text, re.I)
    application = re.search(r'用于|用.{0,20}(?:转录|配送|识别|转成|通话)|转录|转写|运营|部署|辅助|协助|'
                            r'(?:发展|探索).{0,10}(?:出租车|配送|自动驾驶)|transcrib|deliver', text, re.I)
    if practical and application:
        return '实际应用'
    if re.search(r'模型权重|模型参数|论文|科研|研究基准|训练架构|芯片架构|仿真|'
                 r'(?:大模型|语言模型|模型).{0,12}(?:开源|开放)|benchmark|model weights', text, re.I):
        return '前沿科技'
    if application or re.search(r'功能|工具|助手|应用|可后台|试用|体验|工作流', text):
        return '产品应用'
    return 'AI动态'


def choose_people(rows, history):
    fresh = [row for row in rows if in_window(row, PERSON_HOURS)]
    people_cards, selected, names = [], [], []
    pick_people(fresh, history, people_cards, selected, names)
    pick_reviewed_people(history, people_cards, selected, names, PERSON_HOURS)
    older = [row for row in rows if not in_window(row, PERSON_HOURS) and in_window(row, PERSON_FALLBACK_HOURS)]
    pick_people(older, history, people_cards, selected, names)
    pick_reviewed_people(history, people_cards, selected, names, PERSON_FALLBACK_HOURS)
    return people_cards, selected, names


def pick_reviewed_people(history, cards, selected, names, hours):
    for item in MEDIA['people']:
        if len(cards) >= 3:
            break
        profile = verified_person(item['name'])
        if not profile or profile['name'] in names:
            continue
        for event in profile.get('verified_events', []):
            row = candidate(event['title'], event['url'], event['published_at'], event['body'], '核实的原文')
            if (not row or not in_window(row, hours) or blocked(row)
                    or any(duplicate_event(row, old) for old in history + selected)):
                continue
            cards.append(person_card(row, profile, event['title'], event['body']))
            selected.append(row)
            names.append(profile['name'])
            break


def person_card(row, profile, title, body):
    stamp = parse_date(row['published_at']).astimezone(BEIJING).strftime('%m-%d %H:%M')
    return {
        'name': '｜'.join(part for part in (profile['name'], profile.get('role')) if part),
        'title': title, 'body': body,
        'source': row['source'] + ' · ' + stamp, 'published_at': row['published_at'],
        'url': row['article_url'], 'discovered_via': row['discovered_via'],
        'category': 'person', 'publisher': row['source'],
        'person': profile['name'],
        'published_at_beijing': row['published_at_beijing'],
    }


def pick_people(rows, history, people_cards, selected, names):
    for row in rows:
        if len(selected) >= 3:
            break
        lead = re.split(r'(?<=[。！？])|(?<=[.!?])\s+', row.get('description', ''), maxsplit=1)[0]
        name, role = identify_person(row['title'])
        if not name:
            name, role = identify_person(lead)
        if (not name or person_key(name) in {person_key(n) for n in names}
                or not publisher_for(row['article_url']) or blocked(row)
                or not relevant_ai_person_event(row['title'] + ' ' + lead)
                or any(duplicate_event(row, other) for other in history + selected)):
            continue
        approved = reviewed_person_event(row['article_url'])
        if approved and person_key(approved[0]['name']) == person_key(name):
            event = approved[1]
            headline, body = event['title'], event['body']
        else:
            headline = short_headline(row['title'], 23)
            if headline == '人工智能领域出现新进展' or headline.endswith(('发布AI新进展', '公布AI相关动态')):
                published_title = clean_attribution(to_chinese(row['title'])).rstrip('。！？.!?')
                headline = complete_excerpt(published_title, 25) or headline
            body = news_summary(row, 35, 55, headline)
        if not body:
            body = article_body(row, 45)
        profile = {'name': name, 'role': role}
        people_cards.append(person_card(row, profile, headline, body))
        selected.append(row)
        names.append(name)


def headline_priority(row, coverage):
    topic_rank = row.get('hot_topic', {}).get('rank')
    related = [other for other in coverage
               if (topic_rank and other.get('hot_topic', {}).get('rank') == topic_rank)
               or duplicate_event(row, other) or duplicate_event(other, row)]
    media = {other['source'] for other in related if other['category'] in {'tech', 'general'}}
    positions = [other['publisher_position'] for other in related
                 if other.get('publisher_position') and other['category'] in {'tech', 'general'}]
    hot = min((other['hot_topic'] for other in related if other.get('hot_topic')),
              key=lambda topic: topic['rank'], default={})
    text = row['title'] + ' ' + row.get('description', '')
    events = event_profile(text)
    importance = (14 * bool('release' in events and model_tokens(row['title']))
                  + 9 * bool('research' in events and re.search(r'\d|突破|开源', text))
                  + 9 * bool('hardware' in events or 'deployment' in events)
                  + 8 * bool('impact' in events or PRACTICAL_IMPACT.search(text)))
    position_points = max(0, 7 - min(positions)) if positions else 0
    hot_points = (12 - hot['rank']) * 2 + min(8, hot.get('participantCount', 0) // 8) if hot else 0
    total = round(editorial_rank(row) / 4 + importance + min(24, 6 * len(media)) + position_points + hot_points, 2)
    return {'score': total, 'importance_points': importance, 'media': sorted(media),
            'best_media_position': min(positions, default=None), 'aihot': hot}


def choose_news(rows, history, people_rows, *, limit=4, lead=None):
    cards, selected = lead if lead else ([], [])
    cards, selected = list(cards), list(selected)
    eligible = [r for r in rows if in_window(r, NEWS_HOURS) and not blocked(r)
                and relevant_ai_news(r) and quality_gate(r) and event_profile(r['title'])]
    coverage = [r for r in rows if in_window(r, NEWS_HOURS)]
    if not selected:
        headline_rows = [dict(row, headline_signals=headline_priority(row, coverage)) for row in eligible]
        headline_rows.sort(key=lambda row: row['headline_signals']['score'], reverse=True)
    # Reserve one slot for each region when publishable, then prefer different publishers.
    for index in range(9):
        if len(selected) >= limit:
            break
        region = ({'CN': 'international', 'international': 'CN'}[selected[0]['region']]
                  if index == 1 and len(selected) == 1 else None)
        distinct = index < 5
        for row in eligible if selected else headline_rows:
            if region and row['region'] != region:
                continue
            if distinct and row['source'] in {item['source'] for item in selected}:
                continue
            if any(duplicate_event(row, other) for other in history + people_rows + selected):
                continue
            title_limit = 32 if not selected else 22
            if not is_chinese_ready(row) or not publishable_title(row, title_limit):
                continue
            headline = short_headline(row['title'], title_limit)
            # Abbreviating a long headline must retain the technology and the event.
            if not event_profile(headline):
                continue
            row = enrich(row)
            if blocked(row):
                continue
            minimum, maximum = (65, 90) if not selected else (45, 65)
            body = news_summary(row, minimum, maximum, headline)
            if not body:
                continue
            cards.append({
                'title': headline, 'body': body,
                'source': row['source'], 'url': row['article_url'],
                'published_at': row['published_at'], 'discovered_via': row['discovered_via'],
                'category': row['category'], 'publisher': row['source'],
                'published_at_beijing': row['published_at_beijing'],
            })
            if not selected:
                cards[-1]['headline_signals'] = row['headline_signals']
            selected.append(row)
            break
    return cards, selected


def collect_publisher(source):
    if source['type'] == 'web':
        rows, raw = web_source(source)
        return rows, raw, 'web', {}
    try:
        rows, raw = direct_feed(source)
        return rows, raw, source['type'], {}
    except (OSError, ValueError, ET.ParseError) as exc:
        if not source.get('web_fallback'):
            raise
        rows, raw = web_source(source, source['web_fallback'])
        return rows, raw, 'web', {'feed_error': str(exc)[:180], 'effective_url': source['web_fallback']}


def topic_candidate(row):
    return not blocked(row) and (relevant_ai_news(row)
                                  or relevant_ai_person_event(row['title'] + ' ' + row['description']))


def collect():
    watch = SOURCE_CONFIG['person_watchlist']
    queries = [
        ('人工智能 OR 大模型 OR 机器人', False, False),
        ('AI robotics OR AI products OR AI research', False, True),
        ('AI (CEO OR researcher OR founder) interview', True, True),
        ('人工智能 (创始人 OR 科学家 OR 工程师)', True, False),
    ]
    for key, international in [('zh', False), ('en', True)]:
        terms = [person[key] for person in watch]
        for start in range(0, len(terms), 4):
            group = ' OR '.join(f'"{term}"' for term in terms[start:start + 4])
            queries.append((f'({group}) AI', True, international))
    stats, rows = {}, []
    with ThreadPoolExecutor(max_workers=16) as pool:
        jobs = {source['name']: pool.submit(collect_publisher, source) for source in PUBLISHERS}
        jobs['AIHOT'] = pool.submit(aihot_candidates)
        hot_topics = pool.submit(aihot_hot_topics)
        jobs['Hacker News'] = pool.submit(hacker_news)
        jobs['GitHub Trending'] = pool.submit(github_trending)
        searches = [(q[0], pool.submit(search, *q)) for q in queries]
        for source in SOURCE_REGISTRY:
            name = source['name']
            method = source['type']
            gathered, raw, details = [], 0, {}
            try:
                if name == 'Google News':
                    for query, future in searches:
                        try:
                            found, count = future.result()
                            gathered.extend(found)
                            raw += count
                        except (OSError, ValueError, ET.ParseError) as exc:
                            details.setdefault('query_failures', {})[query] = str(exc)[:160]
                    if len(details.get('query_failures', {})) == len(searches):
                        raise ValueError('all Google News queries failed')
                elif name == 'AIHOT':
                    gathered, details = jobs[name].result()
                    feeds = [details['selected_feed'], details['all_feed']]
                    raw = sum(feed.get('raw', 0) for feed in feeds)
                    if not any(feed.get('pages') for feed in feeds):
                        raise ValueError('AIHOT returned no readable page')
                elif name in DISCOVERY:
                    gathered, raw = jobs[name].result()
                else:
                    gathered, raw, method, details = jobs[name].result()
                status = {'access': 'ok', 'last_success_at': dt.datetime.now(dt.timezone.utc).isoformat()}
            except (OSError, ValueError, ET.ParseError) as exc:
                status = {'access': 'failed', 'last_success_at': None, 'error': str(exc)[:200]}
            gathered = list({row['canonical_url']: row for row in gathered}.values())
            eligible = [row for row in gathered if topic_candidate(row)]
            stats[name] = {
                'url': source['url'], 'category': source['category'], 'method': method,
                **details, **status, 'raw': raw,
                'valid_18h': sum(in_window(row, NEWS_HOURS) for row in gathered),
                'valid_48h': sum(in_window(row, PERSON_HOURS) for row in gathered),
                'topic_qualified_18h': sum(in_window(row, NEWS_HOURS) for row in eligible),
                'topic_qualified_48h': sum(in_window(row, PERSON_HOURS) for row in eligible),
                'topic_filtered': len(gathered) - len(eligible),
                'selected_news': 0, 'selected_people': 0,
            }
            rows.extend(dict(row, ingest_source=name) for row in eligible)
        hot_signals = hot_topics.result()
    ranked = sorted(rows, key=editorial_rank, reverse=True)
    by_url = {}
    for row in ranked:
        current = by_url.setdefault(row['canonical_url'], row)
        if row.get('publisher_position'):
            current['publisher_position'] = min(row['publisher_position'], current.get('publisher_position', row['publisher_position']))
        if row['canonical_url'] in hot_signals:
            current['hot_topic'] = hot_signals[row['canonical_url']]
        current.setdefault('discovered_by', [])
        if row['ingest_source'] not in current['discovered_by']:
            current['discovered_by'].append(row['ingest_source'])
    return list(by_url.values()), stats


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def final_problems(news_cards, people_cards):
    problems = []
    if len(news_cards) != 4 or len(people_cards) != 3:
        problems.append(f'栏目数量不足：新闻 {len(news_cards)}/4，人物 {len(people_cards)}/3')
    for card in people_cards:
        if not card.get('person'):
            problems.append(f'报道未识别出人物姓名：{card.get("title")}')
    for card, hours in [(c, NEWS_HOURS) for c in news_cards] + [(c, PERSON_FALLBACK_HOURS) for c in people_cards]:
        if not all(card.get(key) for key in ('title', 'body', 'source', 'url', 'published_at')):
            problems.append(f'缺少标题、正文或溯源字段：{card.get("title")}')
        if not in_window(card, hours):
            problems.append(f'原文发布时间超出北京时间窗口：{card.get("title")}')
        if not publisher_for(card.get('url', '')) or blocked(card):
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
    issue_date = NOW.astimezone(BEIJING).date()
    today = issue_date.isoformat()
    state = json.loads(HISTORY.read_text('utf-8')) if HISTORY.exists() else {'last_issue': 2, 'items': []}
    same_day = any(item.get('date') == today for item in state['items'][-7:])
    issue = (f'{10 + (issue_date - ISSUE_START_DATE).days:03d}' if issue_date >= ISSUE_START_DATE
             else str(int(state.get('last_issue', 2)) + (0 if same_day else 1)).zfill(3))
    history = [item for item in state['items'] if item.get('date', '') < today]
    out = ROOT / ('test-output' if test_mode else 'output')
    out.mkdir(exist_ok=True)
    rows, stats = collect()
    eligible_history = [row for row in rows if not any(duplicate_event(row, old) for old in history)]
    lead = choose_news(eligible_history, [], [], limit=1)
    people_cards, people_rows, _ = choose_people(eligible_history, history + lead[1])
    news_cards, news_rows = choose_news(eligible_history, [], people_rows, lead=lead)
    problems = final_problems(news_cards, people_cards)
    selected = news_rows + people_rows
    for field, chosen in [('selected_news', news_rows), ('selected_people', people_rows)]:
        for row in chosen:
            name = row.get('ingest_source', row['discovered_via'])
            if name in stats:
                stats[name][field] = stats[name].get(field, 0) + 1
    last_report = out / 'source_report.json'
    if last_report.exists():
        try:
            previous = json.loads(last_report.read_text('utf-8'))['sources']
        except (OSError, ValueError, KeyError):
            previous = {}
        for name, source_stats in stats.items():
            if not source_stats.get('last_success_at') and name in previous:
                source_stats['last_success_at'] = previous[name].get('last_success_at')
    report = {
        'date': today, 'as_of_utc': NOW.isoformat(), 'issue_preview': issue,
        'sources': stats, 'unique_candidates': len(rows), 'history_rejected': len(rows) - len(eligible_history),
        'news_selected': news_cards, 'people_selected': people_cards,
        'person_candidates': [
            row for row in eligible_history if in_window(row, PERSON_FALLBACK_HOURS)
            and identify_person(row['title'] + ' ' + row['description'])[0]
        ][:40],
        'candidates': eligible_history[:40], 'problems': problems,
    }
    print(json.dumps({key: report[key] for key in ('date', 'unique_candidates', 'history_rejected', 'problems')},
                     ensure_ascii=False))
    print('AIHOT:', json.dumps(stats['AIHOT'], ensure_ascii=False))
    assets, media_report = prepare_images(news_rows, people_cards)
    report['images'] = media_report
    if any(slot['status'] == 'pending' for slot in media_report['slots'].values()):
        problems.append('存在图片待处理的候选')
    print(f'图片下载：{media_report["downloaded"]}/{media_report["selected"]}，完整早报需要 7 张')
    if not problems:
        data = {
            'issue': issue, 'date': today, 'headline': news_cards[0],
            'updates': [dict(card, tag=classify_news(card)) for card in news_cards[1:]],
            'leaders': people_cards,
        }
        try:
            png = render_png(data, assets)
        except Exception as exc:
            problems.append(f'固定模板出图失败：{exc}')
    report['status'] = 'needs_editorial_review' if problems else 'ready'
    write_json(last_report, report)
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
