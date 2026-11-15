# 변경 내역 — 태블릿 컨트롤 덱 재구성 (2026-11-15)

## 배경
- 마스터 지시: "채광 결과는 테블릿에서 볼 필요 없음. PC화면에서만 표시."
- 기존 FastAPI(8377)를 결과 표시가 아닌 **태블릿 입력 전용**으로 재활용.

## 삭제됨
- `overlay.html` 파일 삭제 — 채광 결과를 표시하던 웹 오버레이 제거
- `calibrate.html` 파일 삭제 — 웹 캘리브레이션 제거. PC에서는 F10(화면 영역 지정)으로만 설정
- 엔드포인트 삭제: `GET /api/state`, `GET /api/screenshot`, `WS /ws`
- `ScanState`의 subscribe/unsubscribe/_broadcast 구조 제거
- `requirements.txt`에서 `websockets` 제거
- `import io` 미사용 제거

## 신규
- `control_deck.html` — 결과 표시 없음. 입력 버튼만 있는 태블릿 페이지
  - 채굴 조작: B/V/N/C/TAB/LCONTROL(홀드 400ms)
  - 이동: W/A/S/D + W/S 홀드 3초
  - 매크로: land(착륙 시퀀스), scan(미네랄 샷), abort(채굴 중단)
  - 기타: F/G/M/SPACE/ENTER/ESC + 테스트/지우기
  - 하단 로그로 전송 성공/실패 표시
- `POST /api/input` 엔드포인트 — `{type: press|hold|macro, key, ms, name}`
- `_send_input()` 헬퍼 — Windows `ctypes` `SendInput`만 사용, 패키지 추가 없음
- `_KEY_CODES` 화이트리스트 — 허용 키만 전송, 외는 400으로 차단
- `_MACROS` — land/scan/abort/mining_on/off/stow/exit_seat

## 수정
- `GET /`가 이제 `control_deck.html`을 서빙 (기존 overlay.html → control_deck.html)
- `config.json` — 누락됐던 `rs_search: "wide"` 추가
- `scm_helper.py` DEFAULTS — `overlay_x 960 → 1150`, `overlay_anchor_v2: True` 추가
  → config 파일 없이 실행해도 업로드 config와 동일 값으로 동작
- `main()` 시작 안내문 — "…/calibrate 에서 지정하세요" → "제어 창의 '화면 영역 지정'(F10)으로 지정하세요"
- README — 태블릿 컨트롤 덱 설명으로 교체, 다운로드/동작확인 문구 갱신
- `.github/workflows/build.yml` — --add-data를 control_deck.html로 교체
- 빌드 스모크 테스트 — `/api/state`(제거됨) 대신 `GET /` 200으로 변경

## 유지
- PC 오버레이: tkinter 투명 창 (`OverlayWindow`) — 그대로. 채광 결과는 PC 화면에만 표시
- `/api/test`, `/api/clear` — 진단용으로 유지
- `/calibrate` 페이지 자체는 제거했지만, PC 내장 calibrate(F10 캘리브레이터) 코드는 유지

## 검증
- `py_compile` 통과
- FastAPI 라우트 확인: `/`, `/api/input`, `/api/test`, `/api/clear`만 존재
- `POST /api/input` 동작 확인 (Mac에서는 "Windows에서만 입력 전송 가능"으로 안전 차단)
- Actions 빌드: 성공 (run 43675857329), smoke test 통과, Release v0.22

## 사용법
1. Releases에서 최신 exe 다운로드 후 실행
2. 태블릿에서 `http://PC_IP:8377` 접속 (같은 Wi-Fi)
3. 버튼을 누르면 PC가 SC 창에 키 입력 전송
