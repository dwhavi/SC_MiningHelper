"""SC 패드 컨트롤 (SC_PadControl) - 단일 파일 버전.

Star Citizen 광물 스캔 결과를 화면 위에 표시하는 로컬 오버레이.
화면 캡처 + OCR만 사용. 게임 정책 및 안티치트 호환성은 보장하지 않습니다.

실행: python sc_pad_control.py  (또는 빌드된 SC_PadControl.exe)
"""
import asyncio
import difflib
import hmac
import json
import os
import re
import secrets
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
    "overlay_x": 1150,
    "overlay_y": 320,
    "overlay_width": 380,
    "overlay_opacity": 0.88,
    "verdict_thresholds": {
        "beta": True,
        "resist_ok_pct": 30.0,
        "resist_hard_pct": 55.0,
        "instability_hard": 800,
    },
    "overlay_anchor_v2": True,
    "pairing_code": "",           # 태블릿 덱 페어링 코드 (비어 있으면 첫 실행 시 생성)
    "only_when_game_focused": True,  # SC가 최전면 창일 때만 입력 전송
    "hold_timeout_ms": 5000,      # 홀드 무응답 자동 해제 (stuck 키 방지)
    "tap_ms": 60,                 # 탭 1회의 키 눌림 길이
    "actionmaps_path": "",        # 사용자가 고른 SC 프로필/내보내기 XML (빈 값=키 지정 필요)
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

    def clear(self):
        with self._lock:
            self._state.update({
                "phase": "idle", "rs": None, "matches": [], "composition": [],
                "inert_pct": None, "resistance_pct": None, "instability": None,
                "mass": None, "rock_verdict": None,
                "updated_at": time.strftime("%H:%M:%S")})



# 가상키 코드 (ctypes SendInput용) — 허용 키만 허용
# SC 기본 키 표기(tablet-deck 방식): lalt/ralt/rctrl/lctrl/lshift/n, np_, minus, backspace 등.
# _KEY_ALIASES가 SC 표기를 윈도우 가상키 상수로 변환하고, _KEY_CODES가 이를 화이트리스트로 쓴다.
_KEY_ALIASES = {
    # 수식자 (방위를 구분)
    "LALT": 0xA4, "RALT": 0xA5, "LSHIFT": 0xA0, "RSHIFT": 0xA1,
    "LCTRL": 0xA2, "RCTRL": 0xA3, "LCONTROL": 0xA2, "RCONTROL": 0xA3,
    "CONTROL": 0x11, "SHIFT": 0x10, "MENU": 0x12,
    "LMETA": 0x5B, "RMETA": 0x5C,
    # 특수
    "SPACE": 0x20, "ENTER": 0x0D, "ESC": 0x1B, "TAB": 0x09, "BS": 0x08,
    "BACKSPACE": 0x08, "BACKSLASH": 0xDC, "DELETE": 0x2E, "DEL": 0x2E,
    "INSERT": 0x2D, "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21,
    "PGUP": 0x21, "PAGEDOWN": 0x22, "PGDOWN": 0x22,
    "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
    "MINUS": 0xBD, "EQUAL": 0xBB, "LBRACKET": 0xDB, "RBRACKET": 0xDD,
    "SEMICOLON": 0xBA, "APOSTROPHE": 0xDE, "COMMA": 0xBC, "PERIOD": 0xBE,
    "SLASH": 0xBF, "BACKQUOTE": 0xC0,
    # 숫자패드 — SendInput 가상키 (Num Lock ON 전제)
    "NP_0": 0x60, "NP_1": 0x61, "NP_2": 0x62, "NP_3": 0x63,
    "NP_4": 0x64, "NP_5": 0x65, "NP_6": 0x66, "NP_7": 0x67,
    "NP_8": 0x68, "NP_9": 0x69, "NP_PERIOD": 0x6E, "NP_DIVIDE": 0x6F,
    "NP_MULTIPLY": 0x6A, "NP_MINUS": 0x6D, "NP_SUBTRACT": 0x6D,
    "NP_ADD": 0x6B, "NP_ENTER": 0x0D,
    # F키
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74,
    "F6": 0x75, "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79,
    "F11": 0x7A, "F12": 0x7B,
    "MOUSE1": None, "MOUSE2": None, "MOUSE3": None,  # 추후 마우스 확장용 예약
}
# 영문 단일키는 ASCII 그대로
for _ch in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    _KEY_ALIASES.setdefault(_ch, ord(_ch))
# SC 기본 바인딩에 등장하는 모든 문자 키를 화이트리스트에 명시
for _name, _code in [
    ("N", 0x4E), ("B", 0x42), ("V", 0x56), ("M", 0x4D), ("C", 0x43),
    ("TAB", 0x09), ("Q", 0x51), ("E", 0x45), ("F", 0x46), ("G", 0x47),
    ("H", 0x48), ("J", 0x4A), ("K", 0x4B), ("L", 0x4C), ("T", 0x54),
    ("U", 0x55), ("Y", 0x59), ("I", 0x49), ("O", 0x4F), ("P", 0x50),
    ("Z", 0x5A), ("X", 0x58), ("R", 0x52), ("D", 0x44), ("W", 0x57),
    ("S", 0x53), ("A", 0x41),
]:
    _KEY_ALIASES.setdefault(_name, _code)

# 마우스 버튼/휠 의사 키 — SendInput 마우스 이벤트로 전송 (가상키가 아닌 sentinel)
_KEY_ALIASES.update({
    "MOUSE1": 0x1001, "MOUSE2": 0x1002, "MOUSE3": 0x1003,
    "MWHEEL_UP": 0x1004, "MWHEEL_DOWN": 0x1005,
})

# 화이트리스트 (조합 허용 포함): 단일키 + 수식자만. 조합은 _send_input에서 파싱.
_KEY_CODES = dict(_KEY_ALIASES)
_MODIFIERS = {k for k in ("LALT", "RALT", "LSHIFT", "RSHIFT", "LCTRL", "RCTRL",
                          "LCONTROL", "RCONTROL", "CONTROL", "SHIFT", "MENU", "LMETA", "RMETA")}

# 홀드 상태 레지스트리 — down/up 홀드의 자동 해제용 (연결 끊김/무응답 시
# 눌린 키가 남지 않도록 타임아웃 해제. tablet-deck의 stuck-key 방지와 같은 방식)
_HOLD_LOCK = threading.Lock()
_ACTIVE_HOLDS = {}  # SC 키 문자열(대문자) -> 마지막 갱신 monotonic 시각

# SC 키 문자열 파싱: "lalt+n" / "ralt+np_8" / "f5" / "TAB" 등
_COMBO_SPLIT_RE = None  # 컴파일 지연 (모듈 로드 무거운 작업 방지)

def _parse_combo(name: str):
    """반환: (수식자 가상키 리스트, 본키 가상키). 실패 시 ValueError."""
    if not name or not name.strip():
        raise ValueError("키가 비어 있음")
    parts = [p.strip() for p in name.strip().upper().split("+") if p.strip()]
    mods, main = [], None
    for p in parts:
        if p in _MODIFIERS:
            mods.append(_KEY_CODES[p])
        elif main is None:
            vk = _KEY_CODES.get(p)
            if vk is None:
                raise ValueError(f"허용되지 않는 키: {name}")
            main = vk
        else:
            raise ValueError(f"키 조합이 너무 깁니다: {name}")
    if main is None and mods:
        # 수식자만 (예: 단독 LCONTROL) — 본키가 없으면 마지막을 본키로 취급
        main = mods.pop()
    if main is None:
        raise ValueError(f"허용되지 않는 키: {name}")
    return mods, main


