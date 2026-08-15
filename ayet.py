import os
import re
import json
import shutil
import time
import uuid
import base64
import subprocess
import threading
from io import BytesIO
from pathlib import Path
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks
from starlette.background import BackgroundTask
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from PIL import Image, ImageDraw, ImageFont

BASE_DIR = Path(__file__).parent.resolve()
load_dotenv(BASE_DIR / ".env")
WORK_DIR = BASE_DIR / "work"
OUT_DIR = BASE_DIR / "output"
BG_CACHE = WORK_DIR / "bg_cache"
FONT_DIR = BASE_DIR / "fonts"
for d in (WORK_DIR, OUT_DIR, BG_CACHE):
    d.mkdir(parents=True, exist_ok=True)

# ----------------------------------------------------------------------------
# NOT (performans/sadeleştirme): Arapça ayet metni artık HER ZAMAN TSX
# tarafında (react-native-view-shot) cihazın kendi native metin motoruyla
# şekillendirilip PNG olarak backend'e gönderiliyor. Bu yüzden burada eskiden
# bulunan arabic_reshaper/bidi fallback'i, Pillow RAQM probe'u, fontTools
# cmap taraması, çok-adaylı font kapsama skorlaması gibi tüm Arapça
# shaping/tofu-koruma makinesi TAMAMEN KALDIRILDI. Backend artık sadece meal,
# ayet numarası ve watermark gibi düz Latin/Unicode metinleri basit bir
# TrueType font ile çiziyor — bu da render.com'un free tier'ındaki (paylaşımlı
# 1 çekirdek, 512MB RAM) CPU/kota baskısını ciddi oranda azaltıyor.
# ----------------------------------------------------------------------------

if FONT_DIR.exists():
    _found_fonts = sorted(p.name for p in FONT_DIR.glob("*.[tT][tT][fF]")) + \
                   sorted(p.name for p in FONT_DIR.glob("*.[oO][tT][fF]"))
    print(f"[BAŞLANGIÇ] fonts/ klasöründe {len(_found_fonts)} font dosyası bulundu.")
else:
    print(f"[UYARI] fonts/ klasörü yok: {FONT_DIR} — meal/ayet no/watermark metinleri "
          f"sistem varsayılan fontuyla (düşük kaliteli) çizilecek.")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")

W, H = 1080, 1920

RESOLUTION_MAP: dict[str, tuple[int, int]] = {
    "480p": (480, 854),
    "720p": (720, 1280),
    "1080p": (1080, 1920),
}
DEFAULT_RESOLUTION = "720p"

PRESET_MAP: dict[str, str] = {"fast": "ultrafast", "balanced": "veryfast", "quality": "medium"}
DEFAULT_RENDER_SPEED = "fast"

# Her ses efektinin varsayılan parametreleri — TSX tarafındaki
# AUDIO_EFFECT_PARAMS ile birebir eşleşir (aynı key isimleri, aynı
# varsayılan/min/max değerleri). Kullanıcı slider'ı hiç oynatmazsa bu
# varsayılanlar kullanılır.
AUDIO_EFFECT_DEFAULTS: dict[str, dict[str, float]] = {
    "none": {},
    "deepen": {"pitch_ratio": 0.82},
    "slowdown": {"rate": 0.85},
    "speedup": {"rate": 1.15},
    "slowed_reverb": {"rate": 0.85, "reverb_amount": 0.4},
}

# TSX'teki slider min/max ile aynı — backend'e ne gelirse gelsin (bozuk
# istemci, eski önbellek vs.) bu aralıklara sıkıştırılır; ffmpeg'e asla
# ham/doğrulanmamış değer gitmez.
AUDIO_EFFECT_PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "pitch_ratio": (0.70, 0.95),
    "rate": (0.5, 1.5),
    "reverb_amount": (0.0, 1.0),
}


def _resolve_audio_effect_params(effect: str, incoming: dict[str, float]) -> dict[str, float]:
    """İstemciden gelen ham parametreleri, o efekt için tanımlı varsayılanlarla
    birleştirir ve her değeri güvenli aralığa (AUDIO_EFFECT_PARAM_BOUNDS)
    sıkıştırır."""
    defaults = AUDIO_EFFECT_DEFAULTS.get(effect, {})
    resolved: dict[str, float] = {}
    for key, default_val in defaults.items():
        val = incoming.get(key, default_val)
        try:
            val = float(val)
        except (TypeError, ValueError):
            val = default_val
        lo, hi = AUDIO_EFFECT_PARAM_BOUNDS.get(key, (float("-inf"), float("inf")))
        resolved[key] = max(lo, min(hi, val))
    return resolved


def _build_audio_effect_filter(effect: str, params: dict[str, float]) -> Optional[str]:
    """Efekt adı + çözümlenmiş parametrelere göre ffmpeg -af filtre string'ini
    kurar. Her zaman AUDIO_EFFECT_PARAM_BOUNDS ile sınırlanmış değerler alır,
    bu yüzden burada ekstra doğrulama gerekmez."""
    if effect == "none" or not params:
        return None

    if effect == "deepen":
        pitch_ratio = params.get("pitch_ratio", 0.82)
        # Ses perdesini düşürüp, süreyi telafi etmek için tempo'yu tersine artırır
        # (asetrate perdeyi + hızı birlikte değiştirir, atempo sadece hızı geri çevirir).
        compensate_tempo = round(1.0 / pitch_ratio, 4)
        return f"asetrate=44100*{pitch_ratio},aresample=44100,atempo={compensate_tempo}"

    if effect == "slowdown":
        rate = params.get("rate", 0.85)
        return f"atempo={rate}"

    if effect == "speedup":
        rate = params.get("rate", 1.15)
        return f"atempo={rate}"

    if effect == "slowed_reverb":
        rate = params.get("rate", 0.85)
        reverb_amount = params.get("reverb_amount", 0.4)
        # aecho: in_gain:out_gain:delay_ms:decay — decay (son parametre)
        # reverb_amount'a göre ölçeklenir (0 → neredeyse yok, 1 → belirgin).
        decay = round(0.15 + reverb_amount * 0.75, 3)  # 0..1 -> 0.15..0.90 aralığı (ffmpeg üst sınırı ~<1)
        return f"asetrate=44100*{rate},aresample=44100,aecho=0.8:0.9:60:{decay}"

    return None


# Meal/ayet no/watermark için varsayılan font — TSX tarafındaki
# DEFAULT_MEAL_FONT ('CairoPlay-Regular.ttf') ile aynı, çok dilli kapsama
# sağladığı için son çare (fallback) olarak kullanılıyor.
DEFAULT_TEXT_FONT = "CairoPlay-Regular.ttf"

CRF_MAP = {"high": 18, "medium": 22, "low": 27}

MODELS: dict[str, dict] = {
    "gemini-3.1-flash-lite": {"id": "models/gemini-3.1-flash-lite", "label": "Gemini 3.1 Flash Lite", "rpm": 15, "tpm": 250000, "emoji": "⚡"},
    "gemini-2.5-flash-lite": {"id": "models/gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash Lite", "rpm": 10, "tpm": 250000, "emoji": "⚖️"},
    "gemini-2.5-flash":      {"id": "models/gemini-2.5-flash",      "label": "Gemini 2.5 Flash",      "rpm": 5,  "tpm": 250000, "emoji": "🧠"},
    "gemini-3-flash":        {"id": "models/gemini-3-flash",        "label": "Gemini 3 Flash",        "rpm": 5,  "tpm": 250000, "emoji": "🔥"},
    "gemini-3.5-flash":      {"id": "models/gemini-3.5-flash",      "label": "Gemini 3.5 Flash",      "rpm": 5,  "tpm": 250000, "emoji": "🚀"},
}
DEFAULT_MODEL = "gemini-2.5-flash-lite"


def _bin(name: str) -> str:
    for cand in (BASE_DIR / f"{name}.exe", BASE_DIR / name):
        if cand.exists():
            return str(cand)
    return name


