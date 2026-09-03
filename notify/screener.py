import yfinance as yf
import pandas as pd
import os
from collections import Counter
from pathlib import Path
import mplfinance as mpf
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from alertsdb import init_db, save_alert, has_alert
from data.paths import repo_file
from data.prices import AUTO_ADJUST, extract_ohlcv, last_close, drop_incomplete_ohlc, patch_incomplete_closes
from notify.company_info import (
    CompanyProfile,
    fetch_profiles,
    format_digest,
    format_slack_caption,
    maybe_enrich_themes,
)
from signals.patterns import classify_pattern, last_bar_date

def load_tickers_from_file(filename="taiwan_stocks.txt"):
    """從檔案讀取股票代碼列表"""
    filepath = repo_file(filename)
    if not filepath.is_file():
        print(f"Warning: Ticker file '{filename}' not found.")
        return []
    with open(filepath, 'r') as f:
        # 移除空白行和多餘的空格
        tickers = [line.strip() for line in f if line.strip()]
    return tickers

# -----------------------------
# 繪製 K 線圖
# -----------------------------
def create_stock_chart(df, ticker, filename, pattern_name, pattern_indices):
    """使用 mplfinance 為股票繪製 K 線圖並儲存"""
    df_plot = df.tail(40).copy()  # 取最近 40 天資料繪圖

    # 準備標記信號：建立一個與 df_plot 相同 index 的 Series，並填滿 NaN
    signal_series = pd.Series(float('nan'), index=df_plot.index)

    # 將負數索引轉換為 df_plot 中的位置，並在 signal_series 中設定價格
    for idx in pattern_indices:
        # 轉換負索引為正索引
        plot_idx = len(df_plot) + idx
        if 0 <= plot_idx < len(df_plot):
            # 取得要標記的日期和價格
            signal_date = df_plot.index[plot_idx]
            price = df_plot['High'].iloc[plot_idx] * 1.015 # 標記在 K 線上方
            # 在 Series 中設定價格
            signal_series[signal_date] = price

    # 建立標記的 addplot
    ap = []
    if not signal_series.isnull().all():
        ap.append(mpf.make_addplot(
            signal_series,
            type='scatter',
            marker='*',
            color='blue',
            markersize=150  # 標記大小
        ))

    # 設定圖表樣式
    mc = mpf.make_marketcolors(up='r', down='g', inherit=True)
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle='--')

    # 繪製並儲存圖表
    mpf.plot(df_plot,
             type='candle',
             style=style,
             title=f'{ticker} - {pattern_name} (Last 40 Days)',
             ylabel='Price',
             addplot=ap,
             mav=(20),  # 加入 20 日均線
             # 為了在本機執行時顯示圖表，可以暫時註解掉下面這行
             savefig=dict(fname=filename, dpi=100, pad_inches=0.25)
            )

# -----------------------------
# Slack 通知
# -----------------------------
def upload_chart(client, channel: str, file_path: str, title: str, comment: str) -> bool:
    """Post a chart into the channel. Bot tokens cannot call files.sharedPublicURL."""
    try:
        client.files_upload_v2(
            channel=channel,
            file=file_path,
            title=title,
            initial_comment=comment,
        )
        return True
    except SlackApiError as e:
        err = e.response.get("error") if getattr(e, "response", None) else str(e)
        print(f"Error uploading file: {err}")
        return False


def send_to_slack(client, channel, text=None, blocks=None):
    """傳送訊息到 Slack。失敗只記 log，不中斷篩選。"""
    try:
        client.chat_postMessage(channel=channel, text=text, blocks=blocks)
        return True
    except SlackApiError as e:
        err = e.response.get("error") if getattr(e, "response", None) else str(e)
        print(f"Error sending to Slack: {err}")
        return False


def chart_blocks(caption: str, image_url: str, title: str) -> list[dict]:
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": caption},
        },
        {
            "type": "image",
            "title": {"type": "plain_text", "text": title},
            "image_url": image_url,
            "alt_text": title,
        },
    ]


def post_alert_charts(
    client,
    channel: str,
    heading: str,
    hits: list[tuple[str, Path]],
    profiles: dict[str, CompanyProfile],
    pattern_title: str,
) -> list[tuple[CompanyProfile, str]]:
    posted: list[tuple[CompanyProfile, str]] = []
    if not hits:
        return posted
    send_to_slack(client, channel, heading)
    for ticker, chart_path in hits:
        profile = profiles.get(ticker) or CompanyProfile(ticker=ticker, symbol=ticker)
        caption = format_slack_caption(profile)
        uploaded = upload_chart(
            client, channel, str(chart_path), f"{ticker} Chart", caption
        )
        if not uploaded:
            send_to_slack(client, channel, text=caption)
        posted.append((profile, pattern_title))
    return posted


def pick_scan_date(dates: list[str]) -> str | None:
    """Most common last-complete-bar date so Slack names the session actually scored."""
    if not dates:
        return None
    return Counter(dates).most_common(1)[0][0]


def format_empty_screener_message(
    signal_date: str | None = None,
    skipped_duplicates: int = 0,
) -> str:
    when = f"（{signal_date}）" if signal_date else ""
    if skipped_duplicates:
        return f"今日台股篩選{when}沒有新的符合標的（先前已通知）。"
    return f"今日台股篩選{when}沒有符合的標的。"


