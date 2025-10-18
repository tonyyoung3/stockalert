import os
import re
import yfinance as yf
import pandas as pd
import mplfinance as mpf
from pathlib import Path
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# -----------------------------
# 初始化
# -----------------------------
# 需要一個 App-Level Token (xapp-...) 來啟用 Socket Mode
# 和一個 Bot Token (xoxb-...) 來呼叫 API
app = App(token=os.environ.get("SLACK_BOT_TOKEN"))

# 建立儲存圖表的資料夾
charts_dir = Path("charts_interactive")
charts_dir.mkdir(exist_ok=True)

# -----------------------------
# 繪圖函式 (可與 screener.py 共用)
# -----------------------------
def create_stock_chart_for_request(df, ticker, filename):
    """為單一請求繪製 K 線圖並儲存"""
    df_plot = df.tail(20).copy()

    mc = mpf.make_marketcolors(up='r', down='g', inherit=True)
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle='--')

    mpf.plot(df_plot,
             type='candle',
             style=style,
             title=f'{ticker} - Last 20 Days',
             ylabel='Price',
             mav=(5, 10), # 加上 5日和10日均線
             savefig=dict(fname=filename, dpi=100, pad_inches=0.25)
            )

# -----------------------------
# Slack 事件監聽
# -----------------------------
# 監聽所有訊息，並從中尋找股票代碼或 'help' 指令
@app.message("") # 監聽所有訊息
def handle_any_message(message, say, logger):
    """處理任何訊息，並從中尋找指令"""
    text = message.get('text', '').strip()
    channel_id = message['channel']
    thread_ts = message.get('ts') # 取得原始訊息的時間戳，用於回覆在同一個 thread

    # 1. 檢查 'help' 指令
    # 1. 優先檢查 'help' 指令
    if text.lower() == 'help':
        say(
            text="如何使用我：\n直接輸入股票代碼（例如 `2330.TW` 或 `TSLA`），我就會回傳最近的 K 線圖。",
            thread_ts=thread_ts
        )
        return

    # 2. 使用正則表達式從訊息中尋找股票代碼
    # 這個正則表達式會尋找像 2330.TW, TSLA, 0050.TW 這樣的字串
    match = re.search(r'\b([A-Z0-9]{2,10}(\.[A-Z]{2,3})?)\b', text.upper())

    # 只有在找到匹配項時才繼續
    if match:
        ticker = match.group(1)
    else:
        # 如果訊息不是 'help' 也找不到股票代碼，就忽略它
        return
    logger.info(f"Received ticker request: {ticker} in channel {channel_id}")

    try:
        # 1. 回覆一個處理中的訊息
        reply = say(text=f"好的，正在查詢 `{ticker}` 的最近 20 天股價...", thread_ts=thread_ts)

        # 2. 抓取資料
        stock_data = yf.download(ticker, period="25d", interval="1d") # 多抓幾天以防假日
        if stock_data.empty:
            app.client.chat_update(
                channel=channel_id,
                ts=reply['ts'],
                text=f"抱歉，找不到股票代碼 `{ticker}` 的資料。請確認代碼是否正確（台股請加上 `.TW` 或 `.TWO`）。"
            )
            return

        # 3. 產生圖表
        chart_path = charts_dir / f"{ticker.replace('.', '_')}_chart.png"
        create_stock_chart_for_request(stock_data, ticker, chart_path)

        # 4. 上傳圖表並更新訊息
        with open(chart_path, "rb") as file_content:
            app.client.files_upload_v2(
                channel=channel_id,
                thread_ts=thread_ts,
                file=file_content,
                initial_comment=f"這是 `{ticker}` 的最近 20 天 K 線圖：",
                title=f"{ticker} Chart"
            )
        
        # 刪除 "處理中" 的訊息
        app.client.chat_delete(channel=channel_id, ts=reply['ts'])
        
        # 5. 刪除本機圖檔
        os.remove(chart_path)

    except Exception as e:
        logger.error(f"Error processing ticker {ticker}: {e}")
        say(text=f"處理 `{ticker}` 時發生錯誤。", thread_ts=thread_ts)


# -----------------------------
# 啟動機器人
# -----------------------------
if __name__ == "__main__":
    # 檢查必要的 token
    if not os.environ.get("SLACK_BOT_TOKEN") or not os.environ.get("SLACK_APP_TOKEN"):
        print("錯誤：SLACK_BOT_TOKEN 和 SLACK_APP_TOKEN 環境變數必須被設定。")
    else:
        print("🤖 Slack bot is running in Socket Mode...")
        # SocketModeHandler 會處理與 Slack 的連線
        SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()