FFMPEG = _bin("ffmpeg")
FFPROBE = _bin("ffprobe")

app = FastAPI(title="Kuran Reels Backend")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

jobs: dict[str, dict] = {}


OUTPUT_MAX_AGE_HOURS = 6
WORK_MAX_AGE_HOURS = 2
BG_CACHE_MAX_TOTAL_GB = 1.0  # render.com free tier: küçük ephemeral disk
CLEANUP_INTERVAL_SECONDS = 30 * 60


def _age_hours(p: Path) -> float:
    try:
        return (time.time() - p.stat().st_mtime) / 3600.0
    except FileNotFoundError:
        return 0.0


def _cleanup_output_dir():
    for p in OUT_DIR.glob("*.mp4"):
        if _age_hours(p) >= OUTPUT_MAX_AGE_HOURS:
            try:
                p.unlink(missing_ok=True)
                print(f"[TEMİZLİK] Eski render silindi (indirilmemiş/unutulmuş): {p.name}")
            except Exception as e:
                print(f"[UYARI] {p.name} silinemedi: {e}")
    stale_ids = []
    for jid, j in list(jobs.items()):
        if j.get("status") in ("done", "error"):
            f = j.get("file")
            if not f or not (OUT_DIR / Path(f).name).exists():
                stale_ids.append(jid)
    for jid in stale_ids:
        jobs.pop(jid, None)


def _cleanup_work_dir():
    active_ids = {jid for jid, j in jobs.items() if j.get("status") in ("queued", "running")}
    for p in WORK_DIR.iterdir():
        if p == BG_CACHE:
            continue
        if _age_hours(p) < WORK_MAX_AGE_HOURS:
            continue
        if any(jid in p.name for jid in active_ids):
            continue
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
            print(f"[TEMİZLİK] work/ içinde öksüz/eski dosya silindi: {p.name}")
        except Exception as e:
            print(f"[UYARI] work/ temizliği: {p.name} silinemedi: {e}")


def _cleanup_bg_cache():
    files = [p for p in BG_CACHE.glob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    limit = BG_CACHE_MAX_TOTAL_GB * (1024 ** 3)
    if total <= limit:
        return
    files.sort(key=lambda p: p.stat().st_atime)
    for p in files:
        if total <= limit:
            break
        try:
            size = p.stat().st_size
            p.unlink(missing_ok=True)
            total -= size
            print(f"[TEMİZLİK] bg_cache boyut limiti ({BG_CACHE_MAX_TOTAL_GB}GB) aşıldığı için silindi: {p.name}")
        except Exception as e:
            print(f"[UYARI] bg_cache temizliği: {p.name} silinemedi: {e}")


def _wipe_all_mp4s(job_id: str | None = None):
    for p in WORK_DIR.rglob("*.mp4"):
        try:
            p.unlink(missing_ok=True)
            print(f"[TEMİZLİK] work/ içindeki mp4 silindi: {p.name}")
        except Exception as e:
            print(f"[UYARI] {p.name} silinemedi: {e}")
    for p in BG_CACHE.glob("*.mp4"):
        try:
            p.unlink(missing_ok=True)
            print(f"[TEMİZLİK] bg_cache içindeki mp4 silindi: {p.name}")
        except Exception as e:
            print(f"[UYARI] {p.name} silinemedi: {e}")
    for p in OUT_DIR.glob("*.mp4"):
        try:
            p.unlink(missing_ok=True)
            print(f"[TEMİZLİK] output içindeki mp4 silindi: {p.name}")
        except Exception as e:
            print(f"[UYARI] {p.name} silinemedi: {e}")


def _cleanup_all():
    for fn in (_cleanup_output_dir, _cleanup_work_dir, _cleanup_bg_cache):
        try:
            fn()
        except Exception as e:
            print(f"[UYARI] {fn.__name__} sırasında hata: {e}")


def _cleanup_loop():
    while True:
        _cleanup_all()
        time.sleep(CLEANUP_INTERVAL_SECONDS)


@app.on_event("startup")
async def _start_cleanup_thread():
    threading.Thread(target=_cleanup_loop, daemon=True).start()


@app.post("/api/cleanup")
async def manual_cleanup():
    _cleanup_all()
    du = shutil.disk_usage(str(BASE_DIR))
    return {
        "cleaned": True,
        "disk_free_gb": round(du.free / (1024 ** 3), 2),
        "disk_total_gb": round(du.total / (1024 ** 3), 2),
    }


async def _upstream_call(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    try:
        r = await client.request(method, url, **kwargs)
    except httpx.ConnectError as e:
        raise HTTPException(
            502,
            f"{url} adresine bağlanılamadı (DNS/ağ hatası): {e}. "
            f"Sunucu makinenin internet bağlantısını, DNS'i, VPN/proxy veya "
            f"antivirüs/güvenlik duvarı ayarlarını kontrol et.",
        )
    except httpx.TimeoutException:
        raise HTTPException(504, f"{url} isteği zaman aşımına uğradı.")
    except httpx.RequestError as e:
        raise HTTPException(502, f"{url} isteğinde ağ hatası: {e}")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, f"Üst servis hatası ({url}): {r.text[:300]}")
    return r


async def _gemini_call_with_fallback(client: httpx.AsyncClient, prompt: str, preferred_model: str) -> tuple[httpx.Response, str]:
    order = [preferred_model] + [k for k in MODELS if k != preferred_model]
    last_err: Optional[HTTPException] = None
    for key in order:
        info = MODELS.get(key)
        if not info:
            continue
        try:
            r = await _upstream_call(
                client, "POST",
                f"https://generativelanguage.googleapis.com/v1beta/{info['id']}:generateContent?key={GEMINI_API_KEY}",
                json={"contents": [{"parts": [{"text": prompt}]}]},
            )
            return r, key
        except HTTPException as e:
            last_err = e
            continue
    raise last_err or HTTPException(502, "Hiçbir Gemini modeli yanıt vermedi.")


class GeminiReq(BaseModel):
    theme: str
    keywords: str = ""
    model: str = DEFAULT_MODEL


@app.get("/api/models")
async def list_models():
    return {"models": [{"key": k, **v} for k, v in MODELS.items()], "default": DEFAULT_MODEL}


@app.post("/api/gemini-query")
async def gemini_query(req: GeminiReq):
    if not GEMINI_API_KEY:
        raise HTTPException(400, "GEMINI_API_KEY env değişkeni tanımlı değil")
    preferred_model = req.model if req.model in MODELS else DEFAULT_MODEL
    prompt = f"""You are helping create background videos for a Quran recitation reel.
Surah/Theme: "{req.theme}" {f'(keywords: {req.keywords})' if req.keywords else ''}
Generate ONE short Pexels video search query (3-5 words) for a beautiful, peaceful, spiritually appropriate background.
Respond ONLY with JSON: {{"query": "..."}}
No explanation. Just JSON."""
    async with httpx.AsyncClient(timeout=30) as client:
        r, used_model = await _gemini_call_with_fallback(client, prompt, preferred_model)
    data = r.json()
    raw = (
        data.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [{}])[0]
        .get("text", "{}")
    )
    clean = raw.replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(clean)
    except Exception:
        parsed = {"query": raw.strip()}
    parsed["model_used"] = used_model
    return parsed


class PexelsQueryReq(BaseModel):
    text: str
    model: str = DEFAULT_MODEL


