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
    "scan_enabled": True,
    "monitor": 1,
    "rs_region": None,
    "panel_region": None,
    "rs_search": "wide",        # wide: 넓은 영역에서 시그니처 숫자 검색 / fixed: 지정 영역만
    "rs_wide_region": None,     # None이면 모니터 중앙 84% 자동
    "poll_interval_ms": 500,
    "ocr_engine": "auto",
    "overlay_enabled": True,
    "overlay_x": 960,
    "overlay_y": 320,
    "overlay_width": 380,
    "overlay_opacity": 0.88,
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


def parse_rs_pos(lines_pos, sig_table):
    """위치 정보가 있는 OCR 줄에서 시그니처 숫자 하나를 고른다.
    핀 아이콘 상자의 숫자(12,000 형태)가 시그니처이고, 나침반 숫자
    (300/320 등)는 표에 맞지 않고 상자보다 아래에 있다."""
    found = []  # (y, n, matched)
    for text, _conf, y in lines_pos:
        # 공백은 유지(토큰 분리), 쉼표만 제거 (12,000 → 12000)
        cleaned = text.replace(",", "")
        for mm in RS_RE.finditer(cleaned):
            n = int(mm.group(1))
            matched = bool(match_signature(n, sig_table, tolerance=0) or
                           match_signature(n, sig_table, tolerance=2))
            found.append((y, n, matched))
    if not found:
        return None
    matched_nums = {n for _y, n, ok in found if ok}
    if len(matched_nums) == 1:
        return matched_nums.pop()
    if matched_nums:
        return min((f for f in found if f[2]), key=lambda f: f[0])[1]
    # 표에 맞는 숫자가 없으면 가장 위 줄의 숫자 (핀 상자가 나침반보다 위)
    return min(found, key=lambda f: f[0])[1]


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


def _group_rows_pos(result):
    """_group_rows + 각 줄의 상단 y 좌표 (핀 상자/나침반 구분용)."""
    tokens = []
    for box, text, conf in result:
        tokens.append({
            "cy": sum(p[1] for p in box) / 4,
            "top": min(p[1] for p in box),
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
                      sum(t["conf"] for t in row) / len(row),
                      min(t["top"] for t in row)))
    return lines


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

    def read_lines_pos(self, img):
        import numpy as np
        result, _ = self._engine(np.array(_preprocess(img)))
        return _group_rows_pos(result) if result else []


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

    def read_lines_pos(self, img):
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
        out = []
        for line in result.lines:
            try:
                y = min(w.bounding_rect.y for w in line.words)
            except Exception:
                y = 0
            out.append((line.text, 1.0, y))
        return out


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

    def mon_size(self):
        mon = self._sct.monitors[self.monitor]
        return {"width": mon["width"], "height": mon["height"]}


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
        cfg["scan_enabled"] = True
        save_config(cfg)
        return {"ok": True, "restart_required": False}

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


# ---------------------------------------------------------------- 내장 오버레이 UI
#
# tkinter 기반. 게임 위에 직접 그리는 투명/최상위/클릭통과 창 + 작은 제어 창.
# 화면 읽기 전용 원칙 유지 (인젝션/후킹 없음). tkinter는 지연 임포트하여
# 서버 전용 환경(테스트/CI)에서는 이 모듈을 로드하지 않는다.

_BG = "#000000"        # transparentcolor 키 (이 색은 완전 투명)
_FG = "#f2f2f2"
_DIM = "#9e9e9e"
_GREEN = "#3ddc84"
_ORANGE = "#ffb74d"
_RED = "#ff5252"
_ACCENT = "#2eccbe"
_VERDICT_COLOR = {"ok": _GREEN, "hard": _ORANGE, "infeasible": _RED,
                  "unknown": _DIM}

LAST_ERROR = None  # 제어 창에 표시할 마지막 오류


