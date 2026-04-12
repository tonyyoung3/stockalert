import yfinance as yf
import pandas as pd
import os
from datetime import date
from pathlib import Path
import mplfinance as mpf
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from db import init_db, save_alert

# -----------------------------
# 使用者設定 (可透過環境變數覆蓋)
# -----------------------------
shadow_ratio         = float(os.environ.get("SHADOW_RATIO",         "1.5"))   # 上影線至少是實體的 N 倍
upper_shadow_min_pct = float(os.environ.get("UPPER_SHADOW_MIN_PCT", "0.02"))  # 前一天上影線至少佔收盤價的 N%
min_daily_gain       = float(os.environ.get("MIN_DAILY_GAIN",       "0.01"))  # 當天漲幅至少 N%

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

# 判斷上影線 + 第二天收復
# -----------------------------
def _check_reversal_pattern(df, day1_idx, day2_idx):
    """檢查指定兩天的上影線反轉模式 (輔助函數)"""
    open1, close1, high1 = df['Open'].iloc[day1_idx], df['Close'].iloc[day1_idx], df['High'].iloc[day1_idx]
    body1 = abs(close1 - open1)
    upper_shadow1 = high1 - max(open1, close1)
    
    close2 = df['Close'].iloc[day2_idx]
    
    # 上影線長度需同時滿足：
    # 1) 長於實體的 shadow_ratio 倍
    # 2) 佔前一日收盤價至少 upper_shadow_min_pct
    upper_shadow_pct = (upper_shadow1 / close1) if close1 != 0 else 0
    first_day_shadow = (body1 > 0 and upper_shadow1 / body1 > shadow_ratio) and (upper_shadow_pct >= upper_shadow_min_pct)
    
    # 第二天收復上影線
    second_day_recover = close2 >= high1
    
    # 第二天漲幅要 min_daily_gain 以上，且成交量高於均量
    daily_gain     = (close2 - df['Close'].iloc[day1_idx]) / df['Close'].iloc[day1_idx] >= min_daily_gain
    vol2           = df['Volume'].iloc[day2_idx]
    volume_confirm = vol2 > df['Volume'].mean()

    return first_day_shadow and second_day_recover and daily_gain and volume_confirm

def check_upper_shadow_reversal(df):
    if len(df) < 22:  # 需要至少22天資料來計算月線和檢查模式
        return False
    
    # 檢查昨天和前天
    pattern_match = _check_reversal_pattern(df, -2, -1)
    
    # 今天收盤價在月線上（20日均線）
    ma20 = df['Close'].rolling(window=20).mean().iloc[-1]
    above_ma20 = df['Close'].iloc[-1] > ma20
    
    return pattern_match and above_ma20

# 判斷 Inside Day（昨天跟前天符合上影線反轉條件，今天收盤價是三天最高）
# -----------------------------
def check_inside_day(df):
    if len(df) < 22:  # 需要足夠資料來檢查模式和計算月線
        return False
    
    # 前天符合上影線反轉條件 (檢查 D-3 和 D-2)
    day_before_match = _check_reversal_pattern(df, -3, -2)
    
    # 昨天符合上影線反轉條件 (檢查 D-2 和 D-1)
    yesterday_match = _check_reversal_pattern(df, -2, -1)
    
    # 今天收盤價是三天最高
    today_close = df['Close'].iloc[-1]
    three_day_high = df['Close'].iloc[-3:].max()
    is_three_day_high = today_close == three_day_high
    
    # 今天收盤價在月線上（20日均線）
    ma20 = df['Close'].rolling(window=20).mean().iloc[-1]
    above_ma20 = today_close > ma20
    
    return day_before_match and yesterday_match and is_three_day_high and above_ma20

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
    data = yf.download(taiwan_stocks, period="2mo", interval="1d", group_by='ticker', threads=True)
    upper_shadow_results = []
    inside_day_results = []
    
    # 建立儲存圖表的資料夾
    charts_dir = Path("charts")
    charts_dir.mkdir(exist_ok=True)

    for ticker in taiwan_stocks:
        try:
            # yfinance 在多股票下載時，會將 ticker 作為列名
            df = data[ticker] if len(taiwan_stocks) > 1 else data
            if df.empty or len(df) < 22:
                # print(f"Skipping {ticker} due to insufficient data ({len(df)} days)")
                continue

            ticker_clean = ticker.split('.')[0]  # 去除 .TW/.TWO
            
            if check_upper_shadow_reversal(df):
                print(f"  -> Upper shadow reversal match: {ticker_clean}")
                chart_path = charts_dir / f"{ticker_clean}_upper_shadow.png"
                create_stock_chart(df, ticker, chart_path, "Upper Shadow Reversal", [-2, -1])
                upper_shadow_results.append((ticker_clean, chart_path))
                save_alert(ticker_clean, "upper_shadow_reversal", str(date.today()), float(df["Close"].iloc[-1]))

            elif check_inside_day(df):
                print(f"  -> Inside day match: {ticker_clean}")
                chart_path = charts_dir / f"{ticker_clean}_inside_day.png"
                create_stock_chart(df, ticker, chart_path, "Inside Day", [-3, -2, -1])
                inside_day_results.append((ticker_clean, chart_path))
                save_alert(ticker_clean, "inside_day", str(date.today()), float(df["Close"].iloc[-1]))

        except Exception as e:
            print(f"Error processing {ticker}: {e}")

    # 顯示結果 + 發 Slack
    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_channel = os.environ.get("SLACK_CHANNEL")

    if slack_token and slack_channel:
        client = WebClient(token=slack_token)
        print("\nSending results to Slack...")
        
        # 傳送上影線反轉結果
        if upper_shadow_results:
            send_to_slack(client, slack_channel, "--- 🔺 台股篩選結果：上影線反轉 (Upper Shadow Reversal) ---")
            for ticker, chart_path in upper_shadow_results:
                public_url = upload_file_and_get_public_url(client, slack_channel, str(chart_path), f"{ticker} Chart")
                if public_url:
                    blocks = [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": f"標的: *{ticker}*"}
                        },
                        {
                            "type": "image",
                            "title": {"type": "plain_text", "text": f"{ticker} - Upper Shadow Reversal"},
                            "image_url": public_url,
                            "alt_text": f"Chart for {ticker}"
                        }
                    ]
                    send_to_slack(client, slack_channel, text=f"標的: {ticker}", blocks=blocks)
        
        # 傳送 Inside Day 結果
        if inside_day_results:
            send_to_slack(client, slack_channel, "--- 📦 台股篩選結果：Inside Day ---")
            for ticker, chart_path in inside_day_results:
                public_url = upload_file_and_get_public_url(client, slack_channel, str(chart_path), f"{ticker} Chart")
                if public_url:
                    blocks = [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": f"標的: *{ticker}*"}
                        },
                        {
                            "type": "image",
                            "title": {"type": "plain_text", "text": f"{ticker} - Inside Day"},
                            "image_url": public_url,
                            "alt_text": f"Chart for {ticker}"
                        }
                    ]
                    send_to_slack(client, slack_channel, text=f"標的: {ticker}", blocks=blocks)
    else:
        print("\nSLACK_BOT_TOKEN or SLACK_CHANNEL not set; skipping Slack notification.")
        print("上影線反轉篩選結果:", [item[0] for item in upper_shadow_results])
        print("Inside Day篩選結果:", [item[0] for item in inside_day_results])

if __name__ == "__main__":
    main()