@app.post("/api/gemini-translate-query")
async def gemini_translate_query(req: PexelsQueryReq):
    text = (req.text or "").strip()
    if not text:
        return {"query": ""}
    if not GEMINI_API_KEY:
        raise HTTPException(400, "GEMINI_API_KEY env değişkeni tanımlı değil")
    preferred_model = req.model if req.model in MODELS else DEFAULT_MODEL
    prompt = f"""Translate/convert the following short phrase (it may be in Turkish or any other language)
into a short, simple ENGLISH Pexels video search query (2-5 words) that will return
beautiful, peaceful, spiritually appropriate background footage.
Phrase: "{text}"
Respond ONLY with JSON: {{"query": "..."}}
No explanation. Just JSON."""
    async with httpx.AsyncClient(timeout=30) as client:
        r, used_model = await _gemini_call_with_fallback(client, prompt, preferred_model)
    data = r.json()
    raw = (
        data.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [{}])[0]
        .get("text", "{}")
    )
    clean = raw.replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(clean)
    except Exception:
        parsed = {"query": raw.strip()}
    if not parsed.get("query"):
        parsed["query"] = text
    parsed["model_used"] = used_model
    return parsed


class VerseRefOut(BaseModel):
    surah_id: int
    ayah: int


class GeminiSuggestVersesReq(BaseModel):
    theme: str
    keywords: str = ""
    count: int = 8
    model: str = DEFAULT_MODEL


_VERSE_REF_RE = re.compile(r"\b(\d{1,3})\s*[:./]\s*(\d{1,3})(?:\s*-\s*(\d{1,3}))?\b")


def _extract_verse_refs(text: str, max_count: int) -> List[VerseRefOut]:
    seen: set[tuple[int, int]] = set()
    out: List[VerseRefOut] = []
    for m in _VERSE_REF_RE.finditer(text or ""):
        sid = int(m.group(1))
        a_from = int(m.group(2))
        a_to = int(m.group(3)) if m.group(3) else a_from
        if a_to < a_from or a_to - a_from > 40:
            continue
        max_ayah = max_ayah_for(sid)
        if not max_ayah:
            continue
        for a in range(a_from, a_to + 1):
            if a < 1 or a > max_ayah:
                continue
            key = (sid, a)
            if key in seen:
                continue
            seen.add(key)
            out.append(VerseRefOut(surah_id=sid, ayah=a))
            if len(out) >= max_count:
                return out
    return out


@app.post("/api/gemini-suggest-verses")
async def gemini_suggest_verses(req: GeminiSuggestVersesReq):
    theme = (req.theme or "").strip()
    if not theme:
        raise HTTPException(400, "theme boş olamaz")
    if not GEMINI_API_KEY:
        raise HTTPException(400, "GEMINI_API_KEY env değişkeni tanımlı değil")
    preferred_model = req.model if req.model in MODELS else DEFAULT_MODEL

    count = max(1, min(req.count or 8, 20))
    prompt = f"""Bir Kur'an-ı Kerim reels videosu hazırlıyorum. Konu/tema: "{theme}" {f'(anahtar kelimeler: {req.keywords})' if req.keywords else ''}

Bu konuyla GERÇEKTEN ilgili, doğru ve mevcut yaklaşık {count} ayet öner. Kısa bir açıklama
yazabilirsin ama her ayeti mutlaka "sure_no:ayet_no" biçiminde (örn. 2:255 ya da 94:5-6 gibi
ardışık aralık) belirt — format ya da açıklama tarzın önemli değil, sadece bu numaraların
doğru ve gerçek olmasına dikkat et. Uydurma referans verme; emin değilsen o ayeti yazma."""

    async with httpx.AsyncClient(timeout=30) as client:
        r, used_model = await _gemini_call_with_fallback(client, prompt, preferred_model)
    data = r.json()
    raw = (
        data.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [{}])[0]
        .get("text", "")
    )
    refs = _extract_verse_refs(raw, count)
    if not refs:
        raise HTTPException(
            422,
            "Gemini bu konu için geçerli bir ayet referansı üretmedi — konuyu biraz daha "
            "netleştirip tekrar dene (ör. 'sabır' yerine 'sıkıntıda sabretmek').",
        )
    return {"refs": [r.model_dump() for r in refs], "raw": raw, "model_used": used_model}


@app.get("/api/pexels-search")
async def pexels_search(query: str):
    if not PEXELS_API_KEY:
        raise HTTPException(400, "PEXELS_API_KEY env değişkeni tanımlı değil")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await _upstream_call(
            client, "GET",
            "https://api.pexels.com/videos/search",
            params={"query": query, "orientation": "portrait", "size": "medium", "per_page": 80},
            headers={"Authorization": PEXELS_API_KEY},
        )
    return r.json()


def _cache_path(url: str, suffix: str) -> Path:
    BG_CACHE.mkdir(parents=True, exist_ok=True)
    return BG_CACHE / (uuid.uuid5(uuid.NAMESPACE_URL, url).hex + suffix)


async def _download(url: str, dest: Path, timeout: int = 120):
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in r.aiter_bytes(1024 * 256):
                    f.write(chunk)


@app.get("/api/pexels-download")
async def pexels_download(url: str):
    dest = _cache_path(url, ".mp4")
    await _download(url, dest)
    return FileResponse(dest, media_type="video/mp4")


class AyahIn(BaseModel):
    key: str
    arabic: str
    meal: str
    audio_url: str
    label: Optional[str] = None
    # TSX tarafında react-native-view-shot ile, cihazın kendi native metin
    # motoru (iOS Core Text / Android) tarafından zaten doğru şekillendirilmiş
    # (bitişik harfler, doğru hareke konumları) Arapça metnin şeffaf arka
    # planlı, base64 kodlu PNG hâli. ARTIK TEK KAYNAK BU — backend'de ayrıca
    # bir Pillow/RAQM/reshape fallback'i YOK. Bu alan boşsa o ayette Arapça
    # metin basitçe atlanır (meal + ayet no yine basılır).
    arabic_png_base64: Optional[str] = None


class StyleIn(BaseModel):
    dimAmount: float = 0.55
    arabicSize: int = 72
    mealSize: int = 32
    arabicColor: str = "#f5e6c0"
    mealColor: str = "#e8e0cc"
    # arabicFont: sadece PNG'yi üreten TSX tarafında kullanılıyor; backend
    # Arapça çizmediği için bu alanı yok sayar, geriye dönük uyumluluk
    # amacıyla kabul ediliyor.
    arabicFont: Optional[str] = None
    mealFont: Optional[str] = None


class RenderReq(BaseModel):
    ayahs: List[AyahIn]
    bg_video_url: str = ""
    bg_video_urls: List[str] = []
    style: StyleIn = StyleIn()
    pause_between: float = 1.0
    include_arabic: bool = True
    include_meal: bool = True
    show_verse_number: bool = True
    watermark_text: Optional[str] = None
    quality: str = "high"
    resolution: str = DEFAULT_RESOLUTION
    render_speed: str = DEFAULT_RENDER_SPEED
    audio_effect: str = "none"
    # Seçili ses efektinin ince ayar değerleri (örn. {"rate": 0.85} veya
    # {"pitch_ratio": 0.82}); TSX tarafındaki inline slider'lardan gelir.
    # Anahtar isimleri AUDIO_EFFECT_DEFAULTS ile eşleşir; eksik/bilinmeyen
    # anahtarlar için varsayılan değer kullanılır.
    audio_effect_params: dict[str, float] = {}
    add_logo_outro: bool = True

    def resolved_bg_urls(self) -> List[str]:
        urls = [u for u in (self.bg_video_urls or []) if u and u.strip()]
        if urls:
            return urls
        if self.bg_video_url and self.bg_video_url.strip():
            return [self.bg_video_url]
        return []


def _wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _clean_text(t: Optional[str]) -> str:
    if not t:
        return ""
    return " ".join(t.split())


