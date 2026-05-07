"""코인 봇 (별도 트랙).

KR 봇(번팅킹) 과는 완전 분리:
  - 별도 telegram bot token (PHASE 2 에 발급)
  - 별도 systemd 서비스 (coin-bunting.service)
  - 별도 DB (coin-bunting.db)
  - 같은 저장소 (코드 공유 편의)

설계 철학:
  - 100% 자동 매수/매도 — 사용자 클릭 X
  - narrow TP/SL (+1.5% / -1.0%) — 사람 필터 없는 만큼 자주 끊음
  - 24/7 잠재력, 일단 09~22h 제한 운영
  - 시드 30만원 파일럿 → 검증 후 확장

Phase:
  1) 백테스트 — Upbit 90일 분봉 + 시그널 그리드 서치
  2) Paper mode — 실시간 가격 + 가상 체결
  3) 30만원 실거래
  4) 확장 (시드 ↑ / 코인 ↑ / 24h)
"""
