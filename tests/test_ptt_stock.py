import unittest
from datetime import date

from ptt_stock import collect_posts, parse_index_html, parse_list_date, parse_push

SAMPLE_INDEX = """
<html><body>
<div class="btn-group btn-group-paging">
  <a class="btn wide" href="/bbs/Stock/index1.html">最舊</a>
  <a class="btn wide" href="/bbs/Stock/index9999.html">‹ 上頁</a>
</div>
<div class="r-ent">
  <div class="nrec"><span>爆</span></div>
  <div class="title"><a href="/bbs/Stock/M.pinned.html">[公告] 股板置底</a></div>
  <div class="author">mod</div>
  <div class="date"> 1/01</div>
</div>
<div class="r-list-sep"></div>
<div class="r-ent">
  <div class="nrec"><span class="hl">99</span></div>
  <div class="title"><a href="/bbs/Stock/M.hot.html">[標的] 2330 多</a></div>
  <div class="author">foo</div>
  <div class="date"> 8/29</div>
</div>
<div class="r-ent">
  <div class="nrec"><span>12</span></div>
  <div class="title"><a href="/bbs/Stock/M.cold.html">[閒聊] 低推</a></div>
  <div class="author">bar</div>
  <div class="date"> 8/28</div>
</div>
<div class="r-ent">
  <div class="nrec"></div>
  <div class="title"><span>本文已被刪除</span></div>
  <div class="author">-</div>
  <div class="date"> 8/28</div>
</div>
</body></html>
"""

OLDER_INDEX = """
<html><body>
<div class="r-ent">
  <div class="nrec"><span>88</span></div>
  <div class="title"><a href="/bbs/Stock/M.old.html">[新聞] 去年的文</a></div>
  <div class="author">old</div>
  <div class="date"> 8/01</div>
</div>
</body></html>
"""


class PttStockTests(unittest.TestCase):
    def test_parse_push(self):
        self.assertEqual(parse_push("爆"), 100)
        self.assertEqual(parse_push("31"), 31)
        self.assertEqual(parse_push("XX"), 0)
        self.assertEqual(parse_push(""), 0)

    def test_parse_list_date_wraps_year(self):
        self.assertEqual(parse_list_date(" 8/29", date(2026, 8, 30)), date(2026, 8, 29))
        self.assertEqual(parse_list_date("12/31", date(2026, 1, 2)), date(2025, 12, 31))

    def test_parse_index_skips_pinned_and_deleted(self):
        posts, prev = parse_index_html(SAMPLE_INDEX, today=date(2026, 8, 30))
        urls = [p.url for p in posts]
        self.assertTrue(any(p.title.startswith("[標的]") for p in posts))
        self.assertFalse(any("置底" in p.title for p in posts))
        self.assertFalse(any("刪除" in p.title for p in posts))
        self.assertTrue(prev.endswith("index9999.html"))
        self.assertIn("https://www.ptt.cc/bbs/Stock/M.hot.html", urls)

    def test_collect_filters_week_and_push(self):
        pages = {
            "https://www.ptt.cc/bbs/Stock/index.html": SAMPLE_INDEX,
            "https://www.ptt.cc/bbs/Stock/index9999.html": OLDER_INDEX,
        }
        posts = collect_posts(
            days=7,
            min_push=30,
            today=date(2026, 8, 30),
            fetch=pages.__getitem__,
            sleep_s=0,
        )
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].title, "[標的] 2330 多")
        self.assertEqual(posts[0].push, 99)


if __name__ == "__main__":
    unittest.main()
