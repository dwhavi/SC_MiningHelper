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
    "poll_interval_ms": 500,
    "ocr_engine": "auto",
    "overlay_enabled": True,
    "overlay_x": 40,
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
_FONT = ("Malgun Gothic", 11, "bold")
_FONT_SM = ("Malgun Gothic", 10)
_VERDICT_COLOR = {"ok": _GREEN, "hard": _ORANGE, "infeasible": _RED,
                  "unknown": _DIM}


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


class OverlayWindow:
    """게임 위에 뜨는 투명 클릭통과 결과 창."""

    def __init__(self, root, cfg):
        import tkinter as tk
        self.tk = tk
        self.cfg = cfg
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=_BG)
        self.lines = []
        for _ in range(9):
            lbl = tk.Label(self.win, text="", bg=_BG, fg=_FG,
                           font=_FONT, anchor="w", justify="left")
            lbl.pack(anchor="w")
            self.lines.append(lbl)
        self.visible = True
        self.adjusting = False
        self._drag = None
        self._last_render = None
        self.apply_geometry()
        self.apply_opacity()
        self.win.attributes("-transparentcolor", _BG)
        self.win.bind("<ButtonPress-1>", self._press)
        self.win.bind("<B1-Motion>", self._move)
        self.win.bind("<ButtonRelease-1>", self._release)
        _win32_set_clickthrough(self.win, True)

    def apply_geometry(self):
        w = int(self.cfg.get("overlay_width", 380))
        x = int(self.cfg.get("overlay_x", 40))
        y = int(self.cfg.get("overlay_y", 320))
        self.win.geometry(f"{w}x10+{x}+{y}")

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
                                    fg=_ORANGE)
            for lbl in self.lines[1:]:
                lbl.configure(text="")
            _win32_set_clickthrough(self.win, False)
        else:
            self.win.configure(bg=_BG)
            self.cfg["overlay_x"] = self.win.winfo_x()
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
        rows = []  # (text, color, font)
        if not scan_on:
            rows.append(("스캔 꺼짐 (제어 창에서 켜기)", _DIM, _FONT_SM))
        rs = st.get("rs")
        matches = st.get("matches") or []
        if rs:
            if matches:
                names = " / ".join(
                    f"{m['mineral']} ×{m['count']}" for m in matches[:3])
                mark = " (OCR 보정)" if any(m.get("corrected") for m in matches) else ""
                rows.append((f"RS {rs} · {names}{mark}", _FG, _FONT))
                rows.append((f"통상 난이도: {matches[0].get('difficulty_label', '')}",
                             _DIM, _FONT_SM))
            else:
                rows.append((f"RS {rs} · 알 수 없는 시그니처", _ORANGE, _FONT))
        comp = st.get("composition") or []
        if comp:
            for c in comp[:6]:
                rows.append((f"{c['name']}  {c['pct']:g}%", _FG, _FONT))
            if st.get("inert_pct") is not None:
                rows.append((f"불활성 {st['inert_pct']:g}%", _DIM, _FONT_SM))
            stats = []
            if st.get("resistance_pct") is not None:
                stats.append(f"저항 {st['resistance_pct']:g}%")
            if st.get("instability") is not None:
                stats.append(f"불안정 {st['instability']:g}")
            if st.get("mass") is not None:
                stats.append(f"질량 {st['mass']:g}")
            if stats:
                rows.append((" · ".join(stats), _DIM, _FONT_SM))
        verdict = st.get("rock_verdict")
        if verdict:
            label = verdict.get("label", "")
            if verdict.get("beta"):
                label += " [BETA]"
            rows.append((label,
                         _VERDICT_COLOR.get(verdict.get("level"), _FG), _FONT))
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


class Calibrator:
    """게임 화면 위에서 RS 영역/패널 영역을 두 번 드래그로 지정."""

    def __init__(self, root, cfg, on_done):
        import tkinter as tk
        self.cfg = cfg
        self.on_done = on_done
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
                             font=("Malgun Gothic", 16, "bold"))
        self.hint.place(relx=0.5, y=40, anchor="n")
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.win.bind("<Escape>", lambda e: self.cancel())
        self.rect = None
        self.regions = {}
        self._set_hint()
        self.win.focus_force()

    def _set_hint(self):
        if self.stage == 0:
            self.hint.configure(
                text="1/2: 스캔 시 RS 숫자가 뜨는 자리를 드래그 (ESC 취소)")
        else:
            self.hint.configure(
                text="2/2: 근접 스캔 광물 목록/수치 패널 자리를 드래그 (ESC 취소)")

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
        if self.stage == 0:
            self.stage = 1
            self._set_hint()
        else:
            self.finish()

    def finish(self):
        self.cfg["rs_region"] = self.regions.get("rs_region")
        self.cfg["panel_region"] = self.regions.get("panel_region")
        self.cfg["capture_enabled"] = True
        self.cfg["scan_enabled"] = True
        save_config(self.cfg)
        self.win.destroy()
        self.on_done(True)

    def cancel(self):
        self.win.destroy()
        self.on_done(False)


