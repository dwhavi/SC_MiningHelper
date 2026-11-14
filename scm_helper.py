"""SC 광물 오버레이 (SC Mining Helper) - 단일 파일 버전.

Star Citizen 광물 스캔 결과를 화면 위에 표시하는 로컬 오버레이.
화면 캡처 + OCR만 사용 (인젝션/메모리 읽기 없음, EAC 안전 범위).

실행: python scm_helper.py  (또는 빌드된 SCMiningHelper.exe)
"""
import asyncio
import difflib
import io
import json
import os
import re
import socket
import sys
import threading
import time

# ---------------------------------------------------------------- 설정/경로

if getattr(sys, "frozen", False):
    BUNDLE_DIR = sys._MEIPASS  # type: ignore[attr-defined]
    EXE_DIR = os.path.dirname(sys.executable)
else:
    BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    EXE_DIR = BUNDLE_DIR

CONFIG_PATH = os.path.join(EXE_DIR, "config.json")

DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8377,
    "capture_enabled": True,
    "monitor": 1,
    "rs_region": None,
    "panel_region": None,
    "poll_interval_ms": 500,
    "ocr_engine": "auto",
    "verdict_thresholds": {
        "beta": True,
        "resist_ok_pct": 30.0,
        "resist_hard_pct": 55.0,
        "instability_hard": 800,
    },
}


def _data_path(name):
    p = os.path.join(BUNDLE_DIR, name)
    return p if os.path.exists(p) else None


