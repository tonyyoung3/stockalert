import yfinance as yf
import pandas as pd
import os
from pathlib import Path
import mplfinance as mpf
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from db import init_db, save_alert, has_alert
from company_info import (
    CompanyProfile,
    fetch_profiles,
    format_digest,
    format_slack_caption,
    maybe_enrich_themes,
)
from prices import AUTO_ADJUST, extract_ohlcv, last_close
from signals.patterns import classify_pattern, last_bar_date

def load_tickers_from_file(filename="taiwan_stocks.txt"):
    """從檔案讀取股票代碼列表"""
    filepath = Path(__file__).parent / filename
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
def upload_file_and_get_public_url(client, channel, file_path, title):
    """上傳檔案到 Slack 並取得公開分享的 URL"""
    try:
        # 1. 上傳檔案
        result = client.files_upload_v2(
            channel=channel,
            file=file_path,
            title=title,
        )
        file_id = result["file"]["id"]

        # 2. 公開分享檔案以取得 URL
        # 注意：這會讓任何擁有連結的人都能看到圖片。
        # 如果你的 Slack Workspace 有限制，這一步可能需要管理員權限或調整設定。
        share_result = client.files_sharedPublicURL(file=file_id)
        if share_result.get("ok"):
            # URL 在 share_result['file']['permalink_public']
            # 我們需要從中提取直接的圖片 URL
            # 格式通常是： https://files.slack.com/files-pri/T...-F.../download/filename.png
            return share_result['file']['permalink_public']
        else:
            print(f"Error making file public: {share_result.get('error')}")
            return None

    except SlackApiError as e:
        print(f"Error uploading or sharing file: {e.response['error']}")
        return None

def send_to_slack(client, channel, text=None, blocks=None):
    """傳送訊息和檔案到 Slack"""
    try:
        client.chat_postMessage(channel=channel, text=text, blocks=blocks)
    except SlackApiError as e:
        print(f"Error sending to Slack: {e.response['error']}")


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
        public_url = upload_file_and_get_public_url(client, channel, str(chart_path), f"{ticker} Chart")
        if not public_url:
            continue
        caption = format_slack_caption(profile)
        send_to_slack(
            client,
            channel,
            text=caption,
            blocks=chart_blocks(caption, public_url, f"{ticker} - {pattern_title}"),
        )
        posted.append((profile, pattern_title))
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
    
    # 建立儲存圖表的資料夾
    charts_dir = Path("charts")
    charts_dir.mkdir(exist_ok=True)

    for ticker in taiwan_stocks:
        try:
            # yfinance 在多股票下載時，會將 ticker 作為列名
            df = extract_ohlcv(data, ticker)
            if df.empty or len(df) < 22:
                # print(f"Skipping {ticker} due to insufficient data ({len(df)} days)")
                continue

            ticker_clean = ticker.split('.')[0]  # 去除 .TW/.TWO
            pattern = classify_pattern(df)
            if not pattern:
                continue

            # Use the last candle date, not the runner's calendar date, so
            # weekend/holiday reruns do not create a second alert for the same bar.
            signal_date = str(last_bar_date(df))
            if has_alert(ticker_clean, pattern, signal_date):
                print(f"  -> Skip duplicate {pattern} for {ticker_clean} on {signal_date}")
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

    # 顯示結果 + 發 Slack
    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_channel = os.environ.get("SLACK_CHANNEL")

    if slack_token and slack_channel:
        client = WebClient(token=slack_token)
        print("\nSending results to Slack...")
        
        hit_tickers = [t for t, _ in upper_shadow_results] + [t for t, _ in inside_day_results]
        profiles = fetch_profiles(hit_tickers)
        maybe_enrich_themes(list(profiles.values()))

        posted: list[tuple[CompanyProfile, str]] = []
        posted += post_alert_charts(
            client,
            slack_channel,
            "--- 🔺 台股篩選結果：上影線反轉 (Upper Shadow Reversal) ---",
            upper_shadow_results,
            profiles,
            "上影線反轉",
        )
        posted += post_alert_charts(
            client,
            slack_channel,
            "--- 📦 台股篩選結果：Inside Day ---",
            inside_day_results,
            profiles,
            "Inside Day",
        )
        if posted:
            send_to_slack(client, slack_channel, format_digest(posted))
    else:
        print("\nSLACK_BOT_TOKEN or SLACK_CHANNEL not set; skipping Slack notification.")
        print("上影線反轉篩選結果:", [item[0] for item in upper_shadow_results])
        print("Inside Day篩選結果:", [item[0] for item in inside_day_results])

if __name__ == "__main__":
    main()