class ControlApp:
    """제어 창 + 오버레이 창 + 전역 단축키. tk mainloop을 메인 스레드에서 돌린다."""

    def __init__(self, cfg, state):
        import queue
        import tkinter as tk
        self.cfg = cfg
        self.state = state
        self.hotkey_q = queue.Queue()
        self.root = tk.Tk()
        self.root.title("SC 광물 오버레이")
        self.root.geometry("330x390+60+60")
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.overlay = OverlayWindow(self.root, cfg)
        self._build_controls()
        self._start_hotkeys()
        self.root.after(250, self._tick)
        self.root.after(150, self._drain_hotkeys)

    def _build_controls(self):
        import tkinter as tk
        f = self.root
        pad = {"padx": 10, "pady": 3, "fill": "x"}
        self.status = tk.Label(f, text="", anchor="w", justify="left",
                               font=_FONT_SM)
        self.status.pack(**pad)
        self.scan_btn = tk.Button(f, text="스캔 끄기", command=self.toggle_scan)
        self.scan_btn.pack(**pad)
        self.ov_btn = tk.Button(f, text="오버레이 숨기기 (F8)",
                                command=self.toggle_overlay)
        self.ov_btn.pack(**pad)
        tk.Button(f, text="화면 영역 지정 (F10)",
                  command=self.start_calibrate).pack(**pad)
        tk.Button(f, text="오버레이 위치 조정 (F9)",
                  command=self.toggle_adjust).pack(**pad)
        op_row = tk.Frame(f)
        op_row.pack(**pad)
        tk.Label(op_row, text="투명도", font=_FONT_SM).pack(side="left")
        self.opacity = tk.Scale(
            op_row, from_=30, to=100, orient="horizontal", showvalue=True,
            command=self._opacity_changed)
        self.opacity.set(int(float(self.cfg.get("overlay_opacity", 0.88)) * 100))
        self.opacity.pack(side="left", fill="x", expand=True)
        tk.Button(f, text="태블릿/웹 페이지 열기",
                  command=self.open_web).pack(**pad)
        tk.Label(f, text="F8 표시/숨기기 · F9 위치조정 · F10 영역지정",
                 font=_FONT_SM, fg="#666666").pack(**pad)
        tk.Button(f, text="종료", command=self.quit).pack(**pad)

    def _opacity_changed(self, v):
        self.cfg["overlay_opacity"] = int(v) / 100.0
        self.overlay.apply_opacity()
        save_config(self.cfg)

    def toggle_scan(self):
        self.cfg["scan_enabled"] = not self.cfg.get("scan_enabled", True)
        save_config(self.cfg)

    def toggle_overlay(self):
        self.overlay.toggle_visible()

    def toggle_adjust(self):
        self.overlay.set_adjust(not self.overlay.adjusting)

    def start_calibrate(self):
        if getattr(self, "_calib", None):
            return

        def done(ok):
            self._calib = None
            if ok:
                self.overlay._last_render = None

        self._calib = Calibrator(self.root, self.cfg, done)

    def open_web(self):
        import webbrowser
        webbrowser.open("http://localhost:{}".format(self.cfg.get("port", 8377)))

    def quit(self):
        save_config(self.cfg)
        self.root.destroy()
        os._exit(0)

    # -- 전역 단축키 (Windows RegisterHotKey, 관리자 권한 불필요) --
    def _start_hotkeys(self):
        if os.name != "nt":
            return

        def pump():
            import ctypes
            from ctypes import wintypes
            u32 = ctypes.windll.user32
            for hid, vk in ((1, 0x77), (2, 0x78), (3, 0x79)):  # F8/F9/F10
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
        except Exception:
            pass
        self.root.after(150, self._drain_hotkeys)

    def _tick(self):
        st = self.state.get()
        scan_on = self.cfg.get("scan_enabled", True)
        self.scan_btn.configure(
            text="스캔 끄기" if scan_on else "스캔 켜기 (현재 꺼짐)")
        self.ov_btn.configure(
            text="오버레이 숨기기 (F8)" if self.overlay.visible
            else "오버레이 보이기 (F8)")
        port = self.cfg.get("port", 8377)
        rs_txt = f"RS {st.get('rs')}" if st.get("rs") else "-"
        region_ok = "설정됨" if self.cfg.get("rs_region") else "미설정 (F10)"
        self.status.configure(
            text=f"서버: http://localhost:{port}\n"
                 f"캡처 영역: {region_ok}\n"
                 f"최근 스캔: {rs_txt} ({st.get('phase', '-')})")
        self.overlay.render(st, scan_on)
        self.root.after(250, self._tick)

    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------- 캡처 루프/메인

def capture_loop(cfg, state):
    """cfg를 매 주기 다시 읽어 캘리브레이션/스캔 토글이 재시작 없이 반영되게 한다."""
    engine = None
    cap = None
    known = list(load_data("minerals.json")["minerals"].keys())
    last_rs = None
    last_panel_sig = None
    while True:
        try:
            rs_region = cfg.get("rs_region")
            panel_region = cfg.get("panel_region")
            if not cfg.get("capture_enabled", True) \
                    or not cfg.get("scan_enabled", True) \
                    or not (rs_region or panel_region):
                time.sleep(0.4)
                continue
            if engine is None:
                engine = create_ocr_engine(cfg.get("ocr_engine", "auto"))
                print(f"[capture] OCR 엔진: {engine.name}")
            if cap is None or cap.monitor != cfg.get("monitor", 1):
                cap = Capturer(cfg.get("monitor", 1))
            interval = max(cfg.get("poll_interval_ms", 500), 200) / 1000.0
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


def main():
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