def _make_icon_image(size=256):
    """앱 아이콘을 런타임에 그린다 (별도 파일 불필요): 어두운 배경 + 청록 크리스탈."""
    from PIL import Image, ImageDraw
    S = size
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(S * 0.22)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=r, fill=(18, 20, 26, 255))
    d.rounded_rectangle([3, 3, S - 4, S - 4], radius=r - 3,
                        outline=(45, 52, 64, 255), width=max(2, S // 85))
    cx = S // 2
    top, mid, bot = int(S * 0.20), int(S * 0.42), int(S * 0.80)
    half = int(S * 0.24)
    d.polygon([(cx, top), (cx + half, mid), (cx, bot), (cx - half, mid)],
              fill=(46, 204, 190, 255))
    d.polygon([(cx, top), (cx + half, mid), (cx, int(S * 0.50))],
              fill=(84, 232, 214, 255))
    d.polygon([(cx, top), (cx - half, mid), (cx, int(S * 0.50))],
              fill=(28, 160, 150, 255))
    d.polygon([(cx - half, mid), (cx, int(S * 0.50)), (cx, bot)],
              fill=(22, 128, 120, 255))
    d.polygon([(cx + half, mid), (cx, int(S * 0.50)), (cx, bot)],
              fill=(34, 178, 168, 255))
    return img


def _pick_font_family(root):
    import tkinter.font as tkfont
    fams = set(tkfont.families(root))
    for cand in ("Malgun Gothic", "Noto Sans CJK KR", "Apple SD Gothic Neo"):
        if cand in fams:
            return cand
    return "TkDefaultFont"


def _win32_set_clickthrough(win, transparent=True):
    """Windows: 창을 클릭통과(WS_EX_TRANSPARENT)+비활성(NOACTIVATE)로 설정."""
    if os.name != "nt":
        return
    import ctypes
    GWL_EXSTYLE = -20
    WS_EX_TRANSPARENT = 0x20
    WS_EX_NOACTIVATE = 0x08000000
    win.update_idletasks()
    hwnd = int(win.wm_frame(), 16)
    u32 = ctypes.windll.user32
    style = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if transparent:
        style |= WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
    else:
        style &= ~WS_EX_TRANSPARENT
    u32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


_RARITY_KR = {"legendary": "레전드", "epic": "에픽", "rare": "레어",
              "uncommon": "언커먼", "common": "커먼"}


def mineral_info_lines(name, minerals):
    """오버레이 하단에 표시할 광물 간략 정보 (최대 2줄, 검증된 데이터만)."""
    m = (minerals or {}).get(name)
    if not m:
        return []
    parts = [name]
    tier = _RARITY_KR.get(m.get("rarity"))
    if tier:
        parts.append(tier + " 등급")
    price = m.get("value_auec_scu")
    if price:
        parts.append(f"시세 ~{price:,} aUEC/SCU")
    lines = [" · ".join(parts)]
    if m.get("special"):
        lines.append(m["special"])
    return lines


def wide_rs_region(cfg, mon):
    """RS 숫자를 찾을 넓은 영역. 함선이 바라보는 방향에 따라 숫자 위치가
    바뀌므로 기본값은 모니터 중앙부 대부분이다."""
    r = cfg.get("rs_wide_region")
    if r:
        return r
    w, h = mon["width"], mon["height"]
    return {"left": int(w * 0.08), "top": int(h * 0.06),
            "width": int(w * 0.84), "height": int(h * 0.88)}


def find_rs_candidates(lines, sig_table):
    """OCR 줄들에서 시그니처 표와 맞는 숫자 후보만 골라낸다."""
    cands = set()
    for text, _conf in lines:
        # 공백은 유지(토큰 분리): "300 320 340"이 300320으로 합쳐지는 것 방지
        cleaned = text.replace(",", "")
        for m in RS_RE.finditer(cleaned):
            n = int(m.group(1))
            if match_signature(n, sig_table, tolerance=0) or \
                    match_signature(n, sig_table, tolerance=2):
                cands.add(n)
    return cands


class RegionOverlay:
    """캡처 영역(RS/패널/넓은 검색)을 화면에 테두리로 보여주는 창."""

    def __init__(self, root, cfg):
        import mss
        import tkinter as tk
        self.cfg = cfg
        with mss.mss() as sct:
            mons = sct.monitors
            idx = cfg.get("monitor", 1)
            if idx >= len(mons):
                idx = 1
            mon = mons[idx]
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=_BG)
        try:
            self.win.attributes("-transparentcolor", _BG)
        except Exception:
            pass
        self.win.geometry("{}x{}+{}+{}".format(
            mon["width"], mon["height"], mon["left"], mon["top"]))
        cv = tk.Canvas(self.win, bg=_BG, highlightthickness=0)
        cv.pack(fill="both", expand=True)
        boxes = []
        panel = cfg.get("panel_region")
        if panel:
            boxes.append((panel, "패널 영역", "#e879f9"))
        if cfg.get("rs_search", "wide") == "fixed":
            r = cfg.get("rs_region")
            if r:
                boxes.append((r, "RS 영역 (고정)", "#22d3ee"))
        else:
            boxes.append((wide_rs_region(cfg, mon),
                          "RS 넓은 검색 영역", "#facc15"))
        for r, label, color in boxes:
            x0, y0 = r["left"], r["top"]
            x1, y1 = x0 + r["width"], y0 + r["height"]
            cv.create_rectangle(x0, y0, x1, y1, outline=color, width=3)
            cv.create_text(x0 + 8, y0 + 6, text=label, fill=color,
                           anchor="nw", font=("Malgun Gothic", 13, "bold"))

    def destroy(self):
        try:
            self.win.destroy()
        except Exception:
            pass


class OverlayWindow:
    """게임 위에 뜨는 투명 클릭통과 결과 창."""

    def __init__(self, root, cfg, family, minerals=None):
        import tkinter as tk
        self.tk = tk
        self.cfg = cfg
        self._minerals = minerals or {}
        self.FONT = (family, 11, "bold")
        self.FONT_SM = (family, 10)
        self.FONT_LG = (family, 13, "bold")
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=_BG)
        self.lines = []
        for _ in range(11):
            lbl = tk.Label(self.win, text="", bg=_BG, fg=_FG,
                           font=self.FONT, anchor="center", justify="center")
            lbl.pack(fill="x")
            self.lines.append(lbl)
        self._fit_width()
        self.visible = True
        self.adjusting = False
        self._drag = None
        self._last_render = None
        if not self.cfg.get("overlay_anchor_v2"):
            # 구버전 config(좌상단 기준 x)를 중심 기준으로 1회 변환
            self.cfg["overlay_x"] = int(self.cfg.get("overlay_x", 40)) \
                + int(self.cfg.get("overlay_width", 380)) // 2
            self.cfg["overlay_anchor_v2"] = True
            save_config(self.cfg)
        self.apply_geometry()
        self.apply_opacity()
        try:
            self.win.attributes("-transparentcolor", _BG)
        except Exception as e:
            print(f"[overlay] 투명 키 설정 실패: {e}")
        self.win.bind("<ButtonPress-1>", self._press)
        self.win.bind("<B1-Motion>", self._move)
        self.win.bind("<ButtonRelease-1>", self._release)
        _win32_set_clickthrough(self.win, True)

    def _fit_width(self):
        w = int(self.cfg.get("overlay_width", 380))
        for lbl in self.lines:
            lbl.configure(wraplength=w - 12)

    def _fit_height(self):
        self.win.update_idletasks()
        h = max(self.win.winfo_reqheight(), 1)
        w = int(self.cfg.get("overlay_width", 380))
        self.win.geometry(f"{w}x{h}+{self.win.winfo_x()}+{self.win.winfo_y()}")

    def apply_geometry(self):
        # overlay_x 는 상자의 가로 중심. 내용이 바뀌어도 중심이 유지된다.
        w = int(self.cfg.get("overlay_width", 380))
        cx = int(self.cfg.get("overlay_x", 40))
        y = int(self.cfg.get("overlay_y", 320))
        self.win.geometry(f"{w}x10+{cx - w // 2}+{y}")

    def apply_opacity(self):
        try:
            self.win.attributes("-alpha",
                                float(self.cfg.get("overlay_opacity", 0.88)))
            self.win.attributes("-transparentcolor", _BG)
        except Exception:
            pass

    # -- 위치 조정 모드 (클릭통과 해제, 드래그로 이동) --
    def set_adjust(self, on):
        self.adjusting = on
        if on:
            self.win.configure(bg="#101418")
            self.lines[0].configure(text="위치 조정: 드래그로 이동, F9로 저장",
                                    fg=_ORANGE, font=self.FONT)
            for lbl in self.lines[1:]:
                lbl.configure(text="")
            self._fit_height()
            _win32_set_clickthrough(self.win, False)
        else:
            self.win.configure(bg=_BG)
            w = int(self.cfg.get("overlay_width", 380))
            self.cfg["overlay_x"] = self.win.winfo_x() + w // 2
            self.cfg["overlay_y"] = self.win.winfo_y()
            save_config(self.cfg)
            _win32_set_clickthrough(self.win, True)
            self._last_render = None

    def _press(self, e):
        if self.adjusting:
            self._drag = (e.x_root - self.win.winfo_x(),
                          e.y_root - self.win.winfo_y())

    def _move(self, e):
        if self.adjusting and self._drag:
            self.win.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _release(self, e):
        self._drag = None

    def toggle_visible(self):
        self.visible = not self.visible
        if not self.visible:
            for lbl in self.lines:
                lbl.configure(text="")
        self._last_render = None

    def render(self, st, scan_on):
        if self.adjusting or not self.visible:
            return
        F, FS, FL = self.FONT, self.FONT_SM, self.FONT_LG
        rows = []  # (text, color, font)
        if not scan_on:
            # 스캔 꺼짐: 오버레이에 아무 글자도 표시하지 않는다
            if self._last_render is not None:
                for lbl in self.lines:
                    lbl.configure(text="")
                self._last_render = None
                self._fit_height()
            return
        rs = st.get("rs")
        matches = st.get("matches") or []
        if rs:
            if matches:
                names = " / ".join(
                    f"{m['mineral']} ×{m['count']}" for m in matches[:3])
                mark = " (OCR 보정)" if any(m.get("corrected") for m in matches) else ""
                rows.append((f"RS {rs} · {names}{mark}", _FG, FL))
                rows.append((f"통상 난이도: {matches[0].get('difficulty_label', '')}",
                             _DIM, FS))
            else:
                rows.append((f"RS {rs} · 알 수 없는 시그니처", _ORANGE, F))
        comp = st.get("composition") or []
        if comp:
            for c in comp[:6]:
                rows.append((f"{c['name']}  {c['pct']:g}%", _FG, F))
            if st.get("inert_pct") is not None:
                rows.append((f"불활성 {st['inert_pct']:g}%", _DIM, FS))
            stats = []
            if st.get("resistance_pct") is not None:
                stats.append(f"저항 {st['resistance_pct']:g}%")
            if st.get("instability") is not None:
                stats.append(f"불안정 {st['instability']:g}")
            if st.get("mass") is not None:
                stats.append(f"질량 {st['mass']:g}")
            if stats:
                rows.append((" · ".join(stats), _DIM, FS))
        verdict = st.get("rock_verdict")
        if verdict:
            label = verdict.get("label", "")
            if verdict.get("beta"):
                label += " [BETA]"
            rows.append((label,
                         _VERDICT_COLOR.get(verdict.get("level"), _FG), FL))
        target = None
        if comp:
            target = comp[0]["name"]
        elif matches:
            target = matches[0]["mineral"]
        if target:
            for info in mineral_info_lines(target, self._minerals):
                rows.append((info, _DIM, FS))
        if not rows:
            key = None
        else:
            key = repr(rows)
        if key == self._last_render:
            return
        self._last_render = key
        for i, lbl in enumerate(self.lines):
            if i < len(rows):
                lbl.configure(text=rows[i][0], fg=rows[i][1], font=rows[i][2])
            else:
                lbl.configure(text="")
        self._fit_height()


