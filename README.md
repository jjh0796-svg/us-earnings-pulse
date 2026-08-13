# us-earnings-pulse

미국 반도체·테크 워치리스트의 분기 실적 발표(8-K, Item 2.02)를 SEC EDGAR에서
실시간 감시하고, 접수 즉시 보도자료 핵심(매출·EPS·가이던스)을 요약해
텔레그램으로 보내는 속보 봇.

- 감시: EDGAR 최신 8-K 아톰 피드, 발표 창(미 장마감 후·장전)에만 75초 간격 폴링
- 요약: Gemini API (무료 티어) — 실패 시 링크만이라도 즉시 발송
- 대상: `watchlist.json` (미국 상장 국내신고사 — 20-F 외국계 제외)
- 스케줄: `.github/workflows/pulse.yml` — 15분 잡 내부 75초 루프 (kr-earnings-pulse 패턴)

## 실행

```bash
python us_earnings_pulse.py --probe      # CIK 매핑 확인
python us_earnings_pulse.py --dry-run    # 1회 조회, 발송 없음
```

Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GEMINI_API_KEY`

주의: SEC 요청에는 연락처 포함 User-Agent 필수. 텔레그램 읽기 타임아웃은
재시도하지 않는다(중복 발송 방지).
