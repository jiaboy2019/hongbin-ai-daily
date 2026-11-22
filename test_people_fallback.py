import datetime as dt
from pathlib import Path
import unittest

from people_fallback import people_backfill, read_evidence


class PeopleFallbackTests(unittest.TestCase):
    def setUp(self):
        self.evidence = read_evidence(Path(__file__).resolve().parent)
        self.now = dt.datetime.fromisoformat('2026-11-22T05:30:00+08:00')

    def select(self, rows=(), history=(), names=(), evidence=None, slots=3):
        return people_backfill(
            rows, evidence if evidence is not None else self.evidence,
            now=self.now, used_people=list(names), history=list(history), used_rows=[],
            similar=lambda a, b: a == b, key_for=lambda title: title,
            headline=lambda title: title,
            body=lambda row: row['description'],
            is_ai=lambda text: True, blocked=lambda row: False, slots=slots,
        )

    def test_reviewed_sources_fill_three_with_local_times(self):
        result = self.select()
        self.assertEqual([person for _, _, person in result],
                         ['Robby Stein', 'Thibault Sottiaux', 'Dario Amodei'])
        self.assertTrue(all(card['published_at'].endswith('+08:00') for card, _, _ in result))
        self.assertIn('11-20 21:19', result[0][0]['source'])

    def test_cross_day_url_and_alias_duplicates(self):
        result = self.select(history=[{'article_url': self.evidence['reviewed_events'][0]['article_url'] + '?ref=test'}],
                             names=['阿莫迪'])
        self.assertEqual([person for _, _, person in result], ['Thibault Sottiaux'])

    def test_expired_future_and_unverified_items_are_not_used(self):
        self.now = dt.datetime.fromisoformat('2026-11-26T05:30:00+08:00')
        self.assertEqual(self.select(), [])
        self.now = dt.datetime.fromisoformat('2026-11-19T05:30:00+08:00')
        self.assertEqual(self.select(), [])

    def test_live_candidate_with_named_speaker_and_reliable_original(self):
        evidence = {**self.evidence, 'reviewed_events': []}
        row = {
            'title': 'Google 推出桌面 AI 搜索侧栏', 'published_at': '2026-11-20T21:19:31+08:00',
            'article_url': 'https://the-decoder.com/news-test',
            'description': 'Robby Stein 宣布用户可以从新侧栏检索最近的搜索记录。',
        }
        self.assertEqual([person for _, _, person in self.select([row], evidence=evidence)], ['Robby Stein'])
        misleading = {**row, 'description': '页面另行提到 Robby Stein。公司发布了与他无关的内容。'}
        bad_domain = {**row, 'article_url': 'https://the-decoder.com.invalid/news-test'}
        self.assertEqual(self.select([misleading, bad_domain], evidence=evidence), [])


if __name__ == '__main__':
    unittest.main()