SURAH_NAMES_TR: List[str] = [
    "Fâtiha", "Bakara", "Âl-i İmrân", "Nisâ", "Mâide", "En'âm", "A'râf", "Enfâl", "Tevbe", "Yunus",
    "Hûd", "Yusuf", "Ra'd", "İbrahim", "Hicr", "Nahl", "İsrâ", "Kehf", "Meryem", "Tâ-Hâ",
    "Enbiyâ", "Hac", "Mü'minûn", "Nûr", "Furkan", "Şuarâ", "Neml", "Kasas", "Ankebût", "Rûm",
    "Lokman", "Secde", "Ahzâb", "Sebe'", "Fâtır", "Yâsin", "Sâffât", "Sâd", "Zümer", "Mü'min",
    "Fussilet", "Şûrâ", "Zuhruf", "Duhân", "Câsiye", "Ahkaf", "Muhammed", "Fetih", "Hucurât", "Kaf",
    "Zâriyât", "Tûr", "Necm", "Kamer", "Rahmân", "Vâkıa", "Hadid", "Mücâdele", "Haşr", "Mümtehine",
    "Saf", "Cum'a", "Münâfikûn", "Teğabün", "Talâk", "Tahrim", "Mülk", "Kalem", "Hâkka", "Meâric",
    "Nuh", "Cin", "Müzzemmil", "Müddessir", "Kıyamet", "İnsan", "Mürselât", "Nebe'", "Nâziât", "Abese",
    "Tekvir", "İnfitâr", "Mutaffifin", "İnşikak", "Bürûc", "Târık", "A'lâ", "Gâşiye", "Fecr", "Beled",
    "Şems", "Leyl", "Duhâ", "İnşirâh", "Tin", "Alak", "Kadir", "Beyyine", "Zilzâl", "Âdiyât",
    "Kâria", "Tekâsür", "Asr", "Hümeze", "Fil", "Kureyş", "Mâûn", "Kevser", "Kâfirûn", "Nasr",
    "Tebbet", "İhlâs", "Felâk", "Nâs",
]

MAX_AYAH_PER_SURAH: List[int] = [
    7, 286, 200, 176, 120, 165, 206, 75, 129, 109,
    123, 111, 43, 52, 99, 128, 111, 110, 98, 135,
    112, 78, 118, 64, 77, 227, 93, 88, 69, 60,
    34, 30, 73, 54, 45, 83, 182, 88, 75, 85,
    54, 53, 89, 59, 37, 35, 38, 29, 18, 45,
    60, 49, 62, 55, 78, 96, 29, 22, 24, 13,
    14, 11, 11, 18, 12, 12, 30, 52, 52, 44,
    28, 28, 20, 56, 40, 31, 50, 40, 46, 42,
    29, 19, 36, 25, 22, 17, 19, 26, 30, 20,
    15, 21, 11, 8, 8, 19, 5, 8, 8, 11,
    11, 8, 3, 9, 5, 4, 7, 3, 6, 3,
    5, 4, 5, 6,
]


def max_ayah_for(sid: int) -> int:
    if 1 <= sid <= len(MAX_AYAH_PER_SURAH):
        return MAX_AYAH_PER_SURAH[sid - 1]
    return 0


_surah_names_from_json: Optional[dict] = None


def _load_surah_names_from_json() -> dict:
    global _surah_names_from_json
    if _surah_names_from_json is not None:
        return _surah_names_from_json
    result: dict = {}
    p = BASE_DIR / "qul_downloads" / "surah.json"
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            for item in data.get("Translate", []):
                sid = item.get("id")
                names = item.get("names") or []
                if sid and names:
                    result[int(sid)] = names[0]
    except Exception as e:
        print(f"[UYARI] qul_downloads/surah.json okunamadı: {e} — sabit sure adı listesine düşülüyor.")
    _surah_names_from_json = result
    return result


def surah_name_for(sid: int) -> str:
    names = _load_surah_names_from_json()
    if sid in names:
        return names[sid]
    if 1 <= sid <= len(SURAH_NAMES_TR):
        return SURAH_NAMES_TR[sid - 1]
    return f"Sure {sid}"


def ayah_display_label(key: str) -> str:
    try:
        sid_str, ayah_str = key.split(":", 1)
        sid = int(sid_str)
    except (ValueError, AttributeError):
        return key
    return f"{surah_name_for(sid)} Suresi, {ayah_str}. Ayet"


def _safe_font_name(name: Optional[str]) -> Optional[str]:
    """Kullanıcıdan (API isteğinden) gelen font dosya adını doğrular: yol
    geçişini engellemek için sadece dosya adı kısmını alır, fonts/ içinde
    gerçekten var olan bir dosya değilse None döner."""
    if not name:
        return None
    candidate = Path(name).name
    if candidate and (FONT_DIR / candidate).is_file():
        return candidate
    return None


