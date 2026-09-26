"""No live Yahoo or macOS calls; boundary and restart tests use a fixed clock."""
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pandas as pd

from market.intraday_prices import Bar, evaluate, fetch_minutes, monitoring_session
from notify.intraday_alert import Monitor, open_store, recent_events, record_signal, worker_lock
from notify.local_app import event_row
from web.tw_calendar import TW


def clock(hour=10, minute=5, second=10):
    return datetime(2026, 9, 24, hour, minute, second, tzinfo=TW)


def bars(end=None, price=102):
    end = end or clock(second=0)
    return [Bar(end-timedelta(minutes=5-i),100,price if i == 4 else 100,1000)
            for i in range(5)]


def members():
    return {"etf":"0050", "asof":"2026-09-23", "version":"test-v1", "stocks":[
        {"code":str(1100+i),"name":f"測試{i}","weight":2} for i in range(50)]}


class DetectionTests(unittest.TestCase):
    def test_exact_two_percent_and_below(self):
        self.assertTrue(evaluate(bars(),clock())["triggered"])
        self.assertFalse(evaluate(bars(price=101.999),clock())["triggered"])
        self.assertEqual(evaluate(bars(),clock())["rise_pct"],2)

    def test_complete_bar_only_even_with_unfinished_spike(self):
        data=bars(price=100)+[Bar(clock(second=0),100,120,1000)]
        self.assertFalse(evaluate(data,clock())["triggered"])

    def test_low_outside_five_minutes_excluded(self):
        data=[Bar(clock(second=0)-timedelta(minutes=6),50,50,1000)]+bars(price=101)
        self.assertFalse(evaluate(data,clock())["triggered"])

    def test_same_minute_low_to_final_close_counts(self):
        data=bars(price=100)
        data[-1]=Bar(data[-1].start,98,100,1000)
        self.assertTrue(evaluate(data,clock())["triggered"])

    def test_reject_gaps_stale_zero_volume_nan_and_overnight(self):
        cases={"incomplete":bars()[1:], "stale":bars(clock()-timedelta(minutes=4,seconds=10)),
               "missing":bars(clock()-timedelta(days=1,seconds=10))}
        for expected,data in cases.items():
            with self.subTest(expected=expected): self.assertEqual(evaluate(data,clock())["status"],expected)
        for lo,price,volume in [(100,102,0),(float('nan'),102,10),(100,0,10),(110,102,10)]:
            data=bars(); data[-1]=Bar(data[-1].start,lo,price,volume)
            self.assertEqual(evaluate(data,clock())["status"],"invalid")

    def test_out_of_order_and_duplicate_bars(self):
        data=list(reversed(bars()))+[bars()[0]]
        self.assertTrue(evaluate(data,clock())["triggered"])

    def test_trading_hours_holidays_unknown_year(self):
        self.assertFalse(monitoring_session(clock(hour=8)))
        self.assertFalse(monitoring_session(clock(hour=14)))
        self.assertFalse(monitoring_session(datetime(2026,9,26,10,tzinfo=TW)))
        self.assertFalse(monitoring_session(datetime(2026,10,9,10,tzinfo=TW)))
        self.assertFalse(monitoring_session(datetime(2030,1,2,10,tzinfo=TW)))

    def test_download_multiindex_and_missing_stock(self):
        frame=pd.DataFrame({('1100.TW','Low'):[100],('1100.TW','Close'):[102],
                            ('1100.TW','Volume'):[1000]},index=pd.DatetimeIndex([clock(second=0)]))
        fetch=Mock(return_value=frame)
        result=fetch_minutes(['1100','1101'],downloader=fetch)
        self.assertEqual(result['1100'][0].close,102)
        self.assertEqual(result['1101'],[])
        self.assertFalse(fetch.call_args.kwargs['auto_adjust'])
        self.assertEqual(fetch.call_args.kwargs['threads'],4)

    def test_timezone_naive_feed_is_rejected(self):
        frame=pd.DataFrame({'Low':[100],'Close':[102],'Volume':[1]},index=pd.to_datetime(['2026-09-24 10:00']))
        with self.assertRaises(ValueError): fetch_minutes(['1100'],downloader=Mock(return_value=frame))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'state.db'
        self.conn=open_store(self.path);self.addCleanup(self.conn.close)
        self.snapshot=members()

    def signal(self,minute=5,price=102):
        now=clock(minute=minute)
        return evaluate(bars(now.replace(second=0),price=price),now)

    def record(self,minute=5,price=102,code='1100'):
        return record_signal(self.conn,code,'測試',self.signal(minute,price),self.snapshot)

    def test_non_member_ignored(self):
        self.assertIsNone(self.record(code='9999'))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM intraday_events').fetchone()[0],0)

    def test_same_wave_and_bar_cannot_repeat(self):
        self.assertIsNotNone(self.record())
        self.assertIsNone(self.record())
        self.assertIsNone(self.record(minute=6))
        self.assertIsNone(self.record(minute=25))

    def test_rearm_requires_below_threshold_and_cooldown(self):
        self.record()
        self.record(minute=6,price=100)
        self.assertIsNone(self.record(minute=7))
        self.assertIsNone(self.record(minute=21))
        self.record(minute=22,price=100)
        self.assertIsNotNone(self.record(minute=23))

    def test_restart_retains_dedup_and_cooldown(self):
        self.record(); other=open_store(self.path)
        try:
            self.assertIsNone(record_signal(other,'1100','測試',self.signal(6),self.snapshot))
        finally: other.close()

    def test_lock_prevents_second_worker(self):
        with worker_lock(self.path):
            with self.assertRaises(RuntimeError):
                with worker_lock(self.path): pass

    def test_recent_events_are_newest_first(self):
        first=self.record();self.record(minute=6,price=100);second=self.record(minute=21)
        got=recent_events(self.conn)
        self.assertEqual([row['event_id'] for row in got],[second['event_id'],first['event_id']])

    def test_recent_events_limit_is_clamped(self):
        event=self.record()
        self.assertEqual(recent_events(self.conn,0)[0]['event_id'],event['event_id'])

    def test_event_table_row_is_single_line(self):
        row=event_row(self.record())
        self.assertEqual(row,('2026-09-24 10:05','測試（1100）','+2.00%','100 → 102'))
        self.assertFalse(any('\n' in value for value in row))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.conn=open_store(Path(self.tmp.name)/'dry.db');self.addCleanup(self.conn.close)
        self.fetch=Mock()
        self.loader=Mock(return_value=members())
        self.worker=Monitor(self.conn,fetcher=self.fetch,loader=self.loader)

    def cycle(self,minute,price=102):
        now=clock(minute=minute)
        self.fetch.return_value={'1100':bars(now.replace(second=0),price=price),
                                 '9999':bars(now.replace(second=0))}
        return self.worker.cycle(now)

    def test_warmup_dryrun_and_reconnect(self):
        self.assertEqual(self.cycle(5)['events'],0)
        for m in range(6,10):self.assertEqual(self.cycle(m)['events'],0)
        self.assertEqual(self.cycle(10)['events'],1)
        row=self.conn.execute('SELECT payload FROM intraday_events').fetchone()
        self.assertIsNotNone(row)
        # A gap forces a new warm-up and doesn't generate catch-up alerts.
        self.assertEqual(self.cycle(20)['events'],0)
        self.assertEqual(self.cycle(21)['counts']['warming'],1)

    def test_off_hours_fetches_nothing(self):
        got=self.worker.cycle(clock(hour=8))
        self.assertEqual(got['status'],'off_hours');self.fetch.assert_not_called()

    def test_bad_membership_blocks_quotes(self):
        self.loader.side_effect=RuntimeError('stale')
        self.assertEqual(self.worker.cycle(clock())['status'],'membership_unavailable')
        self.fetch.assert_not_called()

    def test_preopen_refreshes_members_without_quotes(self):
        result=self.worker.cycle(clock(hour=8,minute=50))
        self.assertEqual(result['status'],'preopen')
        self.loader.assert_called_once();self.fetch.assert_not_called()

    def test_live_clock_checked_after_slow_download(self):
        self.fetch.return_value={'1100':bars()}
        with patch('notify.intraday_alert.taiwan_now',side_effect=[clock(),clock(minute=8)]):
            result=self.worker.cycle()
        self.assertEqual(result['counts']['stale'],1)
        self.assertEqual(result['events'],0)

    def test_stale_gap_restarts_warmup(self):
        self.cycle(5)
        self.fetch.return_value={}
        self.worker.cycle(clock(minute=6))
        self.assertEqual(self.cycle(10)['counts']['warming'],1)

    def test_stale_quotes_are_observable(self):
        self.fetch.return_value={'1100':bars(clock(minute=1,second=0))}
        got=self.worker.cycle(clock())
        self.assertEqual(got['counts']['stale'],1)
        self.assertEqual(got['counts']['missing'],49)
        self.assertEqual(got['events'],0)