# Physical Set-1 modifier positions, independent of the sender's IME/layout.
_PHYSICAL_MODIFIERS = {0xA0: (0x2A, False), 0xA1: (0x36, False),
                       0xA2: (0x1D, False), 0xA3: (0x1D, True),
                       0xA4: (0x38, False), 0xA5: (0x38, True)}
_INPUT_TRACE_LOCK = threading.Lock()


def _keyboard_scan(vk, flags, mapper):
    if vk in _PHYSICAL_MODIFIERS:
        scan, extended = _PHYSICAL_MODIFIERS[vk]
    else:
        mapped = mapper(vk, 4)
        scan, extended = mapped & 0xFF, (mapped >> 8) == 0xE0
        if not scan or mapped >> 8 not in (0, 0xE0):
            raise ValueError(f"키 스캔코드 변환 실패: VK={vk:02X}")
    return scan, 0x0008 | flags | (0x0001 if extended else 0)


def _trace_input(**record):
    """Only app-generated input, never physical keyboard capture. Bounded local log."""
    try:
        with _INPUT_TRACE_LOCK:
            path = os.path.join(EXE_DIR, "input_diagnostics.jsonl")
            if os.path.exists(path) and os.path.getsize(path) > 256 * 1024:
                os.replace(path, path + ".1")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(dict(time=time.strftime("%Y-%m-%dT%H:%M:%S"), **record), ensure_ascii=False) + "\n")
    except OSError:
        pass  # Diagnostics must not prevent a release event.


def _emit_vk(vk, flags=0):
    """Windows SendInput: scan-code keys and single mouse button/wheel events."""
    import ctypes
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]
    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]
    class InputUnion(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]
    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", InputUnion)]
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    u32.SendInput.restype = wintypes.UINT
    inp = INPUT()
    if vk >= 0x1000:
        inp.type = 0
        if vk in (0x1004, 0x1005):
            if flags:
                return
            inp.u.mi = MOUSEINPUT(0, 0, 120 if vk == 0x1004 else 0xFFFFFF88, 0x0800, 0, 0)
        else:
            down, up = {0x1001:(2,4), 0x1002:(8,16), 0x1003:(32,64)}[vk]
            inp.u.mi = MOUSEINPUT(0, 0, 0, up if flags else down, 0, 0)
    else:
        u32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        u32.MapVirtualKeyW.restype = wintypes.UINT
        try:
            scan, event_flags = _keyboard_scan(vk, flags, u32.MapVirtualKeyW)
        except ValueError as e:
            _trace_input(vk=vk, error=str(e), result=0)
            raise
        inp.type = 1
        inp.u.ki = KEYBDINPUT(0, scan, event_flags, 0, 0)
    ctypes.set_last_error(0)
    sent = u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    error = ctypes.get_last_error() if sent != 1 else 0
    _trace_input(vk=vk, scan=inp.u.ki.wScan if inp.type == 1 else None,
                 flags=inp.u.ki.dwFlags if inp.type == 1 else flags,
                 result=sent, win_error=error)
    if sent != 1:
        raise OSError(f"Windows가 입력 전송을 거부했습니다 (오류 {error}). 게임과 실행 권한을 확인하세요.")


# 입력 타이밍 — 사람의 눌림에 가깝게. SC는 입력 상태를 프레임 단위로 읽고
# 오른쪽 Alt 조합을 특히 자주 놓친다. 수식자 직후 바로 본키를 보내고 60ms 만에
# 모두 놓는 짧은 합성 탭은 무시되기 쉬우므로, 수식자를 잠깐 안정시키고 본키를
# 충분히 유지한다. 그래도 탭 1회 = 눌림 1회다 (반복·매크로 없음).
_KEY_EVENT_GAP = 0.02   # 연속 키 이벤트 사이 최소 간격
_TAP_SETTLE = 0.03      # 수식자를 누른 뒤 본키까지 안정화 대기
_TAP_HOLD = 0.12        # 본키 유지 시간 (SC 프레임 2~3개를 확실히 건너도록)


def _combo_down(key_name):
    mods, main = _parse_combo(key_name)
    pressed = []
    try:
        for vk in mods:
            _emit_vk(vk, 0)
            pressed.append(vk)
            time.sleep(_KEY_EVENT_GAP)
        if mods:
            time.sleep(_TAP_SETTLE)
        _emit_vk(main, 0)
        pressed.append(main)
    except Exception:
        for vk in reversed(pressed):
            try:
                _emit_vk(vk, 2)
            except Exception:
                pass
        raise


def _combo_up(key_name):
    mods, main = _parse_combo(key_name)
    first_error = None
    for vk in [main] + list(reversed(mods)):
        try:
            _emit_vk(vk, 2)
        except Exception as exc:
            first_error = first_error or exc
        time.sleep(_KEY_EVENT_GAP)
    if first_error is not None:
        raise first_error


def _combo_press(key_name):
    _combo_down(key_name)
    try:
        time.sleep(_TAP_HOLD)
    finally:
        _combo_up(key_name)


def _game_focused():
    """Star Citizen이 최전면 창인지 확인 (Windows 전용, 그 외 OS는 False).

    확인에 실패하면 입력을 막는다.
    """
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes
    try:
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                  wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        u32.GetForegroundWindow.restype = wintypes.HWND
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u32.GetWindowThreadProcessId.restype = wintypes.DWORD
        hwnd = u32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wintypes.DWORD(0)
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return False
        h = k32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return False
            return os.path.basename(buf.value or "").lower() == "starcitizen.exe"
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


# --- actionmaps.xml 연동: 인게임 리바인드를 덱 키에 반영 -------------------
# 게임 프로필 파일을 읽기만 한다 (쓰기 없음). 파일이나 바인딩이 없으면 비활성화.
_AM_CACHE = {"path": None, "mtime": 0.0, "map": {}, "seen": set(), "owners": {}}


def _resolve_actionmaps_path(cfg):
    manual = str(cfg.get("actionmaps_path") or "").strip()
    return manual if manual and os.path.isfile(manual) else None


def _am_convert(inp):
    """rebind 입력을 덱 키 표기로 변환. 변환 불가(조이스틱/축 등)는 None."""
    if not inp or "_" not in inp:
        return None
    dev, rest = inp.split("_", 1)
    rest = rest.strip().lower()
    if not rest:
        return None
    if dev == "kb1":
        return rest
    if dev == "mouse1":
        m = re.match(r"button([123])$", rest)
        if m:
            return "mouse" + m.group(1)
        if rest in ("mwheel_up", "mwheel_down"):
            return rest
        return None  # maxis_z 등 축은 방향을 알 수 없음
    return None  # 조이스틱/패드는 키보드 전송 불가