class Calibrator:
    """게임 화면 위에서 RS 영역/패널 영역을 두 번 드래그로 지정."""

    def __init__(self, root, cfg, on_done, family="Malgun Gothic"):
        import tkinter as tk
        self.cfg = cfg
        self.on_done = on_done
        self._family = family
        self.stage = 0
        self.start = None
        import mss
        with mss.mss() as sct:
            mons = sct.monitors
            idx = cfg.get("monitor", 1)
            if idx >= len(mons):
                idx = 1
            self.mon = mons[idx]
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.35)
        self.win.configure(bg="#000000")
        self.win.geometry("{}x{}+{}+{}".format(
            self.mon["width"], self.mon["height"],
            self.mon["left"], self.mon["top"]))
        self.win.configure(cursor="cross")
        self.canvas = tk.Canvas(self.win, bg="#000000", highlightthickness=0,
                                cursor="cross")
        self.canvas.pack(fill="both", expand=True)
        self.hint = tk.Label(self.win, text="", bg="#000000", fg="#ffffff",
                             font=(family, 16, "bold"))
        self.hint.place(relx=0.5, y=40, anchor="n")
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.win.bind("<Escape>", lambda e: self.cancel())
        self.rect = None
        self.rect_label = None
        self.regions = {}
        # 현재 설정된 영역을 기준선으로 항상 표시 (F11 표시와 동일한 색)
        self._draw_reference(cfg.get("rs_region"), "현재 RS 영역", "#22d3ee")
        self._draw_reference(cfg.get("panel_region"), "현재 패널 영역", "#e879f9")
        self._set_hint()
        self.win.focus_force()

    def _draw_reference(self, r, label, color):
        if not r:
            return
        x0, y0 = r["left"], r["top"]
        self.canvas.create_rectangle(x0, y0, x0 + r["width"],
                                     y0 + r["height"], outline=color,
                                     width=2, dash=(6, 4))
        self.canvas.create_text(x0 + 8, y0 + 6, text=label, fill=color,
                                anchor="nw",
                                font=(self._family, 12, "bold"))

    def _set_hint(self):
        if self.stage == 0:
            self.hint.configure(
                text="1/2: RS 숫자(핀 상자) 자리를 드래그하면 바로 저장됨 (ESC 취소)")
        else:
            self.hint.configure(
                text="2/2: 근접 스캔 패널 자리를 드래그 (ESC = RS만 저장하고 완료)")

    def _press(self, e):
        self.start = (e.x, e.y)
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline="#00e5ff", width=3)

    def _move(self, e):
        if self.start:
            self.canvas.coords(self.rect, self.start[0], self.start[1],
                               e.x, e.y)

    def _release(self, e):
        if not self.start:
            return
        x0, y0 = self.start
        x1, y1 = e.x, e.y
        self.start = None
        left, top = min(x0, x1), min(y0, y1)
        w, h = abs(x1 - x0), abs(y1 - y0)
        if w < 12 or h < 12:
            return  # 너무 작은 드래그는 무시
        key = "rs_region" if self.stage == 0 else "panel_region"
        self.regions[key] = {"left": left, "top": top, "width": w, "height": h}
        if self.rect_label:
            self.canvas.delete(self.rect_label)
        self.rect_label = self.canvas.create_text(
            left + 8, top + 6,
            text="새 RS 영역 저장됨" if self.stage == 0 else "새 패널 영역 저장됨",
            fill="#00e5ff", anchor="nw",
            font=(self._family, 12, "bold"))
        if self.stage == 0:
            self.stage = 1
            self._set_hint()
        else:
            self.finish()

    def finish(self):
        if self.regions.get("rs_region"):
            self.cfg["rs_region"] = self.regions["rs_region"]
        if self.regions.get("panel_region"):
            self.cfg["panel_region"] = self.regions["panel_region"]
        self.cfg["capture_enabled"] = True
        self.cfg["scan_enabled"] = True
        save_config(self.cfg)
        self.win.destroy()
        self.on_done(True)

    def cancel(self):
        # 1단계(RS)까지 지정했으면 그것만 저장하고 완료, 아무것도 없으면 취소
        if self.regions:
            self.finish()
        else:
            self.win.destroy()
            self.on_done(False)


