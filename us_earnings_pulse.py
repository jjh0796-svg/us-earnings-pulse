"""미국 실적 8-K 즉시 속보 — SEC EDGAR 실시간 감시.

EDGAR 최신 8-K 피드를 짧은 간격으로 조회해, 워치리스트 기업의 실적 공시
(Item 2.02)가 접수되는 즉시 보도자료 핵심(매출·EPS·가이던스)을 Gemini로
요약해 텔레그램으로 보낸다. 상세 분석(차트·서프라이즈)은 다음날 아침
CODEX us_earnings_alert가 담당 — 이 봇은 속보 전용.

    python us_earnings_pulse.py --probe            # 워치 CIK 매핑 확인
    python us_earnings_pulse.py --dry-run          # 1회 조회, 발송 없이 출력
    python us_earnings_pulse.py --loop-minutes 14  # 75초 간격 반복(스케줄용)
    python us_earnings_pulse.py --force-acc 0000723125-26-000013  # 데모 재발송
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import os
import re
import time
from pathlib import Path

import requests

LOGGER = logging.getLogger("us_earnings_pulse")

UA = {"User-Agent": "personal-research-bot jjh0796 (soheeji77@gmail.com)"}
FEED_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=8-K&company=&dateb=&owner=include&count=100&output=atom"
)
WATCHLIST_FILE = Path(__file__).resolve().parent / "watchlist.json"
KST = dt.timezone(dt.timedelta(hours=9))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "75"))  # 서버 상주 시 40초 등으로 단축
MAX_TEXT_CHARS = 40_000


def state_path() -> Path:
    return Path(os.environ.get("PULSE_STATE_FILE", ".pulse/state.json"))


def load_state() -> dict:
    path = state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    state["seen"] = state.get("seen", [])[-500:]
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def _get(url: str) -> requests.Response:
    resp = requests.get(url, headers=UA, timeout=30)
    resp.raise_for_status()
    return resp


def load_watchlist() -> dict[str, str]:
    return json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))


def cik_for_watch(state: dict, watch: dict[str, str]) -> dict[int, str]:
    """{cik: ticker} — 하루 한 번만 SEC 티커맵을 갱신."""
    today = dt.date.today().isoformat()
    cached = state.get("cik_map") or {}
    if state.get("cik_map_date") == today and cached:
        return {int(k): v for k, v in cached.items()}
    data = _get("https://www.sec.gov/files/company_tickers.json").json()
    by_ticker = {v["ticker"].upper(): int(v["cik_str"]) for v in data.values()}
    result = {by_ticker[t]: t for t in watch if t in by_ticker}
    state["cik_map"] = {str(k): v for k, v in result.items()}
    state["cik_map_date"] = today
    return result


ENTRY_RE = re.compile(
    r"<entry>.*?<title>(.*?)</title>.*?href=\"([^\"]*/Archives/edgar/data/(\d+)/"
    r"([0-9-]+)-index\.htm)\".*?<updated>([^<]+)</updated>.*?</entry>",
    re.S,
)


def fetch_feed_entries() -> list[dict]:
    body = _get(FEED_URL).text
    entries = []
    for m in ENTRY_RE.finditer(body):
        title, link, cik, acc, updated = m.groups()
        entries.append(
            {"title": title, "link": link, "cik": int(cik), "acc": acc, "updated": updated}
        )
    return entries


def filing_meta(cik: int, acc: str) -> dict | None:
    """submissions JSON에서 해당 접수번호의 items·일자 확인."""
    subs = _get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json").json()
    rec = subs["filings"]["recent"]
    for acc_no, items, date, form in zip(
        rec["accessionNumber"], rec["items"], rec["filingDate"], rec["form"]
    ):
        if acc_no == acc:
            return {"items": items or "", "date": date, "form": form}
    return None


def press_release_text(cik: int, acc: str) -> tuple[str | None, str | None]:
    """(본문 텍스트, 문서 URL) — EX-99 보도자료 우선."""
    acc_nodash = acc.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}"
    try:
        idx = _get(f"{base}/index.json").json()
    except Exception as exc:
        LOGGER.warning("index.json 실패(%s): %s", acc, exc)
        return None, None
    names = [f["name"] for f in idx["directory"]["item"]]
    cands = [n for n in names if "99" in n and n.endswith(".htm")]
    cands += [n for n in names if "press" in n.lower() and n.endswith(".htm")]
    if not cands:
        return None, None
    url = f"{base}/{cands[0]}"
    raw = _get(url).text
    text = re.sub(r"<script.*?</script>", " ", raw, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return (text[:MAX_TEXT_CHARS] if len(text) > 500 else None), url


_PROMPT = """미국 기업의 분기 실적 보도자료다. 투자자용 속보로 JSON으로만 답하라.
모든 값은 한국어, 보도자료에 명시된 수치만 사용(유추·추정 금지, 없으면 null).