def _load_action_bindings(cfg):
    """actionmaps.xml의 액션 → 키 맵 (mtime 캐시).

    반환: (bindings, seen, path). seen은 파일에 등장한 액션 전체 —
    키보드/마우스 해제가 명시된 경우에만 빈 바인딩으로 반환한다.
    """
    path = _resolve_actionmaps_path(cfg)
    if not path:
        _AM_CACHE.update({"path": None, "mtime": 0.0, "map": {}, "seen": set(), "owners": {}})
        return {}, set(), None
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {}, set(), None
    if _AM_CACHE["path"] == path and _AM_CACHE["mtime"] == mt:
        return _AM_CACHE["map"], _AM_CACHE["seen"], path
    bindings, seen, owners = {}, set(), {}
    try:
        import xml.etree.ElementTree as ET
        if os.path.getsize(path) > 2 * 1024 * 1024:
            raise ValueError("XML 파일은 2MB 이하여야 합니다")
        root = ET.parse(path).getroot()
        for am in root.iter("actionmap"):
            amn = am.get("name") or ""
            for act in am.iter("action"):
                an = act.get("name") or ""
                if not an:
                    continue
                seen.add(amn + "." + an)
                for rb in act.iter("rebind"):
                    raw = rb.get("input") or ""
                    if raw.startswith(("kb1_", "mouse1_")):
                        if not raw.split("_", 1)[1].strip():
                            bindings.setdefault(amn + "." + an, "")
                            continue
                        conv = _am_convert(raw)
                        if conv:
                            try:
                                _parse_combo(conv)
                            except ValueError:
                                continue
                            act_id = amn + "." + an
                            owners.setdefault(_combo_id(conv), set()).add(act_id)
                            if not bindings.get(act_id):
                                bindings[act_id] = conv
    except Exception as e:
        print(f"[deck] actionmaps 읽기 실패: {e}")
        return {}, set(), None
    _AM_CACHE.update({"path": path, "mtime": mt, "map": bindings, "seen": seen, "owners": owners})
    return bindings, seen, path


def _combo_id(key):
    mods, main = _parse_combo(key)
    return tuple(sorted(set(mods))), main


def _deck_buttons(layout):
    yield from layout.get("power_buttons", [])
    yield from layout.get("common_buttons", [])
    for page in layout.get("pages", []):
        yield from page.get("buttons", [])


_KEY_SETTINGS_LOCK = threading.RLock()


def _override_path():
    return os.path.join(EXE_DIR, "deck_key_overrides.json")


