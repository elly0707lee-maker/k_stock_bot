import os
import re
import csv
import io
import logging
import requests
from collections import OrderedDict
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN      = os.environ["TELEGRAM_TOKEN"]
NAVER_CLIENT_ID     = os.environ["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET = os.environ["NAVER_CLIENT_SECRET"]
SHEET_ID            = os.environ["SHEET_ID"]

SHEET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"
HEADERS = {"User-Agent": "Mozilla/5.0"}


# ── Google Sheets 조회 ──────────────────────────────────────────
def fetch_data():
    res = requests.get(SHEET_CSV_URL, timeout=10)
    res.encoding = "utf-8"
    reader = csv.DictReader(io.StringIO(res.text))
    return list(reader)

def search_by_stock(query: str, data: list):
    return [r for r in data if query in r.get("종목명", "")]

def search_by_theme(query: str, data: list):
    return [r for r in data if query in r.get("테마", "")]


# ── 네이버 뉴스 ─────────────────────────────────────────────────
def get_naver_news(query: str, display: int = 5, sort: str = "date"):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {"query": query, "display": display, "sort": sort}
    res = requests.get(url, headers=headers, params=params, timeout=10)
    if res.status_code == 200:
        return res.json().get("items", [])
    return []

def clean_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


# ── 시세 조회 (시트의 종목코드 사용) ────────────────────────────
def get_current_price(code: str):
    """종목코드로 직접 시세 조회 — 자동검색 없음"""
    if not code or not code.strip():
        return None
    try:
        url = f"https://m.stock.naver.com/api/stock/{code.strip()}/basic"
        res = requests.get(url, headers=HEADERS, timeout=5)
        if res.status_code != 200:
            return None
        data = res.json()
        price      = int(str(data.get("closePrice", "0")).replace(",", "") or 0)
        change     = int(str(data.get("compareToPreviousClosePrice", "0")).replace(",", "") or 0)
        change_pct = float(data.get("fluctuationsRatio", 0))
        mktcap_raw = int(str(data.get("marketValue", "0")).replace(",", "") or 0)
        high52     = str(data.get("fiftyTwoWeekHighPrice", "")).replace(",", "")
        low52      = str(data.get("fiftyTwoWeekLowPrice",  "")).replace(",", "")
        per        = data.get("per", "")
        pbr        = data.get("pbr", "")
        if price == 0:
            return None
        arrow = "▲" if change >= 0 else "▼"
        sign  = "+" if change >= 0 else ""
        result_lines = []
        result_lines.append(f"{price:,}원  {arrow} {abs(change):,} ({sign}{change_pct:.2f}%)")
        if mktcap_raw > 0:
            result_lines.append(f"💰 시총 {mktcap_raw // 100000000:,}억원")
        if high52 and low52:
            try:
                result_lines.append(f"📈 52주  최고 {int(high52):,}원 / 최저 {int(low52):,}원")
            except:
                pass
        parts = []
        if per: parts.append(f"PER {per}배")
        if pbr: parts.append(f"PBR {pbr}배")
        if parts:
            result_lines.append(f"📋 {' / '.join(parts)}")
        return "\n".join(result_lines)
    except Exception as e:
        logger.error(f"시세 오류: {e}")
        return None


# ── 메시지 핸들러 ───────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    lines = []

    # ── 괄호 입력: 뉴스 전용 검색 ────────────────────────────
    if query.startswith("(") and query.endswith(")"):
        keyword = query[1:-1].strip()
        try:
            news_date = get_naver_news(keyword, display=3, sort="date")
            news_sim  = get_naver_news(keyword, display=5, sort="sim")
            date_links = {item["link"] for item in news_date}
            news_sim_unique = [n for n in news_sim if n["link"] not in date_links][:2]

            if news_date or news_sim_unique:
                lines.append(f"📰 [{keyword}] 뉴스")
                lines.append("")
                if news_date:
                    lines.append("🕐 최신순")
                    for i, item in enumerate(news_date, 1):
                        lines.append(f"{i}. {clean_html(item['title'])}")
                        lines.append(f"   🔗 {item['link']}")
                if news_sim_unique:
                    lines.append("")
                    lines.append("🎯 관련도순")
                    for i, item in enumerate(news_sim_unique, 1):
                        lines.append(f"{i}. {clean_html(item['title'])}")
                        lines.append(f"   🔗 {item['link']}")
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
            grouped = OrderedDict()
            for r in stock_hits:
                name = r.get("종목명", "")
                if name not in grouped:
                    grouped[name] = []
                grouped[name].append(r)

            for name, rows in grouped.items():
                lines.append(f"📌 [{name}]")
                for r in rows:
                    theme = r.get("테마", "")
                    desc  = r.get("특징", "").strip()
                    lines.append(f"☑️ {theme}")
                    if desc:
                        lines.append(f"➡️{desc}")
                    lines.append("")

                # 종목코드는 첫 번째 행에서 가져옴
                code = rows[0].get("종목코드", "").strip()
                price_str = get_current_price(code)
                if price_str:
                    lines.append("")
                    lines.append(f"📊 {price_str}")

            lines.append("")

            # 뉴스
            try:
                news_date = get_naver_news(query, display=3, sort="date")
                news_sim  = get_naver_news(query, display=5, sort="sim")
                date_links = {item["link"] for item in news_date}
                news_sim_unique = [n for n in news_sim if n["link"] not in date_links][:2]

                if news_date or news_sim_unique:
                    lines.append(f"📰 뉴스 ({query})")
                    lines.append("")
                    if news_date:
                        lines.append("🕐 최신순")
                        for i, item in enumerate(news_date, 1):
                            lines.append(f"{i}. {clean_html(item['title'])}")
                            lines.append(f"   🔗 {item['link']}")
                    if news_sim_unique:
                        lines.append("")
                        lines.append("🎯 관련도순")
                        for i, item in enumerate(news_sim_unique, 1):
                            lines.append(f"{i}. {clean_html(item['title'])}")
                            lines.append(f"   🔗 {item['link']}")
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