JSON 스키마:
{{
 "period_end": "이번 실적 분기의 마감일 YYYY-MM-DD (보도자료에 명시된 날짜)",
 "fiscal_label": "보도자료가 부르는 분기명 축약 (예: FY2026 4Q). 없으면 null",
 "revenue": "매출, 예: $2.05B",
 "revenue_yoy": "전년 동기 대비, 예: +34% (없으면 null)",
 "eps": "대표 EPS, 예: $1.74",
 "eps_basis": "위 EPS의 기준 — 비GAAP 또는 GAAP",
 "eps_gaap": "비GAAP을 대표로 썼을 때 GAAP EPS, 예: $1.19 (없으면 null)",
 "guide_revenue": "다음 분기 매출 가이던스, 예: $2.2~2.4B (없으면 null)",
 "guide_margin": "다음 분기 마진 가이던스, 기준 포함, 예: 비GAAP 총마진 39.5~41.5% (없으면 null)",
 "note": "그 외 투자자가 알아야 할 특이사항 한 줄 (없으면 null)"
}}

보도자료:
{text}
"""

_MONTH_NAMES = {
    1: "january", 2: "february", 3: "march", 4: "april", 5: "may", 6: "june",
    7: "july", 8: "august", 9: "september", 10: "october", 11: "november", 12: "december",
}


def _verify_period_end(period_end, transcript: str) -> str | None:
    """보도자료 본문에 실제로 등장하는 날짜만 인정 (환각 차단)."""
    pe = str(period_end or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", pe):
        return None
    y, m, d = (int(x) for x in pe.split("-"))
    low = transcript.lower()
    full, abbr = _MONTH_NAMES[m], _MONTH_NAMES[m][:3]
    for pat in (f"{full} {d}", f"{abbr}. {d}", f"{abbr} {d}"):
        if pat in low:
            return pe
    return None


def _calendar_label(period_end: str | None) -> str | None:
    """마감일 → '달력 2Q26 (6/28 마감)'. 월초 마감 경계는 −45일 중간점으로 보정."""
    if not period_end:
        return None
    y, m, d = (int(x) for x in period_end.split("-"))
    mid = dt.date(y, m, d) - dt.timedelta(days=45)
    return f"달력 {(mid.month - 1) // 3 + 1}Q{mid.year % 100} ({m}/{d} 마감)"


def summarize(text: str) -> dict | None:
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key:
        return None
    body = {
        "contents": [{"parts": [{"text": _PROMPT.format(text=text)}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
    }
    for model in ("gemini-3.6-flash", "gemini-flash-latest", "gemini-3.1-flash-lite"):
        try:
            resp = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={key}",
                json=body,
                timeout=60,
            )
            if resp.status_code != 200:
                LOGGER.info("Gemini %s 응답 %s", model, resp.status_code)
                continue
            data = json.loads(resp.json()["candidates"][0]["content"]["parts"][0]["text"])
        except Exception as exc:
            LOGGER.info("Gemini %s 실패: %s", model, exc)
            continue
        if isinstance(data, dict) and str(data.get("revenue") or "").strip():
            for field in (
                "period_end", "fiscal_label", "revenue", "revenue_yoy", "eps",
                "eps_basis", "eps_gaap", "guide_revenue", "guide_margin", "note",
            ):
                value = data.get(field)
                data[field] = str(value).strip() if value not in (None, "") else None
            data["period_end"] = _verify_period_end(data["period_end"], text)
            return data
    return None


def build_message(name: str, ticker: str, brief: dict | None, doc_url: str | None, when_kst: str) -> str:
    e = html.escape
    parts = [f"⚡ <b>실적 속보 — {e(name)}({ticker})</b>\n"]
    if brief:
        # 분기 표기: 회계 분기명 + 달력 분기 병기 (혼동 방지)
        cal = _calendar_label(brief.get("period_end"))
        quarter_bits = [b for b in (brief.get("fiscal_label"), cal) if b]
        quarter_line = " = ".join(quarter_bits)
        parts.append(f"🗓 {e(quarter_line)} · 접수 {e(when_kst)}\n\n" if quarter_line else f"🗓 접수 {e(when_kst)}\n\n")
        rev = f"📈 매출 {e(brief['revenue'])}"
        if brief.get("revenue_yoy"):
            rev += f" (YoY {e(brief['revenue_yoy'])})"
        parts.append(rev + "\n")
        if brief.get("eps"):
            eps = f"💰 EPS {e(brief['eps'])}"
            if brief.get("eps_basis"):
                eps += f" {e(brief['eps_basis'])}"
            if brief.get("eps_gaap"):
                eps += f" (GAAP {e(brief['eps_gaap'])})"
            parts.append(eps + "\n")
        guides = [g for g in (brief.get("guide_revenue"), brief.get("guide_margin")) if g]
        if guides:
            parts.append("\n🧭 <b>다음 분기 가이던스</b>\n")
            labels = ["매출 ", ""] if brief.get("guide_revenue") else [""]
            for label, g in zip(labels, guides):
                parts.append(f"• {label}{e(g)}\n")
        if brief.get("note"):
            parts.append(f"\n❗ {e(brief['note'])}\n")
    else:
        parts.append(f"🗓 접수 {e(when_kst)}\n\n보도자료 요약 실패 — 원문을 확인하세요.\n")
    parts.append("\n")
    if doc_url:
        parts.append(f'🔗 <a href="{e(doc_url)}">보도자료(SEC)</a> · ')
    parts.append("상세 분석·차트는 내일 아침 자동 발송")
    return "".join(parts)


def send(message: str, dry_run: bool) -> None:
    if dry_run:
        print("=" * 60)
        print(message)
        return
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID가 필요합니다.")
    session = requests.Session()
    session.trust_env = False
    # 읽기 타임아웃은 재시도하지 않는다(중복 발송 사고 방지) — 연결 실패만 예외.
    session.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
        timeout=30,
    ).raise_for_status()


def to_kst(updated: str) -> str:
    if len(updated) <= 10:  # 날짜만 있는 경우(강제 처리 데모)
        return updated
    try:
        stamp = dt.datetime.fromisoformat(updated.replace("Z", "+00:00"))
        return stamp.astimezone(KST).strftime("%H:%M KST")
    except ValueError:
        return updated


def process_accession(
    cik: int, acc: str, ticker: str, name: str, updated: str, dry_run: bool
) -> bool:
    meta = filing_meta(cik, acc)
    if meta is None or "2.02" not in meta["items"]:
        LOGGER.info("%s %s: 실적(2.02) 아님 — 무시 (items=%s)", ticker, acc, meta and meta["items"])
        return False
    text, doc_url = press_release_text(cik, acc)
    brief = summarize(text) if text else None
    message = build_message(name, ticker, brief, doc_url, to_kst(updated))
    send(message, dry_run)
    LOGGER.info("속보 %s: %s %s", "출력" if dry_run else "발송", ticker, acc)
    return True


def poll_once(state: dict, watch: dict[str, str], cik_map: dict[int, str], dry_run: bool) -> int:
    entries = fetch_feed_entries()
    if not entries:
        LOGGER.warning("피드 파싱 0건")
        return 0
    seen = set(state.get("seen", []))
    first_run = not seen
    sent = 0
    for entry in entries:
        if entry["acc"] in seen:
            continue
        seen.add(entry["acc"])
        if entry["cik"] not in cik_map:
            continue
        if first_run:
            LOGGER.info("초기 실행 — %s 기준선만 기록", entry["acc"])
            continue
        ticker = cik_map[entry["cik"]]
        if process_accession(
            entry["cik"], entry["acc"], ticker, watch.get(ticker, ticker), entry["updated"], dry_run
        ):
            sent += 1
    state["seen"] = sorted(seen)
    return sent


def main() -> None:
    parser = argparse.ArgumentParser(description="미국 실적 8-K 즉시 속보.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--loop-minutes", type=int, default=0)
    parser.add_argument("--force-acc", default=None, help="접수번호 강제 처리(데모)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    watch = load_watchlist()
    state = load_state()
    cik_map = cik_for_watch(state, watch)

    if args.probe:
        for cik, ticker in sorted(cik_map.items(), key=lambda x: x[1]):
            print(f"{ticker:6s} CIK {cik}")
        missing = set(watch) - set(cik_map.values())
        if missing:
            print("CIK 미해결:", sorted(missing))
        return

    if args.force_acc:
        # 접수번호로 CIK 역추적
        for cik, ticker in cik_map.items():
            meta = filing_meta(cik, args.force_acc)
            if meta:
                process_accession(
                    cik, args.force_acc, ticker, watch.get(ticker, ticker),
                    meta["date"], args.dry_run,
                )
                save_state(state)
                return
        raise SystemExit("워치리스트에서 해당 접수번호를 찾지 못했습니다.")

    deadline = time.time() + args.loop_minutes * 60
    total = 0
    while True:
        try:
            total += poll_once(state, watch, cik_map, args.dry_run)
        except Exception as exc:
            LOGGER.warning("조회 실패(다음 루프에서 재시도): %s", exc)
        save_state(state)
        if time.time() >= deadline:
            break
        time.sleep(POLL_SECONDS)
    print(f"발송 {total}건")


if __name__ == "__main__":
    main()
