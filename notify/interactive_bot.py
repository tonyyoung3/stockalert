import os
import re
import sys
import yfinance as yf
import pandas as pd

# 解決在 macOS 背景執行緒中使用 matplotlib 的問題
# 必須在 import mplfinance 或 matplotlib.pyplot 之前設定
import matplotlib
matplotlib.use('Agg')
import mplfinance as mpf
from pathlib import Path
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError
from yfinance.exceptions import YFPricesMissingError
from dotenv import load_dotenv

import logging
# -----------------------------
# 初始化
# -----------------------------
# 載入 .env 檔案中的環境變數，方便本地開發
load_dotenv()

# 需要一個 App-Level Token (xapp-...) 來啟用 Socket Mode
# 隔離 yfinance 的日誌，防止其干擾 slack-bolt
logging.getLogger('yfinance').setLevel(logging.WARNING)
# 精準地將 yfinance 及其底層網路函式庫的日誌級別調高，
# 防止它們的 INFO 等級日誌干擾 slack-bolt。這是解決遞迴查詢 "HTTP" 問題的根本方法。
logging.getLogger('yfinance').setLevel(logging.ERROR)
logging.getLogger('urllib3').setLevel(logging.ERROR)

# 和一個 Bot Token (xoxb-...) 來呼叫 API
app = App(token=os.environ.get("SLACK_BOT_TOKEN"))


def harness_enabled() -> bool:
    from harness.config import harness_enabled as _enabled

    return _enabled()