def _load_font(name: Optional[str], size: int, fallback: str = DEFAULT_TEXT_FONT) -> "ImageFont.FreeTypeFont":
    """Basit font yükleyici — sadece fonts/ klasöründen isimle TTF/OTF yükler.
    Arapça shaping burada yok (bkz. dosya başındaki not); bu sadece meal,
    ayet numarası ve watermark gibi düz metinler için kullanılır."""
    for candidate in (_safe_font_name(name), _safe_font_name(fallback)):
        if not candidate:
            continue
        try:
            return ImageFont.truetype(str(FONT_DIR / candidate), size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _load_arabic_png(b64: Optional[str], max_w: int, max_h: Optional[int] = None) -> Optional["Image.Image"]:
    """TSX'ten (react-native-view-shot, pixelRatio: 3) gelen, cihazın kendi
    native metin motoruyla zaten doğru şekillendirilmiş şeffaf Arapça PNG'sini
    çözer. Kalite kaybı olmaması için görsel SADECE gerektiğinde küçültülür
    (downscale, LANCZOS) — asla büyütülmez."""
    if not b64:
        return None
    try:
        raw = b64.split(",", 1)[1] if b64.strip().startswith("data:") else b64
        data = base64.b64decode(raw)
        im = Image.open(BytesIO(data)).convert("RGBA")
    except Exception:
        return None
    w, h = im.size
    if w <= 0 or h <= 0:
        return None
    scale = 1.0
    if w > max_w:
        scale = max_w / w
    if max_h and h * scale > max_h:
        scale = min(scale, max_h / h)
    if scale < 1.0:
        new_w = max(1, round(w * scale))
        new_h = max(1, round(h * scale))
        im = im.resize((new_w, new_h), Image.LANCZOS)
    return im


def draw_overlay(
    ayah: AyahIn,
    style: StyleIn,
    img_w: int = W,
    img_h: int = H,
    include_arabic: bool = True,
    include_meal: bool = True,
    show_verse_number: bool = True,
    watermark_text: Optional[str] = None,
) -> Path:
    img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    dim = Image.new("RGBA", (img_w, img_h), (0, 0, 0, int(255 * style.dimAmount)))
    img = Image.alpha_composite(img, dim)
    draw = ImageDraw.Draw(img)

    max_w = img_w - 160

    font_meal = _load_font(style.mealFont, style.mealSize)
    font_small = _load_font(None, 26)

    # Tek kaynak: TSX'ten gelen hazır PNG. Yoksa Arapça satırı basitçe atlanır.
    arabic_png = _load_arabic_png(ayah.arabic_png_base64, max_w, int(img_h * 0.6)) if include_arabic else None

    meal_clean = _clean_text(ayah.meal) if include_meal else ""
    meal_lines = _wrap(draw, meal_clean, font_meal, max_w) if meal_clean else []

    ar_block_h = arabic_png.height if arabic_png is not None else 0
    meal_block_h = len(meal_lines) * (style.mealSize + 10) if meal_lines else 0
    gap = 40 if (arabic_png is not None and meal_lines) else 0
    total_h = ar_block_h + gap + meal_block_h
    y = (img_h - total_h) // 2

    if arabic_png is not None:
        px = (img_w - arabic_png.width) // 2
        img.paste(arabic_png, (px, int(y)), arabic_png)
        y += ar_block_h + gap

    for line in meal_lines:
        w = draw.textlength(line, font=font_meal)
        draw.text(((img_w - w) // 2, y), line, font=font_meal, fill=style.mealColor)
        y += style.mealSize + 10

    if show_verse_number:
        label = _clean_text((ayah.label or "").strip() or ayah_display_label(ayah.key))
        w = draw.textlength(label, font=font_small)
        draw.text(((img_w - w) // 2, 110), label, font=font_small, fill="#d4a547")

    if watermark_text:
        wm = _clean_text(watermark_text)
        w = draw.textlength(wm, font=font_small)
        icon = _render_logo_png(int(font_small.size * 2.0), filled=False)
        gap2 = int(icon.width * 0.22)
        text_y = img_h - 90
        total_w = icon.width + gap2 + w
        start_x = (img_w - total_w) // 2
        icon_top = text_y + (font_small.size - icon.height) // 2
        img.paste(icon, (int(start_x), int(icon_top)), icon)
        draw.text((start_x + icon.width + gap2, text_y), wm, font=font_small, fill="#ffffffcc")

    out = WORK_DIR / f"overlay_{uuid.uuid4().hex}.png"
    img.save(out)
    return out


# ============================================================================
# LOGO (logo-full.svg ile birebir aynı geometri)
# ----------------------------------------------------------------------------
# Eskiden video sonu (outro) için bu logo, spring-fizik animasyonuyla ~76 ayrı
# kare hâlinde Pillow'la tek tek diske PNG olarak yazılıp sonra ffmpeg'e
# veriliyordu — render.com free tier'da bu hem CPU hem disk I/O açısından en
# pahalı kısımdı. Artık logo TEK SEFERLİK statik bir görsel olarak (bellekte
# cache'lenerek) çiziliyor; "belirme" animasyonu ffmpeg'in kendi fade
# filtresiyle yapılıyor. Watermark ikonu da aynı fonksiyonu kullanıyor.
# ============================================================================

OUTRO_LETTER_COLOR = (103, 58, 21)
OUTRO_CIRCLE_COLOR = (69, 123, 47)
OUTRO_HANDLE_COLOR = (168, 144, 120)
LOGO_SVG_BASE_SIZE = 384.0
OUTRO_CIRCLES = [
    (151.6, 193.5, 23.6), (225.6, 193.2, 23.6), (255.9, 157.3, 17.9),
    (238.1, 115.9, 23.6), (190.9, 92.3, 23.6), (128.0, 153.0, 18.1),
    (145.8, 113.8, 21.5), (167.3, 152.5, 13.5), (197.6, 135.2, 13.5),
    (221.1, 154.9, 11.1),
]

_OUTRO_LETTER_PATH_D = (
    "M 186.789062 167.183594 C 189.878906 159.972656 191.863281 159.828125 "
    "192.746094 166.742188 L 197.820312 220.808594 C 203.414062 269.503906 "
    "202.75 300.398438 195.835938 313.492188 C 194.804688 315.257812 "
    "193.996094 315.699219 193.40625 314.816406 C 192.820312 314.082031 "
    "192.527344 312.832031 192.527344 311.066406 C 192.820312 298.707031 "
    "191.9375 284.804688 189.878906 269.359375 C 189.878906 268.769531 "
    "188.332031 254.792969 185.242188 227.429688 L 180.386719 187.484375 "
    "C 180.09375 184.542969 180.386719 182.042969 181.269531 179.980469 Z"
)


def _parse_outro_letter_polygon(d: str, samples_per_curve: int = 24) -> List[tuple]:
    tokens = d.replace(",", " ").split()
    i = 0
    pts: List[tuple] = []
    cur = (0.0, 0.0)

    def _num() -> float:
        nonlocal i
        v = float(tokens[i]); i += 1
        return v

    cmd = None
    while i < len(tokens):
        tok = tokens[i]
        if tok and tok[0].isalpha():
            cmd = tok
            i += 1
        if cmd == "M":
            cur = (_num(), _num())
            pts.append(cur)
            cmd = "L"
        elif cmd == "L":
            cur = (_num(), _num())
            pts.append(cur)
        elif cmd == "C":
            x1, y1 = _num(), _num()
            x2, y2 = _num(), _num()
            x3, y3 = _num(), _num()
            x0, y0 = cur
            for s in range(1, samples_per_curve + 1):
                tt = s / samples_per_curve
                mt = 1 - tt
                x = (mt ** 3) * x0 + 3 * (mt ** 2) * tt * x1 + 3 * mt * (tt ** 2) * x2 + (tt ** 3) * x3
                y = (mt ** 3) * y0 + 3 * (mt ** 2) * tt * y1 + 3 * mt * (tt ** 2) * y2 + (tt ** 3) * y3
                pts.append((x, y))
            cur = (x3, y3)
        elif cmd == "Z":
            i += 1
        else:
            i += 1
    return pts


_OUTRO_LETTER_POLY = _parse_outro_letter_polygon(_OUTRO_LETTER_PATH_D)

_logo_png_cache: dict = {}
_logo_png_cache_lock = threading.Lock()


def _render_logo_png(size: int, filled: bool = True) -> "Image.Image":
    """logo-full.svg ile aynı geometriyi (çemberler + harf) verilen boyutta
    tek seferlik render eder ve bellekte cache'ler. filled=True → outro'daki
    gibi dolgulu/renkli büyük logo, filled=False → watermark'taki gibi ince
    çizgili beyaz küçük ikon."""
    key = (size, filled)
    with _logo_png_cache_lock:
        cached = _logo_png_cache.get(key)
    if cached is not None:
        return cached

    scale = size / LOGO_SVG_BASE_SIZE
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if filled:
        for cx, cy, r in OUTRO_CIRCLES:
            rr = r * scale
            draw.ellipse(
                [cx * scale - rr, cy * scale - rr, cx * scale + rr, cy * scale + rr],
                fill=OUTRO_CIRCLE_COLOR + (255,),
            )
        poly = [(x * scale, y * scale) for x, y in _OUTRO_LETTER_POLY]
        draw.polygon(poly, fill=OUTRO_LETTER_COLOR + (255,))
    else:
        stroke_w = max(1, int(size * 0.018))
        color = (255, 255, 255, 210)
        for cx, cy, r in OUTRO_CIRCLES:
            rr = r * scale
            draw.ellipse(
                [cx * scale - rr, cy * scale - rr, cx * scale + rr, cy * scale + rr],
                outline=color, width=stroke_w,
            )
        poly = [(x * scale, y * scale) for x, y in _OUTRO_LETTER_POLY]
        draw.polygon(poly, outline=color, width=stroke_w)

    with _logo_png_cache_lock:
        _logo_png_cache[key] = img
    return img


OUTRO_DURATION = 3.0
OUTRO_FADE_IN = 0.6
OUTRO_BRIGHTEN = 0.45  # arka plan videosunun üzerine binen beyazlatma (logo koyu renkli olduğu için karartma yerine beyazlatma kullanılıyor — aksi halde logo koyu zeminde kayboluyor)
OUTRO_FPS = 30

# --- Animasyon zamanlaması (TSX tarafındaki spring animasyonuyla eşleşecek
# şekilde saniye cinsinden) ---
OUTRO_ANIM_LETTER_START = 0.0
OUTRO_ANIM_LETTER_DUR = 0.55        # gövde (harf) büyüme süresi
OUTRO_ANIM_CIRCLES_START = 0.40     # ilk daire bu saniyede başlar
OUTRO_ANIM_CIRCLE_STAGGER = 0.10    # her daire arasındaki gecikme
OUTRO_ANIM_CIRCLE_DUR = 0.30        # her dairenin kendi pop-up süresi
OUTRO_ANIM_OVERSHOOT = 1.15         # spring benzeri hafif "taşma" oranı


def _ease_out_back(t: float, overshoot: float = OUTRO_ANIM_OVERSHOOT) -> float:
    """Reanimated'daki withSpring'e görsel olarak yakın bir "overshoot" easing:
    0 → 1 arası ilerlerken hedefi hafifçe geçip geri oturur (pop efekti)."""
    t = max(0.0, min(1.0, t))
    c = (overshoot - 1.0) * 1.70158 + 1.0
    t2 = t - 1.0
    return 1.0 + c * (t2 ** 3) + (overshoot - 1.0) * (t2 ** 2)


def _render_logo_outro_frame(size: int, t: float) -> "Image.Image":
    """t saniyesindeki tek bir animasyon karesini çizer: harf (gövde) spring
    ile büyür, ardından yeşil daireler sırayla (staggered) pop-up yapar.
    Bu, TSX tarafındaki örnek Animated.spring akışının Python/Pillow
    karşılığıdır — aynı zamanlamayı (delay + tension/friction hissi) üretir.

    NOT: logo-full.svg'nin orijinaliyle birebir aynı stil korunuyor — dolgu
    (fill) YOK, sadece kontur (stroke). Eskiden burada Pillow'un fill= ile
    dolgulu şekiller çizdiği bir varyant vardı; artık her iki şekil de
    (harf ve daireler) outline/width ile, SVG'deki stroke-width="2" oranına
    uygun kalınlıkta çiziliyor."""
    scale = size / LOGO_SVG_BASE_SIZE
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    stroke_w = max(1, round(2 * scale))  # SVG'deki stroke-width="2" (384px tabanına göre ölçeklenir)

    # 1. Gövde (harf) ölçeği: 0 -> 1, hafif overshoot ile.
    letter_t = (t - OUTRO_ANIM_LETTER_START) / OUTRO_ANIM_LETTER_DUR
    letter_scale = _ease_out_back(letter_t) if letter_t > 0 else 0.0
    letter_scale = max(0.0, letter_scale)

    if letter_scale > 0.001:
        poly = [(x * scale, y * scale) for x, y in _OUTRO_LETTER_POLY]
        cx0 = sum(p[0] for p in poly) / len(poly)
        cy0 = sum(p[1] for p in poly) / len(poly)
        poly_scaled = [
            (cx0 + (x - cx0) * letter_scale, cy0 + (y - cy0) * letter_scale)
            for x, y in poly
        ]
        draw = ImageDraw.Draw(img)
        draw.polygon(poly_scaled, outline=OUTRO_LETTER_COLOR + (255,), width=stroke_w)

    # 2. Yeşil daireler: her biri kendi gecikmesiyle 0 -> 1 skalaya pop yapar.
    draw = ImageDraw.Draw(img)
    for idx, (cx, cy, r) in enumerate(OUTRO_CIRCLES):
        start = OUTRO_ANIM_CIRCLES_START + idx * OUTRO_ANIM_CIRCLE_STAGGER
        c_t = (t - start) / OUTRO_ANIM_CIRCLE_DUR
        if c_t <= 0:
            continue
        c_scale = _ease_out_back(c_t)
        c_scale = max(0.0, c_scale)
        if c_scale <= 0.001:
            continue
        rr = r * scale * c_scale
        if rr <= 0.05:
            continue
        px, py = cx * scale, cy * scale
        draw.ellipse([px - rr, py - rr, px + rr, py + rr], outline=OUTRO_CIRCLE_COLOR + (255,), width=stroke_w)

    return img


def _build_animated_logo_png_sequence(job_id: str, size: int) -> tuple[Path, float]:
    """Harf + daire pop-up animasyonunun tüm karelerini bir klasöre PNG olarak
    yazar. Dönen süre, son dairenin animasyonunun bittiği ana göre otomatik
    hesaplanır (yeni daire eklenirse zamanlama otomatik uzar)."""
    frames_dir = WORK_DIR / f"outro_frames_{job_id}"
    frames_dir.mkdir(parents=True, exist_ok=True)

    last_circle_end = (
        OUTRO_ANIM_CIRCLES_START
        + (len(OUTRO_CIRCLES) - 1) * OUTRO_ANIM_CIRCLE_STAGGER
        + OUTRO_ANIM_CIRCLE_DUR
    )
    anim_duration = max(last_circle_end, OUTRO_ANIM_LETTER_DUR) + 0.15  # küçük "nefes" payı
    total_frames = max(1, int(round(anim_duration * OUTRO_FPS)))

    for f in range(total_frames):
        t = f / OUTRO_FPS
        frame = _render_logo_outro_frame(size, t)
        frame.save(frames_dir / f"frame_{f:04d}.png")

    return frames_dir, anim_duration


def build_logo_outro_segment(
    job_id: str, w: int, h: int, preset: str, crf: int, handle_text: str, bg_video: Path,
) -> Path:
    """Video sonu: logo harfi + yeşil daireler gerçek bir "pop-up" animasyonu
    olarak (kare kare render edilip ffmpeg'de video akışına dönüştürülerek)
    gerçek arka plan videosunun (o reels'te kullanılan son sahne) üzerine
    bindirilir. Marka adı ve handle, animasyon bittikten sonra fade-in ile
    belirir."""
    logo_size = int(w * 0.5)
    brand_size = max(20, int(w * 0.06))
    handle_size = max(12, int(brand_size * 0.4))

    font_brand = _load_font(None, brand_size, fallback="Amiri-Bold.ttf")
    font_handle = _load_font(None, handle_size)

    brand_text = "Reisul Qurra"
    handle_clean = _clean_text(handle_text) or "@reisulqurra"

    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    bw = probe.textlength(brand_text, font=font_brand)
    hw = probe.textlength(handle_clean, font=font_handle)

    gap = int(logo_size * 0.08)
    content_h = logo_size + gap + brand_size + int(handle_size * 1.3)
    top = max(0, (h - content_h) // 2)
    logo_x = (w - logo_size) // 2

    # --- 1) Logo animasyon kare dizisini üret (harf büyür + daireler pop) ---
    frames_dir, anim_duration = _build_animated_logo_png_sequence(job_id, logo_size)

    # Her kareyi tam ekran (w x h) tuval üzerine, doğru konuma yapıştırıp
    # marka adı + handle metnini animasyon bittikten sonra ekleyerek nihai
    # outro karelerini oluştur.
    full_frames_dir = WORK_DIR / f"outro_full_frames_{job_id}"
    full_frames_dir.mkdir(parents=True, exist_ok=True)
    text_fade_start = anim_duration
    text_fade_dur = 0.35

    try:
        frame_files = sorted(frames_dir.glob("frame_*.png"))
        for fp in frame_files:
            idx = int(fp.stem.split("_")[1])
            t = idx / OUTRO_FPS
            canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            logo_frame = Image.open(fp)
            canvas.paste(logo_frame, (logo_x, top), logo_frame)

            text_t = (t - text_fade_start) / text_fade_dur
            text_alpha = max(0.0, min(1.0, text_t))
            if text_alpha > 0.001:
                d = ImageDraw.Draw(canvas)
                a = int(255 * text_alpha)
                by = top + logo_size + gap
                d.text(((w - bw) / 2, by), brand_text, font=font_brand, fill=OUTRO_LETTER_COLOR + (a,))
                hy = by + brand_size + int(handle_size * 0.5)
                d.text(((w - hw) / 2, hy), handle_clean, font=font_handle, fill=OUTRO_HANDLE_COLOR + (a,))

            canvas.save(full_frames_dir / fp.name)
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)

    # Outro toplam süresi sabit OUTRO_DURATION (3sn) — logo animasyonu + marka
    # adının fade-in'i bittikten sonra kalan süre kadar sabit durur.
    total_duration = max(OUTRO_DURATION, text_fade_start + text_fade_dur)
    hold_after_text = max(0.0, total_duration - (text_fade_start + text_fade_dur))

    # Kare sayısını hold süresi için son kareyi tekrarlayarak tamamla.
    existing_frame_count = len(list(full_frames_dir.glob("frame_*.png")))
    target_frame_count = max(existing_frame_count, int(round(total_duration * OUTRO_FPS)))
    if existing_frame_count > 0 and target_frame_count > existing_frame_count:
        last_frame_path = sorted(full_frames_dir.glob("frame_*.png"))[-1]
        for i in range(existing_frame_count, target_frame_count):
            shutil.copyfile(last_frame_path, full_frames_dir / f"frame_{i:04d}.png")

    seg_path = WORK_DIR / f"outro_{job_id}.mp4"
    logo_anim_video = WORK_DIR / f"outro_anim_{job_id}.mov"

    try:
        # --- 2) PNG kare dizisini şeffaf (RGBA) bir video akışına çevir ---
        run_ffmpeg([
            FFMPEG, "-y",
            "-framerate", str(OUTRO_FPS),
            "-i", str(full_frames_dir / "frame_%04d.png"),
            "-c:v", "qtrle",  # şeffaflığı (alpha) korur; overlay adımında kullanılacak
            str(logo_anim_video),
        ])

        # --- 3) Animasyonlu logo videosunu, gerçek arka plan sahnesinin
        # üzerine bindir (karart + overlay), sesi sessiz bırak. ---
        run_ffmpeg([
            FFMPEG, "-y",
            "-stream_loop", "-1", "-i", str(bg_video),
            "-i", str(logo_anim_video),
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-filter_complex",
            f"[0:v]trim=duration={total_duration},setpts=PTS-STARTPTS,"
            f"eq=brightness={OUTRO_BRIGHTEN}[bg];"
            f"[1:v]format=rgba,fade=t=in:st=0:d={OUTRO_FADE_IN}:alpha=1[logo];"
            f"[bg][logo]overlay=(W-w)/2:(H-h)/2:format=auto[v]",
            "-map", "[v]", "-map", "2:a",
            "-t", str(total_duration),
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            str(seg_path),
        ])
    finally:
        shutil.rmtree(full_frames_dir, ignore_errors=True)
        logo_anim_video.unlink(missing_ok=True)

    return seg_path


def _apply_audio_effect(src: Path, effect: str, effect_params: Optional[dict[str, float]] = None) -> Path:
    """Seçili ses efektini, kullanıcının inline slider'lardan girdiği ince
    ayar değerleriyle (effect_params) uygular. Değerler önce
    _resolve_audio_effect_params ile varsayılanlarla birleştirilip güvenli
    aralığa sıkıştırılır, sonra _build_audio_effect_filter ffmpeg -af
    string'ini kurar."""
    if effect == "none":
        return src

    if effect not in AUDIO_EFFECT_DEFAULTS:
        print(f"[UYARI] Bilinmeyen audio_effect='{effect}' — hiçbir ses efekti UYGULANMADI. "
              f"Geçerli değerler: {[k for k in AUDIO_EFFECT_DEFAULTS if k != 'none']}")
        return src

    resolved = _resolve_audio_effect_params(effect, effect_params or {})
    filt = _build_audio_effect_filter(effect, resolved)
    if not filt:
        return src

    param_str = ",".join(f"{k}={v:g}" for k, v in resolved.items())
    out = src.with_name(src.stem + f"_fx_{effect}" + src.suffix)
    print(f"[SES EFEKTİ] '{effect}' ({param_str}) uygulanıyor → filtre: {filt} | {src.name} -> {out.name}")
    run_ffmpeg([FFMPEG, "-y", "-i", str(src), "-af", filt, "-ar", "44100", "-ac", "2", str(out)])
    return out


def run_ffmpeg(cmd: List[str]):
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise RuntimeError(res.stderr[-2500:])


def ffprobe_duration(path: Path) -> float:
    res = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return float(json.loads(res.stdout)["format"]["duration"])


DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}


def ffprobe_has_audio(path: Path) -> bool:
    res = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_type", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    try:
        streams = json.loads(res.stdout).get("streams", [])
    except Exception:
        return False
    return len(streams) > 0


def _sync_download(url: str, dest: Path, headers: dict, timeout: int = 60):
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.stream("GET", url, timeout=timeout, headers=headers) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes(1024 * 128):
                    f.write(chunk)
    except httpx.ConnectError as e:
        raise RuntimeError(
            f"{url} adresine bağlanılamadı (DNS/ağ hatası): {e}. "
            f"Sunucunun internet/DNS/VPN/güvenlik duvarı ayarlarını kontrol et."
        )
    except httpx.TimeoutException:
        raise RuntimeError(f"{url} isteği zaman aşımına uğradı.")
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"{url} indirilemedi (HTTP {e.response.status_code}).")


BG_PREP_MAX_SECONDS = 20.0


def _prepare_bg(bg_path: Path, w: int = W, h: int = H, preset: str = "veryfast") -> Path:
    scaled = BG_CACHE / (bg_path.stem + f"_scaled_{w}x{h}.mp4")
    if scaled.exists():
        return scaled
    run_ffmpeg([
        FFMPEG, "-y", "-t", str(BG_PREP_MAX_SECONDS), "-i", str(bg_path),
        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
        "-an", "-r", "30",
        "-c:v", "libx264", "-preset", preset, "-crf", "20",
        str(scaled),
    ])
    return scaled


def render_job(job_id: str, req: RenderReq):
    try:
        jobs[job_id]["status"] = "running"
        n = len(req.ayahs)
        segments = []

        for d in (WORK_DIR, OUT_DIR, BG_CACHE):
            d.mkdir(parents=True, exist_ok=True)

        _cleanup_all()
        du = shutil.disk_usage(str(BASE_DIR))
        free_gb = du.free / (1024 ** 3)
        if free_gb < 0.3:
            raise RuntimeError(
                f"Disk alanı yetersiz (sadece {free_gb:.2f} GB boş). "
                f"Render başlatılmadan durduruldu. Sunucudaki output/work/bg_cache "
                f"klasörlerini temizle veya /api/cleanup uç noktasını çağır."
            )

        for exe, name in ((FFMPEG, "ffmpeg"), (FFPROBE, "ffprobe")):
            if not (Path(exe).exists() or shutil.which(exe)):
                raise RuntimeError(
                    f"'{exe}' bulunamadı. ffmpeg/ffprobe sistemde kurulu ve PATH'te "
                    f"olmalı, ya da ayet.py ile aynı klasörde {name}.exe olmalı."
                )

        w, h = RESOLUTION_MAP.get(req.resolution, RESOLUTION_MAP[DEFAULT_RESOLUTION])
        preset = PRESET_MAP.get(req.render_speed, PRESET_MAP[DEFAULT_RENDER_SPEED])
        crf = CRF_MAP.get(req.quality, 20)

        bg_urls = req.resolved_bg_urls()
        if not bg_urls:
            raise RuntimeError("En az bir arka plan videosu seçilmeli (bg_video_url / bg_video_urls boş).")

        jobs[job_id]["message"] = f"Arka plan video(lar)ı hazırlanıyor (0/{len(bg_urls)})…"
        jobs[job_id]["progress"] = 2
        bg_prep_lock = threading.Lock()
        bg_prep_done = {"n": 0}

        def _prepare_one_bg(url: str) -> Path:
            bg_path = _cache_path(url, ".mp4")
            if not bg_path.exists():
                _sync_download(url, bg_path, DOWNLOAD_HEADERS, timeout=120)
            scaled = _prepare_bg(bg_path, w, h, preset)
            with bg_prep_lock:
                bg_prep_done["n"] += 1
                jobs[job_id]["message"] = f"Arka plan video(lar)ı hazırlanıyor ({bg_prep_done['n']}/{len(bg_urls)})…"
                jobs[job_id]["progress"] = 2 + int(bg_prep_done["n"] / len(bg_urls) * 6)
            return scaled

        with ThreadPoolExecutor(max_workers=1) as bg_pool:
            bg_scaled_list = list(bg_pool.map(_prepare_one_bg, bg_urls))

        jobs[job_id]["message"] = "Arka plan video(lar)ı hazır — ayetler render ediliyor…"
        jobs[job_id]["progress"] = 10

        # render.com free tier: paylaşımlı tek çekirdek + 512MB RAM. Paralel ffmpeg
        # process'leri OOM/timeout'a sebep oluyordu — tek seferde 1 ayet render edilir.
        MAX_WORKERS = 1
        progress_lock = threading.Lock()
        done_count = {"n": 0}

        def _render_one(i: int, ayah):
            WORK_DIR.mkdir(parents=True, exist_ok=True)
            audio_path = WORK_DIR / f"a_{job_id}_{i}.mp3"
            fx_audio_path = audio_path
            overlay_png: Optional[Path] = None
            try:
                _sync_download(ayah.audio_url, audio_path, DOWNLOAD_HEADERS, timeout=60)

                if not ffprobe_has_audio(audio_path):
                    raise RuntimeError(
                        f"Ayet {ayah.key} için indirilen ses dosyası geçersiz/boş "
                        f"(URL yanlış olabilir): {ayah.audio_url}"
                    )

                fx_audio_path = _apply_audio_effect(audio_path, req.audio_effect, req.audio_effect_params)
                dur = ffprobe_duration(fx_audio_path) + req.pause_between

                overlay_png = draw_overlay(
                    ayah, req.style, img_w=w, img_h=h,
                    include_arabic=req.include_arabic,
                    include_meal=req.include_meal,
                    show_verse_number=req.show_verse_number,
                    watermark_text=req.watermark_text,
                )
                seg_path = WORK_DIR / f"seg_{job_id}_{i}.mp4"

                bg_for_this = bg_scaled_list[i % len(bg_scaled_list)]

                run_ffmpeg([
                    FFMPEG, "-y",
                    "-stream_loop", "-1", "-i", str(bg_for_this),
                    "-i", str(overlay_png),
                    "-i", str(fx_audio_path),
                    "-filter_complex", "[0:v][1:v]overlay=0:0[v]",
                    "-map", "[v]", "-map", "2:a",
                    "-t", str(dur),
                    "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                    "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                    str(seg_path),
                ])

                with progress_lock:
                    done_count["n"] += 1
                    jobs[job_id]["progress"] = 10 + int(done_count["n"] / n * 80)
                    jobs[job_id]["message"] = f"Ayet {done_count['n']}/{n} tamamlandı"

                return i, seg_path, dur, bg_for_this
            finally:
                if overlay_png is not None:
                    overlay_png.unlink(missing_ok=True)
                audio_path.unlink(missing_ok=True)
                if fx_audio_path != audio_path:
                    fx_audio_path.unlink(missing_ok=True)

        results: dict[int, tuple[Path, float, Path]] = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(_render_one, i, ayah): i for i, ayah in enumerate(req.ayahs)}
            for fut in as_completed(futures):
                i, seg_path, dur, bg_used = fut.result()
                results[i] = (seg_path, dur, bg_used)

        segments = [results[i][0] for i in range(n)]
        durations = [results[i][1] for i in range(n)]
        last_bg_video = results[n - 1][2] if n else bg_scaled_list[0]

        if req.add_logo_outro:
            jobs[job_id]["message"] = "Logo outro render ediliyor…"
            jobs[job_id]["progress"] = 96
            handle_text = (req.watermark_text or "@reisulqurra").strip() or "@reisulqurra"
            outro_seg = build_logo_outro_segment(job_id, w, h, preset, crf, handle_text, last_bg_video)
            segments.append(outro_seg)
            durations.append(ffprobe_duration(outro_seg))

        jobs[job_id]["message"] = "Geçiş efektleriyle birleştiriliyor"
        jobs[job_id]["progress"] = 95

        out_path = OUT_DIR / f"reels_{job_id}.mp4"

        FADE = 0.3

        if len(segments) == 1:
            run_ffmpeg([
                FFMPEG, "-y", "-i", str(segments[0]),
                "-c", "copy", "-movflags", "+faststart",
                str(out_path),
            ])
        else:
            use_fast_concat = (req.render_speed == "fast")

            if use_fast_concat:
                concat_list_path = WORK_DIR / f"concat_{job_id}.txt"
                with open(concat_list_path, "w", encoding="utf-8") as f:
                    for p in segments:
                        f.write(f"file '{p.resolve().as_posix()}'\n")
                run_ffmpeg([
                    FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list_path),
                    "-c", "copy", "-movflags", "+faststart",
                    str(out_path),
                ])
                concat_list_path.unlink(missing_ok=True)
            else:
                inputs: List[str] = []
                for p in segments:
                    inputs += ["-i", str(p)]

                filter_parts: List[str] = []
                v_prev, a_prev = "0:v", "0:a"
                running_len = durations[0]
                for i in range(1, len(segments)):
                    v_out, a_out = f"v{i}", f"a{i}"
                    offset = max(running_len - FADE, 0.0)
                    filter_parts.append(
                        f"[{v_prev}][{i}:v]xfade=transition=fade:duration={FADE}:offset={offset:.3f}[{v_out}]"
                    )
                    filter_parts.append(
                        f"[{a_prev}][{i}:a]acrossfade=d={FADE}:c1=tri:c2=tri[{a_out}]"
                    )
                    running_len = running_len + durations[i] - FADE
                    v_prev, a_prev = v_out, a_out

                run_ffmpeg([
                    FFMPEG, "-y", *inputs,
                    "-filter_complex", ";".join(filter_parts),
                    "-map", f"[{v_prev}]", "-map", f"[{a_prev}]",
                    "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                    "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                    "-movflags", "+faststart",
                    str(out_path),
                ])

        for p in segments:
            p.unlink(missing_ok=True)

        jobs[job_id].update(status="done", progress=100, message="Bitti", file=f"/output/{out_path.name}")
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        jobs[job_id].update(status="error", message=str(e), traceback=tb)
    finally:
        try:
            for p in WORK_DIR.glob(f"*{job_id}*"):
                if BG_CACHE in p.parents:
                    continue
                if p.is_file():
                    p.unlink(missing_ok=True)
        except Exception as cleanup_err:
            print(f"[UYARI] work/ temizliği sırasında hata: {cleanup_err}")

        _cleanup_all()


@app.post("/api/render")
async def start_render(req: RenderReq, background_tasks: BackgroundTasks):
    if not req.ayahs:
        raise HTTPException(400, "ayahs boş olamaz")
    if not req.resolved_bg_urls():
        raise HTTPException(400, "En az bir arka plan videosu seçilmeli (bg_video_url / bg_video_urls)")
    job_id = uuid.uuid4().hex
    jobs[job_id] = {"status": "queued", "progress": 0, "message": ""}
    background_tasks.add_task(render_job, job_id, req)
    return {"job_id": job_id}


@app.get("/api/render/{job_id}")
async def render_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "job bulunamadı")
    return jobs[job_id]


