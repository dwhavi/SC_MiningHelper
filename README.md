# SC Mining Helper (SC 광물 오버레이)

Star Citizen 광물 스캔 결과를 게임 화면 위에 띄워 주는 로컬 오버레이입니다.
화면 캡처 + OCR만 사용하며 게임 메모리 읽기, 인젝션, 후킹이 없습니다.

## 기능

- 스캔 시그니처(RS) 아래에 주 광물, 암석 개수, 통상 채굴 난이도 표시
- 근접 스캔 패널을 읽어 광물별 실제 구성 %, 저항, 불안정도, 질량 표시
- 기준 장비(Prospector + Helix I + Rieger-C3 + Focus III)로 채굴 가능 / 어려움 / 사실상 불가 판정 (BETA)
- SC Overlay의 Web Page 위젯, 또는 같은 Wi-Fi의 태블릿/폰 브라우저에서 표시

## 다운로드와 실행 (Python 설치 불필요)

1. [Releases](../../releases)에서 최신 `SCMiningHelper.exe` 다운로드
2. 아무 폴더에 두고 더블클릭 (처음 실행은 준비에 10~20초 걸릴 수 있습니다. SmartScreen 경고가 뜨면 "추가 정보" → "실행")
3. 브라우저에서 http://localhost:8377 열기

동작 확인 (게임 없이 가능): 오른쪽 아래 "테스트 모드" → RS에 `6340` 입력 → "Quantainium ×2"가 뜨면 정상.

## 캘리브레이션 (최초 1회)

1. 게임을 보더리스 모드로 실행하고 스캔 HUD가 보이는 화면을 띄웁니다.
2. http://localhost:8377/calibrate 접속
3. 1단계: RS 숫자가 뜨는 자리를 드래그 → Enter / 2단계: 근접 스캔 패널 자리를 드래그 → 저장
4. exe를 다시 시작하면 적용됩니다.

## SC Overlay와 함께 쓰기

SC Overlay에서 Web Page 위젯을 추가하고 주소에 `http://localhost:8377` 을 입력하면 게임 위에 표시됩니다. 태블릿은 같은 Wi-Fi에서 `http://<PC IP>:8377` 로 접속하세요 (IP는 실행 창에 표시됨).

## 참고

- 게임 언어는 영문 권장 (OCR 정확도). RS가 ±1~2 어긋나게 읽히면 자동 보정이 동작합니다.
- 채굴 판정 임계값은 인게임 실측 보정 전 추정치(BETA)입니다. `config.json`의 `verdict_thresholds`에서 조정할 수 있습니다.
- 시그니처/광물/장비 데이터 출처: [Jokers Gambit](https://jokersgambit.org/mining), [starcitizen.tools](https://starcitizen.tools), [scminer.rocks](https://scminer.rocks). 게임 패치로 수치가 바뀌면 json 파일만 수정하면 됩니다.
- 팬이 만든 비공식 도구이며 Cloud Imperium Games와 무관합니다.

## 빌드

GitHub Actions (windows-latest)가 PyInstaller로 단일 exe를 빌드하고 Release에 올립니다. 로컬에서 직접 빌드하려면:

```
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --onefile --name SCMiningHelper ^
  --collect-all rapidocr_onnxruntime --collect-all onnxruntime ^
  --add-data "signatures.json;." --add-data "minerals.json;." ^
  --add-data "equipment.json;." --add-data "config.json;." ^
  --add-data "overlay.html;." --add-data "calibrate.html;." scm_helper.py
```