def load_config():
    cfg = dict(DEFAULTS)
    src = CONFIG_PATH if os.path.exists(CONFIG_PATH) else _data_path("config.json")
    if src:
        try:
            with open(src, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print(f"[config] config 읽기 실패, 기본값 사용: {e}")
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def load_data(name):
    p = _data_path(name)
    if not p:
        raise FileNotFoundError(name)
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- 시그니처 매칭

def match_signature(rs, table, tolerance=0):
    """RS = 광물 고정값 × 암석 개수. exact 우선, 없으면 ±tolerance 유일 후보 보정."""
    if not isinstance(rs, int) or rs <= 0:
        return []
    entries = []
    for kind in ("ship_ores", "roc_gems", "hand_gems"):
        for name, sig in table.get(kind, {}).items():
            if isinstance(sig, int):
                entries.append((kind, name, sig))
    exact = [{"kind": k, "mineral": n, "count": rs // s, "corrected": False}
             for k, n, s in entries if rs % s == 0]
    if exact or tolerance <= 0:
        return exact
    near = []
    for k, n, s in entries:
        count = round(rs / s)
        if count >= 1 and 0 < abs(s * count - rs) <= tolerance:
            near.append({"kind": k, "mineral": n, "count": count,
                         "corrected": True, "assumed_rs": s * count})
    return near if len(near) == 1 else []


# ---------------------------------------------------------------- OCR 텍스트 파싱

NUM = r"[\d][\d,\.]*"
RS_RE = re.compile(r"(\d{3,6})")
PCT_RE = re.compile(r"([A-Za-z][A-Za-z \-\']*?)\s*[:\-]?\s*(" + NUM + r")\s*%")
KEYVAL_RE = re.compile(r"([A-Za-z ]+?)\s*[:\-]?\s*(" + NUM + r")\s*(%?)")


def _to_float(s):
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def parse_rs(text):
    m = RS_RE.search(text.replace(",", "").replace(" ", ""))
    return int(m.group(1)) if m else None


def _canon_name(raw, known):
    raw = raw.strip()
    for k in known:
        if k.lower() == raw.lower():
            return k
    close = difflib.get_close_matches(raw, known, n=1, cutoff=0.72)
    return close[0] if close else None


def parse_panel(lines, known_minerals):
    out = {"minerals": [], "inert_pct": None, "resistance_pct": None,
           "instability": None, "mass": None, "unparsed": []}
    known = list(known_minerals) + ["Inert Materials"]
    for line in lines:
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith(("resistance", "instability", "mass")):
            m = KEYVAL_RE.search(line)
            v = _to_float(m.group(2)) if m else None
            if low.startswith("resistance"):
                out["resistance_pct"] = v
            elif low.startswith("instability"):
                out["instability"] = v
            else:
                out["mass"] = v
            continue
        m = PCT_RE.search(line)
        if m:
            name = _canon_name(m.group(1), known)
            pct = _to_float(m.group(2))
            if pct is None:
                out["unparsed"].append(line)
            elif name == "Inert Materials":
                out["inert_pct"] = pct
            elif name:
                out["minerals"].append({"name": name, "pct": pct})
            else:
                out["unparsed"].append(line)
            continue
        out["unparsed"].append(line)
    return out


# ---------------------------------------------------------------- 채굴 판정

TIER_LABELS = {"high": "어려움 예상", "mid": "보통", "low": "쉬움", "unknown": "정보 없음"}


def preview_tier(mineral_info):
    if not mineral_info:
        return "unknown", TIER_LABELS["unknown"]
    res = mineral_info.get("resistance")
    inst = mineral_info.get("instability")
    if res is None:
        return "unknown", TIER_LABELS["unknown"]
    if res >= 0.9 or (inst is not None and inst >= 900 and res >= 0.6):
        return "high", f"{TIER_LABELS['high']} (기본 저항 {res}, 불안정 {inst})"
    if res >= 0.45:
        return "mid", f"{TIER_LABELS['mid']} (기본 저항 {res}, 불안정 {inst})"
    return "low", f"{TIER_LABELS['low']} (기본 저항 {res}, 불안정 {inst})"


def judge_rock(resistance_pct, instability, equip, thresholds):
    """바위 실측 판정. level: ok/hard/infeasible/unknown. 임계값은 BETA 추정치."""
    beta = bool(thresholds.get("beta", True))
    if resistance_pct is None:
        return {"level": "unknown", "beta": beta, "label": "판정 보류",
                "reason": "저항 값을 읽지 못함 (근접 스캔 패널 OCR 필요)"}
    res_mod = equip["combined"].get("resistance_modifier", 0.0)
    res_eff = resistance_pct * (1.0 + res_mod)
    if res_eff <= thresholds["resist_ok_pct"]:
        level, label = "ok", "가능"
        reason = f"실효 저항 {res_eff:.0f}% (원래 {resistance_pct:.0f}%에 Helix I 저항 감소 적용)"
    elif res_eff <= thresholds["resist_hard_pct"]:
        level, label = "hard", "어려움"
        reason = f"실효 저항 {res_eff:.0f}%, 최대 출력 근처 조작 필요"
    else:
        level, label = "infeasible", "사실상 불가"
        reason = (f"실효 저항 {res_eff:.0f}%로 기준 장비 출력 한계 초과 추정. "
                  "저항 감소 가젯 사용 또는 MOLE 다중 레이저 필요")
    if instability is not None and instability >= thresholds["instability_hard"]:
        if level == "ok":
            level, label = "hard", "어려움"
        reason += f". 불안정도 {instability:g}로 에너지 급변 주의"
    return {"level": level, "label": label, "reason": reason, "beta": beta,
            "resistance_effective": round(res_eff, 1)}


# ---------------------------------------------------------------- OCR 엔진

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")


def _preprocess(img):
    from PIL import ImageOps
    g = img.convert("L")
    g = g.resize((g.width * 2, g.height * 2))
    return ImageOps.autocontrast(g)


def _group_rows(result):
    """OCR 토큰을 같은 줄끼리 묶어 '이름 값%' 한 줄로 재구성."""
    tokens = []
    for box, text, conf in result:
        tokens.append({
            "cy": sum(p[1] for p in box) / 4,
            "cx": sum(p[0] for p in box) / 4,
            "h": max(p[1] for p in box) - min(p[1] for p in box),
            "text": text, "conf": float(conf)})
    if not tokens:
        return []
    tokens.sort(key=lambda t: t["cy"])
    med_h = sorted(t["h"] for t in tokens)[len(tokens) // 2]
    thresh = max(med_h * 0.6, 4)
    rows = []
    for t in tokens:
        if rows and abs(t["cy"] - rows[-1][0]["cy"]) <= thresh:
            rows[-1].append(t)
        else:
            rows.append([t])
    lines = []
    for row in rows:
        row.sort(key=lambda t: t["cx"])
        lines.append((" ".join(t["text"] for t in row),
                      sum(t["conf"] for t in row) / len(row)))
    return lines


class RapidOcrBackend:
    name = "rapidocr"

    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR
        self._engine = RapidOCR()

    def read_lines(self, img):
        import numpy as np
        result, _ = self._engine(np.array(_preprocess(img)))
        return _group_rows(result) if result else []


class WindowsOcrBackend:
    name = "windows"

    def __init__(self):
        from winsdk.windows.globalization import Language
        from winsdk.windows.media.ocr import OcrEngine
        lang = Language("en")
        if not OcrEngine.is_language_supported(lang):
            raise RuntimeError("Windows OCR 영문 언어팩 없음")
        self._engine = OcrEngine.try_create_from_language(lang)

    def read_lines(self, img):
        import numpy as np
        from winsdk.windows.graphics.imaging import (
            BitmapDecoder, BitmapPixelFormat, SoftwareBitmap)
        from winsdk.windows.storage.streams import (
            DataWriter, InMemoryRandomAccessStream)

        arr = np.array(_preprocess(img))

        async def run():
            stream = InMemoryRandomAccessStream()
            writer = DataWriter(stream)
            writer.write_bytes(arr.tobytes())
            await writer.store_async()
            stream.seek(0)
            decoder = await BitmapDecoder.create_async(stream)
            sb = await decoder.get_software_bitmap_async()
            sb = SoftwareBitmap.convert(sb, BitmapPixelFormat.GRAY8)
            return await self._engine.recognize_async(sb)

        result = asyncio.run(run())
        return [(line.text, 1.0) for line in result.lines]


def create_ocr_engine(preference="auto"):
    errors = []
    order = ["windows", "rapidocr"] if preference == "auto" else [preference]
    for name in order:
        try:
            if name == "windows":
                return WindowsOcrBackend()
            if name == "rapidocr":
                return RapidOcrBackend()
        except Exception as e:
            errors.append(f"{name}: {e}")
    raise RuntimeError("사용 가능한 OCR 엔진 없음: " + "; ".join(errors))


# ---------------------------------------------------------------- 화면 캡처

class Capturer:
    def __init__(self, monitor=1):
        import mss
        self._sct = mss.mss()
        if monitor >= len(self._sct.monitors):
            print(f"[capture] 모니터 {monitor} 없음, 주 모니터(1)로 대체")
            monitor = 1
        self.monitor = monitor

    def grab(self, region):
        from PIL import Image
        mon = self._sct.monitors[self.monitor]
        shot = self._sct.grab({
            "left": mon["left"] + region["left"],
            "top": mon["top"] + region["top"],
            "width": region["width"],
            "height": region["height"]})
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def grab_full(self):
        from PIL import Image
        shot = self._sct.grab(self._sct.monitors[self.monitor])
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


# ---------------------------------------------------------------- 상태/서버

class ScanState:
    def __init__(self, sig_table, minerals, equipment, thresholds):
        self._lock = threading.Lock()
        self._state = {
            "phase": "idle", "rs": None, "matches": [], "composition": [],
            "inert_pct": None, "resistance_pct": None, "instability": None,
            "mass": None, "rock_verdict": None,
            "equipment_name": equipment.get("name", ""), "updated_at": None}
        self._sig_table = sig_table
        self._minerals = minerals
        self._equipment = equipment
        self._thresholds = thresholds
        self._subscribers = []

    def subscribe(self):
        q = asyncio.Queue(maxsize=5)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        if q in self._subscribers:
            self._subscribers.remove(q)

    def _broadcast(self):
        data = json.dumps(self._state, ensure_ascii=False)
        for q in self._subscribers:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass

    def get(self):
        with self._lock:
            return dict(self._state)

    def _enrich_matches(self, rs, tolerance=2):
        matches = match_signature(rs, self._sig_table, tolerance=0) or \
            match_signature(rs, self._sig_table, tolerance=tolerance)
        for m in matches:
            tier, label = preview_tier(self._minerals.get(m["mineral"]))
            m["difficulty"] = tier
            m["difficulty_label"] = label
        return matches

    def update_scan(self, rs=None, panel=None, phase="scan"):
        with self._lock:
            if rs is not None:
                self._state["rs"] = rs
                self._state["matches"] = self._enrich_matches(rs)
            if panel is not None:
                self._state["composition"] = panel.get("minerals", [])
                self._state["inert_pct"] = panel.get("inert_pct")
                self._state["resistance_pct"] = panel.get("resistance_pct")
                self._state["instability"] = panel.get("instability")
                self._state["mass"] = panel.get("mass")
                self._state["rock_verdict"] = judge_rock(
                    panel.get("resistance_pct"), panel.get("instability"),
                    self._equipment, self._thresholds)
                phase = "close_scan"
            self._state["phase"] = phase
            self._state["updated_at"] = time.strftime("%H:%M:%S")
        self._broadcast()

    def clear(self):
        with self._lock:
            self._state.update({
                "phase": "idle", "rs": None, "matches": [], "composition": [],
                "inert_pct": None, "resistance_pct": None, "instability": None,
                "mass": None, "rock_verdict": None,
                "updated_at": time.strftime("%H:%M:%S")})
        self._broadcast()


def create_app(state, cfg):
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse, JSONResponse, Response
    from pydantic import BaseModel

    class TestInput(BaseModel):
        rs: int | None = None
        minerals: list | None = None
        resistance_pct: float | None = None
        instability: float | None = None
        mass: float | None = None

    class CalibrateInput(BaseModel):
        rs_region: dict
        panel_region: dict

    app = FastAPI(title="SC Mining Helper", docs_url=None, redoc_url=None)
    shot_cache = {"ts": 0.0, "png": None}

    def read_html(name):
        with open(os.path.join(BUNDLE_DIR, name), encoding="utf-8") as f:
            return f.read()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return read_html("overlay.html")

    @app.get("/calibrate", response_class=HTMLResponse)
    def calibrate_page():
        return read_html("calibrate.html")

    @app.get("/api/state")
    def get_state():
        return JSONResponse(state.get())

    @app.get("/api/screenshot")
    def screenshot():
        now = time.time()
        if shot_cache["png"] is None or now - shot_cache["ts"] > 15:
            img = Capturer(cfg.get("monitor", 1)).grab_full()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            shot_cache["png"] = buf.getvalue()
            shot_cache["ts"] = now
        return Response(content=shot_cache["png"], media_type="image/png")

    @app.post("/api/calibrate")
    def calibrate(body: CalibrateInput):
        cfg["rs_region"] = body.rs_region
        cfg["panel_region"] = body.panel_region
        cfg["capture_enabled"] = True
        save_config(cfg)
        return {"ok": True, "restart_required": True}

    @app.post("/api/test")
    def test_input(body: TestInput):
        panel = None
        if body.minerals is not None or body.resistance_pct is not None:
            panel = {"minerals": body.minerals or [], "inert_pct": None,
                     "resistance_pct": body.resistance_pct,
                     "instability": body.instability, "mass": body.mass}
        state.update_scan(rs=body.rs, panel=panel, phase="test")
        return {"ok": True}

    @app.post("/api/clear")
    def clear():
        state.clear()
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        q = state.subscribe()
        try:
            await websocket.send_text(json.dumps(state.get(), ensure_ascii=False))
            while True:
                await websocket.send_text(await q.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            state.unsubscribe(q)

    return app


# ---------------------------------------------------------------- 캡처 루프/메인

def capture_loop(cfg, state):
    engine = create_ocr_engine(cfg.get("ocr_engine", "auto"))
    print(f"[capture] OCR 엔진: {engine.name}")
    cap = Capturer(cfg.get("monitor", 1))
    known = list(load_data("minerals.json")["minerals"].keys())
    last_rs = None
    last_panel_sig = None
    interval = max(cfg.get("poll_interval_ms", 500), 200) / 1000.0
    rs_region = cfg.get("rs_region")
    panel_region = cfg.get("panel_region")
    while True:
        try:
            if rs_region:
                text = " ".join(t for t, _ in engine.read_lines(cap.grab(rs_region)))
                rs = parse_rs(text)
                if rs and rs != last_rs:
                    last_rs = rs
                    print(f"[capture] RS {rs}")
                    state.update_scan(rs=rs, phase="scan")
            if panel_region:
                lines = engine.read_lines(cap.grab(panel_region))
                panel = parse_panel([t for t, c in lines if c >= 0.5], known)
                sig_key = repr(panel["minerals"]) + repr(panel["resistance_pct"])
                if (panel["minerals"] or panel["resistance_pct"] is not None) \
                        and sig_key != last_panel_sig:
                    last_panel_sig = sig_key
                    print(f"[capture] 패널: {panel}")
                    state.update_scan(panel=panel)
        except Exception as e:
            print(f"[capture] 오류 (계속 시도): {e}")
        time.sleep(interval)


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "알 수 없음"


def main():
    import uvicorn
    cfg = load_config()
    state = ScanState(load_data("signatures.json"),
                      load_data("minerals.json")["minerals"],
                      load_data("equipment.json"),
                      cfg.get("verdict_thresholds", {}))
    if cfg.get("capture_enabled") and cfg.get("rs_region"):
        threading.Thread(target=capture_loop, args=(cfg, state), daemon=True).start()
    else:
        print("[안내] 캡처 영역 미설정: 화면 읽기 꺼짐. "
              "http://localhost:{}/calibrate 에서 지정하세요.".format(cfg.get("port", 8377)))
    port = cfg.get("port", 8377)
    print("=" * 56)
    print(" SC 광물 오버레이 서버 시작")
    print(f"  이 PC에서:        http://localhost:{port}")
    print(f"  태블릿/같은Wi-Fi: http://{lan_ip()}:{port}")
    print("  종료: 이 창을 닫거나 Ctrl+C")
    print("=" * 56)
    uvicorn.run(create_app(state, cfg), host=cfg.get("host", "0.0.0.0"),
                port=port, log_level="warning")


if __name__ == "__main__":
    main()
