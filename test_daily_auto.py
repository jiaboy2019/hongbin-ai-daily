import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import daily_auto as daily


class EditorialTests(unittest.TestCase):

    def test_reviewed_people_replay_expiry_and_history(self):
        as_of = daily.parse_date('2026-11-24T08:32:04+08:00')
        with patch.object(daily, 'NOW', as_of):
            cards, rows, names = daily.choose_people([], [])
            self.assertEqual(names, ['黄仁勋', 'Logan Kilpatrick', 'Simon Willison'])
            self.assertEqual(len(cards), 3)
            self.assertEqual(daily.final_problems([], cards), ['栏目数量不足：新闻 0/4，人物 3/3'])
            self.assertIsNone(daily.publisher_for('https://x.com/OfficialLoganK/status/123'))
            self.assertNotIn('黄仁勋', daily.choose_people([], rows[:1])[2])
        with patch.object(daily, 'NOW', daily.parse_date('2026-11-27T08:32:04+08:00')):
            self.assertEqual(len(daily.choose_people([], [])[0]), 3)
        with patch.object(daily, 'NOW', daily.parse_date('2026-11-29T08:32:04+08:00')):
            self.assertEqual(daily.choose_people([], [])[0], [])
    def setUp(self):
        now = patch.object(daily, 'NOW', dt.datetime(2026, 11, 21, 12, tzinfo=dt.timezone.utc))
        now.start()
        self.addCleanup(now.stop)

    def raw(self, published='2026-11-21T10:00:00+00:00', url='https://openai.com/index/release?utm_source=test'):
        return {'title': 'OpenAI发布新一代语言模型GPT-5', 'summary': '新版本支持更长文本，开发者可以在接口中试用。',
                'publishedAt': published, 'score': 90, 'source': {'name': '假来源'},
                'links': {'original': url, 'aihot': 'https://aihot.news/items/test'}}

    def test_aihot_original_and_authority(self):
        row = daily.parse_aihot_item(self.raw())
        self.assertEqual(row['article_url'], self.raw()['links']['original'])
        self.assertEqual(row['canonical_url'], 'https://openai.com/index/release')
        self.assertEqual(row['source'], 'OpenAI')
        self.assertNotIn('heat', row)
        self.assertIsNone(daily.parse_aihot_item(self.raw(url='https://openai.com.example.org/release')))
        self.assertIsNone(daily.parse_aihot_item(self.raw(url='https://aihot.news/items/test')))

    def test_time_window_and_pagination(self):
        first = {'items': [self.raw(), self.raw('2026-11-21T13:00:00Z', 'https://openai.com/future')],
                 'page': {'hasMore': True, 'nextCursor': 'cursor-2'}}
        second = {'items': [self.raw('2026-11-20T17:59:59Z', 'https://openai.com/old')],
                  'page': {'hasMore': False}}
        with patch.object(daily, 'fetch', side_effect=[json.dumps(first), json.dumps(second)]) as fetch:
            rows, counts = daily.aihot_page_rows('selected', 4)
        self.assertTrue(fetch.call_args.args[0].endswith('cursor=cursor-2'))
        self.assertEqual(len(rows), 2)
        self.assertEqual(counts['valid_18h'], 1)
        edge = daily.parse_aihot_item(self.raw('2026-11-20T18:00:00Z'))
        self.assertTrue(daily.in_window(edge, daily.NEWS_HOURS))
        self.assertIsNone(daily.parse_aihot_item(self.raw('2026-11-21')))

    def test_cross_day_legacy_and_bilingual_dedup(self):
        row = daily.parse_aihot_item(self.raw())
        self.assertTrue(daily.duplicate_event(row, {'key': row['key'], 'title': row['title']}))
        self.assertTrue(daily.duplicate_event(row, {'title': 'OpenAI launches GPT-5 model'}))
        self.assertFalse(daily.duplicate_event(row, {'title': 'OpenAI发布新一代语言模型GPT-4'}))
        self.assertTrue(daily.duplicate_event(row, {'article_url': row['article_url'], 'title': '另一种标题'}))

    def test_channel_and_hot_score_do_not_affect_rank(self):
        first = daily.parse_aihot_item(self.raw())
        self.assertEqual(daily.editorial_rank(first), daily.editorial_rank(
            dict(first, discovered_via='direct', heat=0, category='tech', source='任何媒体')))

    def test_exact_person_window_and_finance_noise(self):
        at_limit = daily.parse_aihot_item(self.raw('2026-11-19T12:00:00Z'))
        outside = daily.parse_aihot_item(self.raw('2026-11-19T11:59:59Z'))
        self.assertTrue(daily.in_window(at_limit, daily.PERSON_HOURS))
        self.assertFalse(daily.in_window(outside, daily.PERSON_HOURS))
        self.assertTrue(daily.blocked({'title': 'Anthropic’s I.P.O. turns researchers into millionaires'}))

    def test_beijing_time_normalization_and_content_classification(self):
        row = daily.parse_aihot_item(self.raw('2026-11-21T08:00:00-02:00'))
        self.assertEqual(row['published_at_beijing'], '2026-11-21T18:00:00+08:00')
        self.assertFalse(daily.in_window({'published_at': '2026-11-21T10:00:00'}, daily.NEWS_HOURS))
        self.assertEqual(daily.classify_news({'title': 'AI 帮助转录消防无线电'}), '实际应用')
        self.assertEqual(daily.classify_news({'title': '腾讯混元公开 Hy4 模型权重'}), '前沿科技')
        self.assertFalse(daily.person_action_event('谈人工智能的未来', '表示行业会更好。'))

    def test_reject_placeholder_and_retry_alternate_image(self):
        refs = [{'url': 'https://example.org/unavailable', 'evidence': 'https://example.org/story'},
                {'url': 'https://example.org/related', 'evidence': 'https://example.org/story'}]
        with (patch.object(daily, 'media_candidates', return_value=refs),
              patch.object(daily, 'download_image', side_effect=[None, ('test-image', 'hash')])):
            images, report = daily.prepare_images([self.raw()], [])
        self.assertEqual(images, {'headline': 'test-image'})
        self.assertEqual(report['slots']['headline']['attempts'], [r['url'] for r in refs])
        with self.assertRaisesRegex(ValueError, '图片待处理'):
            daily.render_png({}, images)

    def test_summary_expands_thin_feed_from_original_article(self):
        row = {
            'title': '腾讯混元开放 Hy4 模型权重',
            'description': '腾讯混元开放 Hy4 模型权重。',
            'article_url': 'https://www.ithome.com/1/029/841.htm',
        }
        article = ('标题', '', '', (
            '模型支持大约一百万 token 的上下文，能处理文本、图片和视频。',
            '开发者可以从四个平台下载模型权重，并在自己的环境部署。',
            '点击查看最新资讯，立即注册领取会员福利。',
        ))
        with patch.object(daily, 'publisher_article', return_value=article) as fetch:
            body = daily.news_summary(row, 45, 65, row['title'])
        fetch.assert_called_once_with(row['article_url'])
        self.assertIn('上下文', body)
        self.assertIn('四个平台', body)
        self.assertNotIn('注册', body)
        self.assertNotIn(row['title'], body)
        self.assertTrue(45 <= len(body) <= 65)

    def test_summary_uses_only_supported_detail_when_source_is_thin(self):
        row = {'title': '新版本上线', 'description': '该入口仅面向已有账号的付费用户开放。',
               'article_url': 'https://openai.com/index/feature'}
        with patch.object(daily, 'publisher_article', return_value=None):
            self.assertEqual(daily.news_summary(row, 45, 65), row['description'])

    def test_pollution_and_person_name(self):
        self.assertTrue(daily.blocked({'description': 'm6米乐app在线登陆官网'}))
        self.assertTrue(daily.blocked({'title': '有传言称公司推出机器人'}))
        self.assertEqual(daily.identify_person('AI科学家张明展示机器人控制系统'), ('张明', '科学家'))
        self.assertEqual(daily.identify_person('AI科学家预测机器人将落地'), (None, None))
        self.assertEqual(daily.person_key('Sam Altman'), daily.person_key('奥尔特曼'))

    def test_failed_run_keeps_official_files_in_both_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            official = root / 'output'
            official.mkdir()
            history = root / 'published_history.json'
            history.write_text('{"last_issue":4,"items":[]}', encoding='utf-8')
            daily_json = official / 'daily.json'
            png = official / '洪滨每日AI早报.png'
            daily_json.write_bytes(b'official json')
            png.write_bytes(b'official png')
            before = [p.read_bytes() for p in (history, daily_json, png)]
            for mode, review_dir in [([], root / 'test-output'), (['--formal'], official)]:
                with (patch.object(daily, 'ROOT', root), patch.object(daily, 'HISTORY', history),
                      patch.object(daily, 'collect', return_value=([], {'AIHOT': {}})),
                      patch.object(sys, 'argv', ['daily_auto.py', *mode])):
                    self.assertEqual(daily.main(), 2)
                self.assertEqual(before, [p.read_bytes() for p in (history, daily_json, png)])
                self.assertEqual(json.loads((review_dir / 'editorial_review.json').read_text('utf-8'))['status'],
                                 'needs_editorial_review')

    def test_successful_test_render_keeps_same_day_issue_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / 'published_history.json'
            state = '{"last_issue":4,"items":[{"key":"prior","date":"2026-11-21"}]}'
            history.write_text(state, encoding='utf-8')
            row = daily.parse_aihot_item(self.raw())
            card = {
                'title': 'OpenAI发布新模型', 'body': '研究团队同步公布了模型在多种任务上的评测。',
                'source': 'OpenAI', 'url': row['article_url'], 'published_at': row['published_at'],
            }
            profile = daily.verified_person('Robby Stein')
            person = {**card, 'person': profile['name'], 'name': profile['name'] + '｜' + profile['role'],
                      'role_source': profile['role_source']}
            with (patch.object(daily, 'ROOT', root), patch.object(daily, 'HISTORY', history),
                  patch.object(daily, 'collect', return_value=([row], {'AIHOT': {}})),
                  patch.object(daily, 'choose_news', return_value=([card] * 4, [row] * 4)),
                  patch.object(daily, 'choose_people', return_value=([person] * 3, [row] * 3, ['Robby Stein'] * 3)),
                  patch.object(daily, 'prepare_images',
                               return_value=({}, {'downloaded': 7, 'selected': 7, 'required': 7, 'slots': {}})),
                  patch.object(daily, 'render_png', return_value=b'test PNG'),
                  patch.object(sys, 'argv', ['daily_auto.py', '--test'])):
                self.assertEqual(daily.main(), 0)
            self.assertEqual(history.read_text('utf-8'), state)
            self.assertFalse((root / 'output').exists())
            data = json.loads((root / 'test-output/daily.json').read_text('utf-8'))
            self.assertEqual(data['issue'], '004')


if __name__ == '__main__':
    unittest.main()
