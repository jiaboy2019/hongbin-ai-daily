"""Backfill verified technology-person stories when the regular 48-hour pool is sparse."""
import datetime as dt
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


NORMAL_HOURS = 48
BACKFILL_HOURS = 96
BEIJING = dt.timezone(dt.timedelta(hours=8))
TRUSTED = {
    'blog.google': 'Google 官方博客', 'research.google': 'Google Research',
    'the-decoder.com': 'The Decoder', 'ithome.com': 'IT之家',
    'techcrunch.com': 'TechCrunch', 'blogs.microsoft.com': '微软官方博客',
    'blogs.nvidia.com': 'NVIDIA', 'anthropic.com': 'Anthropic', 'openai.com': 'OpenAI',
    'microsoft.com': 'Microsoft', '36kr.com': '36氪', 'jiqizhixin.com': '机器之心',
    'qbitai.com': '量子位', 'aiera.com.cn': '新智元', 'hunyuan.tencent.com': '腾讯混元',
    'deepmind.google': 'Google DeepMind', 'amd.com': 'AMD', 'hai.stanford.edu': 'Stanford HAI',
}
ACTION = re.compile(r'发布|推出|上线|宣布|开源|介绍|展示|披露|测试|试用|重申|提出|承诺|'
                    r'\b(?:announc|releas|publish|demonstrat|present|test|reiterat|explain)\w*', re.I)
UNSAFE = re.compile(r'有传言|未经证实|消息人士|网传|传闻|博彩|投注|赌场|推广|股价|股票|'
                    r'\b(?:rumou?r|unverified|casino|gambling|speculat)\w*', re.I)


def read_evidence(root):
    return json.loads((root / 'people_evidence.json').read_text(encoding='utf-8'))


def article_time(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return None
    return parsed.astimezone(BEIJING) if parsed.tzinfo else None


def original_key(url):
    if not isinstance(url, str):
        return ''
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower().removeprefix('www.')
    except ValueError:
        return ''
    if parsed.scheme != 'https' or host not in TRUSTED or parsed.username is not None or not parsed.path.strip('/'):
        return ''
    return host + parsed.path.rstrip('/')


def bilingual_key(text):
    return re.sub(r'[\W_]+', '', text.casefold())


def people_backfill(rows, evidence, *, now, used_people, history, used_rows, similar,
                    key_for, headline, body, is_ai, blocked, slots, max_age_hours=BACKFILL_HOURS):
    """Use verified current reporting first, then eligible older reporting up to 96 hours."""
    if slots <= 0:
        return []
    profiles = {}
    for item in evidence['profiles']:
        checked = dt.date.fromisoformat(item['verified_on'])
        if abs((now.astimezone(BEIJING).date() - checked).days) > 180:
            continue
        for alias in (item['name'], *item.get('aliases', [])):
            profiles[alias] = item

    supported = [dict(row, reviewed=True) for row in evidence['reviewed_events']] + list(rows)
    pool = []
    previous_titles = [item.get('title', '') for item in history + used_rows]
    previous_keys = {original_key(item.get('article_url')) for item in history + used_rows} - {''}
    seen_people = {bilingual_key(name) for name in used_people}
    for used in used_people:
        for alias, profile in profiles.items():
            if bilingual_key(alias) == bilingual_key(used):
                seen_people.add(bilingual_key(profile['name']))
    for row in supported:
        url = row.get('article_url', '')
        link = original_key(url)
        published = article_time(row.get('published_at'))
        if not link or not published or link in previous_keys:
            continue
        age = (now - published).total_seconds() / 3600
        if not 0 <= age <= max_age_hours:
            continue
        title = row.get('title', '')
        description = row.get('description', '')
        if (not title or not description or UNSAFE.search(title + ' ' + description)
                or not is_ai(title + ' ' + description) or blocked(row)):
            continue
        if row.get('reviewed'):
            profile = profiles.get(row['person'])
            title_zh, body_zh = title, description
        else:
            # A name mentioned in passing or appearing only in a byline does not make a person story.
            attribution = [
                (action.start(), person) for alias, person in profiles.items()
                if (action := re.search(
                    r'(?<![A-Za-z])' + re.escape(alias) + r'(?![A-Za-z])[^。！？!?;；]{0,27}'
                    + '(?:' + ACTION.pattern + ')', title + '。' + description, re.I))
            ]
            profile = min(attribution, key=lambda item: item[0])[1] if attribution else None
            if not profile:
                continue
            title_zh, body_zh = headline(title), body(row)
        if (not profile or not title_zh or not body_zh or len(title_zh) > 24 or len(body_zh) > 60
                or not ACTION.search(title + ' ' + description)
                or len(re.findall(r'[\u4e00-\u9fff]', title_zh)) < 5
                or len(re.findall(r'[\u4e00-\u9fff]', body_zh)) < 14
                or similar(title_zh, body_zh)
                or any(similar(title, previous) for previous in previous_titles if previous)):
            continue
        pool.append((age, not row.get('reviewed'), link, published, profile, row, title_zh, body_zh))

    selected = []
    for age, _, link, published, profile, row, title_zh, body_zh in sorted(
            pool, key=lambda item: (item[0] > NORMAL_HOURS, item[1], item[0])):
        person_id = bilingual_key(profile['name'])
        if person_id in seen_people or link in previous_keys or any(
                similar(row['title'], previous) for previous in previous_titles if previous):
            continue
        source = TRUSTED[link.split('/', 1)[0]]
        card = {
            'name': profile['name'] + '｜' + profile['role'], 'title': title_zh, 'body': body_zh,
            'source': f'{source} · {published:%m-%d %H:%M}',
            'published_at': published.isoformat(), 'url': row['article_url'],
            'role_source': profile['role_source'],
        }
        selected_row = {
            **row, 'key': key_for(row['title']), 'source': source, 'published_at': published.isoformat(),
        }
        selected.append((card, selected_row, profile['name']))
        seen_people.add(person_id)
        previous_keys.add(link)
        previous_titles.append(row['title'])
        if len(selected) == slots:
            break
    return selected
