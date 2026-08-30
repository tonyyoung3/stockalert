import unittest
from datetime import date

from ptt_stock import (
    Post,
    build_digest,
    chat_kind,
    cluster_themes,
    collect_posts,
    extract_tickers,
    format_digest,
    parse_index_html,
    parse_list_date,
    parse_push,
    summarize_chat,
    theme_key,
)

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
  <div class="nrec"><span>8</span></div>
  <div class="title"><a href="/bbs/Stock/M.chat.html">[閒聊] 2026/08/29 盤中閒聊</a></div>
  <div class="author">laptic</div>
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
        titles = {p.title for p in posts}
        self.assertEqual(titles, {"[標的] 2330 多", "[閒聊] 2026/08/29 盤中閒聊"})
        self.assertEqual(next(p.push for p in posts if "2330" in p.title), 99)

    def test_theme_key_merges_replies(self):
        self.assertEqual(theme_key("Re: [新聞] 遭控陸製變MIT！欣興涉「洗產地」被搜索"), "欣興")
        self.assertEqual(theme_key("[標的] 2330 多"), "2330")
        self.assertEqual(chat_kind("[閒聊] 2026/08/28 盤後閒聊"), "盤後閒聊")
        self.assertIsNone(chat_kind("[閒聊] 低推"))

    def test_extract_tickers_skips_years(self):
        self.assertEqual(extract_tickers("2026 年 2330 跟 0050"), ["2330", "0050"])
        self.assertEqual(extract_tickers("看到 1100元 跟 1500萬 還有 8320元"), [])
        self.assertEqual(extract_tickers("買 2330 到 1000"), ["2330"])

    def test_cluster_skips_routine_and_chat(self):
        posts = [
            Post("2026-08-29", 100, "爆", "[新聞] 欣興涉洗產地", "a", "http://a"),
            Post("2026-08-29", 99, "99", "Re: [新聞] 欣興搜索", "b", "http://b"),
            Post("2026-08-28", 100, "爆", "[情報] 三大法人買賣金額統計表", "c", "http://c"),
            Post("2026-08-28", 100, "爆", "[閒聊] 2026/08/28 盤後閒聊", "d", "http://d"),
            Post("2026-08-27", 80, "80", "[標的] 2330 多", "e", "http://e"),
        ]
        themes = cluster_themes(posts)
        keys = [t.key for t in themes]
        self.assertIn("欣興", keys)
        self.assertIn("2330", keys)
        self.assertNotIn("三大法人", "".join(keys))
        xin = next(t for t in themes if t.key == "欣興")
        self.assertEqual(len(xin.posts), 2)

    def test_summarize_chat_counts_tickers(self):
        html = """
        <div id="main-content">
          <div class="push"><span class="push-tag">推 </span><span class="push-userid">aa</span>
            <span class="push-content">: 2330 好強今天</span></div>
          <div class="push"><span class="push-tag">推 </span><span class="push-userid">bb</span>
            <span class="push-content">: 3037 洗產地</span></div>
          <div class="push"><span class="push-tag">噓 </span><span class="push-userid">cc</span>
            <span class="push-content">: 2330 看空</span></div>
          <div class="push"><span class="push-tag">推 </span><span class="push-userid">dd</span>
            <span class="push-content">: 2330 再來</span></div>
          <div class="push"><span class="push-tag">推 </span><span class="push-userid">ee</span>
            <span class="push-content">: https://i.imgur.com/abc.gif</span></div>
          <div class="push"><span class="push-tag">推 </span><span class="push-userid">gg</span>
            <span class="push-content">: https://i.imgur.com/UE2rvN9.jpeg 1110 →110</span></div>
          <div class="push"><span class="push-tag">推 </span><span class="push-userid">ff</span>
            <span class="push-content">: 1100元太貴了吧還有1500萬</span></div>
        </div>
        """
        post = Post("2026-08-28", 100, "爆", "[閒聊] 2026/08/28 盤後閒聊", "laptic", "http://chat")
        chat = summarize_chat(post, html, comment_limit=5)
        self.assertEqual(chat.tickers[0], ("2330", 2))
        self.assertNotIn("1100", [code for code, _ in chat.tickers])
        self.assertTrue(any("3037" in p.content for p in chat.comments))
        self.assertFalse(any(p.user == "cc" for p in chat.comments))
        self.assertFalse(any(p.user == "ee" for p in chat.comments))
        self.assertFalse(any(p.user == "gg" for p in chat.comments))
        self.assertNotIn("1110", [code for code, _ in chat.tickers])

    def test_build_digest_uses_injected_chat_html(self):
        posts = [
            Post("2026-08-29", 99, "99", "[標的] 2330 多", "foo", "http://t"),
            Post("2026-08-28", 100, "爆", "[閒聊] 2026/08/28 盤後閒聊", "laptic", "http://chat"),
        ]
        pages = {
            "http://chat": """
            <div class="push"><span class="push-tag">推 </span><span class="push-userid">zz</span>
              <span class="push-content">: 今天 2330 還行</span></div>
            """
        }
        digest = build_digest(posts, fetch=pages.__getitem__, sleep_s=0)
        text = format_digest(digest, 7, 30)
        self.assertIn("## 題材", text)
        self.assertIn("## 標的文", text)
        self.assertIn("盤後閒聊", text)
        self.assertIn("2330", text)


if __name__ == "__main__":
    unittest.main()