def _read_key_overrides():
    try:
        if os.path.getsize(_override_path()) > 128 * 1024:
            return {}
        with open(_override_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_key_overrides(overrides):
    """사용자 지정 키/시퀀스 간격을 exe 옆 JSON에 원자적으로 저장."""
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=".deck-keys-", suffix=".json", dir=EXE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(overrides, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _override_path())
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _validate_override_key(key, mode="tap", allow_space=False):
    if len(key) > 64 or any(c in key for c in (";", ",", "\n", "\r")):
        raise ValueError("단일 키 또는 조합키만 지원합니다")
    parts = key.strip().lower().split("+")
    if any(not part or part != part.strip() for part in parts):
        raise ValueError("예: f5 또는 lalt+f5")
    mods, main = _parse_combo(key)
    if len(mods) != len(set(mods)) or parts[-1].upper() in _MODIFIERS:
        raise ValueError("수식자만 지정할 수 없습니다")
    if any(v in (0x5B, 0x5C) for v in mods) or main in (0x5B, 0x5C):
        raise ValueError("Windows 키는 지원하지 않습니다")
    if main in {ord(c) for c in "WASDQE"} | {0x20, 0x25, 0x26, 0x27, 0x28} and not (allow_space and main == 0x20 and not mods):
        raise ValueError("이동 키는 지정할 수 없습니다")
    alt = any(v in (0x12, 0xA4, 0xA5) for v in mods)
    ctrl = any(v in (0x11, 0xA2, 0xA3) for v in mods)
    if ((alt and main in (0x09, 0x1B, 0x73)) or (ctrl and main == 0x1B)
            or (ctrl and alt and main in (0x2E, 0x77, 0x78, 0x79, 0x7A))):
        raise ValueError("시스템/프로그램 단축키와 겹치는 조합입니다")
    if mode == "hold" and main in (0x1004, 0x1005):
        raise ValueError("휠은 누르는 동안 유지할 수 없습니다")
    return key.strip().lower()


# 시간텀 시퀀스 — 단계별 지연/홀드 상한. 1회 실행만, 반복 없음
_SEQ_MIN_DELAY_MS = 200
_SEQ_MAX_DELAY_MS = 10000
_SEQ_MAX_STEPS = 8
_SEQ_MAX_HOLD_MS = 5000


def _resolve_sequence(btn, bindings, owners, overrides):
    """시간텀 시퀀스 버튼: 단계 키는 XML에서 확인된 것만. 하나라도 없으면 잠금."""
    approved = {
        "launch-seq": ["spaceship_movement.v_atc_request", "spaceship_movement.v_strafe_up",
                       "spaceship_movement.v_toggle_landing_system"],
        "landing-seq": ["spaceship_docking.v_toggle_docking_request",
                        "spaceship_movement.v_toggle_landing_system"],
    }
    raw = btn.get("sequence") or []
    if (not isinstance(raw, list) or any(not isinstance(st, dict) for st in raw)
            or [st.get("action") for st in raw] != approved.get(btn.get("id"))):
        btn["disabled_reason"] = "승인된 시퀀스 정의가 아닙니다"
        return
    steps = []
    custom = overrides.get(btn.get("id"), {})
    saved = (custom.get("delays") if isinstance(custom, dict)
             and custom.get("action") == "sequence" else None)
    step_keys = custom.get("step_keys", {}) if isinstance(custom, dict) and custom.get("action") == "sequence" else {}
    if not isinstance(step_keys, dict):
        step_keys = {}
    btn["custom_step_keys"] = bool(step_keys)
    btn["custom_delays"] = bool(saved)
    for i, st in enumerate(btn.get("sequence") or []):
        if not isinstance(st, dict):
            continue
        try:
            delay = int(st.get("delay_ms", 1000))
            hold = int(st.get("hold_ms", 0))
        except (TypeError, ValueError):
            continue
        if (isinstance(saved, list) and len(saved) == len(raw)
                and all(type(d) is int and _SEQ_MIN_DELAY_MS <= d <= _SEQ_MAX_DELAY_MS for d in saved)):
            delay = saved[i]
        else:
            saved = None
        if st.get("action") != "spaceship_movement.v_strafe_up":
            hold = 0
        act = str(st.get("action") or "")
        xml_key = bindings.get(act, "")
        override_key = step_keys.get(act, "")
        key = override_key or xml_key
        if not isinstance(key, str):
            key = ""
        steps.append({"label": str(st.get("label") or f"{i + 1}단계"),
                      "action": act, "key": key, "xml_key": xml_key,
                      "override_key": override_key, "keys_source": "override" if override_key else "actionmaps",
                      "delay_ms": min(max(delay, _SEQ_MIN_DELAY_MS), _SEQ_MAX_DELAY_MS),
                      "hold_ms": min(max(hold, 0), _SEQ_MAX_HOLD_MS)})
    btn["steps"] = steps
    if not steps or len(steps) > _SEQ_MAX_STEPS:
        btn["disabled_reason"] = "시퀀스 정의 확인 필요"
        return
    missing = [s["label"] for s in steps if not s["key"]]
    try:
        for st in steps:
            if not st["key"]:
                continue
            st["key"] = _validate_override_key(st["key"], "hold" if st["hold_ms"] else "tap",
                allow_space=btn.get("id") == "launch-seq" and st["action"] == "spaceship_movement.v_strafe_up")
            combo = _combo_id(st["key"])
            if owners.get(combo, set()) - {st["action"]}:
                btn["disabled_reason"] = "XML 키 중복 · 확인 필요"
                return
            if any(other.get("key") and other["action"] != st["action"] and _combo_id(other["key"]) == combo for other in steps):
                btn["disabled_reason"] = "시퀀스 단계 키 중복 · 확인 필요"
                return
    except (ValueError, TypeError) as e:
        btn["disabled_reason"] = "시퀀스 키 확인 필요: " + str(e)
        return
    if missing:
        btn["disabled_reason"] = "시퀀스 키 지정 필요: " + ", ".join(missing)
        return
    btn.update(keys="SEQ", unbound=False, keys_source="sequence",
               disabled_reason="", custom_delays=bool(saved))


# 시간텀 시퀀스 실행 상태 — 단일 실행, 취소/포커스 확인 내장
_DECK_INPUT_LOCK = threading.RLock()
_SEQ_LOCK = threading.Lock()
_SEQ_ABORT = threading.Event()
_SEQ_STATE = {"running": False, "button_id": "", "step": 0, "total": 0,
              "label": "", "msg": ""}


def _seq_set(**kw):
    with _SEQ_LOCK:
        _SEQ_STATE.update(kw)


def _seq_running():
    with _SEQ_LOCK:
        return _SEQ_STATE["running"]


def _run_sequence(cfg, btn):
    """단계별 탭/홀드 전송. 매 단계 전에 취소 여부와 게임 포커스를 다시 확인한다."""
    steps = list(btn.get("steps") or [])
    _seq_set(running=True, button_id=btn.get("id", ""), step=0,
             total=len(steps), label=btn.get("label", ""), msg="")
    try:
        for i, st in enumerate(steps):
            if _SEQ_ABORT.is_set():
                _seq_set(msg="취소했습니다")
                return
            if not _game_focused():
                _seq_set(msg="게임 창이 앞에 없어 중단했습니다")
                return
            _seq_set(step=i + 1)
            try:
                if st.get("hold_ms"):
                    _combo_down(st["key"])
                    try:
                        remaining = st["hold_ms"] / 1000.0
                        while remaining > 0:
                            dt = min(0.05, remaining)
                            if _SEQ_ABORT.wait(dt):
                                _seq_set(msg="취소했습니다")
                                return
                            if not _game_focused():
                                _seq_set(msg="게임 창이 앞에 없어 중단했습니다")
                                return
                            remaining -= dt
                    finally:
                        _combo_up(st["key"])
                else:
                    _combo_press(st["key"])
            except OSError as e:
                _seq_set(msg=str(e))
                return
            wait = st.get("delay_ms", 0) / 1000.0
            while wait > 0 and not _SEQ_ABORT.is_set():
                dt = min(0.05, wait)
                if _SEQ_ABORT.wait(dt):
                    break
                if not _game_focused():
                    _seq_set(msg="게임 창이 앞에 없어 중단했습니다")
                    return
                wait -= dt
        _seq_set(msg="취소했습니다" if _SEQ_ABORT.is_set()
                 else f"{btn.get('label', '시퀀스')} 입력 전송 완료 (게임 완료 확인 아님)")
    finally:
        _SEQ_ABORT.clear()
        _seq_set(running=False, step=0)


def _resolved_deck_layout(cfg, overrides=None):
    """Fail closed: no guessed defaults or stale pre-v4 user layouts."""
    user_path = os.path.join(EXE_DIR, "deck_layout.json")
    bundled_path = _data_path("deck_layout.json")
    p = user_path if os.path.exists(user_path) else bundled_path
    try:
        with open(p, encoding="utf-8") as f:
            layout = json.load(f)
        if not isinstance(layout, dict):
            raise ValueError("레이아웃 형식 오류")
        if layout.get("version", 0) < 4:
            with open(bundled_path, encoding="utf-8") as f:
                layout = json.load(f)
            layout["migration_notice"] = "이전 덱 파일은 보존하고 새 안전 레이아웃을 사용합니다"
            if layout.get("version", 0) < 4:
                raise ValueError("v4 이상 레이아웃 필요")
    except Exception:
        return {"pages": [], "common_buttons": [], "error": "덱 파일을 확인하세요"}
    # Lift distribution controls even in existing customized v4 layouts.
    if "power_buttons" not in layout:
        layout["power_buttons"] = []
        for page in layout.get("pages", []):
            if page.get("id") == "power":
                lift = [b for b in page.get("buttons", [])
                        if b.get("action", "").startswith("spaceship_power.v_engineering_assignment_")]
                layout["power_buttons"].extend(lift)
                page["buttons"] = [b for b in page.get("buttons", []) if b not in lift]
    overrides = _read_key_overrides() if overrides is None else overrides
    bindings, seen, path = _load_action_bindings(cfg)
    layout["actionmaps_active"] = bool(path)
    # Duplicate XML keys can trigger several actions in the same game context.
    # Do not infer contexts or treat a new key as safe just because it parses.
    owners = _AM_CACHE.get("owners", {}) if path else {}
    for btn in _deck_buttons(layout):
        btn["keys"] = ""
        btn["unbound"] = True
        btn["keys_source"] = "unverified"
        btn["disabled_reason"] = btn.get("blocked_reason") or "키 지정 필요"
        if btn.get("blocked_reason"):
            continue
        # ESC is an explicitly labeled literal system key, not a game bind.
        if btn.get("id") == "escape" and btn.get("fixed_key") == "esc":
            btn.update(keys="esc", unbound=False, keys_source="fixed")
            continue
        if btn.get("sequence"):
            _resolve_sequence(btn, bindings, owners, overrides)
            continue
        act = btn.get("action") or ""
        custom = overrides.get(btn.get("id"), {})
        use_override = not btn.get("xml_only") and isinstance(custom, dict) and custom.get("action") == act and bool(custom.get("keys"))
        key = custom.get("keys", "") if use_override else bindings.get(act, "")
        btn["override_keys"] = key if use_override else ""
        if use_override:
            try:
                key = _validate_override_key(key, btn.get("mode", "tap"))
            except (ValueError, TypeError):
                btn["disabled_reason"] = "저장된 키 확인 필요"
                continue
        if not key:
            continue
        combo = _combo_id(key)
        if key.lower() in {"w", "a", "s", "d", "q", "e", "space", "up", "down", "left", "right"}:
            btn["disabled_reason"] = "이동 키 제외"
            continue
        xml_conflict = bool(owners.get(combo, set()) - {act}) if use_override else len(owners.get(combo, set())) > 1
        if xml_conflict:
            btn["disabled_reason"] = "XML 키 중복 · 확인 필요"
            continue
        if btn.get("mode") == "hold" and combo[1] in (0x1004, 0x1005):
            btn["disabled_reason"] = "휠 홀드 불가"
            continue
        if btn.get("mode", "tap") not in ("tap", "hold"):
            btn["disabled_reason"] = "지원하지 않는 동작"
            continue
        btn.update(keys=key, unbound=False, keys_source="override" if use_override else "actionmaps")
    effective = {}
    for btn in _deck_buttons(layout):
        if btn.get("sequence"):
            continue
        if not btn.get("unbound") and btn.get("keys"):
            effective.setdefault(_combo_id(btn["keys"]), []).append(btn)
    for buttons in effective.values():
        if len({b.get("action") or b.get("id") for b in buttons}) > 1:
            for btn in buttons:
                btn.update(keys="", unbound=True, disabled_reason="덱 키 중복 · 확인 필요")
    assigned = [(b.get("action"), b["keys"]) for b in _deck_buttons(layout)
                if not b.get("sequence") and not b.get("unbound") and b.get("keys")]
    seq_buttons = [b for b in _deck_buttons(layout) if b.get("sequence")]
    for b in seq_buttons:
        for st in b.get("steps", []):
            try:
                if st.get("key"):
                    _combo_id(st["key"])
                    assigned.append((st["action"], st["key"]))
            except (ValueError, TypeError):
                pass
    for b in seq_buttons:
        if b.get("unbound") and not b.get("disabled_reason", "").startswith("시퀀스 키 지정 필요"):
            continue
        for st in b.get("steps", []):
            if not st.get("key"):
                continue
            if any(act != st["action"] and _combo_id(key) == _combo_id(st["key"]) for act, key in assigned):
                b.update(keys="", unbound=True, disabled_reason="덱/시퀀스 키 중복 · 확인 필요")
                break
    return layout


def _send_input(action: str, key: str, ms: int = 0):
    """태블릿 -> PC 입력 전송. 반환: (ok, msg).

    type: press/tap(단일 눌림) | timed/hold(ms만큼 유지) | down/up(누르는 동안)
    | refresh(down 홀드 갱신). 1버튼=1키만 지원 (매크로 없음).
    tablet-deck 표기(lalt+n, ralt+np_5 등 포함) 지원.
    """
    if action == "tap":
        action = "press"
    if action == "hold":
        action = "timed"
    if action == "release":
        action = "up"
    norm = (key or "").strip().upper()
    if sys.platform != "win32":
        return (False, "Windows에서만 입력 전송 가능")
    try:
        if action == "press":
            if not norm:
                return (False, "키가 비어 있음")
            _combo_press(norm)
            return (True, f"{norm} 눌림")
        elif action == "timed":
            if not norm:
                return (False, "키가 비어 있음")
            ms = max(50, min(ms, 10000))
            with _HOLD_LOCK:
                _combo_down(norm)
                _ACTIVE_HOLDS[norm] = time.monotonic()
            try:
                time.sleep(ms / 1000.0)
            finally:
                with _HOLD_LOCK:
                    _ACTIVE_HOLDS.pop(norm, None)
                    _combo_up(norm)
            return (True, f"{norm} {ms}ms 유지")
        elif action == "down":
            if not norm:
                return (False, "키가 비어 있음")
            if _parse_combo(norm)[1] in (0x1004, 0x1005):
                return (False, "휠은 탭만 지원합니다")
            with _HOLD_LOCK:
                if norm not in _ACTIVE_HOLDS:
                    _combo_down(norm)
                _ACTIVE_HOLDS[norm] = time.monotonic()
            return (True, f"{norm} 누르는 중")
        elif action == "refresh":
            with _HOLD_LOCK:
                if norm in _ACTIVE_HOLDS:
                    _ACTIVE_HOLDS[norm] = time.monotonic()
            return (True, "홀드 갱신")
        elif action == "up":
            if not norm:
                return (False, "키가 비어 있음")
            with _HOLD_LOCK:
                _ACTIVE_HOLDS.pop(norm, None)
                _combo_up(norm)
            return (True, f"{norm} 뗌")
        else:
            return (False, f"알 수 없는 액션: {action}")
    except ValueError as e:
        return (False, str(e))
    except Exception as e:
        return (False, f"입력 전송 실패: {e}")


def hold_reaper(cfg):
    """무응답 홀드 자동 해제 스레드 (stuck 키 방지)."""
    timeout = max(1.0, float(cfg.get("hold_timeout_ms", 5000)) / 1000.0)
    while True:
        time.sleep(0.5)
        with _HOLD_LOCK:
            if not _ACTIVE_HOLDS:
                continue
        now = time.monotonic()
        lost_focus = cfg.get("only_when_game_focused", True) and not _game_focused()
        with _HOLD_LOCK:
            expired = [k for k, t in _ACTIVE_HOLDS.items() if now - t > timeout or lost_focus]
        for k in expired:
            with _HOLD_LOCK:
                if k not in _ACTIVE_HOLDS:
                    continue
                # Recheck refreshed timestamps under the lock before releasing.
                if not lost_focus and time.monotonic() - _ACTIVE_HOLDS[k] <= timeout:
                    continue
                _ACTIVE_HOLDS.pop(k, None)
                if sys.platform == "win32":
                    try:
                        _combo_up(k)
                        print(f"[deck] 안전 해제: {k}")
                    except Exception:
                        pass


def create_app(state, cfg):
    """FastAPI 앱 — 태블릿 컨트롤 덱 전용.

    태블릿은 입력만 전송하고, 채광 결과는 PC 오버레이에만 표시한다.
    보안 (tablet-deck 참고): 페어링 코드 + 5회 실패 1분 차단, 게임 포커스 확인,
    홀드 자동 해제(hold_reaper), release는 항상 통과.
    """
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse
    from pydantic import BaseModel, StrictInt

    class InputRequest(BaseModel):
        type: str                      # press/tap | timed/hold | down/up/refresh
        key: str = ""                  # SC 키 표기 (예: "b", "lalt+n", "np_5")
        ms: int = 0                    # timed용 유지 시간 (ms)

    class KeySetting(BaseModel):
        button_id: str
        keys: str = ""
        step_keys: list[str] | None = None  # empty item restores that step to XML
        delays: list[StrictInt] | None = None   # 시퀀스 버튼의 단계별 간격(ms)

    class SeqRequest(BaseModel):
        button_id: str

    class TestInput(BaseModel):
        rs: int | None = None
        minerals: list | None = None
        resistance_pct: float | None = None
        instability: float | None = None
        mass: float | None = None

    app = FastAPI(title="SC_PadControl — Tablet Control Deck", docs_url=None, redoc_url=None)
    pair_fails = {}
    pair_block = {}

    def read_html(name):
        with open(os.path.join(BUNDLE_DIR, name), encoding="utf-8") as f:
            return f.read()

    def _check_pair(request: Request):
        """페어링 코드 확인. 통과 시 None, 실패 시 JSONResponse."""
        ip = request.client.host if request.client else "?"
        now = time.monotonic()
        if pair_block.get(ip, 0) > now:
            return JSONResponse({"ok": False, "msg": "잠시 차단됨 (1분 후 재시도)"},
                                status_code=429)
        want = str(cfg.get("pairing_code") or "")
        got = request.headers.get("x-pairing-code", "")
        if want and hmac.compare_digest(got, want):
            pair_fails.pop(ip, None)
            return None
        rec = [t for t in pair_fails.get(ip, []) if now - t < 60]
        rec.append(now)
        pair_fails[ip] = rec
        if len(rec) >= 5:
            pair_block[ip] = now + 60
            pair_fails[ip] = []
            return JSONResponse({"ok": False, "msg": "코드 5회 오류 - 1분 차단"},
                                status_code=429)
        return JSONResponse({"ok": False, "msg": "페어링 코드가 맞지 않음"},
                            status_code=401)

    @app.get("/api/auth")
    def auth_status(request: Request):
        deny = _check_pair(request)
        return deny if deny is not None else {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def index():
        # 태블릿 컨트롤 덱 메인 페이지
        return read_html("control_deck.html")

    @app.get("/api/layout")
    def deck_layout():
        return _resolved_deck_layout(cfg)

    @app.get("/api/key-settings")
    def key_settings(request: Request):
        deny = _check_pair(request)
        if deny is not None:
            return deny
        return _resolved_deck_layout(cfg)

    @app.post("/api/key-settings")
    def save_key_setting(body: KeySetting, request: Request):
        deny = _check_pair(request)
        if deny is not None:
            return deny
        with _KEY_SETTINGS_LOCK:
            layout = _resolved_deck_layout(cfg)
            matches = [b for b in _deck_buttons(layout) if b.get("id") == body.button_id]
            if len(matches) != 1 or matches[0].get("blocked_reason") or matches[0].get("xml_only"):
                return JSONResponse({"msg": "이 버튼은 변경할 수 없습니다"}, status_code=400)
            btn = matches[0]
            overrides = _read_key_overrides()
            if btn.get("sequence"):
                steps = btn.get("steps") or []
                if not steps:
                    return JSONResponse({"msg": "시퀀스 정의 확인 필요"}, status_code=400)
                if body.delays is None and body.step_keys is None:
                    overrides.pop(body.button_id, None)
                else:
                    entry = dict(overrides.get(body.button_id, {}))
                    entry["action"] = "sequence"
                    if body.delays is not None:
                        if len(body.delays) != len(steps) or any(d < _SEQ_MIN_DELAY_MS or d > _SEQ_MAX_DELAY_MS for d in body.delays):
                            return JSONResponse({"msg": "단계 간격은 200~10000ms, 단계 수와 같아야 합니다"}, status_code=400)
                        entry["delays"] = list(body.delays)
                    if body.step_keys is not None:
                        if len(body.step_keys) != len(steps):
                            return JSONResponse({"msg": "단계 키 개수가 맞지 않습니다"}, status_code=400)
                        keys = {}
                        try:
                            for st, key in zip(steps, body.step_keys):
                                if key.strip():
                                    keys[st["action"]] = _validate_override_key(key, "hold" if st["hold_ms"] else "tap",
                                        allow_space=body.button_id == "launch-seq" and st["action"] == "spaceship_movement.v_strafe_up")
                        except ValueError as e:
                            return JSONResponse({"msg": str(e)}, status_code=400)
                        entry["step_keys"] = keys
                    overrides[body.button_id] = entry
                    proposed = _resolved_deck_layout(cfg, overrides)
                    candidate = next(b for b in _deck_buttons(proposed) if b.get("id") == body.button_id)
                    # Missing XML for a different, not-yet-edited step is allowed; conflicts are not.
                    if candidate.get("unbound") and not candidate.get("disabled_reason", "").startswith("시퀀스 키 지정 필요"):
                        return JSONResponse({"msg": candidate.get("disabled_reason", "키 확인 필요")}, status_code=400)
            elif not btn.get("action"):
                return JSONResponse({"msg": "이 버튼은 변경할 수 없습니다"}, status_code=400)
            elif body.keys.strip():
                try:
                    key = _validate_override_key(body.keys, btn.get("mode", "tap"))
                except ValueError as e:
                    return JSONResponse({"msg": str(e)}, status_code=400)
                overrides[body.button_id] = {"keys": key, "action": btn["action"]}
                proposed = _resolved_deck_layout(cfg, overrides)
                candidate = next(b for b in _deck_buttons(proposed) if b.get("id") == body.button_id)
                if candidate.get("unbound"):
                    return JSONResponse({"msg": candidate.get("disabled_reason", "키 확인 필요")}, status_code=400)
            else:
                overrides.pop(body.button_id, None)
            try:
                _write_key_overrides(overrides)
            except OSError:
                return JSONResponse({"msg": "PC 설정 저장 실패 · 쓰기 가능한 폴더를 확인하세요"}, status_code=500)
            return {"ok": True, "layout": _resolved_deck_layout(cfg)}

    @app.post("/api/input")
    def tablet_input(body: InputRequest, request: Request):
        """태블릿 → PC 입력. release(up)는 항상 통과 (stuck 키 방지)."""
        deny = _check_pair(request)
        if deny is not None:
            return deny
        with _DECK_INPUT_LOCK:
            action = body.type
            if action not in ("up", "release", "refresh") \
                    and cfg.get("only_when_game_focused", True) \
                    and not _game_focused():
                return JSONResponse(
                    {"ok": False, "error": "game_not_focused",
                     "msg": "게임 창이 앞에 있을 때만 전송됨"}, status_code=409)
            if action not in ("up", "release", "refresh") and _seq_running():
                return JSONResponse({"ok": False, "msg": "시퀀스 실행 중 · 끝난 뒤 다시 누르세요"},
                                    status_code=409)
            if action not in ("up", "release"):
                layout = _resolved_deck_layout(cfg)
                allowed = False
                try:
                    combo = _combo_id(body.key)
                    for btn in _deck_buttons(layout):
                        if btn.get("sequence") or btn.get("unbound") or not btn.get("keys"):
                            continue
                        if _combo_id(btn["keys"]) != combo:
                            continue
                        mode = btn.get("mode", "tap")
                        if ((mode == "tap" and action in ("press", "tap")) or
                                (mode == "hold" and action in ("down", "refresh"))):
                            allowed = True
                            break
                except ValueError:
                    pass
                if not allowed:
                    return JSONResponse({"ok": False, "msg": "키 지정 필요 또는 허용되지 않은 입력"}, status_code=400)
            ok, msg = _send_input(action, body.key, body.ms)
            return JSONResponse({"ok": ok, "msg": msg}, status_code=200 if ok else 400)

    @app.post("/api/sequence")
    def run_sequence(body: SeqRequest, request: Request):
        """시간텀 시퀀스 시작. 단일 실행 · 매 단계 포커스 재확인 · 취소 가능."""
        deny = _check_pair(request)
        if deny is not None:
            return deny
        with _DECK_INPUT_LOCK:
            layout = _resolved_deck_layout(cfg)
            matches = [b for b in _deck_buttons(layout)
                       if b.get("id") == body.button_id and b.get("sequence")]
            if len(matches) != 1:
                return JSONResponse({"ok": False, "msg": "시퀀스 버튼이 아닙니다"}, status_code=400)
            btn = matches[0]
            if btn.get("unbound") or not btn.get("steps"):
                return JSONResponse({"ok": False,
                                     "msg": btn.get("disabled_reason", "시퀀스 키 지정 필요")},
                                    status_code=400)
            if _seq_running():
                return JSONResponse({"ok": False, "msg": "다른 시퀀스 실행 중"}, status_code=409)
            with _HOLD_LOCK:
                if _ACTIVE_HOLDS:
                    return JSONResponse({"ok": False,
                                         "msg": "누르고 있는 키를 놓은 뒤 시작하세요"}, status_code=409)
            if not _game_focused():
                return JSONResponse(
                    {"ok": False, "error": "game_not_focused",
                     "msg": "게임 창이 앞에 있을 때만 전송됨"}, status_code=409)
            with _SEQ_LOCK:
                if _SEQ_STATE["running"]:
                    return JSONResponse({"ok": False, "msg": "다른 시퀀스 실행 중"}, status_code=409)
                _SEQ_ABORT.clear()
                _SEQ_STATE.update(running=True, button_id=btn["id"], step=0,
                                  total=len(btn["steps"]), label=btn.get("label", ""), msg="")
            try:
                threading.Thread(target=_run_sequence, args=(cfg, btn), daemon=True).start()
            except RuntimeError:
                _seq_set(running=False, msg="실행을 시작하지 못했습니다")
                return JSONResponse({"ok": False, "msg": "실행 시작 실패"}, status_code=500)
            return {"ok": True, "total": len(btn["steps"])}

    @app.get("/api/sequence")
    def sequence_status(request: Request):
        deny = _check_pair(request)
        if deny is not None:
            return deny
        with _SEQ_LOCK:
            return dict(_SEQ_STATE)

    @app.post("/api/sequence-cancel")
    def cancel_sequence(request: Request):
        """시퀀스 중단은 release처럼 항상 통과."""
        deny = _check_pair(request)
        if deny is not None:
            return deny
        _SEQ_ABORT.set()
        return {"ok": True}

    @app.post("/api/test")
    def test_input(body: TestInput, request: Request):
        deny = _check_pair(request)
        if deny is not None:
            return deny
        panel = None
        if body.minerals is not None or body.resistance_pct is not None:
            panel = {"minerals": body.minerals or [], "inert_pct": None,
                     "resistance_pct": body.resistance_pct,
                     "instability": body.instability, "mass": body.mass}
        state.update_scan(rs=body.rs, panel=panel, phase="test")
        return {"ok": True}

    @app.post("/api/clear")
    def clear(request: Request):
        deny = _check_pair(request)
        if deny is not None:
            return deny
        state.clear()
        return {"ok": True}

    return app


# ---------------------------------------------------------------- 내장 오버레이 UI
#
# tkinter 기반. 게임 위에 직접 그리는 투명/최상위/클릭통과 창 + 작은 제어 창.
# 화면 읽기 전용 원칙 유지 (인젝션/후킹 없음). tkinter는 지연 임포트하여
# 서버 전용 환경(테스트/CI)에서는 이 모듈을 로드하지 않는다.

_BG = "#000000"        # transparentcolor 키 (이 색은 완전 투명)
_FG = "#eef4fa"
_DIM = "#8fa8bd"
_GREEN = "#41d99a"
_ORANGE = "#ffb24d"
_RED = "#ff5f5f"
_ACCENT = "#46c8e6"      # 홀로그램 시안
_ACCENT_HOV = "#2ea9c6"
_ON_ACCENT = "#03222b"
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
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=r, fill=(12, 18, 26, 255))
    d.rounded_rectangle([3, 3, S - 4, S - 4], radius=r - 3,
                        outline=(34, 54, 70, 255), width=max(2, S // 85))
    cx = S // 2
    top, mid, bot = int(S * 0.20), int(S * 0.42), int(S * 0.80)
    half = int(S * 0.24)
    d.polygon([(cx, top), (cx + half, mid), (cx, bot), (cx - half, mid)],
              fill=(56, 186, 219, 255))
    d.polygon([(cx, top), (cx + half, mid), (cx, int(S * 0.50))],
              fill=(110, 220, 245, 255))
    d.polygon([(cx, top), (cx - half, mid), (cx, int(S * 0.50))],
              fill=(30, 140, 175, 255))
    d.polygon([(cx - half, mid), (cx, int(S * 0.50)), (cx, bot)],
              fill=(24, 115, 150, 255))
    d.polygon([(cx + half, mid), (cx, int(S * 0.50)), (cx, bot)],
              fill=(40, 165, 200, 255))
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
            boxes.append((panel, "패널 영역", "#c4a2ff"))
        if cfg.get("rs_search", "wide") == "fixed":
            r = cfg.get("rs_region")
            if r:
                boxes.append((r, "RS 영역 (고정)", "#46c8e6"))
        else:
            boxes.append((wide_rs_region(cfg, mon),
                          "RS 넓은 검색 영역", "#ffc857"))
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
            self.win.configure(bg="#0b1118")
            self.lines[0].configure(text="위치 조정: 드래그로 이동, Ctrl+Alt+F9로 저장",
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
        # 현재 설정된 영역을 기준선으로 항상 표시 (Ctrl+Alt+F11 표시와 동일한 색)
        self._draw_reference(cfg.get("rs_region"), "현재 RS 영역", "#46c8e6")
        self._draw_reference(cfg.get("panel_region"), "현재 패널 영역", "#c4a2ff")
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
            e.x, e.y, e.x, e.y, outline="#46c8e6", width=3)

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
            fill="#46c8e6", anchor="nw",
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
        self.root.title("SC_PadControl")
        self.root.geometry("360x640+60+60")
        self.root.resizable(False, False)
        self.root.configure(fg_color="#0a0f16")
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
        card = ctk.CTkFrame(self.content, fg_color="#0d141d", corner_radius=12,
                            border_width=1, border_color="#1c2a38")
        card.pack(padx=14, pady=(10, 0), fill="x")
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(10, 4))
        ctk.CTkLabel(head, text=title, font=self.F_H,
                     text_color="#dfe9f2").pack(side="left")
        return card, head

    def _build(self):
        ctk = self.ctk
        # ---- 헤더
        header = ctk.CTkFrame(self.root, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(14, 2))
        if self._icon_img:
            ctk.CTkLabel(header, text="", image=self._icon_img).pack(side="left")
        ctk.CTkLabel(header, text="  SC 패드 컨트롤",
                     font=self.F_TITLE, text_color="#eef4fa").pack(side="left")
        self.pill = ctk.CTkLabel(header, text="  스캔 중  ", font=self.F_SM,
                                 fg_color="#0d2833", text_color=_ACCENT,
                                 corner_radius=10)
        self.pill.pack(side="right", pady=6)

        # Always visible above the scrollable settings, including on short windows.
        connection = ctk.CTkFrame(self.root, fg_color="#10212b", corner_radius=10,
                                  border_width=1, border_color="#254958")
        connection.pack(fill="x", padx=14, pady=(8, 4))
        ctk.CTkLabel(connection, text="태블릿 접속 주소 · 같은 Wi-Fi", font=self.F_H,
                     text_color="#b4e5ec").pack(anchor="w", padx=12, pady=(9, 3))
        self.connection_url = ctk.StringVar(value="")
        self.connection_entry = ctk.CTkEntry(
            connection, textvariable=self.connection_url, state="readonly",
            font=self.F_B, height=32, fg_color="#0a1720", border_color="#254958",
            text_color="#eef4fa")
        self.connection_entry.pack(fill="x", padx=12, pady=(0, 5))
        connection_row = ctk.CTkFrame(connection, fg_color="transparent")
        connection_row.pack(fill="x", padx=12, pady=(0, 9))
        self.pair_label = ctk.CTkLabel(connection_row, text="", font=self.F_B,
                                      text_color="#b4e5ec")
        self.pair_label.pack(side="left")
        self.copy_address_button = ctk.CTkButton(
            connection_row, text="주소 복사", width=82, height=28, font=self.F_B,
            command=self.copy_connection_address, fg_color="#1b4759",
            hover_color="#245d74", text_color="#eef4fa")
        self.copy_address_button.pack(side="right")
        self._connection_refresh_at = 0
        self._refresh_connection_address()

        self.content = ctk.CTkScrollableFrame(self.root, fg_color="transparent",
                                              scrollbar_button_color="#253746")
        self.content.pack(fill="both", expand=True, padx=0, pady=0)
        # ---- 스캔 카드
        card, head = self._card("스캔")
        self.scan_sw = ctk.CTkSwitch(
            head, text="", width=42, command=self._scan_switch,
            progress_color=_ACCENT)
        self.scan_sw.pack(side="right")
        if self.cfg.get("scan_enabled", True):
            self.scan_sw.select()
        self.last_scan = ctk.CTkLabel(card, text="아직 스캔 없음",
                                      font=self.F_B, text_color="#7e93a6")
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
                     text_color="#7e93a6").pack(side="left")
        self.op_label = ctk.CTkLabel(op_row, text="", font=self.F_SM,
                                     text_color="#7e93a6")
        self.op_label.pack(side="right")
        self.opacity = ctk.CTkSlider(
            card, from_=30, to=100, command=self._opacity_changed,
            progress_color=_ACCENT, button_color=_ACCENT,
            button_hover_color=_ACCENT_HOV)
        self.opacity.set(float(self.cfg.get("overlay_opacity", 0.88)) * 100)
        self.opacity.pack(fill="x", padx=14, pady=(0, 6))
        ctk.CTkButton(card, text="위치 조정 (Ctrl+Alt+F9)", command=self.toggle_adjust,
                      fg_color="#14202c", hover_color="#1a2c3c",
                      text_color="#dfe9f2", height=30).pack(
            fill="x", padx=14, pady=(0, 12))

        # ---- 설정 카드
        card, head = self._card("설정")
        ctk.CTkButton(card, text="화면 영역 지정 (Ctrl+Alt+F10)",
                      command=self.start_calibrate,
                      fg_color=_ACCENT, hover_color=_ACCENT_HOV,
                      text_color=_ON_ACCENT, height=32,
                      font=self.F_H).pack(fill="x", padx=14, pady=(2, 6))
        ctk.CTkButton(card, text="스캔 영역 표시 (Ctrl+Alt+F11)",
                      command=self.toggle_regions,
                      fg_color="#14202c", hover_color="#1a2c3c",
                      text_color="#dfe9f2", height=30).pack(
            fill="x", padx=14, pady=(0, 6))
        ctk.CTkLabel(card, text="RS 읽기 방식", font=self.F_SM,
                     text_color="#7e93a6").pack(anchor="w", padx=14)
        self.rs_mode_seg = ctk.CTkSegmentedButton(
            card, values=["넓게 찾기", "고정 영역"],
            command=self._rs_mode_changed,
            fg_color="#14202c", selected_color="#164b5e",
            selected_hover_color="#1a5c74",
            unselected_color="#14202c", unselected_hover_color="#1a2c3c",
            text_color="#dfe9f2")
        self.rs_mode_seg.set("고정 영역" if self.cfg.get("rs_search") == "fixed"
                             else "넓게 찾기")
        self.rs_mode_seg.pack(fill="x", padx=14, pady=(2, 10))
        ctk.CTkLabel(card, text="태블릿 덱", font=self.F_SM,
                     text_color="#7e93a6").pack(anchor="w", padx=14)
        ctk.CTkButton(card, text="태블릿 덱 열기", command=self.open_web,
                      fg_color="#14202c", hover_color="#1a2c3c",
                      text_color="#dfe9f2", height=30).pack(
            fill="x", padx=14, pady=(0, 4))
        ctk.CTkButton(card, text="덱 설정 파일 열기",
                      command=self.open_deck_settings,
                      fg_color="#14202c", hover_color="#1a2c3c",
                      text_color="#dfe9f2", height=30).pack(
            fill="x", padx=14, pady=(0, 4))
        ctk.CTkButton(card, text="키바인드 XML 선택", command=self.choose_actionmaps,
                      fg_color="#14202c", hover_color="#1a2c3c",
                      text_color="#dfe9f2", height=30).pack(fill="x", padx=14, pady=(0, 4))
        _am_b, _am_s, _am_p = _load_action_bindings(self.cfg)
        _am_txt = ("키바인드: actionmaps.xml 추적 중" if _am_p
                   else "키바인드: XML 선택 필요 (ESC 외 입력 차단)")
        self.am_status = ctk.CTkLabel(card, text=_am_txt, font=self.F_SM,
                                     text_color="#5a6b7a")
        self.am_status.pack(anchor="w", padx=14, pady=(0, 2))

        # ---- 오류 배너
        self.err_banner = ctk.CTkLabel(self.root, text="", font=self.F_SM,
                                       fg_color="#2c1719", text_color="#f28b82",
                                       corner_radius=8, wraplength=320)

        # ---- 푸터
        ctk.CTkLabel(self.root,
                     text="Ctrl+Alt + F8 표시 · F9 위치 · F10 영역 · F11 확인",
                     font=self.F_SM, text_color="#46545f").pack(pady=(12, 0))
        ctk.CTkButton(self.root, text="종료", command=self.quit, width=90,
                      fg_color="transparent", hover_color="#2c1719",
                      text_color="#f28b82", height=28).pack(pady=(6, 10))

    def _refresh_connection_address(self):
        now = time.monotonic()
        if now >= self._connection_refresh_at:
            port = self.cfg.get("port", 8377)
            self.connection_url.set(f"http://{lan_ip()}:{port}")
            self._connection_refresh_at = now + 10
        self.pair_label.configure(text=f"페어링 코드  {self.cfg.get('pairing_code') or '-'}")

    def copy_connection_address(self):
        self._connection_refresh_at = 0
        self._refresh_connection_address()
        self.root.clipboard_clear()
        self.root.clipboard_append(self.connection_url.get())
        self.copy_address_button.configure(text="복사됨")
        self.root.after(1500, lambda: self.copy_address_button.configure(text="주소 복사"))

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

    def choose_actionmaps(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(parent=self.root,
            title="현재 사용하는 SC 프로필 actionmaps.xml 또는 내보내기 파일 선택",
            filetypes=[("Star Citizen keybindings", "*.xml")])
        if path:
            self.cfg["actionmaps_path"] = path
            save_config(self.cfg)
            _, _, active = _load_action_bindings(self.cfg)
            self.am_status.configure(text="키바인드: 선택한 XML 연결됨" if active
                                     else "키바인드: XML 확인 실패 (입력 잠금)")
            from tkinter import messagebox
            messagebox.showinfo("키바인드", "선택을 저장했습니다. 태블릿 페이지를 새로고침하세요.",
                                parent=self.root)

    def open_deck_settings(self):
        """exe 옆 deck_layout.json을 연다. 없으면 내장 기본값을 복사해 둔다."""
        p = os.path.join(EXE_DIR, "deck_layout.json")
        if not os.path.exists(p):
            src = _data_path("deck_layout.json")
            try:
                if src:
                    import shutil
                    shutil.copyfile(src, p)
            except OSError:
                pass
        try:
            if sys.platform == "win32":
                os.startfile(p)  # type: ignore[attr-defined]
            else:
                print(f"[deck] 설정 파일: {p}")
        except OSError as e:
            print(f"[deck] 설정 파일 열기 실패: {e}")

    def quit(self):
        with _HOLD_LOCK:
            held = list(_ACTIVE_HOLDS)
            _ACTIVE_HOLDS.clear()
        for key in held:
            try:
                _combo_up(key)
            except Exception:
                pass
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
                            (4, 0x7A)):  # Ctrl+Alt+F8/Ctrl+Alt+F9/Ctrl+Alt+F10/Ctrl+Alt+F11
                if not u32.RegisterHotKey(None, hid, 0x4000 | 0x0002 | 0x0001, vk):
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
            fg_color="#0d2833" if scan_on else "#1a222c",
            text_color=_ACCENT if scan_on else "#7e93a6")
        rs = st.get("rs")
        phase = {"idle": "대기", "scan": "스캔", "close_scan": "근접 스캔",
                 "test": "테스트"}.get(st.get("phase"), st.get("phase", "-"))
        region_ok = bool(self.cfg.get("rs_region"))
        if rs:
            txt = f"RS {rs} · {phase} · {st.get('updated_at', '')}"
        elif region_ok:
            txt = f"대기 중 ({phase})"
        else:
            txt = "캡처 영역 미설정 — Ctrl+Alt+F10으로 지정"
        self.last_scan.configure(text=txt)
        self._refresh_connection_address()
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
            log = open(os.path.join(EXE_DIR, "sc_pad_control.log"), "a",
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
    if not cfg.get("pairing_code"):
        cfg["pairing_code"] = f"{secrets.randbelow(1000000):06d}"
        try:
            save_config(cfg)
        except OSError:
            pass
    threading.Thread(target=hold_reaper, args=(cfg,), daemon=True).start()
    state = ScanState(load_data("signatures.json"),
                      load_data("minerals.json")["minerals"],
                      load_data("equipment.json"),
                      cfg.get("verdict_thresholds", {}))
    port = cfg.get("port", 8377)
    threading.Thread(target=capture_loop, args=(cfg, state),
                     daemon=True).start()
    if not cfg.get("rs_region"):
        print("[안내] 캡처 영역 미설정: 제어 창의 '화면 영역 지정'(Ctrl+Alt+F10) 또는 "
              f"제어 창의 '화면 영역 지정'(Ctrl+Alt+F10)으로 지정하세요.")
    print("=" * 56)
    print(" SC 패드 컨트롤 시작")
    print(f"  이 PC에서:        http://localhost:{port}")
    print(f"  태블릿/같은Wi-Fi: http://{lan_ip()}:{port}")
    print(f"  페어링 코드:      {cfg.get('pairing_code')}")
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