def answer_with_harness(question: str) -> str:
    from harness.agent import run_agent
    from harness.models import OpenAICompatModel

    result = run_agent(question, OpenAICompatModel.from_env())
    return result.answer or "沒有得到可用的回答。"

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
    logger.info(f"--- New message received: {message.get('text', '')} ---")
    
    # 雙重保險：
    # 1. 忽略所有帶有 subtype 的訊息（機器人回覆、檔案上傳等）
    # 2. 確保訊息中有 'user' 欄位，這可以過濾掉非使用者事件（例如被錯誤捕捉的日誌）
    # 這是消除 "Unhandled request" 警告的最佳實踐
    if message.get('subtype') is not None or 'user' not in message:
        return

    text = message.get('text', '').strip()
    channel_id = message['channel']
    thread_ts = message.get('ts') # 取得原始訊息的時間戳，用於回覆在同一個 thread

    # 1. 檢查 'help' 指令
    # 1. 優先檢查 'help' 指令
    if text.lower() == 'help':
        help_text = "如何使用我：\n直接輸入股票代碼（例如 `2330.TW` 或 `TSLA`），我就會回傳最近的 K 線圖。"
        if harness_enabled():
            help_text += "\n也可以問訊號或績效，例如：`最近 alerts`、`2330 有沒有出過訊號`、`28 天績效如何`。"
        say(
            text=help_text,
            thread_ts=thread_ts,
            reply_broadcast=True # 將 help 訊息也廣播到頻道
        )
        return
    text = text.replace("http://", "")
    # 2. 使用正則表達式從訊息中尋找股票代碼
    # 這個正則表達式會尋找像 2330.TW, TSLA, 0050.TW 這樣的字串
    match = re.search(r'\b([A-Z0-9]{2,10}(\.[A-Z]{2,3})?)\b', text.upper())
    
    # 只有在找到匹配項時才繼續
    if match:
        ticker = match.group(1)
    else:
        # 沒有 ticker 時預設忽略；開了 harness 才把整句當問題。
        if harness_enabled():
            try:
                say(text=answer_with_harness(text), thread_ts=thread_ts)
            except Exception as exc:
                logger.error(f"Harness failed: {exc}")
                say(text="harness 暫時無法回答這個問題。", thread_ts=thread_ts)
        return
    logger.info(f"Step 1: Parsed ticker '{ticker}' from message.")

    reply = None  # 先將 reply 初始化為 None
    try:
        # 1. 回覆一個處理中的訊息
        reply = say(text=f"好的，正在查詢 `{ticker}` 的最近 20 天股價...", thread_ts=thread_ts)
        logger.info(f"Step 2: Sent 'in-progress' message for ticker '{ticker}'.")

        # 2. 抓取資料
        logger.info(f"Step 3: Downloading data for ticker '{ticker}'...")
        stock_data = yf.download( ticker, period="25d", interval="1d", group_by='ticker', progress=False, auto_adjust=True)
        stock_data = stock_data[ticker]
        
        # 檢查 yfinance 是否回傳了空的 DataFrame
        if stock_data.empty:
            logger.warning(f"Ticker '{ticker}' data is empty. Updating Slack message.")
            app.client.chat_update(
                channel=channel_id,
                ts=reply['ts'],
                text=f"抱歉，找不到股票代碼 `{ticker}` 的資料。請確認代碼是否正確（例如 `2330.TW` 或 `TSLA`）。"
            )
            return # 處理完畢，提前返回，讓 finally 區塊執行清理

        logger.info(f"Step 4: Data for '{ticker}' downloaded successfully. Creating chart...")
        # 3. 產生圖表
        chart_path = charts_dir / f"{ticker.replace('.', '_')}_chart.png"
        create_stock_chart_for_request(stock_data, ticker, chart_path)

        logger.info(f"Step 5: Chart for '{ticker}' created. Uploading to Slack...")
        # 4. 上傳圖表並更新訊息
        with open(chart_path, "rb") as file_content:
            app.client.files_upload_v2(
                channel=channel_id,
                # thread_ts=thread_ts, # 移除 thread_ts，讓它成為一則新訊息
                # reply_broadcast=True, # 因為不是回覆，所以這個參數也不需要了
                file=file_content,
                initial_comment=f"這是 `{ticker}` 的最近 20 天 K 線圖：",
                title=f"{ticker} Chart"
            )
        
        logger.info(f"Step 6: Chart for '{ticker}' uploaded. Deleting temporary message and local file.")
        # 5. 刪除 "處理中" 的訊息
        if reply and reply.get('ts'):
            app.client.chat_delete(channel=channel_id, ts=reply['ts'])
        # 6. 刪除本機圖檔
        os.remove(chart_path)

    except YFPricesMissingError:
        logger.warning(f"yfinance could not find ticker '{ticker}'.")
        if reply and reply.get('ts'):
            app.client.chat_update(
                channel=channel_id,
                ts=reply['ts'],
                text=f"抱歉，找不到股票代碼 `{ticker}` 的資料。請確認代碼是否正確（例如 `2330.TW` 或 `TSLA`）。"
            )
    except Exception as e:
        logger.error(f"Error processing ticker {ticker}: {e}")
        # 同樣使用 chat_update 更新訊息，而不是 say
        if reply and reply.get('ts'):
            app.client.chat_update(
                channel=channel_id,
                ts=reply['ts'],
                text=f"處理 `{ticker}` 時發生未預期的錯誤，請聯繫管理員。"
            )
    finally:
        # 無論成功或失敗，最後都嘗試刪除 "處理中" 的訊息
        logger.info(f"Step 7: Entering finally block for cleanup (ticker: '{ticker}').")
        # 在成功的情況下，訊息已經被刪除。
        # 在失敗的情況下，訊息會被 chat_update 更新為錯誤提示，我們希望保留它，所以這裡不需要做任何事。
        pass


# -----------------------------
# 啟動機器人
# -----------------------------
if __name__ == "__main__":
    # 檢查必要的 token
    if not os.environ.get("SLACK_BOT_TOKEN") or not os.environ.get("SLACK_APP_TOKEN"):
        print("錯誤：SLACK_BOT_TOKEN 和 SLACK_APP_TOKEN 環境變數必須被設定。")
    else:
        logging.basicConfig(level=logging.INFO) # 設定日誌級別為 INFO
        print("🤖 Slack bot is running in Socket Mode...")
        # SocketModeHandler 會處理與 Slack 的連線
        SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()