class ControlApp:
    """customtkinter 기반 제어 창 + 오버레이 창 + 전역 단축키."""

    def __init__(self, cfg, state):
        import queue
        import customtkinter as ctk
        from PIL import Image
        self.ctk = ctk
        self.cfg = cfg
        self.state = state
        self.hotkey_q = queue.Queue()
        ctk.set_appearance_mode("dark")
        self.root = ctk.CTk()
        self.root.title("SC Mining Helper")
        self.root.geometry("360x640+60+60")
        self.root.resizable(False, False)
        self.root.configure(fg_color="#0f1115")
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        family = _pick_font_family(self.root)
        self._family = family
        self.F_TITLE = ctk.CTkFont(family=family, size=17, weight="bold")
        self.F_H = ctk.CTkFont(family=family, size=13, weight="bold")
        self.F_B = ctk.CTkFont(family=family, size=12)
        self.F_SM = ctk.CTkFont(family=family, size=11)
        icon_img = _make_icon_image(128)
        self._icon_img = ctk.CTkImage(icon_img, size=(34, 34))
        try:
            from PIL import ImageTk
            self.root.iconphoto(True, ImageTk.PhotoImage(icon_img))
        except Exception:
            pass
        self.overlay = OverlayWindow(self.root, cfg, family,
                                     minerals=self.state._minerals)
        self.region_overlay = None
        self._build()
        self._start_hotkeys()
        self.root.after(250, self._tick)
        self.root.after(150, self._drain_hotkeys)

    def _card(self, title):
        ctk = self.ctk
        card = ctk.CTkFrame(self.root, fg_color="#171a21", corner_radius=12,
                            border_width=1, border_color="#23262e")
        card.pack(padx=14, pady=(10, 0), fill="x")
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(10, 4))
        ctk.CTkLabel(head, text=title, font=self.F_H,
                     text_color="#e8eaf0").pack(side="left")
        return card, head

    def _build(self):
        ctk = self.ctk
        # ---- 헤더
        header = ctk.CTkFrame(self.root, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(14, 2))
        if self._icon_img:
            ctk.CTkLabel(header, text="", image=self._icon_img).pack(side="left")
        ctk.CTkLabel(header, text="  SC 광물 오버레이",
                     font=self.F_TITLE, text_color="#f2f4f8").pack(side="left")
        self.pill = ctk.CTkLabel(header, text="  스캔 중  ", font=self.F_SM,
                                 fg_color="#12312e", text_color=_ACCENT,
                                 corner_radius=10)
        self.pill.pack(side="right", pady=6)

        # ---- 스캔 카드
        card, head = self._card("스캔")
        self.scan_sw = ctk.CTkSwitch(
            head, text="", width=42, command=self._scan_switch,
            progress_color=_ACCENT)
        self.scan_sw.pack(side="right")
        if self.cfg.get("scan_enabled", True):
            self.scan_sw.select()
        self.last_scan = ctk.CTkLabel(card, text="아직 스캔 없음",
                                      font=self.F_B, text_color="#9aa0ab")
        self.last_scan.pack(anchor="w", padx=14, pady=(0, 10))

        # ---- 오버레이 카드
        card, head = self._card("오버레이")
        self.ov_sw = ctk.CTkSwitch(head, text="", width=42,
                                   command=self._ov_switch,
                                   progress_color=_ACCENT)
        self.ov_sw.pack(side="right")
        self.ov_sw.select()
        op_row = ctk.CTkFrame(card, fg_color="transparent")
        op_row.pack(fill="x", padx=14, pady=(2, 0))
        ctk.CTkLabel(op_row, text="투명도", font=self.F_SM,
                     text_color="#9aa0ab").pack(side="left")
        self.op_label = ctk.CTkLabel(op_row, text="", font=self.F_SM,
                                     text_color="#9aa0ab")
        self.op_label.pack(side="right")
        self.opacity = ctk.CTkSlider(
            card, from_=30, to=100, command=self._opacity_changed,
            progress_color=_ACCENT, button_color=_ACCENT,
            button_hover_color="#25a89b")
        self.opacity.set(float(self.cfg.get("overlay_opacity", 0.88)) * 100)
        self.opacity.pack(fill="x", padx=14, pady=(0, 6))
        ctk.CTkButton(card, text="위치 조정 (F9)", command=self.toggle_adjust,
                      fg_color="#23262e", hover_color="#2c313b",
                      text_color="#e8eaf0", height=30).pack(
            fill="x", padx=14, pady=(0, 12))

        # ---- 설정 카드
        card, head = self._card("설정")
        ctk.CTkButton(card, text="화면 영역 지정 (F10)",
                      command=self.start_calibrate,
                      fg_color=_ACCENT, hover_color="#25a89b",
                      text_color="#062622", height=32,
                      font=self.F_H).pack(fill="x", padx=14, pady=(2, 6))
        ctk.CTkButton(card, text="스캔 영역 표시 (F11)",
                      command=self.toggle_regions,
                      fg_color="#23262e", hover_color="#2c313b",
                      text_color="#e8eaf0", height=30).pack(
            fill="x", padx=14, pady=(0, 6))
        ctk.CTkLabel(card, text="RS 읽기 방식", font=self.F_SM,
                     text_color="#9aa0ab").pack(anchor="w", padx=14)
        self.rs_mode_seg = ctk.CTkSegmentedButton(
            card, values=["넓게 찾기", "고정 영역"],
            command=self._rs_mode_changed)
        self.rs_mode_seg.set("고정 영역" if self.cfg.get("rs_search") == "fixed"
                             else "넓게 찾기")
        self.rs_mode_seg.pack(fill="x", padx=14, pady=(2, 10))
        ctk.CTkButton(card, text="태블릿/웹에서 보기", command=self.open_web,
                      fg_color="#23262e", hover_color="#2c313b",
                      text_color="#e8eaf0", height=30).pack(
            fill="x", padx=14, pady=(0, 4))
        self.srv_label = ctk.CTkLabel(card, text="", font=self.F_SM,
                                      text_color="#6b7078")
        self.srv_label.pack(anchor="w", padx=14, pady=(0, 10))

        # ---- 오류 배너
        self.err_banner = ctk.CTkLabel(self.root, text="", font=self.F_SM,
                                       fg_color="#3a1d20", text_color="#ff8a80",
                                       corner_radius=8, wraplength=320)

        # ---- 푸터
        ctk.CTkLabel(self.root,
                     text="F8 표시/숨기기 · F9 위치 · F10 영역 지정",
                     font=self.F_SM, text_color="#565b64").pack(pady=(12, 0))
        ctk.CTkButton(self.root, text="종료", command=self.quit, width=90,
                      fg_color="transparent", hover_color="#3a1d20",
                      text_color="#ff8a80", height=28).pack(pady=(6, 10))

    def _scan_switch(self):
        self.cfg["scan_enabled"] = bool(self.scan_sw.get())
        save_config(self.cfg)

    def _ov_switch(self):
        if bool(self.ov_sw.get()) != self.overlay.visible:
            self.overlay.toggle_visible()

    def toggle_scan(self):
        self.cfg["scan_enabled"] = not self.cfg.get("scan_enabled", True)
        if self.cfg["scan_enabled"]:
            self.scan_sw.select()
        else:
            self.scan_sw.deselect()
        save_config(self.cfg)

    def toggle_overlay(self):
        self.overlay.toggle_visible()
        if self.overlay.visible:
            self.ov_sw.select()
        else:
            self.ov_sw.deselect()

    def _opacity_changed(self, v):
        self.cfg["overlay_opacity"] = round(float(v)) / 100.0
        self.overlay.apply_opacity()
        self.op_label.configure(text=f"{int(float(v))}%")
        save_config(self.cfg)

    def toggle_adjust(self):
        self.overlay.set_adjust(not self.overlay.adjusting)

    def toggle_regions(self):
        if self.region_overlay is not None:
            self.region_overlay.destroy()
            self.region_overlay = None
        else:
            try:
                self.region_overlay = RegionOverlay(self.root, self.cfg)
            except Exception as e:
                print(f"[region] 표시 실패: {e}")
                self.region_overlay = None

    def _rs_mode_changed(self, label):
        self.cfg["rs_search"] = "fixed" if label == "고정 영역" else "wide"
        save_config(self.cfg)
        if self.region_overlay is not None:
            self.toggle_regions()
            self.toggle_regions()

    def start_calibrate(self):
        if getattr(self, "_calib", None):
            return
        # 캘리브레이션 중에는 영역 표시 창을 잠시 숨기고, 끝나면 다시 표시
        had_regions = self.region_overlay is not None
        if had_regions:
            self.toggle_regions()

        def done(ok):
            self._calib = None
            if ok:
                # 영역을 직접 지정했으면 그 영역을 쓰는 고정 모드로 전환
                self.cfg["rs_search"] = "fixed"
                save_config(self.cfg)
                self.rs_mode_seg.set("고정 영역")
                self.overlay._last_render = None
            if had_regions:
                self.toggle_regions()

        self._calib = Calibrator(self.root, self.cfg, done, self._family)

    def open_web(self):
        import webbrowser
        webbrowser.open("http://localhost:{}".format(self.cfg.get("port", 8377)))

    def quit(self):
        save_config(self.cfg)
        try:
            self.root.destroy()
        finally:
            os._exit(0)

    # -- 전역 단축키 (Windows RegisterHotKey, 관리자 권한 불필요) --
    def _start_hotkeys(self):
        if os.name != "nt":
            return

        def pump():
            import ctypes
            from ctypes import wintypes
            u32 = ctypes.windll.user32
            for hid, vk in ((1, 0x77), (2, 0x78), (3, 0x79),
                            (4, 0x7A)):  # F8/F9/F10/F11
                if not u32.RegisterHotKey(None, hid, 0x4000, vk):
                    print(f"[hotkey] F{vk - 0x76} 등록 실패 (다른 프로그램이 사용 중)")
            msg = wintypes.MSG()
            while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == 0x0312:  # WM_HOTKEY
                    self.hotkey_q.put(msg.wParam)

        threading.Thread(target=pump, daemon=True).start()

    def _drain_hotkeys(self):
        try:
            while True:
                hid = self.hotkey_q.get_nowait()
                if hid == 1:
                    self.toggle_overlay()
                elif hid == 2:
                    self.toggle_adjust()
                elif hid == 3:
                    self.start_calibrate()
                elif hid == 4:
                    self.toggle_regions()
        except Exception:
            pass
        self.root.after(150, self._drain_hotkeys)

    def _tick(self):
        global LAST_ERROR
        st = self.state.get()
        scan_on = self.cfg.get("scan_enabled", True)
        port = self.cfg.get("port", 8377)
        self.pill.configure(
            text="  스캔 중  " if scan_on else "  스캔 꺼짐  ",
            fg_color="#12312e" if scan_on else "#2a2d34",
            text_color=_ACCENT if scan_on else "#9aa0ab")
        rs = st.get("rs")
        phase = {"idle": "대기", "scan": "스캔", "close_scan": "근접 스캔",
                 "test": "테스트"}.get(st.get("phase"), st.get("phase", "-"))
        region_ok = bool(self.cfg.get("rs_region"))
        if rs:
            txt = f"RS {rs} · {phase} · {st.get('updated_at', '')}"
        elif region_ok:
            txt = f"대기 중 ({phase})"
        else:
            txt = "캡처 영역 미설정 — F10으로 지정"
        self.last_scan.configure(text=txt)
        self.srv_label.configure(
            text=f"http://localhost:{port}  ·  같은 Wi-Fi: http://{lan_ip()}:{port}")
        if LAST_ERROR:
            self.err_banner.configure(text=f"최근 오류: {LAST_ERROR}")
            self.err_banner.pack(padx=14, pady=(10, 0), fill="x",
                                 before=self.root.children[list(self.root.children)[-1]])
        else:
            self.err_banner.pack_forget()
        self.overlay.render(st, scan_on)
        self.root.after(250, self._tick)

    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------- 캡처 루프/메인

