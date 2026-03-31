import os
import re
import csv
import io
import logging
import requests
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN      = os.environ["TELEGRAM_TOKEN"]
NAVER_CLIENT_ID     = os.environ["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET = os.environ["NAVER_CLIENT_SECRET"]
SHEET_ID            = os.environ["SHEET_ID"]

SHEET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"


# ── Google Sheets 조회 ──────────────────────────────────────────
def fetch_data():
    res = requests.get(SHEET_CSV_URL, timeout=10)
    res.encoding = "utf-8"
    reader = csv.DictReader(io.StringIO(res.text))
    return list(reader)

def search_by_stock(query: str, data: list):
    """종목명 부분일치"""
    return [r for r in data if query in r.get("종목명", "")]

def search_by_theme(query: str, data: list):
    """테마 부분일치"""
    return [r for r in data if query in r.get("테마", "")]


# ── 네이버 뉴스 ─────────────────────────────────────────────────
def get_naver_news(query: str, display: int = 5):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {"query": query, "display": display, "sort": "date"}
    res = requests.get(url, headers=headers, params=params, timeout=10)
    if res.status_code == 200:
        return res.json().get("items", [])
    return []

def clean_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


# ── 네이버 증권 시세 ────────────────────────────────────────────
def get_stock_price_by_name(name: str):
    """종목명으로 시세 조회 (pykrx 활용)"""
    try:
        from pykrx import stock
        from datetime import datetime, timedelta

        # 오늘 기준 날짜 설정
        today = datetime.today()
        today_str = today.strftime("%Y%m%d")
        ago_3m   = (today - timedelta(days=95)).strftime("%Y%m%d")
        ago_1m   = (today - timedelta(days=35)).strftime("%Y%m%d")
        ago_5d   = (today - timedelta(days=10)).strftime("%Y%m%d")

        # 종목명 → 코드 변환
        tickers = stock.get_market_ticker_list(today_str, market="ALL")
        code = None
        for t in tickers:
            if stock.get_market_ticker_name(t) == name:
                code = t
                break

        if not code:
            return None

        def get_rate(start, end, code):
            df = stock.get_market_price_change(start, end, market="ALL")
            if code in df.index:
                return df.loc[code, "등락률"]
            return None

        return {
            "1일":  get_rate((today - timedelta(days=2)).strftime("%Y%m%d"), today_str, code),
            "5일":  get_rate(ago_5d, today_str, code),
            "1개월": get_rate(ago_1m, today_str, code),
            "3개월": get_rate(ago_3m, today_str, code),
        }

    except Exception as e:
        logger.error(f"시세 조회 오류: {e}")
        return None

def format_rate(rate) -> str:
    try:
        f = float(rate)
        sign = "+" if f >= 0 else ""
        return f"{sign}{f:.2f}%"
    except:
        return str(rate)


# ── 메시지 핸들러 ───────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    lines = []

    # ── 괄호 입력: 뉴스 전용 검색 ────────────────────────────
    if query.startswith("(") and query.endswith(")"):
        keyword = query[1:-1].strip()
        try:
            news = get_naver_news(keyword, display=5)
            if news:
                lines.append(f"📰 [{keyword}] 최신 뉴스")
                lines.append("")
                for i, item in enumerate(news, 1):
                    title = clean_html(item["title"])
                    link  = item["link"]
                    lines.append(f"{i}. {title}")
                    lines.append(f"   🔗 {link}")
            else:
                lines.append(f"📰 '{keyword}' 관련 뉴스를 찾지 못했어요.")
        except Exception as e:
            logger.error(f"뉴스 오류: {e}")
            lines.append("⚠️ 뉴스를 불러오지 못했어요.")

        await update.message.reply_text(
            "\n".join(lines),
            disable_web_page_preview=True
        )
        return

    try:
        data = fetch_data()
        stock_hits = search_by_stock(query, data)
        theme_hits = search_by_theme(query, data)

        # ── 케이스 1: 종목명 검색 ──────────────────────────────
        if stock_hits:
            for r in stock_hits:
                name  = r.get("종목명", "")
                theme = r.get("테마", "")
                desc  = r.get("특징", "").strip()
                line  = f"📌 [{name}]\n🩷 {theme}"
                if desc:
                    line += f"\n➡️{desc}"
                lines.append(line)

            # 시세 조회
            try:
                first_name = stock_hits[0].get("종목명", "")
                price = get_stock_price_by_name(first_name)
                if price:
                    lines.append("")
                    lines.append("📊 시세")
                    lines.append(f"• 1일    {format_rate(price.get('1일', '-'))}")
                    lines.append(f"• 5일    {format_rate(price.get('5일', '-'))}")
                    lines.append(f"• 1개월  {format_rate(price.get('1개월', '-'))}")
                    lines.append(f"• 3개월  {format_rate(price.get('3개월', '-'))}")
            except Exception as e:
                logger.error(f"시세 오류: {e}")

            lines.append("")

            # 뉴스는 종목명 검색에만 붙임
            try:
                news = get_naver_news(query)
                if news:
                    lines.append(f"📰 최신 뉴스 ({query})")
                    for i, item in enumerate(news, 1):
                        title = clean_html(item["title"])
                        link  = item["link"]
                        lines.append(f"{i}. {title}")
                        lines.append(f"   🔗 {link}")
                else:
                    lines.append("📰 관련 뉴스를 찾지 못했어요.")
            except Exception as e:
                logger.error(f"뉴스 오류: {e}")
                lines.append("⚠️ 뉴스를 불러오지 못했어요.")

        # ── 케이스 2: 테마명 검색 ──────────────────────────────
        if theme_hits:
            if stock_hits:
                lines.append("")
                lines.append("─" * 20)
                lines.append("")

            lines.append(f"🗂 [{query}] 관련 종목")
            for i, r in enumerate(theme_hits, 1):
                name  = r.get("종목명", "")
                theme = r.get("테마", "")
                desc  = r.get("특징", "").strip()
                line  = f"{i}. {name}  ({theme})"
                if desc:
                    line += f"\n ➡️ {desc}"
                lines.append(line)

        # ── 케이스 3: 없음 ─────────────────────────────────────
        if not stock_hits and not theme_hits:
            lines.append(f"❓ '{query}' — 등록된 종목/테마가 없어요.")
            lines.append("시트에 추가하시면 바로 반영돼요!")

    except Exception as e:
        logger.error(f"Sheets 오류: {e}")
        lines.append("⚠️ 데이터를 불러오지 못했어요. 잠시 후 다시 시도해주세요.")

    await update.message.reply_text(
        "\n".join(lines),
        disable_web_page_preview=True
    )


# ── 실행 ────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("테마 검색 봇 시작!")
    app.run_polling()

if __name__ == "__main__":
    main()