def _wipe_dir_contents(d: Path):
    for p in d.iterdir():
        if p == BG_CACHE:
            continue
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        except Exception as e:
            print(f"[UYARI] {p} silinemedi: {e}")


@app.delete("/api/render/{job_id}")
async def render_cleanup(job_id: str):
    jobs.pop(job_id, None)
    _wipe_dir_contents(OUT_DIR)
    _wipe_dir_contents(WORK_DIR)
    return {"deleted": True}


@app.get("/output/{filename}")
async def serve_output_video(filename: str):
    file_path = OUT_DIR / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "Dosya bulunamadı")
    return FileResponse(
        file_path,
        media_type="video/mp4",
        filename=filename,
        background=BackgroundTask(_wipe_all_mp4s),
    )


if (BASE_DIR / "qul_downloads").exists():
    app.mount("/qul_downloads", StaticFiles(directory=str(BASE_DIR / "qul_downloads")), name="qul_downloads")


@app.get("/")
async def root():
    return FileResponse(BASE_DIR / "ayet.html")


@app.get("/health")
async def health():
    """Hafif keep-alive/uyandırma endpoint'i. Mobil uygulama, backend'i
    (render.com free tier gibi uyku moduna geçen ortamlarda) ayakta tutmak
    için bunu periyodik olarak çağırır. Dosya okumaz, DB'ye dokunmaz —
    sadece işlemin ayakta olduğunu doğrular."""
    return {"status": "ok", "time": time.time()}


@app.get("/render.html")
async def render_page():
    return FileResponse(BASE_DIR / "render.html")