def capture_loop(cfg, state):
    """cfg를 매 주기 다시 읽어 캘리브레이션/스캔 토글이 재시작 없이 반영되게 한다."""
    global LAST_ERROR
    engine = None
    cap = None
    known = list(load_data("minerals.json")["minerals"].keys())
    sig_table = load_data("signatures.json")
    last_rs = None
    last_panel_sig = None
    pending_rs = None  # 넓은 검색: 2회 연속 같은 후보일 때만 채택
    while True:
        try:
            rs_region = cfg.get("rs_region")
            panel_region = cfg.get("panel_region")
            rs_wide = cfg.get("rs_search", "wide") != "fixed"
            if not cfg.get("capture_enabled", True) \
                    or not cfg.get("scan_enabled", True) \
                    or not (rs_region or panel_region or rs_wide):
                time.sleep(0.4)
                continue
            if engine is None:
                engine = create_ocr_engine(cfg.get("ocr_engine", "auto"))
                print(f"[capture] OCR 엔진: {engine.name}")
            if cap is None or cap.monitor != cfg.get("monitor", 1):
                cap = Capturer(cfg.get("monitor", 1))
            interval = max(cfg.get("poll_interval_ms", 500), 200) / 1000.0
            rs = None
            if rs_wide:
                # 함선이 바라보는 방향에 따라 RS 숫자 위치가 바뀌므로 넓은
                # 영역을 읽고 시그니처 표와 맞는 숫자만 채택한다.
                interval = max(interval, 0.8)
                region = wide_rs_region(cfg, cap.mon_size())
                img = cap.grab(region)
                if img.width > 1600:
                    img = img.resize((1600, int(img.height * 1600 / img.width)))
                cands = find_rs_candidates(engine.read_lines(img), sig_table)
                cand = cands.pop() if len(cands) == 1 else None
                if cand is not None and cand == pending_rs:
                    rs = cand
                pending_rs = cand
            elif rs_region:
                # 지정 영역 안에서 핀 상자 숫자만: 시그니처 표 매칭 우선,
                # 없으면 가장 위 줄 숫자 (나침반 숫자 오인식 방지)
                lines_pos = engine.read_lines_pos(cap.grab(rs_region))
                rs = parse_rs_pos(lines_pos, sig_table)
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
            LAST_ERROR = f"{type(e).__name__}: {e}"
            print(f"[capture] 오류 (계속 시도): {e}")
            interval = 1.0
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