def post_screener_results(
    client,
    channel: str,
    upper_shadow_results: list[tuple[str, Path]],
    inside_day_results: list[tuple[str, Path]],
    profiles: dict[str, CompanyProfile],
    signal_date: str | None = None,
    skipped_duplicates: int = 0,
) -> list[tuple[CompanyProfile, str]]:
    """Post charts + digest, or a no-hit notice so a quiet day is not silent."""
    if not upper_shadow_results and not inside_day_results:
        send_to_slack(
            client,
            channel,
            format_empty_screener_message(signal_date, skipped_duplicates),
        )
        return []

    posted: list[tuple[CompanyProfile, str]] = []
    posted += post_alert_charts(
        client,
        channel,
        "--- 🔺 台股篩選結果：上影線反轉 (Upper Shadow Reversal) ---",
        upper_shadow_results,
        profiles,
        "上影線反轉",
    )
    posted += post_alert_charts(
        client,
        channel,
        "--- 📦 台股篩選結果：Inside Day ---",
        inside_day_results,
        profiles,
        "Inside Day",
    )
    if posted:
        send_to_slack(client, channel, format_digest(posted))
    return posted

# -----------------------------
# 主要執行邏輯
# -----------------------------
def main():
    init_db()

    taiwan_stocks = load_tickers_from_file("taiwan_stocks.txt")
    if not taiwan_stocks:
        print("No tickers loaded. Exiting.")
        return

    # 批次抓取（S&P 500 股票不多，可以一次抓）
    # 期間設為 "2mo" 確保有足夠資料計算月線和繪圖
    data = yf.download(
        taiwan_stocks,
        period="2mo",
        interval="1d",
        group_by="ticker",
        threads=True,
        auto_adjust=AUTO_ADJUST,
    )
    upper_shadow_results = []
    inside_day_results = []
    last_bar_dates: list[str] = []
    skipped_duplicates = 0
    skipped_short = 0

    # 建立儲存圖表的資料夾
    charts_dir = Path("charts")
    charts_dir.mkdir(exist_ok=True)

    raw_frames: dict[str, pd.DataFrame] = {}
    for ticker in taiwan_stocks:
        raw_frames[ticker] = extract_ohlcv(data, ticker, complete_only=False)
    raw_frames = patch_incomplete_closes(raw_frames)

    for ticker in taiwan_stocks:
        try:
            df = drop_incomplete_ohlc(raw_frames.get(ticker))
            if df.empty or len(df) < 22:
                skipped_short += 1
                continue

            ticker_clean = ticker.split('.')[0]  # 去除 .TW/.TWO
            signal_date = str(last_bar_date(df))
            last_bar_dates.append(signal_date)
            pattern = classify_pattern(df)
            if not pattern:
                continue

            # Use the last candle date, not the runner's calendar date, so
            # weekend/holiday reruns do not create a second alert for the same bar.
            if has_alert(ticker_clean, pattern, signal_date):
                print(f"  -> Skip duplicate {pattern} for {ticker_clean} on {signal_date}")
                skipped_duplicates += 1
                continue

            price = last_close(df, ticker)
            if price is None:
                print(f"  -> Skip {ticker_clean}: could not read close")
                continue
            if pattern == "inside_day":
                print(f"  -> Inside day match: {ticker_clean} ({signal_date})")
                chart_path = charts_dir / f"{ticker_clean}_inside_day.png"
                create_stock_chart(df, ticker, chart_path, "Inside Day", [-3, -2, -1])
                if save_alert(ticker_clean, pattern, signal_date, price):
                    inside_day_results.append((ticker_clean, chart_path))
            else:
                print(f"  -> Upper shadow reversal match: {ticker_clean} ({signal_date})")
                chart_path = charts_dir / f"{ticker_clean}_upper_shadow.png"
                create_stock_chart(df, ticker, chart_path, "Upper Shadow Reversal", [-2, -1])
                if save_alert(ticker_clean, pattern, signal_date, price):
                    upper_shadow_results.append((ticker_clean, chart_path))

        except Exception as e:
            print(f"Error processing {ticker}: {e}")

    scan_date = pick_scan_date(last_bar_dates)
    unique_dates = ", ".join(sorted(set(last_bar_dates)))
    print(
        f"\nScan complete: {len(taiwan_stocks)} tickers, "
        f"{skipped_short} insufficient data, "
        f"{skipped_duplicates} already notified, "
        f"{len(upper_shadow_results)} upper-shadow, "
        f"{len(inside_day_results)} inside-day"
        + (f"; last complete bar {unique_dates}" if unique_dates else "")
    )

    # 顯示結果 + 發 Slack
    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_channel = os.environ.get("SLACK_CHANNEL")

    if slack_token and slack_channel:
        client = WebClient(token=slack_token)
        print("\nSending results to Slack...")
        
        hit_tickers = [t for t, _ in upper_shadow_results] + [t for t, _ in inside_day_results]
        profiles = fetch_profiles(hit_tickers)
        maybe_enrich_themes(list(profiles.values()))
        post_screener_results(
            client,
            slack_channel,
            upper_shadow_results,
            inside_day_results,
            profiles,
            signal_date=scan_date,
            skipped_duplicates=skipped_duplicates,
        )
    else:
        print("\nSLACK_BOT_TOKEN or SLACK_CHANNEL not set; skipping Slack notification.")
        print("上影線反轉篩選結果:", [item[0] for item in upper_shadow_results])
        print("Inside Day篩選結果:", [item[0] for item in inside_day_results])
        if not upper_shadow_results and not inside_day_results:
            print(format_empty_screener_message(scan_date, skipped_duplicates))

if __name__ == "__main__":
    main()
