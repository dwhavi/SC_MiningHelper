# SC_PadControl (SC 패드 컨트롤)

Star Citizen 광물 스캔 결과를 게임 화면 위에 직접 띄워 주는 프로그램입니다.
화면 캡처 + OCR만 사용하며 게임 메모리 읽기, 인젝션, 후킹이 없습니다.
별도 프로그램(SC Overlay 등) 설치 없이 exe 하나로 동작합니다.

## 기능

- 게임 화면 위에 뜨는 내장 투명 오버레이 (클릭 통과, 항상 위, 보더리스 모드에서 동작)
- 스캔 시그니처(RS) 아래에 주 광물, 암석 개수, 통상 채굴 난이도 표시
- 근접 스캔 패널을 읽어 광물별 실제 구성 %, 저항, 불안정도, 질량 표시
- 기준 장비(Prospector + Helix I + Rieger-C3 + Focus III)로 채굴 가능 / 어려움 / 사실상 불가 판정 (BETA)
- 실행 시 뜨는 제어 창(다크 테마): 스캔 ON/OFF, 오버레이 표시/숨기기, 영역 지정, 위치 조정, 투명도, 상태 표시, 종료 — cmd 창 없이 앱 창만 표시
- 전역 단축키: **F8** 오버레이 표시/숨기기 · **F9** 위치 조정 모드 · **F10** 화면 영역 지정
- (선택) 태블릿 컨트롤 덱: http://PC_IP:8377 — PC에 입력만 전송, 결과 표시 없음

## 다운로드와 실행 (Python 설치 불필요)

1. [Releases](../../releases)에서 최신 `SC_PadControl.exe` 다운로드
2. 아무 폴더에 두고 더블클릭 (처음 실행은 준비에 10~20초 걸릴 수 있습니다. SmartScreen 경고가 뜨면 "추가 정보" → "실행")
3. 검은 cmd 창 없이 제어 창이 바로 뜹니다. 게임을 보더리스 모드로 실행하고 스캔하세요.

제어 창에서 스캔 ON/OFF, 오버레이 표시, 투명도, 위치 조정, 화면 영역 지정, 종료를 할 수 있습니다. 오류가 나면 제어 창 배너에 표시되고, 자세한 로그는 exe 옆 `sc_pad_control.log`에 기록됩니다.

동작 확인 (게임 없이 가능): 태블릿/브라우저에서 http://PC_IP:8377 접속 → 입력 버튼 동작 확인. RS 테스트는 브라우저에서 /api/test 로 가능한데, PC 오버레이는 tkinter 창에 표시됨.

## 첫 설정 (최초 1회, 1분)

1. 게임을 **보더리스(테두리 없음) 모드**로 실행하고 스캔 HUD가 보이는 화면을 띄웁니다.
2. **F10** (또는 제어 창의 "화면 영역 지정")을 누릅니다.
3. 1/2: 스캔 시 RS 숫자가 뜨는 자리를 드래그 → 2/2: 근접 스캔 패널 자리를 드래그. 끝 (재시작 불필요).
4. 오버레이 위치를 옮기고 싶으면 **F9** → 드래그 → 다시 **F9**로 저장.

해상도나 HUD 위치가 바뀌면 F10으로 다시 지정하세요.

## 참고

- 게임 언어는 영문 권장 (OCR 정확도). RS가 ±1~2 어긋나게 읽히면 자동 보정이 동작합니다.
- 오버레이는 클릭 통과 창이라 게임 조작을 방해하지 않습니다. 단축키 F8~F10이 다른 프로그램과 겹치면 등록에 실패할 수 있으며, 그 경우 제어 창 버튼으로 같은 동작을 할 수 있습니다.
- 채굴 판정 임계값은 인게임 실측 보정 전 추정치(BETA)입니다. exe 옆 `config.json`의 `verdict_thresholds`에서 조정할 수 있습니다.
- 시그니처/광물/장비 데이터는 exe에 내장되어 있으며 출처: [Jokers Gambit](https://jokersgambit.org/mining), [starcitizen.tools](https://starcitizen.tools), [scminer.rocks](https://scminer.rocks). 게임 패치로 수치가 바뀌면 저장소의 json을 고쳐 커밋하면 새 exe가 자동으로 빌드됩니다.
- 팬이 만든 비공식 도구이며 Cloud Imperium Games와 무관합니다.

## 빌드

GitHub Actions (windows-latest)가 PyInstaller로 단일 exe를 빌드하고 Release에 올립니다. 로컬에서 직접 빌드하려면:

```
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --onefile --name SC_PadControl ^
  --collect-all rapidocr_onnxruntime --collect-all onnxruntime ^
  --add-data "signatures.json;." --add-data "minerals.json;." ^
  --add-data "equipment.json;." --add-data "config.json;." ^
  --add-data "control_deck.html;." sc_pad_control.py
```