def _setup_windowed_logging():
    """콘솔 없는 exe: 출력을 로그 파일로 돌리고, 치명적 오류는 앱 창/로그에 표시."""
    import traceback
    if getattr(sys, "frozen", False):
        try:
            log = open(os.path.join(EXE_DIR, "scm_helper.log"), "a",
                       encoding="utf-8", buffering=1)
            if sys.stdout is None or not hasattr(sys.stdout, "write"):
                sys.stdout = log
            if sys.stderr is None or not hasattr(sys.stderr, "write"):
                sys.stderr = log
        except OSError:
            pass

    def hook(etype, value, tb):
        global LAST_ERROR
        LAST_ERROR = f"{etype.__name__}: {value}"
        try:
            traceback.print_exception(etype, value, tb)
        except Exception:
            pass

    sys.excepthook = hook

    def thread_hook(args):
        global LAST_ERROR
        LAST_ERROR = f"{args.exc_type.__name__}: {args.exc_value}"
        try:
            traceback.print_exception(args.exc_type, args.exc_value,
                                      args.exc_traceback)
        except Exception:
            pass

    threading.excepthook = thread_hook


def main():
    _setup_windowed_logging()
    cfg = load_config()
    state = ScanState(load_data("signatures.json"),
                      load_data("minerals.json")["minerals"],
                      load_data("equipment.json"),
                      cfg.get("verdict_thresholds", {}))
    port = cfg.get("port", 8377)
    threading.Thread(target=capture_loop, args=(cfg, state),
                     daemon=True).start()
    if not cfg.get("rs_region"):
        print("[안내] 캡처 영역 미설정: 제어 창의 '화면 영역 지정'(F10) 또는 "
              f"http://localhost:{port}/calibrate 에서 지정하세요.")
    print("=" * 56)
    print(" SC 광물 오버레이 시작")
    print(f"  이 PC에서:        http://localhost:{port}")
    print(f"  태블릿/같은Wi-Fi: http://{lan_ip()}:{port}")
    print("  종료: 제어 창의 종료 버튼 또는 이 창 닫기")
    print("=" * 56)

    def run_server():
        import uvicorn
        uvicorn.run(create_app(state, cfg), host=cfg.get("host", "0.0.0.0"),
                    port=port, log_level="warning")

    server_started = False
    if cfg.get("overlay_enabled", True) and not os.environ.get("SC_HEADLESS"):
        try:
            threading.Thread(target=run_server, daemon=True).start()
            server_started = True
            ControlApp(cfg, state).run()  # 메인 스레드: tk mainloop
            return
        except Exception as e:
            print(f"[overlay] 내장 오버레이를 시작하지 못했습니다 "
                  f"(웹 모드로 동작): {e}")
    if not server_started:
        run_server()  # 웹/헤드리스 모드: 서버가 메인 스레드에서 실행
    else:
        threading.Event().wait()  # UI만 죽은 경우 서버 스레드 유지


if __name__ == "__main__":
    main()
