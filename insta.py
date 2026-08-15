"""
Hafızlık Reels Studio — Backend
================================
reels.html frontend'ini birebir (aynı endpoint sözleşmesiyle) besleyen FastAPI
sunucusu. İki iş yapar:

1) /api/generate  -> reels.html içindeki THEMES şablonlarından birini seçer
   (renk / font / animasyon / dekor / hizalama / satır sayısı SABİT kalır),
   sadece `lines[].text` alanlarını konuya göre Gemini ile YENİDEN YAZAR.
   Yani "tema birebir, içerik AI ile güncel" mantığı budur.

2) /api/render    -> frontend'in gönderdiği (slideToBackend formatındaki)
   sahneleri gerçek bir MP4'e render eder: her sahne için Pillow ile kare
   kare görsel (metin animasyonlu), edge-tts ile seslendirme, ffmpeg ile
   xfade/acrossfade geçişli tek video. İş arka planda job_id ile takip edilir.

Çalıştırma:
    pip install -r requirements.txt
    # .env içine GEMINI_API_KEY=... yaz
    uvicorn main:app --host 0.0.0.0 --port 8000

reels.html'deki API_BASE değişkenini bu sunucunun adresine göre ayarla.
"""
import os
import re
import json
import math
import random
import shutil
import string
import asyncio
import logging
import subprocess
import threading
import traceback
import uuid
from pathlib import Path
from typing import List, Optional, Literal

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from PIL import Image, ImageDraw, ImageFont, ImageSequence
import edge_tts

# qrcode opsiyonel bir bağımlılıktır: kuruluysa outro'da App Store/Google Play
# QR kodu render edilir; kurulu değilse sistem sessizce QR'sız devam eder
# (yalnızca metin rozetleri gösterilir), hiçbir şey çökmez.
try:
    import qrcode
    QRCODE_AVAILABLE = True
except ImportError:
    QRCODE_AVAILABLE = False

# ──────────────────────────────────────────────────────────────────────────
# Kurulum / sabitler
# ──────────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.resolve()
load_dotenv(BASE_DIR / ".env")

WORK_DIR = BASE_DIR / "work"
OUT_DIR = BASE_DIR / "output"
FONT_DIR = BASE_DIR / "fonts"
GIF_CACHE_DIR = BASE_DIR / "gif_cache"
# Uygulama ekran görüntüleri (app-ozellik.json içindeki gorsel_url alanlarının
# işaret ettiği dosyalar) buradan servis edilir. Örn. gorsel_url="home_agac.png"
# ise dosya screenshots/home_agac.png olarak beklenir. Klasör boşsa/dosya
# yoksa sistem GIF akışına sessizce geri düşer (mevcut davranış bozulmaz).
SCREENSHOTS_DIR = BASE_DIR / "screenshots"
# Reels'in kapanışında gösterilecek mağaza rozetleri (App Store / Google Play)
# ve QR kod görseli — CTA/outro sahnesini güçlendirmek için opsiyonel varlıklar.
ASSETS_DIR = BASE_DIR / "assets"
for d in (WORK_DIR, OUT_DIR, FONT_DIR, GIF_CACHE_DIR, SCREENSHOTS_DIR, ASSETS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────
# "Kur'an — Mushaf Okuması" teması — mushaf_viewer/ klasörüne deneme.html'i
# (reels.html olarak) ve onun data/ klasörünü KOPYALAMAN gerekiyor:
#   insta.py
#   mushaf_viewer/
#     reels.html      <- deneme.html'in kendisi (dosya adı reels.html OLMALI)
#     data/            <- deneme.html'in fetch ettiği QuranData/fonts vb.
# render_mushaf_job(), Playwright ile bu klasörü (aşağıdaki /mushaf-viewer
# static mount'u üzerinden, AYNI sunucu içinde) headless tarayıcıda açar;
# dışarıdan tarayıcıyla ziyaret edilmesi GEREKMEZ, sadece render motoru
# için var. MUSHAF_VIEWER_URL .env'den override edilebilir (ör. sunucu
# başka bir host/port'ta dinliyorsa).
MUSHAF_VIEWER_DIR = BASE_DIR / "mushaf_viewer"
MUSHAF_VIEWER_DIR.mkdir(parents=True, exist_ok=True)
MUSHAF_VIEWER_URL = os.environ.get("MUSHAF_VIEWER_URL", "http://127.0.0.1:8000/mushaf-viewer/mushaf-render.html")

# ──────────────────────────────────────────────────────────────────────────
# "Kur'an — Bil Bakalım Quiz" teması — quiz.html, AYRI bir quiz_viewer/
# klasörü yerine doğrudan mushaf_viewer/ İÇİNE konur (mushaf-render.html ile
# aynı klasör), böylece ikisi aynı data/ klasörünü (QuranData, fontlar)
# hiçbir kopyalama/symlink yapmadan paylaşır:
#   insta.py
#   mushaf_viewer/
#     data/              <- ikisi de BURADAN okur
#     mushaf-render.html
#     quiz.html          <- BU dosyanın kendisi buraya konur
# render_quiz_job(), Playwright ile bunu (/mushaf-viewer static mount'u
# üzerinden, mushaf render'ıyla AYNI mount) headless tarayıcıda açar.
QUIZ_VIEWER_URL = os.environ.get("QUIZ_VIEWER_URL", "http://127.0.0.1:8000/mushaf-viewer/quiz.html")

# ──────────────────────────────────────────────────────────────────────────
# Logo — SVG dosyasına/cairosvg'ye bağımlı olmadan, geometri doğrudan koda
# gömülü (daireler + gövde bezier path'i), Pillow ile çiziliyor.
# ──────────────────────────────────────────────────────────────────────────
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
    """Basit bir 'M/L/C/Z' bezier path parser'ı: SVG path verisini, Pillow'un
    polygon() ile çizebileceği düz bir nokta listesine örnekler."""
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

# Watermark: sahnenin köşesinde sürekli görünen küçük logo.
WATERMARK_WIDTH_RATIO = 0.11   # video genişliğinin oranı
WATERMARK_MARGIN_RATIO = 0.045
WATERMARK_OPACITY = 0.85
# Outro: video sonuna eklenen, logonun ortada büyüyerek belirdiği sahne.
OUTRO_DURATION = 2.6
OUTRO_BG = "#111111"
OUTRO_LOGO_WIDTH_RATIO = 0.42
# NOT: app-ozellik.json içindeki "uygulama" alanı "ReisulQurra" — outro'daki
# marka adı bununla tutarlı olsun diye buradan senkron ediliyor (aşağıda
# APP_FEATURES_DATA yüklendikten sonra override edilir).
APP_NAME = "ReisulQurra"

# Intro: videonun başına eklenen, aynı logo animasyonunun tekrarı.
INTRO_DURATION = 2.6
INTRO_BG = OUTRO_BG
INTRO_LOGO_WIDTH_RATIO = OUTRO_LOGO_WIDTH_RATIO

# ── Mağaza rozetleri / QR (CTA güçlendirme) ──────────────────────────────
# Gerçek App Store / Google Play linkleri .env üzerinden verilir (kişiye/
# projeye özel oldukları için koda gömülmez). Tanımlı değillerse outro'da
# rozet+QR bölümü sessizce atlanır, sadece logo+isim gösterilir (mevcut
# davranış). CTA sahnesi outro'nun SONUNA eklenir, mevcut logo animasyonuna
# dokunmaz.
APP_STORE_URL = os.environ.get("APP_STORE_URL", "")
GOOGLE_PLAY_URL = os.environ.get("GOOGLE_PLAY_URL", "")
CTA_DURATION = 2.2       # mağaza rozetleri + QR'ın gösterildiği ek sahne süresi
CTA_BG = OUTRO_BG

# ── Sahne zamanlaması (React Native useEffect'teki spring/timing sırasının
# birebir karşılığı):
#   1) t=0.00s     harf (gövde) spring ile büyür
#   2) t=0.10s...  daireler sırayla (stagger) pop-up ile büyür
#   3) t=0.55s     uygulama adı fade-in olur
#   4) son 0.5s    her şey birlikte fade-out olur
_LOGO_STAGE_LETTER_DUR = 0.55      # harf büyüme animasyonunun süresi
_LOGO_STAGE_CIRCLE_START = 0.12    # ilk dairenin başlama anı
_LOGO_STAGE_CIRCLE_STAGGER = 0.045 # her daire arasındaki gecikme
_LOGO_STAGE_CIRCLE_DUR = 0.35      # tek bir dairenin pop-up süresi
_LOGO_STAGE_NAME_START = 0.62      # yazının fade-in başlama anı
_LOGO_STAGE_NAME_DUR = 0.45        # yazının fade-in süresi
_LOGO_STAGE_FADEOUT_DUR = 0.5      # sahnenin sonundaki genel fade-out süresi

log = logging.getLogger("reels-backend")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DEFAULT_VOICE = os.environ.get("TTS_VOICE", "tr-TR-EmelNeural")

# ──────────────────────────────────────────────────────────────────────────
# ElevenLabs TTS — edge-tts'e (Microsoft) ALTERNATİF ikinci bir seslendirme
# sağlayıcısı. .env dosyasından okunur, koda GÖMÜLMEZ. Anahtar yoksa
# ElevenLabs sesleri kataloğa hiç eklenmez ve sistem sessizce yalnızca
# edge-tts ile çalışmaya devam eder (mevcut davranış bozulmaz).
# ──────────────────────────────────────────────────────────────────────────
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
ELEVENLABS_MODEL_ID = os.environ.get("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
ELEVENLABS_TIMEOUT_SECONDS = 25.0

# ──────────────────────────────────────────────────────────────────────────
# Klipy (GIF) — .env dosyasından okunur, koda GÖMÜLMEZ.
# https://klipy.com API'si; anahtar yoksa GIF özelliği sessizce devre dışı
# kalır (render/generate çökmez, sadece GIF'siz devam eder).
# ──────────────────────────────────────────────────────────────────────────
KLIPY_API_KEY = os.environ.get("KLIPY_API_KEY", "")
KLIPY_SEARCH_URL = "https://api.klipy.co/api/v1/{key}/gifs/search"
KLIPY_TIMEOUT_SECONDS = 12.0

# ──────────────────────────────────────────────────────────────────────────
# Uygulama Özellikleri (app-ozellik.json) — reels metinlerinin ReisulQurra'nın
# gerçek özelliklerine dayanarak üretilebilmesi için Post.py ile aynı mantıkla
# okunur. Kullanıcı AI sekmesinden bir özellik seçebilir (ya da "random"/"none"
# bırakabilir); seçilen özellik _build_prompt() içine enjekte edilir.
# ──────────────────────────────────────────────────────────────────────────
def _load_app_features() -> tuple[str, dict, list[dict]]:
    json_path = BASE_DIR / "app-ozellik.json"
    if not json_path.exists():
        log.warning("app-ozellik.json bulunamadı (%s) — özellik bazlı üretim devre dışı.", json_path)
        return "", {}, []
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.warning("app-ozellik.json okunamadı: %s", e)
        return "", {}, []

    flat: list[dict] = []
    for modul_key, modul in data.get("ozellikler", {}).items():
        for oz in modul.get("ozellikler", []):
            oz_copy = dict(oz)
            oz_copy["_modul"] = modul.get("baslik", modul_key)
            flat.append(oz_copy)

    lines: list[str] = []
    lines.append(f"Uygulama: {data.get('uygulama', 'ReisulQurra')} (Versiyon: {data.get('versiyon', '1.0')})")
    for modul_key, modul in data.get("ozellikler", {}).items():
        baslik = modul.get("baslik", modul_key)
        lines.append(f"\n### {baslik}")
        for oz in modul.get("ozellikler", []):
            lines.append(
                f"  [{oz.get('id','')}] {oz.get('ad','')} — "
                f"{oz.get('aciklama','')} (Teknik: {oz.get('teknik','')})"
            )

    return "\n".join(lines), data, flat


APP_FEATURES_TEXT, APP_FEATURES_DATA, APP_FEATURES_FLAT = _load_app_features()
if APP_FEATURES_DATA.get("uygulama"):
    APP_NAME = APP_FEATURES_DATA["uygulama"]

# ──────────────────────────────────────────────────────────────────────────
# Seslendirme (TTS) — sahnelerin seslendirilmesinde seçilebilecek ses
# kataloğu. edge_tts.list_voices() ağ üzerinden (Microsoft servisine) her
# çağrıda yeniden sorgu atar; bunun yerine sık kullanılan, kaliteli
# nöral sesleri sabit bir liste olarak tutuyoruz — hem /api/voices anında
# döner hem de frontend'de dil/cinsiyet etiketiyle gösterilebilir.
# ──────────────────────────────────────────────────────────────────────────
VOICE_CATALOG: list[dict] = [
    {"id": "tr-TR-EmelNeural", "label": "Emel (Kadın)", "lang": "Türkçe", "gender": "female", "provider": "edge"},
    {"id": "tr-TR-AhmetNeural", "label": "Ahmet (Erkek)", "lang": "Türkçe", "gender": "male", "provider": "edge"},
    {"id": "en-US-AriaNeural", "label": "Aria (Kadın)", "lang": "İngilizce (ABD)", "gender": "female", "provider": "edge"},
    {"id": "en-US-GuyNeural", "label": "Guy (Erkek)", "lang": "İngilizce (ABD)", "gender": "male", "provider": "edge"},
    {"id": "en-US-JennyNeural", "label": "Jenny (Kadın)", "lang": "İngilizce (ABD)", "gender": "female", "provider": "edge"},
    {"id": "en-GB-SoniaNeural", "label": "Sonia (Kadın)", "lang": "İngilizce (UK)", "gender": "female", "provider": "edge"},
    {"id": "en-GB-RyanNeural", "label": "Ryan (Erkek)", "lang": "İngilizce (UK)", "gender": "male", "provider": "edge"},
    {"id": "de-DE-KatjaNeural", "label": "Katja (Kadın)", "lang": "Almanca", "gender": "female", "provider": "edge"},
    {"id": "de-DE-ConradNeural", "label": "Conrad (Erkek)", "lang": "Almanca", "gender": "male", "provider": "edge"},
    {"id": "fr-FR-DeniseNeural", "label": "Denise (Kadın)", "lang": "Fransızca", "gender": "female", "provider": "edge"},
    {"id": "es-ES-ElviraNeural", "label": "Elvira (Kadın)", "lang": "İspanyolca", "gender": "female", "provider": "edge"},
    {"id": "ar-SA-HamedNeural", "label": "Hamed (Erkek)", "lang": "Arapça", "gender": "male", "provider": "edge"},
]

# ElevenLabs "premade" sesleri — free-tier bir API anahtarıyla da erişilebilen,
# hesaba göre değişmeyen sabit voice_id'ler. eleven_multilingual_v2 modeliyle
# Türkçe dahil çok sayıda dili aynı ses üzerinden okuyabilirler; bu yüzden
# ayrı bir "lang" etiketi yerine "Çok Dilli" olarak işaretlendi.
ELEVENLABS_VOICE_CATALOG: list[dict] = [
    {"id": "21m00Tcm4TlvDq8ikWAM", "label": "Rachel (Kadın)", "lang": "Çok Dilli", "gender": "female", "provider": "elevenlabs"},
    {"id": "pNInz6obpgDQGcFmaJgB", "label": "Adam (Erkek)", "lang": "Çok Dilli", "gender": "male", "provider": "elevenlabs"},
    {"id": "EXAVITQu4vr4xnSDxMaL", "label": "Bella (Kadın)", "lang": "Çok Dilli", "gender": "female", "provider": "elevenlabs"},
    {"id": "ErXwobaYiN019PkySvjV", "label": "Antoni (Erkek)", "lang": "Çok Dilli", "gender": "male", "provider": "elevenlabs"},
    {"id": "yoZ06aMxZJJ28mfd3POQ", "label": "Sam (Erkek)", "lang": "Çok Dilli", "gender": "male", "provider": "elevenlabs"},
    {"id": "MF3mGyEYCl7XYWbV9V6O", "label": "Elli (Kadın)", "lang": "Çok Dilli", "gender": "female", "provider": "elevenlabs"},
]

# Tüm sesler tek bir haritada birleştirilir; ElevenLabs anahtarı yoksa o
# katalog listeye hiç eklenmez (mevcut edge-tts davranışı bozulmaz).
ALL_VOICES: list[dict] = list(VOICE_CATALOG)
if ELEVENLABS_API_KEY:
    ALL_VOICES += ELEVENLABS_VOICE_CATALOG
VOICE_MAP: dict[str, dict] = {v["id"]: v for v in ALL_VOICES}
VOICE_IDS = set(VOICE_MAP.keys())

W, H = 1080, 1920
RESOLUTION_MAP: dict[str, tuple[int, int]] = {
    "480p": (480, 854),
    "720p": (720, 1280),
    "1080p": (1080, 1920),
}
SPEED_FPS = {"fast": 24, "balanced": 30, "quality": 30}
SPEED_PRESET = {"fast": "veryfast", "balanced": "fast", "quality": "slow"}
QUALITY_CRF = {"high": 18, "medium": 23, "low": 28}

SAFE_TOP = 220
SAFE_BOTTOM = 380
# Reels/TikTok'ta 1080px genişlikte gerçek okunabilirlik için taban punto.
MIN_FONT_SIZE = 52
ANIM_DUR = 0.35  # her sahnenin başındaki metin animasyonu süresi (sn)
TRANS_DUR_MAX = 0.35  # sahneler arası geçiş üst sınırı (sn)

# ── Reels/TikTok formatı için toplam video süresi hedefi ──
MIN_TOTAL_VIDEO_DURATION = 15.0
MAX_TOTAL_VIDEO_DURATION = 60.0

# ── GIF yerleşimi (sahnenin boş üst/alt bandı) ──
# Metin bloğu, güvenli bölgenin (SAFE_TOP..H-SAFE_BOTTOM) ne kadarını
# kaplıyorsa, kalan boşluğa GIF yerleştirilir. Bir sahnenin GIF'e uygun
# sayılması için: en fazla 2 metin satırı olması VE metin bloğunun
# kullanılabilir dikey alanın belirli bir oranından azını kaplaması gerekir.
GIF_MAX_LINES = 2
GIF_MIN_FREE_RATIO = 0.30   # kullanılabilir alanın en az %30'u boş olmalı
GIF_BAND_MARGIN = 28        # gif ile metin arasındaki boşluk (px, 1080 tabanında)
GIF_MAX_HEIGHT_RATIO = 0.85  # gif, kendi bandının en fazla bu oranını kaplasın
# Sahne süresi, GIF'in doğal (indirildiği haldeki) toplam süresinden kısaysa,
# GIF'in sadece en durağan/başlangıç kısmı gösterilip "resim gibi" donuk
# kalmasın diye zaman çizelgesi hızlandırılır (tam bir döngü sahne süresine
# sığdırılır). Aşırı hızlanmayı (göz yorucu titreme) önlemek için bir üst
# sınır konur — bu sınırın üzerinde GIF tam döngüyü tamamlamasa da normalden
# GIF_MAX_SPEEDUP kat daha hızlı, dolayısıyla belirgin şekilde hareketli akar.
GIF_MAX_SPEEDUP = 3.0

MODELS: dict[str, dict] = {
    "gemini-3.1-flash-lite": {"id": "models/gemini-3.1-flash-lite", "label": "Gemini 3.1 Flash Lite", "rpm": 15, "tpm": 250000, "emoji": "⚡"},
    "gemini-2.5-flash-lite": {"id": "models/gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash Lite", "rpm": 10, "tpm": 250000, "emoji": "⚖️"},
    "gemini-2.5-flash":      {"id": "models/gemini-2.5-flash",      "label": "Gemini 2.5 Flash",      "rpm": 5,  "tpm": 250000, "emoji": "🧠"},
    "gemini-3-flash":        {"id": "models/gemini-3-flash",        "label": "Gemini 3 Flash",        "rpm": 5,  "tpm": 250000, "emoji": "🔥"},
    "gemini-3.5-flash":      {"id": "models/gemini-3.5-flash",      "label": "Gemini 3.5 Flash",      "rpm": 5,  "tpm": 250000, "emoji": "🚀"},
}
DEFAULT_MODEL = "gemini-2.5-flash-lite"

with open(BASE_DIR / "themes.json", "r", encoding="utf-8") as f:
    THEMES: list[dict] = json.load(f)
log.info(f"[BAŞLANGIÇ] {len(THEMES)} tema, toplam {sum(len(t['slides']) for t in THEMES)} sahne şablonu yüklendi.")

if not KLIPY_API_KEY:
    log.warning(
        "[BAŞLANGIÇ] ⚠️ KLIPY_API_KEY tanımlı değil (.env dosyasına ekleyin). "
        "GIF özelliği devre dışı kalacak; sahneler GIF'siz üretilecek."
    )

if not ELEVENLABS_API_KEY:
    log.info(
        "[BAŞLANGIÇ] ℹ️ ELEVENLABS_API_KEY tanımlı değil (.env dosyasına ekleyin). "
        "Seslendirme yalnızca edge-tts (Microsoft) ile yapılacak; ElevenLabs "
        "sesleri /api/voices listesinde görünmeyecek."
    )
else:
    log.info(f"[BAŞLANGIÇ] ElevenLabs TTS etkin — {len(ELEVENLABS_VOICE_CATALOG)} ses eklendi.")

# ──────────────────────────────────────────────────────────────────────────
# Font kaynağı sırası: ÖNCE her zaman `fonts/` klasöründeki dosyalar denenir
# (bkz. _resolve_font_path / _found_fonts). Bir tema fontu (ör. "Anton")
# fonts/ klasöründe YOKSA, sistem fontuna düşmeden ÖNCE aşağıdaki
# PROJE_FONT_FALLBACK_ORDER listesindeki (yine fonts/ klasöründen, kullanıcının
# sağladığı 15 dosyalık set) bir fontla devam edilir. Sistem yollarındaki
# (_SYSTEM_FONT_FALLBACKS) TrueType fontlar artık gerçek EN SON çare — sadece
# fonts/ klasöründe hiçbir uygun dosya kalmamışsa devreye girer.
#
# NOT: Bu sözlük, aşağıdaki _found_fonts taramasından ÖNCE tanımlanmak
# ZORUNDA (uyarı mesajları PROJECT_FONT_FILES.keys()'i kullanıyor) — sıra
# bozulursa modül import edilirken NameError ile sunucu hiç açılmaz.
# ──────────────────────────────────────────────────────────────────────────
PROJECT_FONT_FILES = {
    "Inter.ttf": "latin",
    "Inter-Bold.ttf": "latin",
    "Lato.ttf": "latin",
    "Lato-Bold.ttf": "latin",
    "Nunito.ttf": "latin",
    "Nunito-Bold.ttf": "latin",
    "OpenDyslexic.otf": "latin",
    "OpenDyslexic-Bold.otf": "latin",
    "Poppins.ttf": "latin",
    "Poppins-Bold.ttf": "latin",
    "Roboto.ttf": "latin",
    "Roboto-Bold.ttf": "latin",
    "Rubik-Bold.ttf": "latin",
    "Rubik-Regular.ttf": "latin",
    "SpaceMono.ttf": "latin",
}

_found_fonts = sorted(p.name for p in FONT_DIR.glob("*.[tT][tT][fF]")) + \
               sorted(p.name for p in FONT_DIR.glob("*.[oO][tT][fF]"))
log.info(f"[BAŞLANGIÇ] fonts/ klasöründe {len(_found_fonts)} font dosyası bulundu.")
if not _found_fonts:
    log.warning(
        "[BAŞLANGIÇ] ⚠️ fonts/ klasörü BOŞ! Sistem fallback fontuna düşülecek; "
        "bulunamazsa PIL'in 10px'lik minik varsayılan bitmap fontuna düşülür ve "
        "METİNLER PRATİKTE GÖRÜNMEZ OLUR. fonts/ klasörüne aşağıdaki proje font "
        "setini eklemeniz şiddetle önerilir:\n  " + ", ".join(sorted(PROJECT_FONT_FILES.keys()))
    )
else:
    _missing_project_fonts = sorted(set(PROJECT_FONT_FILES.keys()) - set(_found_fonts))
    if _missing_project_fonts:
        log.warning(
            "[BAŞLANGIÇ] ⚠️ Proje font setinden eksik dosyalar var, bunlar için "
            f"sistem fontuna düşülebilir: {', '.join(_missing_project_fonts)}"
        )

# Sistem geneline kurulu fontlar: yalnızca fonts/ klasöründe (yukarıdaki
# PROJECT_FONT_FILES dahil) HİÇBİR şey bulunamazsa denenir.
_SYSTEM_FONT_FALLBACKS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
]

# Tema fontu adı -> fonts/ klasöründeki dosya adında aranacak anahtar
# kelime(ler). Kullanıcının verdiği 15 dosyalık proje font setiyle
# (Inter, Lato, Nunito, OpenDyslexic, Poppins, Roboto, Rubik, SpaceMono)
# birebir eşleşenler doğrudan; Anton/Oswald/Bebas Neue/Montserrat gibi
# proje setinde bulunmayan tema fontları için ise EN YAKIN proje fontuna
# (kalın/geometrik afiş fontları -> Poppins-Bold; dar/uzun başlık fontları
# -> Rubik-Bold) bir yedek eşleme eklendi — böylece fonts/ klasöründe o
# özel tema fontu bulunamadığında sistem fontuna düşmeden ÖNCE proje
# setinden görsel olarak en yakın kalın font kullanılır.
_FONT_ALIASES = {
    # ── Proje font seti (birebir) ──
    "inter": ["inter"],
    "lato": ["lato"],
    "nunito": ["nunito"],
    "opendyslexic": ["opendyslexic"],
    "poppins": ["poppins"],
    "roboto": ["roboto"],
    "rubik": ["rubik"],
    "space mono": ["spacemono"],
    "spacemono": ["spacemono"],
    # ── Tema şablonlarında kullanılan, proje setinde birebir karşılığı
    #    olmayan fontlar: en yakın proje fontuna yönlendirilir ──
    "anton": ["poppins", "rubik"],
    "oswald": ["rubik", "poppins"],
    "bebas neue": ["poppins", "rubik"],
    "montserrat": ["poppins", "inter"],
}
# fonts/ klasöründe PROJECT_FONT_FILES'daki dosyalardan hiç bulunamazsa,
# get_font() bu sırayla (proje seti önceliğiyle) genel bir yedek dener.
_PROJECT_FONT_FALLBACK_ORDER = [
    "Poppins-Bold.ttf", "Inter-Bold.ttf", "Rubik-Bold.ttf", "Roboto-Bold.ttf",
    "Nunito-Bold.ttf", "Lato-Bold.ttf", "SpaceMono.ttf", "OpenDyslexic-Bold.otf",
]
_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

_logo_png_cache: dict = {}
_logo_png_cache_lock = threading.Lock()


def _render_logo_master(supersample: int = 4) -> Image.Image:
    base = int(LOGO_SVG_BASE_SIZE)
    ss = base * supersample
    layer = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    scl = supersample

    for cx, cy, r in OUTRO_CIRCLES:
        x, y, rr = cx * scl, cy * scl, r * scl
        draw.ellipse([x - rr, y - rr, x + rr, y + rr], fill=OUTRO_CIRCLE_COLOR + (255,))

    if _OUTRO_LETTER_POLY:
        poly = [(x * scl, y * scl) for x, y in _OUTRO_LETTER_POLY]
        draw.polygon(poly, fill=OUTRO_LETTER_COLOR + (255,))

    return layer.resize((base, base), Image.LANCZOS)


def _render_logo_master_staged(letter_scale: float, circle_scales: List[float], supersample: int = 4) -> Image.Image:
    base = int(LOGO_SVG_BASE_SIZE)
    ss = base * supersample
    layer = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    scl = supersample

    for (cx, cy, r), cs in zip(OUTRO_CIRCLES, circle_scales):
        if cs <= 0:
            continue
        rr = r * scl * max(0.0, cs)
        x, y = cx * scl, cy * scl
        draw.ellipse([x - rr, y - rr, x + rr, y + rr], fill=OUTRO_CIRCLE_COLOR + (255,))

    if _OUTRO_LETTER_POLY and letter_scale > 0:
        xs = [p[0] for p in _OUTRO_LETTER_POLY]
        ys = [p[1] for p in _OUTRO_LETTER_POLY]
        pivot_x = (min(xs) + max(xs)) / 2.0
        pivot_y = (min(ys) + max(ys)) / 2.0
        ls = max(0.0, letter_scale)
        poly = [
            ((pivot_x + (x - pivot_x) * ls) * scl, (pivot_y + (y - pivot_y) * ls) * scl)
            for x, y in _OUTRO_LETTER_POLY
        ]
        draw.polygon(poly, fill=OUTRO_LETTER_COLOR + (255,))

    return layer.resize((base, base), Image.LANCZOS)


def get_logo(width_px: int) -> Optional[Image.Image]:
    width_px = max(1, int(width_px))
    with _logo_png_cache_lock:
        cached = _logo_png_cache.get(width_px)
    if cached is not None:
        return cached
    try:
        master = _render_logo_master()
        h_px = max(1, int(width_px * master.height / master.width))
        img = master.resize((width_px, h_px), Image.LANCZOS)
    except Exception as e:
        log.error(f"[LOGO] çizim hatası: {e}")
        return None
    with _logo_png_cache_lock:
        _logo_png_cache[width_px] = img
    return img


_logo_staged_cache: dict = {}
_logo_staged_cache_lock = threading.Lock()


def get_logo_staged(width_px: int, letter_scale: float, circle_scales: List[float]) -> Optional[Image.Image]:
    width_px = max(1, int(width_px))
    ls_key = round(max(0.0, letter_scale), 2)
    cs_key = tuple(round(max(0.0, c), 2) for c in circle_scales)
    cache_key = (width_px, ls_key, cs_key)
    with _logo_staged_cache_lock:
        cached = _logo_staged_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        master = _render_logo_master_staged(ls_key, list(cs_key))
        h_px = max(1, int(width_px * master.height / master.width))
        img = master.resize((width_px, h_px), Image.LANCZOS)
    except Exception as e:
        log.error(f"[LOGO] aşamalı çizim hatası: {e}")
        return None
    with _logo_staged_cache_lock:
        if len(_logo_staged_cache) > 4000:
            _logo_staged_cache.clear()
        _logo_staged_cache[cache_key] = img
    return img


def _resolve_font_path(family: str) -> Optional[Path]:
    key = (family or "").strip().lower()
    needles = _FONT_ALIASES.get(key, [key.replace(" ", "")])
    for name in _found_fonts:
        norm = re.sub(r"[^a-z0-9]", "", name.lower())
        for needle in needles:
            if needle and needle in norm:
                if "bold" in norm or "black" in norm or "-" not in name:
                    return FONT_DIR / name
    for name in _found_fonts:
        norm = re.sub(r"[^a-z0-9]", "", name.lower())
        for needle in needles:
            if needle and needle in norm:
                return FONT_DIR / name
    return None


_warned_missing_font_families: set[str] = set()


def get_font(family: str, size: int) -> ImageFont.FreeTypeFont:
    size = max(int(size), 6)
    cache_key = (family, size)
    if cache_key in _font_cache:
        return _font_cache[cache_key]

    path = _resolve_font_path(family)

    # Öncelik sırası (kullanıcı isteği: sistem fontu yerine HER ZAMAN
    # fonts/ klasöründeki proje font seti önceliklidir):
    #   1) İstenen tema fontuyla fonts/ klasöründe bulunan eşleşme (_resolve_font_path)
    #   2) fonts/ klasöründeki proje font seti (PROJECT_FONT_FILES) üzerinden
    #      genel bir yedek — sistem fontuna düşmeden önce hep bu denenir
    #   3) Sistem genelindeki TrueType fontlar (GERÇEK EN SON çare — yalnızca
    #      fonts/ klasöründe PROJECT_FONT_FILES'dan hiçbir dosya bulunamazsa)
    project_fallback_paths = [
        str(FONT_DIR / name) for name in _PROJECT_FONT_FALLBACK_ORDER
        if name in _found_fonts
    ]

    font: Optional[ImageFont.FreeTypeFont] = None
    candidates = (
        ([str(path)] if path else [])
        + project_fallback_paths
        + _SYSTEM_FONT_FALLBACKS
    )
    for candidate in candidates:
        try:
            font = ImageFont.truetype(candidate, size)
            break
        except Exception:
            continue

    if font is None:
        key = family or "(boş)"
        if key not in _warned_missing_font_families:
            log.error(
                f"[FONT] ⚠️ '{key}' için hiçbir TrueType font bulunamadı (ne fonts/ "
                "klasöründeki proje font setinde ne sistemde). Metin GÖRÜNMEZ "
                "OLABİLİR. fonts/ klasörüne PROJECT_FONT_FILES listesindeki "
                "dosyaların eklenmesi şiddetle önerilir."
            )
            _warned_missing_font_families.add(key)
        font = ImageFont.load_default()

    _font_cache[cache_key] = font
    return font


# ──────────────────────────────────────────────────────────────────────────
# Pydantic şemaları (frontend'in slideToBackend/slideFromBackend'i ile birebir)
# ──────────────────────────────────────────────────────────────────────────
class LineIO(BaseModel):
    text: str = ""
    color: str = "#ffffff"
    size: int = 100
    is_title: bool = False
    is_button: bool = False


class SlideIO(BaseModel):
    duration: float = 2.5
    bg: str = "#e63946"
    accent1: str = "#ffffff"
    accent2: str = "#ffffff"
    align: str = "middle"
    font: str = "Anton"
    text_anim: str = "slide_up"
    transition: str = "none"
    deco: str = "none"
    deco_color: str = "#ffffff"
    deco_opacity: float = 12
    line_height: float = 1.15
    text_padding: float = 100
    layout: Optional[str] = None
    voice: Optional[str] = None
    voice_rate: Optional[float] = None
    muted: bool = False
    # ── GIF alanları ──
    # gif_query: Gemini'nin ürettiği İngilizce Klipy arama terimi (varsa).
    # gif_url:   Klipy'den seçilen GIF'in doğrudan indirme URL'i (varsa).
    #            Frontend bunu /api/generate cevabından alır; /api/render'a
    #            gönderirken de aynen geri iletmesi yeterlidir — backend
    #            render sırasında bu URL'den GIF'i indirir.
    # gif_band:  "top" | "bottom" | None — GIF'in metne göre hangi boş banda
    #            yerleştirileceği. /api/generate tarafından hesaplanır.
    gif_query: Optional[str] = None
    gif_url: Optional[str] = None
    gif_band: Optional[str] = None
    # screenshot_url: app-ozellik.json -> gorsel_url'den çözülen, ReisulQurra'nın
    # gerçek bir ekranını gösteren statik görsel (örn. "/screenshots/home_agac.png").
    # Doluysa render aşamasında gif_band'e GIF yerine BU görsel yapıştırılır
    # (gif_url ile screenshot_url aynı anda dolu olsa bile screenshot_url önceliklidir).
    screenshot_url: Optional[str] = None
    lines: List[LineIO] = Field(default_factory=list)


class GenerateRequest(BaseModel):
    topic: str
    tone: str = "enerjik"
    slide_count: int = 10
    model: str = DEFAULT_MODEL
    current_slides: Optional[List[SlideIO]] = None
    enable_gifs: bool = True
    # "random" -> app-ozellik.json'dan rastgele bir özellik seçilir ve metinler
    # buna göre yazılır. "none" -> özellik enjeksiyonu yapılmaz (eski davranış).
    # Belirli bir özellik id'si (ör. "ezber_stt") -> o özellik odak alınır.
    feature_id: Optional[str] = "random"


class ReviseRequest(BaseModel):
    """Tek bir sahne için hedefli AI revizyonu. Kullanıcı, o sahnenin mevcut
    metnini ve GIF'ini (varsa) görür, bir 'şöyle değiştir' notu yazar; bu not
    Gemini'ye sahnenin mevcut içeriğiyle birlikte gönderilir ve YALNIZCA o
    sahnenin metinleri (ve istenirse GIF'i) yeniden üretilir. Diğer sahneler
    ve tasarım (renk/font/animasyon) hiç etkilenmez."""
    topic: str = ""
    tone: str = "enerjik"
    model: str = DEFAULT_MODEL
    slide: SlideIO
    instruction: str
    revise_gif: bool = False
    # GIF'e özel serbest talimat (ör. "daha komik bir GIF olsun", "şaşkın yüz
    # ifadesi", "aşk/kalp temalı bir şey dene"). Boşsa ve revise_gif=True ise,
    # genel `instruction` metni GIF arama terimi üretiminde de kullanılır
    # (eski davranışla geriye dönük uyumlu — GIF hâlâ metinle tutarlı kalır).
    # Doluysa bu talimat metinden BAĞIMSIZ olarak GIF seçimine öncelikli
    # şekilde yansır.
    gif_instruction: Optional[str] = None
    enable_gifs: bool = True
    feature_id: Optional[str] = None


class RenderRequest(BaseModel):
    slides: List[SlideIO]
    resolution: Literal["480p", "720p", "1080p"] = "1080p"
    render_speed: Literal["fast", "balanced", "quality"] = "fast"
    quality: Literal["high", "medium", "low"] = "high"


class MushafRenderRequest(BaseModel):
    """'Kur'an — Mushaf Okuması' teması: reels.html'deki normal slayt akışının
    DIŞINDA, gerçek Mushaf sayfasının (deneme.html -> mushaf_viewer/) ayet
    ayet ekran görüntüsü alınmasıyla üretilen video için istek gövdesi."""
    # Kullanıcı formda "Başlangıç Sayfası" girerse (page_start dolu), bu değer
    # surah/ayah_start'ın YERİNE geçer: render_mushaf_job, Playwright ile o
    # sayfanın ilk ayetini (__mushafAPI.gotoPage) sorgulayıp surah/ayah_start'ı
    # buna göre otomatik belirler. page_start boşsa eski davranış (doğrudan
    # surah + ayah_start) aynen çalışmaya devam eder.
    surah: Optional[int] = Field(default=None, ge=1, le=114)
    ayah_start: Optional[int] = Field(default=None, ge=1)
    # Sure modunda ZORUNLU (kullanıcı bitiş ayetini seçer). Sayfa modunda
    # (page_start dolu) artık KULLANILMIYOR — o sayfada kaç ayet olduğunu
    # kullanıcıya sormak yerine, render_mushaf_job sayfadaki TÜM ayetleri
    # (__mushafAPI.gotoVerse'in döndürdüğü verseKeys listesinden) otomatik
    # okur; ayah_end o durumda tamamen görmezden gelinir.
    ayah_end: Optional[int] = Field(default=None, ge=1)
    reciter_folder: str = "7"
    page_start: Optional[int] = Field(default=None, ge=1, le=604)
    # Frontend'den page_start ile birlikte gönderilir; sadece niyeti belgeler
    # (page_start dolu olduğu sürece davranış zaten "tüm sayfa" olur) —
    # backend tarafında ayrıca dallanma gerektirmiyor ama isteği okuyan biri
    # için açık bir sözleşme sağlar.
    full_page: bool = False
    # Arayüz dili (Oynat/Durdur, Sayfa, Cüz vb. sabit etiketler). "tr" ise
    # (varsayılan) hiçbir çeviri yapılmaz — kaynak dil zaten Türkçe. Başka
    # bir dil kodu gelirse render_mushaf_job, translate_ui_dict() ile
    # Gemini'den çeviri alıp Playwright üzerinden sayfaya enjekte eder.
    lang: str = "tr"

    # ── İlk (açılış) ve son (kapanış) sahne — manuel override ──
    # Doldurulmazsa (None/boş) mevcut otomatik davranış aynen sürer: açılışta
    # sure adı + okuyucu adı (Mushaf sayfasından/MUSHAF_RECITER_NAMES'ten
    # otomatik), kapanışta sabit logo + APP_NAME. Kullanıcı bu alanları
    # doldurursa ilgili metin(ler) otomatik üretilenin YERİNE geçer.
    intro_title: Optional[str] = None       # Açılış ekranındaki sure adının yerine geçer
    intro_subtitle: Optional[str] = None    # Açılış ekranındaki okuyucu adının yerine geçer
    outro_text: Optional[str] = None        # Kapanış logosunun altındaki uygulama adının yerine geçer


class QuizRenderRequest(BaseModel):
    """'Kur'an — Bil Bakalım Quiz' teması: quiz.html'in kendi soru üretim/
    kontrol mantığını (mod/zorluk/alan seçimi, doğru cevap belirleme) hiç
    değiştirmeden, Playwright ile headless kontrol edip her sorunun
    (a) TTS ile Türkçe okunuşu, (b) 10 sn düşünme süresi ekran görüntüsü,
    (c) doğru cevabın gösterildiği ekran görüntüsü olacak şekilde bir video
    üretir. Alanlar quiz.html -> App.setup ile birebir eşleşir."""
    mode: Literal["missing-word", "verse-nav", "selection", "mixed"] = "missing-word"
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    selection_mode: Literal["juz", "surah", "page"] = "juz"
    juz_start: int = Field(default=1, ge=1, le=30)
    juz_end: int = Field(default=1, ge=1, le=30)
    surah_selection: List[int] = Field(default_factory=list)
    page_start: int = Field(default=1, ge=1, le=604)
    page_end: int = Field(default=1, ge=1, le=604)
    question_count: int = Field(default=10, ge=1, le=20)
    # Her soru için düşünme süresi (saniye) — istekte belirtildiği gibi
    # varsayılan 10 sn. Cevap ekranı sabit kısa bir süre (3 sn) gösterilir.
    think_seconds: float = Field(default=10.0, ge=3.0, le=30.0)
    answer_seconds: float = Field(default=3.0, ge=1.5, le=10.0)
    voice: Optional[str] = None
    voice_rate: Optional[float] = None

    # ── İlk (açılış) ve son (kapanış) sahne — manuel override ──
    # Doldurulmazsa mevcut otomatik davranış (sabit logo + APP_NAME, hem
    # açılışta hem kapanışta) aynen sürer. intro_text/outro_text doluysa,
    # o ekranda logonun altındaki uygulama adı yerine bu metin gösterilir.
    intro_text: Optional[str] = None
    outro_text: Optional[str] = None


class TTSPreviewRequest(BaseModel):
    text: str
    voice: Optional[str] = None
    voice_rate: Optional[float] = None


class GifSearchRequest(BaseModel):
    query: str
    limit: int = 9


# ──────────────────────────────────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Hafızlık Reels Studio Backend")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
app.mount("/output", StaticFiles(directory=str(OUT_DIR)), name="output")
app.mount("/screenshots", StaticFiles(directory=str(SCREENSHOTS_DIR)), name="screenshots")
app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")
app.mount("/mushaf-viewer", StaticFiles(directory=str(MUSHAF_VIEWER_DIR), html=True), name="mushaf_viewer")


@app.get("/api/models")
def api_models():
    return {
        "models": [{"key": k, **v} for k, v in MODELS.items()],
        "default": DEFAULT_MODEL,
    }


@app.get("/api/app-features")
def api_app_features():
    """reels.html'in 'AI' sekmesindeki Uygulama Özelliği seçicisini beslemek
    için app-ozellik.json'daki tüm özellikleri düz (flat) liste + modül
    başlıklarıyla birlikte döndürür."""
    return {
        "uygulama": APP_FEATURES_DATA.get("uygulama", "ReisulQurra"),
        "flat_features": APP_FEATURES_FLAT,
        "modules": [
            {"key": k, "baslik": v.get("baslik", k)}
            for k, v in APP_FEATURES_DATA.get("ozellikler", {}).items()
        ],
    }


@app.get("/api/voices")
def api_voices():
    return {
        "voices": ALL_VOICES,
        "default": DEFAULT_VOICE,
        "elevenlabs_enabled": bool(ELEVENLABS_API_KEY),
    }


@app.get("/api/gif-status")
def api_gif_status():
    """Frontend'in GIF özelliğinin sunucuda aktif olup olmadığını (API key
    tanımlı mı) kontrol etmesi için basit bir durum endpoint'i."""
    return {"enabled": bool(KLIPY_API_KEY)}


@app.post("/api/gif-search")
async def api_gif_search(req: GifSearchRequest):
    """Kullanıcının GIF sekmesinden manuel olarak (AI'nin seçtiği GIF
    beğenilmediğinde) yeni bir arama terimiyle GIF aratmasını sağlar.
    Birden fazla sonuç döner; kullanıcı bunlardan birini seçip sahneye
    uygular (bkz. reels.html -> searchGifsForCurrentSlide / pickGifResult)."""
    if not KLIPY_API_KEY:
        raise HTTPException(400, "KLIPY_API_KEY tanımlı değil (.env dosyasına ekleyin).")
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(400, "Arama terimi boş olamaz.")
    limit = max(1, min(req.limit or 9, 20))
    urls = await _klipy_search_gifs(query, limit=limit)
    return {"query": query, "results": urls}


# ──────────────────────────────────────────────────────────────────────────
# GIF (Klipy) yardımcıları
# ──────────────────────────────────────────────────────────────────────────
def _slide_free_band(slide_lines_count: int) -> bool:
    """Sahnenin GIF'e aday olup olmadığına satır sayısına bakarak kaba bir
    ilk eleme yapar (asıl doluluk hesabı _slide_gif_band'de, gerçek font
    ölçümüyle yapılır)."""
    return 0 < slide_lines_count <= GIF_MAX_LINES


def _measure_text_block_height(template_slide: dict, w: int = 1080, h: int = 1920) -> float:
    """Bir şablon sahnenin metin bloğunun toplam yüksekliğini (render'daki ile
    aynı font/line_height mantığıyla) piksel cinsinden hesaplar. Gerçek fit_font
    küçültmesini de hesaba katmak için basitleştirilmiş bir tahmin kullanır:
    satırın kendi 'size' değeri baz alınır (fit_font zaten metni sığdırana
    kadar küçültüyor, o yüzden bu üst sınır bir tahmindir, kesin ölçüm değil)."""
    line_h_mult = float(template_slide.get("lineHeight", 1.15))
    total_h = 0.0
    for ln in template_slide.get("lines", []):
        size = max(int(ln.get("size", 100)), MIN_FONT_SIZE)
        # Basit tahmin: bir satırın yüksekliği ~ size * line_h_mult
        total_h += size * line_h_mult
    return total_h


def _slide_gif_band(template_slide: dict) -> Optional[str]:
    """Bir şablon sahnenin GIF'e uygun olup olmadığını ve GIF'in hangi banda
    (top/bottom) yerleştirileceğini belirler. Uygun değilse None döner.

    Mantık: sahnenin align değeri (top/middle/bottom) metin bloğunun dikey
    konumunu belirler. Kullanılabilir alan (SAFE_TOP..H-SAFE_BOTTOM) içinde
    metnin kaplamadığı taraf GIF için kullanılır:
      - align='top'    -> metin üstte, GIF alta gelir
      - align='bottom' -> metin altta, GIF üste gelir
      - align='middle' -> metin ortada; büyük boş alan üstte VE altta olduğu
                            için GIF üst banda yerleştirilir (öncelik üst)
      - left/right/diagonal/cta gibi yatay-ağırlıklı hizalamalarda GIF
        yerleştirmiyoruz (çakışma riski yüksek, tasarım karmaşıklaşır).
    """
    lines = template_slide.get("lines", [])
    if not _slide_free_band(len(lines)):
        return None
    align = template_slide.get("align", "middle")
    if align not in ("top", "middle", "bottom"):
        return None
    if template_slide.get("layout") == "to_grow_online":
        return None  # özel layout, GIF için elverişli değil

    usable_h = (H - SAFE_BOTTOM) - SAFE_TOP
    text_h = _measure_text_block_height(template_slide)
    free_h = usable_h - text_h
    if free_h < usable_h * GIF_MIN_FREE_RATIO:
        return None  # yeterli boşluk yok

    if align == "top":
        return "bottom"
    if align == "bottom":
        return "top"
    return "top"  # middle -> üst banda yerleştir


def _gif_band_box(template_slide: dict, band: str) -> tuple[int, int, int, int]:
    """band ('top'/'bottom') için, 1080x1920 tabanında GIF'in çizileceği
    (x0, y0, x1, y1) kutusunu döndürür. Metin bloğuyla çakışmaması için
    GIF_BAND_MARGIN kadar pay bırakır."""
    align = template_slide.get("align", "middle")
    text_h = _measure_text_block_height(template_slide)
    pad = float(template_slide.get("textPadding", 100))
    x0, x1 = int(pad), int(1080 - pad)

    if band == "bottom":
        # Metin üstte (align=top) ise metin alt sınırı ~ SAFE_TOP + text_h
        text_bottom = SAFE_TOP + text_h + GIF_BAND_MARGIN
        y0 = int(min(text_bottom, H - SAFE_BOTTOM - 100))
        y1 = int(H - SAFE_BOTTOM)
    else:  # top
        if align == "bottom":
            text_top = (H - SAFE_BOTTOM) - text_h - GIF_BAND_MARGIN
        else:  # middle -> metin dikey ortada, üst boşluk yaklaşık ortaya kadar
            center = (SAFE_TOP + (H - SAFE_BOTTOM)) / 2
            text_top = center - text_h / 2 - GIF_BAND_MARGIN
        y0 = int(SAFE_TOP)
        y1 = int(max(text_top, SAFE_TOP + 100))

    if y1 - y0 < 120:
        # Pay çok daralmışsa GIF'i makul bir minimuma sabitle
        if band == "bottom":
            y0 = max(SAFE_TOP, y1 - 220)
        else:
            y1 = min(H - SAFE_BOTTOM, y0 + 220)
    return (x0, y0, x1, y1)


async def _gemini_gif_query(topic: str, slide_text: str, model_key: str,
                             user_instruction: Optional[str] = None) -> Optional[str]:
    """Sahne metninden (Türkçe) kısa, İngilizce, Klipy'de arama yapılabilir
    bir anahtar kelime/ifade ürettirir. Hata durumunda None döner — çağıran
    taraf bunu "GIF atla" olarak yorumlamalı, render'ı çökertmemeli.

    user_instruction doluysa (kullanıcının revize ekranında GIF için yazdığı
    serbest talimat, ör. "daha komik bir şey olsun", "şaşkın yüz ifadesi",
    "bunun yerine kalp/aşk temalı bir GIF") bu talimat sahne metninden/konudan
    DAHA ÖNCELİKLİDİR — prompt bunu açıkça belirtir."""
    if not GEMINI_API_KEY:
        return None
    instruction_block = (
        f"""
KULLANICI TALİMATI (EN ÖNCELİKLİ — buna göre karar ver, gerekirse sahne
metninden/genel konudan tamamen farklı bir arama terimi üret):
"{user_instruction.strip()}"
""" if user_instruction and user_instruction.strip() else ""
    )
    prompt = f"""Aşağıdaki kısa video sahnesi için, bir GIF arama motorunda (Klipy/Giphy tarzı)
kullanılacak KISA bir İngilizce arama terimi üret. 1-3 kelime olsun, sahnenin
duygusunu/konusunu görsel olarak temsil etsin (soyut değil, GIF'i olabilecek somut
bir kavram/duygu/aksiyon seç: ör. "mind blown", "typing fast", "clock ticking",
"lightbulb idea", "confused thinking").
{instruction_block}
Genel konu: "{topic}"
Sahne metni: "{slide_text}"

SADECE arama terimini düz metin olarak yaz, tırnak/açıklama/markdown YOK.
Örnek çıktı: mind blown"""
    try:
        model_id = MODELS.get(model_key, MODELS[DEFAULT_MODEL])["id"]
        url = f"https://generativelanguage.googleapis.com/v1beta/{model_id}:generateContent"
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 20},
        }
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(url, params={"key": GEMINI_API_KEY}, json=body)
        if res.status_code != 200:
            log.warning(f"[GIF] Gemini anahtar kelime isteği başarısız ({res.status_code})")
            return None
        data = res.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        text = re.sub(r'^["\'\-\s]+|["\'\-\s]+$', "", text)
        text = text.splitlines()[0].strip()
        return text[:60] if text else None
    except Exception as e:
        log.warning(f"[GIF] Gemini anahtar kelime üretimi hata: {e}")
        return None


def _extract_gif_url(item: dict) -> Optional[str]:
    """Klipy'nin tek bir sonuç öğesinden GIF/MP4 URL'ini çıkarır. Klipy'nin
    cevap şeması sürümden sürüme değişebilir; birkaç olası yapıyı esnek
    biçimde deniyoruz."""
    candidates = [
        item.get("file", {}).get("md", {}).get("gif", {}).get("url") if isinstance(item.get("file"), dict) else None,
        item.get("file", {}).get("hd", {}).get("gif", {}).get("url") if isinstance(item.get("file"), dict) else None,
        item.get("url"),
        item.get("gif_url"),
        item.get("images", {}).get("original", {}).get("url") if isinstance(item.get("images"), dict) else None,
    ]
    for c in candidates:
        if c:
            return c
    return None


async def _klipy_search_gifs(query: str, limit: int = 8) -> list[str]:
    """Klipy'de query ile arama yapar, bulunan GIF/MP4 URL'lerinin bir
    listesini (en fazla `limit` adet) döner. Anahtar yoksa veya istek
    başarısız olursa boş liste döner (sessiz düşüş)."""
    if not KLIPY_API_KEY or not query:
        return []
    url = KLIPY_SEARCH_URL.format(key=KLIPY_API_KEY)
    params = {"q": query, "per_page": max(1, limit), "customer_id": "hafizlik-reels"}
    try:
        async with httpx.AsyncClient(timeout=KLIPY_TIMEOUT_SECONDS) as client:
            res = await client.get(url, params=params)
        if res.status_code != 200:
            log.warning(f"[GIF] Klipy arama hatası ({res.status_code}): {res.text[:200]}")
            return []
        data = res.json()
        items = (
            data.get("data", {}).get("data")
            or data.get("data")
            or data.get("results")
            or []
        )
        urls: list[str] = []
        for item in items:
            u = _extract_gif_url(item)
            if u:
                urls.append(u)
            if len(urls) >= limit:
                break
        return urls
    except Exception as e:
        log.warning(f"[GIF] Klipy isteği başarısız: {e}")
        return []


async def _klipy_search_gif(query: str) -> Optional[str]:
    """Geriye dönük uyumluluk: tek bir (ilk) sonucu döner."""
    urls = await _klipy_search_gifs(query, limit=1)
    return urls[0] if urls else None


async def _resolve_gif_for_slide(topic: str, template_slide: dict, out_slide: dict, model_key: str,
                                  user_instruction: Optional[str] = None) -> dict:
    """Bir sahne için GIF banda uygunsa: Gemini'den arama terimi üret, Klipy'de
    ara, sonucu out_slide üzerine (gif_query/gif_url/gif_band) yazar. Herhangi
    bir adım başarısız olursa sahneyi GIF'siz bırakır — asla exception fırlatmaz.

    user_instruction: revize akışında kullanıcının GIF için yazdığı serbest
    talimat (bkz. _gemini_gif_query). /api/generate akışında hep None'dır
    (ilk üretimde henüz kullanıcı talimatı yoktur).

    Ayrıca HER durumda (başarılı ya da değil) bir teşhis (debug) sözlüğü döner;
    /api/generate cevabındaki "gif_debug" alanı üzerinden tam olarak hangi
    adımda ve neden bir sahnenin GIF'siz kaldığı görülebilir."""
    band = _slide_gif_band(template_slide)
    if not band:
        return {"eligible": False, "reason": "sahne_uygun_degil (satır sayısı/hizalama/boşluk yetersiz)"}

    slide_text = " ".join(l.get("text", "") for l in out_slide.get("lines", []))
    if not slide_text.strip() and not (user_instruction and user_instruction.strip()):
        return {"eligible": True, "band": band, "reason": "sahne_metni_bos"}

    try:
        query = await _gemini_gif_query(topic, slide_text, model_key, user_instruction)
        if not query:
            return {"eligible": True, "band": band, "reason": "gemini_anahtar_kelime_uretilemedi"}
        gif_url = await _klipy_search_gif(query)
        if not gif_url:
            log.info(f"[GIF] '{query}' için sonuç bulunamadı, sahne GIF'siz kalıyor.")
            return {"eligible": True, "band": band, "query": query, "reason": "klipy_sonuc_yok"}
        out_slide["gif_query"] = query
        out_slide["gif_url"] = gif_url
        out_slide["gif_band"] = band
        return {"eligible": True, "band": band, "query": query, "gif_url": gif_url, "reason": "ok"}
    except Exception as e:
        # Son bir güvenlik ağı: GIF akışında beklenmeyen HERHANGİ bir hata
        # /api/generate'i asla çökertmemeli.
        log.warning(f"[GIF] Sahne için GIF çözümleme hatası, atlanıyor: {e}")
        return {"eligible": True, "band": band, "reason": f"hata: {e}"}


async def _download_gif_frames(gif_url: str, dest_dir: Path) -> Optional[list[tuple[Path, float]]]:
    """Bir GIF/MP4 URL'ini indirir, Pillow ile kare kare açar ve her karenin
    (dosya_yolu, süre_saniye) listesini döner. Animasyonsuz/okunamaz
    dosyalarda ya da indirme hatasında None döner (render bu sahneyi
    GIF'siz, sadece metinle tamamlar)."""
    try:
        async with httpx.AsyncClient(timeout=KLIPY_TIMEOUT_SECONDS, follow_redirects=True) as client:
            res = await client.get(gif_url)
        if res.status_code != 200 or not res.content:
            log.warning(f"[GIF] indirme başarısız ({res.status_code}): {gif_url[:100]}")
            return None
        raw_path = dest_dir / "source.gif"
        raw_path.write_bytes(res.content)

        frames: list[tuple[Path, float]] = []
        with Image.open(raw_path) as im:
            n_frames_total = getattr(im, "n_frames", 1)
            for idx, frame in enumerate(ImageSequence.Iterator(im)):
                frame_rgba = frame.convert("RGBA")
                frame_path = dest_dir / f"frame_{idx:04d}.png"
                frame_rgba.save(frame_path)
                dur_ms = frame.info.get("duration", 80)
                frames.append((frame_path, max(20, int(dur_ms)) / 1000.0))
                if idx >= 240:  # aşırı uzun/ağır GIF'lere karşı üst sınır
                    break
        if not frames:
            return None
        return frames
    except Exception as e:
        log.warning(f"[GIF] indirme/parse hatası: {e}")
        return None


def _gif_cache_key(gif_url: str) -> str:
    import hashlib
    return hashlib.sha1(gif_url.encode("utf-8")).hexdigest()[:24]


async def _get_gif_frames_cached(gif_url: str) -> Optional[list[tuple[Path, float]]]:
    """_download_gif_frames'in disk cache'li hali — aynı GIF birden fazla
    sahnede/renderda kullanılsa bile tekrar tekrar indirilmez."""
    key = _gif_cache_key(gif_url)
    cache_dir = GIF_CACHE_DIR / key
    meta_path = cache_dir / "meta.json"
    if cache_dir.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            frames = [(cache_dir / f["file"], f["dur"]) for f in meta["frames"]]
            if all(p.exists() for p, _ in frames):
                return frames
        except Exception:
            pass  # cache bozuksa yeniden indir

    cache_dir.mkdir(parents=True, exist_ok=True)
    frames = await _download_gif_frames(gif_url, cache_dir)
    if not frames:
        shutil.rmtree(cache_dir, ignore_errors=True)
        return None
    try:
        meta_path.write_text(json.dumps({
            "frames": [{"file": p.name, "dur": d} for p, d in frames]
        }))
    except Exception:
        pass
    return frames


# ──────────────────────────────────────────────────────────────────────────
# /api/generate — tema şablonunu birebir korur, sadece metinleri AI üretir
# ──────────────────────────────────────────────────────────────────────────
def _current_slides_to_theme(slides: List[SlideIO]) -> dict:
    tpl_slides = []
    for s in slides:
        tpl_slides.append({
            "bg": s.bg, "font": s.font, "textAnim": s.text_anim, "deco": s.deco,
            "decoColor": s.deco_color, "decoOpacity": s.deco_opacity,
            "accent1": s.accent1, "accent2": s.accent2, "duration": s.duration,
            "align": s.align, "transition": s.transition,
            "lineHeight": s.line_height, "textPadding": s.text_padding,
            "layout": s.layout, "voice": s.voice, "voiceRate": s.voice_rate, "muted": s.muted,
            "lines": [
                {"text": l.text, "color": l.color, "size": l.size,
                 "isTitle": l.is_title, "isButton": l.is_button}
                for l in s.lines
            ],
        })
    return {"label": "Seçili Tema", "desc": "", "slides": tpl_slides}


def _pick_theme(slide_count: int) -> dict:
    candidates = sorted(THEMES, key=lambda t: abs(len(t["slides"]) - slide_count))
    best_diff = abs(len(candidates[0]["slides"]) - slide_count)
    pool = [t for t in candidates if abs(len(t["slides"]) - slide_count) == best_diff]
    return random.choice(pool)


def _resolve_feature_info(feature_id: Optional[str]) -> tuple[str, Optional[dict]]:
    """feature_id'ye göre app-ozellik.json'dan bir özellik seçer ve prompt'a
    eklenecek kısa bir açıklama metni döndürür. "none" -> boş. "random"/None
    -> rastgele bir özellik. Belirli id -> o özellik (bulunamazsa rastgele)."""
    if feature_id == "none":
        return "", None
    if not APP_FEATURES_FLAT:
        return "", None
    if feature_id in (None, "", "random"):
        selected = random.choice(APP_FEATURES_FLAT)
    else:
        selected = next((f for f in APP_FEATURES_FLAT if f.get("id") == feature_id), None)
        if selected is None:
            selected = random.choice(APP_FEATURES_FLAT)
    info = f"{selected.get('ad', '')} — {selected.get('aciklama', '')}"
    return info, selected


def _resolve_screenshot_path(gorsel_url: Optional[str]) -> Optional[Path]:
    """app-ozellik.json içindeki gorsel_url alanını gerçek bir dosya yoluna
    çevirir. None/boş -> None (çağıran taraf GIF'e düşer). "http(s)://" ile
    başlıyorsa uzak URL kabul edilmez (senkron/offline render pipeline'ı için
    yerel dosya bekleniyor) — sadece SCREENSHOTS_DIR altındaki dosya adları
    çözülür. Dosya yoksa da None döner (sessiz fallback, render çökmez)."""
    if not gorsel_url:
        return None
    if gorsel_url.startswith("http://") or gorsel_url.startswith("https://"):
        log.warning("[SCREENSHOT] Uzak URL desteklenmiyor, GIF'e düşülüyor: %s", gorsel_url)
        return None
    candidate = (SCREENSHOTS_DIR / gorsel_url).resolve()
    try:
        candidate.relative_to(SCREENSHOTS_DIR.resolve())
    except ValueError:
        log.warning("[SCREENSHOT] Geçersiz yol (dizin dışı), yok sayılıyor: %s", gorsel_url)
        return None
    if not candidate.exists():
        log.info("[SCREENSHOT] Dosya henüz yüklenmemiş, GIF'e düşülüyor: %s", candidate)
        return None
    return candidate


def _build_prompt(topic: str, tone: str, theme: dict, feature_info: str = "") -> str:
    outline = []
    for i, s in enumerate(theme["slides"]):
        seen_groups = []
        group_of_line = []
        for line in s["lines"]:
            key = (line["text"] or "").strip()
            match_idx = next((gi for gi, g in enumerate(seen_groups) if g["key"] == key and key), None)
            if match_idx is not None:
                group_of_line.append(match_idx)
                seen_groups[match_idx]["count"] += 1
            else:
                seen_groups.append({"key": key, "size": line["size"], "count": 1})
                group_of_line.append(len(seen_groups) - 1)

        fixed_group = group_of_line[-1] if s.get("layout") == "to_grow_online" and group_of_line else None

        line_specs = []
        for gi, g in enumerate(seen_groups):
            if gi == fixed_group:
                continue
            approx_chars = max(4, int(1800 / max(g["size"], 20)))
            note = f" [bu metin ekranda {g['count']}x tekrarlanan bir vurgu efektidir, TEK metin yaz]" if g["count"] > 1 else ""
            line_specs.append(f"~{approx_chars} karaktere kadar{note}")
        outline.append(f"  Sahne {i + 1}: {len(line_specs)} farklı metin ({', '.join(line_specs)})")
    outline_txt = "\n".join(outline)
    feature_block = (
        f"""
ODAK UYGULAMA ÖZELLİĞİ (ReisulQurra): {feature_info}
Kurguyu bu özelliğin çözdüğü soruna göre kur — SORUN/AJİTASYON kısmında bu
özelliğin çözdüğü problemi hissettir, ÇÖZÜM/KANIT kısmında doğrudan bu özelliği
öne çıkar, CTA'da uygulamayı (ReisulQurra) indirmeye/denemeye yönlendir.
""" if feature_info else ""
    )
    return f"""Sen kısa dikey video (Reels/TikTok) metin yazarısın. Aşağıda SABİT bir sahne
şablonunun yapısı var (kaç sahne, her sahnede kaç FARKLI metin ve karakter sınırı).
Bazı sahnelerde aynı metin ekranda birden fazla kez tekrarlanan bir görsel efekt
olarak kullanılır — bu durumda senden yalnızca o TEK metni yazman istenir, tekrar
otomatik uygulanır. Görsel tasarım (renk, font, animasyon) zaten sabit — SEN SADECE

metinleri konuya göre yeniden yazacaksın. Kurgu SORUN → AJİTASYON → ÇÖZÜM → KANIT → CTA
akışını takip etmeli (şablon zaten bu sırayla dizilmiştir, sırayı bozma).
{feature_block}
Konu: "{topic}"
Ton: {tone}
Dil: Türkçe, TÜMÜ BÜYÜK HARF, kısa ve çarpıcı, emoji YOK.

Şablon yapısı (sahne sırasına göre; satır sayısını ve sırasını değiştirme):
{outline_txt}

SADECE şu JSON formatında, başka hiçbir açıklama/markdown olmadan cevap ver:
{{"slides": [{{"lines": ["METİN1", "METİN2", ...]}}, ...]}}
Toplam sahne sayısı tam olarak {len(theme['slides'])} olmalı ve her sahnenin "lines"
dizisindeki eleman sayısı, o sahne için yukarıda belirtilen "farklı metin" sayısıyla
birebir eşleşmeli (tekrarlı vurgu efekti olan sahnelerde bile TEK metin yaz, birden
fazla değil)."""


def _slide_line_groups(lines: List[dict]) -> tuple[list[dict], list[int], Optional[int]]:
    """Bir sahnenin satırlarını, aynı metnin tekrarlandığı 'grup'lara ayırır.
    _build_prompt / _slide_to_out ile aynı gruplama mantığı — tek bir yerde
    tutulup hem /api/generate hem /api/revise tarafından kullanılır."""
    seen_groups: list[dict] = []
    group_of_line: list[int] = []
    for line in lines:
        key = (line.get("text") or "").strip()
        match_idx = next((gi for gi, g in enumerate(seen_groups) if g["key"] == key and key), None)
        if match_idx is not None:
            group_of_line.append(match_idx)
            seen_groups[match_idx]["count"] += 1
        else:
            seen_groups.append({"key": key, "size": line.get("size", 100), "count": 1})
            group_of_line.append(len(seen_groups) - 1)
    return seen_groups, group_of_line, None


def _build_revise_prompt(topic: str, tone: str, slide: "SlideIO", instruction: str) -> tuple[str, list[int]]:
    """TEK bir sahne için revizyon prompt'u üretir. Sahnenin mevcut metnini
    (bağlam olarak) ve kullanıcının 'şöyle değiştir' notunu Gemini'ye verir;
    tasarım (renk/font/animasyon/GIF) hiç değişmeyeceği için prompt'a hiç
    dahil edilmez — sadece metinler yeniden yazılır.

    Döndürdüğü ikinci değer (ai_index_of_group benzeri) çağıran tarafın
    Gemini cevabındaki "lines" dizisini orijinal satırlara geri eşlemesi
    içindir (_slide_to_out'taki mantıkla aynı)."""
    lines = [l.dict() if hasattr(l, "dict") else l for l in slide.lines]
    seen_groups, group_of_line, _ = _slide_line_groups(lines)

    current_lines_txt = []
    line_specs = []
    for gi, g in enumerate(seen_groups):
        approx_chars = max(4, int(1800 / max(g["size"], 20)))
        note = f" [ekranda {g['count']}x tekrarlanan bir vurgu efekti, TEK metin yaz]" if g["count"] > 1 else ""
        line_specs.append(f"~{approx_chars} karaktere kadar{note}")
        current_lines_txt.append(f'  {gi + 1}. "{g["key"]}"' + (f" (x{g['count']})" if g["count"] > 1 else ""))

    prompt = f"""Sen kısa dikey video (Reels/TikTok) metin yazarısın. Kullanıcı, bir video
sahnesindeki metni SENİN önerine göre revize etmeni istiyor. Sahnenin görsel
tasarımı (renk, font, animasyon, düzen) SABİT — SEN SADECE metin(ler)i
kullanıcının talimatına göre yeniden yazacaksın.

Genel video konusu: "{topic or '(belirtilmedi)'}"
Ton: {tone}
Dil: Türkçe, TÜMÜ BÜYÜK HARF, kısa ve çarpıcı, emoji YOK.

Sahnenin ŞU ANKİ metin(ler)i:
{chr(10).join(current_lines_txt) if current_lines_txt else '  (boş)'}

Kullanıcının revizyon talimatı: "{instruction}"

Bu talimatı uygulayarak sahnenin metinlerini yeniden yaz. Satır sayısını ve
sırasını DEĞİŞTİRME — sadece her satırın (grubun) içeriğini talimata göre
güncelle. Karakter sınırlarına uy: {', '.join(line_specs) if line_specs else '(yok)'}.

SADECE şu JSON formatında, başka hiçbir açıklama/markdown olmadan cevap ver:
{{"lines": ["METİN1", "METİN2", ...]}}
"lines" dizisindeki eleman sayısı tam olarak {len(seen_groups)} olmalı (tekrarlı
vurgu efekti olan gruplarda bile TEK metin yaz, birden fazla değil)."""
    return prompt, group_of_line


async def _call_gemini(model_key: str, prompt: str) -> dict:
    if not GEMINI_API_KEY:
        raise HTTPException(500, "GEMINI_API_KEY tanımlı değil (.env dosyasına ekle).")
    model_id = MODELS.get(model_key, MODELS[DEFAULT_MODEL])["id"]
    url = f"https://generativelanguage.googleapis.com/v1beta/{model_id}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9},
    }
    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.post(url, params={"key": GEMINI_API_KEY}, json=body)
    if res.status_code != 200:
        raise HTTPException(502, f"Gemini API hatası ({res.status_code}): {res.text[:300]}")
    data = res.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise HTTPException(502, "Gemini yanıtı beklenmedik biçimde geldi.")
    text = re.sub(r"^```json\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise HTTPException(502, "Gemini yanıtı JSON olarak ayrıştırılamadı.")


def _slide_to_out(template_slide: dict, new_lines: Optional[List[str]]) -> dict:
    lines = template_slide["lines"]

    seen_groups: list[dict] = []
    group_of_line: list[int] = []
    for line in lines:
        key = (line["text"] or "").strip()
        match_idx = next((gi for gi, g in enumerate(seen_groups) if g["key"] == key and key), None)
        if match_idx is not None:
            group_of_line.append(match_idx)
        else:
            seen_groups.append({"key": key})
            group_of_line.append(len(seen_groups) - 1)

    fixed_group = group_of_line[-1] if template_slide.get("layout") == "to_grow_online" and group_of_line else None
    ai_index_of_group: dict[int, int] = {}
    ai_i = 0
    for gi in range(len(seen_groups)):
        if gi == fixed_group:
            continue
        ai_index_of_group[gi] = ai_i
        ai_i += 1

    out_lines = []
    for i, line in enumerate(lines):
        text = line["text"]
        gi = group_of_line[i]
        ai_idx = ai_index_of_group.get(gi)
        if ai_idx is not None and new_lines and ai_idx < len(new_lines) and str(new_lines[ai_idx]).strip():
            text = str(new_lines[ai_idx]).strip().upper()
        out_lines.append({
            "text": text, "color": line.get("color", "#ffffff"), "size": line.get("size", 100),
            "is_title": line.get("isTitle", False), "is_button": line.get("isButton", False),
        })
    return {
        "duration": template_slide.get("duration", 2.5),
        "bg": template_slide.get("bg", "#111111"),
        "accent1": template_slide.get("accent1", "#ffffff"),
        "accent2": template_slide.get("accent2", "#ffffff"),
        "align": template_slide.get("align", "middle"),
        "font": template_slide.get("font", "Anton"),
        "text_anim": template_slide.get("textAnim", "slide_up"),
        "transition": template_slide.get("transition", "none"),
        "deco": template_slide.get("deco", "none"),
        "deco_color": template_slide.get("decoColor", "#ffffff"),
        "deco_opacity": template_slide.get("decoOpacity", 12),
        "line_height": template_slide.get("lineHeight", 1.15),
        "text_padding": template_slide.get("textPadding", 100),
        "layout": template_slide.get("layout"),
        "voice": template_slide.get("voice"),
        "muted": template_slide.get("muted", False),
        "gif_query": None,
        "gif_url": None,
        "gif_band": None,
        "screenshot_url": None,
        "lines": out_lines,
    }


@app.post("/api/generate")
async def api_generate(req: GenerateRequest):
    if not req.topic.strip():
        raise HTTPException(400, "Konu boş olamaz.")
    if req.current_slides:
        theme = _current_slides_to_theme(req.current_slides)
    else:
        theme = _pick_theme(max(10, min(req.slide_count, 12)))
    feature_info, selected_feature = _resolve_feature_info(req.feature_id)
    prompt = _build_prompt(req.topic, req.tone, theme, feature_info)
    try:
        ai_data = await _call_gemini(req.model, prompt)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"AI üretim hatası: {e}")

    ai_slides = ai_data.get("slides", []) if isinstance(ai_data, dict) else []
    out_slides = []
    for i, template_slide in enumerate(theme["slides"]):
        new_lines = ai_slides[i].get("lines") if i < len(ai_slides) else None
        out_slides.append(_slide_to_out(template_slide, new_lines))

    # ── UYGULAMA EKRAN GÖRÜNTÜSÜ ── Seçilen özelliğin gerçek bir ekran
    # görüntüsü varsa (app-ozellik.json -> gorsel_url, screenshots/ klasöründe
    # dosya olarak mevcutsa), bunu GIF aramasının ÖNÜNE geçirip, GIF-uygun
    # sahnelerden birine (varsa CTA'dan bir önceki sahne, yoksa ortadaki)
    # doğrudan yerleştiriyoruz. Görsel yoksa hiçbir şey değişmez — sistem
    # eskisi gibi Klipy GIF aramasına devam eder.
    screenshot_slide_idx: Optional[int] = None
    screenshot_path: Optional[Path] = None
    if selected_feature:
        screenshot_path = _resolve_screenshot_path(selected_feature.get("gorsel_url"))
    if screenshot_path:
        gif_eligible_idxs = [i for i, ts in enumerate(theme["slides"]) if ts.get("gif_band")]
        if gif_eligible_idxs:
            # Sondan ikinci uygun sahneyi tercih et (genelde "çözüm/özellik"
            # anlatısına denk gelir); yoksa ortadaki uygun sahneyi kullan.
            screenshot_slide_idx = gif_eligible_idxs[max(0, len(gif_eligible_idxs) - 2)]
            out_slides[screenshot_slide_idx]["screenshot_url"] = f"/screenshots/{selected_feature.get('gorsel_url')}"

    # ── GIF ÇÖZÜMLEME ── Metinler üretildikten sonra, uygun sahnelere GIF
    # ata. Tüm sahneler için PARALEL çalıştırılır (Gemini+Klipy istekleri
    # sıralı olsaydı 10+ sahnede çok yavaş olurdu). Herhangi bir sahnede
    # hata olursa o sahne sadece GIF'siz kalır — "✨ AI ile Üret" cevabı
    # her koşulda tamamlanır. Ekran görüntüsü atanmış sahne GIF aramasından
    # HARİÇ tutulur (ikisi aynı bantta çakışmasın).
    gif_debug: list[dict] = []
    if not req.enable_gifs:
        gif_debug = [{"eligible": False, "reason": "enable_gifs=false (istekte kapatılmış)"}] * len(out_slides)
    elif not KLIPY_API_KEY:
        gif_debug = [{"eligible": False, "reason": "KLIPY_API_KEY tanımlı değil (.env dosyasına ekleyin)"}] * len(out_slides)
    elif not GEMINI_API_KEY:
        gif_debug = [{"eligible": False, "reason": "GEMINI_API_KEY tanımlı değil — GIF arama terimi üretilemiyor"}] * len(out_slides)
    else:
        gif_debug = await asyncio.gather(*[
            _resolve_gif_for_slide(req.topic, template_slide, out_slide, req.model)
            if idx != screenshot_slide_idx
            else asyncio.sleep(0, result={"eligible": False, "reason": "Bu sahnede uygulama ekran görüntüsü kullanılıyor"})
            for idx, (template_slide, out_slide) in enumerate(zip(theme["slides"], out_slides))
        ], return_exceptions=False)  # _resolve_gif_for_slide zaten kendi içinde yutuyor

    gif_added = sum(1 for s in out_slides if s.get("gif_url"))
    log.info(f"[GIF] /api/generate: {gif_added}/{len(out_slides)} sahneye GIF eklendi.")

    theme_label = theme["label"] if not theme.get("desc") else f"{theme['label']} ({theme['desc']})"
    return {
        "slides": out_slides,
        "model_used": req.model,
        "theme_used": theme_label,
        "gif_debug": gif_debug,
        "feature_used": selected_feature,
        "screenshot_slide_index": screenshot_slide_idx,
    }


@app.post("/api/revise")
async def api_revise(req: ReviseRequest):
    """Sadece TEK bir sahneyi (metnini ve isteğe bağlı GIF'ini) kullanıcının
    yazdığı serbest metin talimatına göre revize eder. Diğer sahnelere hiç
    dokunmaz; frontend bu cevaptaki tek sahneyi kendi listesinde ilgili
    index'e yazar. Tasarım alanları (renk/font/animasyon/vb.) request'teki
    `slide` neyse aynen korunur — yalnızca `lines[].text` (ve istenirse
    gif_query/gif_url/gif_band) değişir."""
    instruction = (req.instruction or "").strip()
    if not instruction:
        raise HTTPException(400, "Revizyon talimatı boş olamaz.")
    if not req.slide.lines:
        raise HTTPException(400, "Revize edilecek sahnede hiç metin satırı yok.")

    template_slide = {
        "bg": req.slide.bg, "font": req.slide.font, "textAnim": req.slide.text_anim,
        "deco": req.slide.deco, "decoColor": req.slide.deco_color, "decoOpacity": req.slide.deco_opacity,
        "accent1": req.slide.accent1, "accent2": req.slide.accent2, "duration": req.slide.duration,
        "align": req.slide.align, "transition": req.slide.transition,
        "lineHeight": req.slide.line_height, "textPadding": req.slide.text_padding,
        "layout": req.slide.layout, "voice": req.slide.voice, "voiceRate": req.slide.voice_rate,
        "muted": req.slide.muted,
        "lines": [
            {"text": l.text, "color": l.color, "size": l.size, "isTitle": l.is_title, "isButton": l.is_button}
            for l in req.slide.lines
        ],
    }

    prompt, _group_of_line = _build_revise_prompt(req.topic, req.tone, req.slide, instruction)
    try:
        ai_data = await _call_gemini(req.model, prompt)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"AI revizyon hatası: {e}")

    new_lines = ai_data.get("lines") if isinstance(ai_data, dict) else None
    out_slide = _slide_to_out(template_slide, new_lines)

    # Kullanıcı GIF'i de revize etmek istemediyse, mevcut GIF'i aynen koru
    # (varsayılan davranış: sadece metin değişir).
    if not req.revise_gif:
        out_slide["gif_query"] = req.slide.gif_query
        out_slide["gif_url"] = req.slide.gif_url
        out_slide["gif_band"] = req.slide.gif_band
        gif_debug = {"eligible": bool(req.slide.gif_url), "reason": "revise_gif=false, mevcut GIF korundu"}
    elif not req.enable_gifs:
        gif_debug = {"eligible": False, "reason": "enable_gifs=false (istekte kapatılmış)"}
    elif not KLIPY_API_KEY:
        gif_debug = {"eligible": False, "reason": "KLIPY_API_KEY tanımlı değil (.env dosyasına ekleyin)"}
    elif not GEMINI_API_KEY:
        gif_debug = {"eligible": False, "reason": "GEMINI_API_KEY tanımlı değil — GIF arama terimi üretilemiyor"}
    else:
        # gif_instruction boşsa genel `instruction`'ı GIF için de kullan —
        # böylece kullanıcı tek bir kutuya "3. satırı değiştir, GIF'i de
        # buna göre güncelle" yazdığında GIF de aynı talimattan haberdar olur.
        gif_instruction = (req.gif_instruction or instruction or "").strip() or None
        gif_debug = await _resolve_gif_for_slide(req.topic, template_slide, out_slide, req.model, gif_instruction)

    return {"slide": out_slide, "model_used": req.model, "gif_debug": gif_debug}


# ──────────────────────────────────────────────────────────────────────────
# CANVAS ÇİZİM (Pillow)
# ──────────────────────────────────────────────────────────────────────────
def _hex(c: str) -> tuple:
    c = (c or "#000000").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def ease_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def ease_out_back(t: float) -> float:
    t = max(0.0, min(1.0, t))
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def anim_state(anim: str, t: float) -> dict:
    st = {"dx": 0.0, "dy": 0.0, "scale": 1.0, "rot": 0.0, "alpha": 1.0, "reveal": 1.0}
    if anim == "none" or t >= 1:
        return st
    e = ease_out_cubic(t)
    eb = ease_out_back(t)
    if anim == "fade":
        st["alpha"] = e
    elif anim == "slide_up":
        st["dy"] = (1 - e) * 90; st["alpha"] = e
    elif anim == "slide_left":
        st["dx"] = (1 - e) * 140; st["alpha"] = e
    elif anim == "slide_right":
        st["dx"] = -(1 - e) * 140; st["alpha"] = e
    elif anim == "bounce" or anim == "drop_bounce":
        st["dy"] = (1 - eb) * -160; st["alpha"] = e
    elif anim == "scale_up":
        st["scale"] = 0.55 + 0.45 * eb; st["alpha"] = e
    elif anim == "rotate_in":
        st["rot"] = (1 - e) * -14; st["alpha"] = e
    elif anim == "skew_pop" or anim == "pop_side":
        st["dx"] = (1 if anim == "pop_side" else -1) * (1 - eb) * 120
        st["scale"] = 0.85 + 0.15 * eb; st["alpha"] = e
    elif anim == "typewriter":
        st["reveal"] = e
    else:
        st["alpha"] = e
    return st


def draw_deco(img: Image.Image, w: int, h: int, deco: str, color: str, opacity: float):
    if not deco or deco == "none" or opacity <= 0:
        return
    rgb = _hex(color)
    alpha = max(0, min(255, int(255 * (opacity / 100.0))))
    fill = rgb + (alpha,)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    scl = w / 1080
    if deco == "circles":
        for i in range(6):
            r = int((90 + i * 60) * scl)
            cx, cy = int(w * 0.5), int(h * 0.42)
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=fill, width=max(1, int(3 * scl)))
    elif deco in ("dots", "halftone", "dots_corner"):
        step = int((34 if deco != "halftone" else 20) * scl)
        radius = max(1, int((3 if deco != "halftone" else 2.4) * scl))
        x0, x1 = (0, w) if deco != "dots_corner" else (0, int(w * 0.45))
        y0, y1 = (0, h) if deco != "dots_corner" else (0, int(h * 0.35))
        for y in range(y0, y1, step):
            for x in range(x0, x1, step):
                d.ellipse([x - radius, y - radius, x + radius, y + radius], fill=fill)
    elif deco == "grid":
        step = int(70 * scl)
        for x in range(0, w, step):
            d.line([(x, 0), (x, h)], fill=fill, width=1)
        for y in range(0, h, step):
            d.line([(0, y), (w, y)], fill=fill, width=1)
    elif deco == "diagonal":
        step = int(60 * scl)
        for x in range(-h, w, step):
            d.line([(x, 0), (x + h, h)], fill=fill, width=max(1, int(2 * scl)))
    elif deco == "big_circle":
        r = int(420 * scl)
        cx, cy = int(w * 0.78), int(h * 0.22)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)
    elif deco == "arc_corner":
        r = int(500 * scl)
        d.pieslice([-r, -r, r, r], 0, 90, fill=fill)
    elif deco == "dot_arrow":
        step = int(40 * scl)
        for y in range(0, h, step):
            for x in range(0, w, step):
                d.ellipse([x - 2, y - 2, x + 2, y + 2], fill=fill)
        cx, cy = int(w * 0.5), int(h * 0.72)
        sz = int(70 * scl)
        d.polygon([(cx - sz, cy), (cx + sz, cy), (cx, cy + sz)], fill=fill)
    img.paste(layer, (0, 0), layer)


ALIGN_ANCHORS = {
    "top": ("center", "top"), "middle": ("center", "middle"), "bottom": ("center", "bottom"),
    "left": ("left", "middle"), "right": ("right", "middle"),
    "diagonal_tl": ("left", "top"), "diagonal_br": ("right", "bottom"),
    "cta": ("center", "bottom"),
}


def _contrasting_outline_color(rgb: tuple) -> tuple:
    def lum(c):
        def f(v):
            v = v / 255
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        r, g, b = c
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

    text_lum = lum(rgb)
    black_contrast = (text_lum + 0.05) / 0.05
    white_contrast = 1.05 / (text_lum + 0.05)
    return (0, 0, 0) if black_contrast >= white_contrast else (255, 255, 255)


def fit_font(family: str, text: str, size: int, max_w: float) -> ImageFont.FreeTypeFont:
    size = max(int(size), MIN_FONT_SIZE)
    font = get_font(family, size)
    while size > MIN_FONT_SIZE:
        bbox = font.getbbox(text or " ")
        tw = bbox[2] - bbox[0]
        if tw <= max_w:
            break
        size -= 4
        font = get_font(family, size)
    return font


def _paste_gif_frame_in_band(img: Image.Image, w: int, h: int, slide: dict,
                              gif_frame_path: Optional[Path], safe_scale: float):
    """Verilen GIF karesini, sahnenin gif_band bilgisine göre hesaplanan
    kutunun içine, en-boy oranını koruyarak (contain) ortalar ve yapıştırır.
    gif_frame_path None ise ya da açılamazsa hiçbir şey yapmaz (sessiz no-op)."""
    band = slide.get("gif_band")
    if not band or not gif_frame_path:
        return
    try:
        with Image.open(gif_frame_path) as gf:
            gf = gf.convert("RGBA")
            box = _gif_band_box_backend(slide, band, w, h, safe_scale)
            bx0, by0, bx1, by1 = box
            box_w, box_h = max(1, bx1 - bx0), max(1, by1 - by0)
            gw, gh = gf.size
            scale = min(box_w / gw, (box_h * GIF_MAX_HEIGHT_RATIO) / gh)
            scale = max(0.01, scale)
            nw, nh = max(1, int(gw * scale)), max(1, int(gh * scale))
            resized = gf.resize((nw, nh), Image.LANCZOS)
            px = bx0 + (box_w - nw) // 2
            py = by0 + (box_h - nh) // 2
            img.paste(resized, (px, py), resized)
    except Exception as e:
        log.warning(f"[GIF] kare yapıştırma hatası, atlanıyor: {e}")


def _gif_band_box_backend(slide: dict, band: str, w: int, h: int, safe_scale: float) -> tuple[int, int, int, int]:
    """_gif_band_box'ın (1080x1920 tabanlı) sonucunu, gerçek render
    çözünürlüğüne (w,h; ör. 720p) ölçekler. slide burada out_slide (snake_case
    backend formatı) olduğundan, _gif_band_box'ın beklediği camelCase şablon
    anahtarlarına küçük bir adapte yapıyoruz."""
    adapted = {
        "align": slide.get("align", "middle"),
        "lineHeight": slide.get("line_height", 1.15),
        "textPadding": slide.get("text_padding", 100),
        "lines": slide.get("lines", []),
    }
    x0, y0, x1, y1 = _gif_band_box(adapted, band)
    scl = w / 1080
    return (int(x0 * scl), int(y0 * scl), int(x1 * scl), int(y1 * scl))


def render_slide_frame(slide: dict, w: int, h: int, t_in_slide: float, safe_scale: float,
                        gif_frame_path: Optional[Path] = None) -> Image.Image:
    img = Image.new("RGB", (w, h), _hex(slide["bg"]))
    draw_deco(img, w, h, slide.get("deco", "none"), slide.get("deco_color", "#ffffff"),
              float(slide.get("deco_opacity", 12)))

    # GIF, metnin ARKASINDA değil, kendi boş bandında (metinle çakışmayan bir
    # kutuda) çizilir — deco'dan sonra, metinden önce yapıştırılır ki metin
    # her zaman en üstte, net okunur kalsın.
    _paste_gif_frame_in_band(img, w, h, slide, gif_frame_path, safe_scale)

    scl = w / 1080
    pad = float(slide.get("text_padding", 100)) * scl
    max_w = w - 2 * pad
    line_h_mult = float(slide.get("line_height", 1.15))
    anim_progress = min(1.0, t_in_slide / ANIM_DUR) if ANIM_DUR > 0 else 1.0
    st = anim_state(slide.get("text_anim", "none"), anim_progress)

    lines = slide.get("lines", []) or [{"text": "", "color": "#fff", "size": 100}]
    rendered = []
    total_h = 0.0
    for ln in lines:
        size = max(int(int(ln.get("size", 100)) * scl), MIN_FONT_SIZE)
        text = ln.get("text", "")
        if st["reveal"] < 1.0:
            n = max(0, int(len(text) * st["reveal"]))
            text = text[:n]
        font = fit_font(slide.get("font", "Anton"), text, size, max_w)
        bbox = font.getbbox(text or " ")
        lh = (bbox[3] - bbox[1]) * line_h_mult
        rendered.append((text, font, ln.get("color", "#ffffff"), lh, bbox))
        total_h += lh

    h_align, v_align = ALIGN_ANCHORS.get(slide.get("align", "middle"), ("center", "middle"))
    top = SAFE_TOP * scl * safe_scale
    bottom = h - SAFE_BOTTOM * scl * safe_scale
    if v_align == "top":
        y = top + 40 * scl
    elif v_align == "bottom":
        y = bottom - total_h - 40 * scl
    else:
        y = (top + bottom) / 2 - total_h / 2

    text_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    tdraw = ImageDraw.Draw(text_layer)
    for text, font, color, lh, bbox in rendered:
        tw = bbox[2] - bbox[0]
        if h_align == "left":
            x = pad
        elif h_align == "right":
            x = w - pad - tw
        else:
            x = (w - tw) / 2
        rgb = _hex(color)
        alpha = int(255 * st["alpha"])
        stroke_rgb = _contrasting_outline_color(rgb)
        stroke_w = max(2, int(round(font.size * 0.035)))
        tdraw.text(
            (x - bbox[0], y), text, font=font, fill=rgb + (alpha,),
            stroke_width=stroke_w, stroke_fill=stroke_rgb + (alpha,),
        )
        y += lh

    if st["dx"] or st["dy"] or st["rot"] or st["scale"] != 1.0:
        cx, cy = w / 2, h / 2
        if st["scale"] != 1.0:
            nw, nh = max(1, int(w * st["scale"])), max(1, int(h * st["scale"]))
            text_layer = text_layer.resize((nw, nh), Image.LANCZOS)
            canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            canvas.paste(text_layer, (int(cx - nw / 2), int(cy - nh / 2)), text_layer)
            text_layer = canvas
        if st["rot"]:
            text_layer = text_layer.rotate(st["rot"], resample=Image.BICUBIC, center=(cx, cy))
        if st["dx"] or st["dy"]:
            shifted = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            shifted.paste(text_layer, (int(st["dx"]), int(st["dy"])), text_layer)
            text_layer = shifted

    img.paste(text_layer, (0, 0), text_layer)

    logo_w = int(w * WATERMARK_WIDTH_RATIO)
    logo = get_logo(logo_w)
    if logo is not None:
        margin = int(w * WATERMARK_MARGIN_RATIO)
        lx = w - logo.width - margin
        ly = margin
        if WATERMARK_OPACITY < 1.0:
            alpha = logo.getchannel("A").point(lambda a: int(a * WATERMARK_OPACITY))
            logo = logo.copy()
            logo.putalpha(alpha)
        img.paste(logo, (lx, ly), logo)

    return img


# ──────────────────────────────────────────────────────────────────────────
# ffmpeg yardımcıları
# ──────────────────────────────────────────────────────────────────────────
def run_ffmpeg(args: list[str]):
    proc = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                           capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg hatası: {proc.stderr[-1500:]}")


def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def _spring_progress(t: float, tension: float = 20.0, friction: float = 7.0) -> float:
    if t <= 0:
        return 0.0
    if t >= 1:
        return 1.0
    omega = math.sqrt(tension)
    zeta = friction / (2 * math.sqrt(tension))
    zeta = min(zeta, 0.999)
    omega_d = omega * math.sqrt(1 - zeta ** 2)
    decay = math.exp(-zeta * omega * (t * 6))
    return 1 - decay * (math.cos(omega_d * (t * 6)) + (zeta * omega / max(omega_d, 1e-6)) * math.sin(omega_d * (t * 6)))


def render_logo_intro_frame(w: int, h: int, t: float, duration: float, bg_hex: str,
                             logo_width_ratio: float, show_app_name: bool = True,
                             display_text: Optional[str] = None) -> Image.Image:
    """display_text: logonun altında gösterilecek metin. None/boş ise
    varsayılan APP_NAME kullanılır; doluysa kullanıcının manuel girdiği
    açılış/kapanış metniyle DEĞİŞTİRİLİR (bkz. intro_text/outro_text)."""
    img = Image.new("RGB", (w, h), _hex(bg_hex))
    name_text = (display_text or "").strip() or APP_NAME

    letter_t = min(1.0, t / _LOGO_STAGE_LETTER_DUR) if _LOGO_STAGE_LETTER_DUR > 0 else 1.0
    letter_scale = _spring_progress(letter_t, tension=20, friction=7)

    circle_scales = []
    for i in range(len(OUTRO_CIRCLES)):
        start = _LOGO_STAGE_CIRCLE_START + i * _LOGO_STAGE_CIRCLE_STAGGER
        ct = (t - start) / _LOGO_STAGE_CIRCLE_DUR if _LOGO_STAGE_CIRCLE_DUR > 0 else 1.0
        ct = max(0.0, min(1.0, ct))
        circle_scales.append(_spring_progress(ct, tension=100, friction=6))

    target_w = int(w * logo_width_ratio)
    logo = get_logo_staged(target_w, letter_scale, circle_scales)

    fade_start = max(0.0, duration - _LOGO_STAGE_FADEOUT_DUR)
    if t <= fade_start or _LOGO_STAGE_FADEOUT_DUR <= 0:
        global_alpha = 1.0
    else:
        global_alpha = 1.0 - ease_out_cubic(min(1.0, (t - fade_start) / _LOGO_STAGE_FADEOUT_DUR))

    if logo is not None:
        composed = logo
        if global_alpha < 1.0:
            a_channel = composed.getchannel("A").point(lambda a: int(a * max(0.0, global_alpha)))
            composed = composed.copy()
            composed.putalpha(a_channel)
        lx = (w - composed.width) // 2
        ly = (h - composed.height) // 2 - int(h * 0.04)
        img.paste(composed, (lx, ly), composed)

    if show_app_name and name_text:
        name_t = (t - _LOGO_STAGE_NAME_START) / _LOGO_STAGE_NAME_DUR if _LOGO_STAGE_NAME_DUR > 0 else 1.0
        name_alpha = ease_out_cubic(max(0.0, min(1.0, name_t))) * max(0.0, global_alpha)
        if name_alpha > 0.002:
            font_size = max(18, int(w * 0.075))
            font = get_font("Poppins", font_size)
            txt_layer = Image.new("RGBA", (w, font_size * 2), (0, 0, 0, 0))
            tdraw = ImageDraw.Draw(txt_layer)
            bbox = tdraw.textbbox((0, 0), name_text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            tdraw.text(((w - tw) / 2 - bbox[0], (font_size * 2 - th) / 2 - bbox[1]),
                        name_text, font=font, fill=(255, 255, 255, int(255 * name_alpha)))
            name_y = (h // 2) + int(h * 0.16)
            img.paste(txt_layer, (0, name_y), txt_layer)

    return img


def _render_logo_intro_clip(job_dir: Path, subdir_name: str, w: int, h: int, fps: int,
                             preset: str, crf: int, duration: float, bg_hex: str,
                             logo_width_ratio: float,
                             display_text: Optional[str] = None) -> tuple[Path, float]:
    clip_dir = job_dir / subdir_name
    clip_dir.mkdir(exist_ok=True)
    n_frames = max(1, int(duration * fps))
    for fi in range(n_frames):
        t = fi / fps
        frame = render_logo_intro_frame(w, h, t, duration, bg_hex, logo_width_ratio,
                                         display_text=display_text)
        frame.save(clip_dir / f"{fi:05d}.png")

    audio_path = clip_dir / "audio.mp3"
    run_ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", f"{duration:.3f}",
                "-q:a", "9", str(audio_path)])

    clip_path = clip_dir / "clip.mp4"
    run_ffmpeg([
        "-framerate", str(fps), "-i", str(clip_dir / "%05d.png"),
        "-i", str(audio_path),
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        "-r", str(fps), str(clip_path),
    ])
    return clip_path, duration


def render_intro_clip(job_dir: Path, w: int, h: int, fps: int, preset: str, crf: int,
                       display_text: Optional[str] = None) -> tuple[Path, float]:
    return _render_logo_intro_clip(job_dir, "intro", w, h, fps, preset, crf,
                                    INTRO_DURATION, INTRO_BG, INTRO_LOGO_WIDTH_RATIO,
                                    display_text=display_text)


def render_outro_clip(job_dir: Path, w: int, h: int, fps: int, preset: str, crf: int,
                       display_text: Optional[str] = None) -> tuple[Path, float]:
    return _render_logo_intro_clip(job_dir, "outro", w, h, fps, preset, crf,
                                    OUTRO_DURATION, OUTRO_BG, OUTRO_LOGO_WIDTH_RATIO,
                                    display_text=display_text)


MUSHAF_RECITER_NAMES = {
    "10": "Sa‘ud ash-Shuraym",
    "2": "AbdulBaset AbdulSamad (Murattal)",
    "1": "AbdulBaset AbdulSamad (Mücevved)",
    "4": "Abu Bakr al-Shatri",
    "7": "Mishari Rashid al-‘Afasy",
    "3": "Abdur-Rahman as-Sudais",
    "9": "Mohamed Siddiq al-Minshawi (Murattal)",
    "8": "Mohamed Siddiq al-Minshawi (Mücevved)",
}


def render_mushaf_title_frame(w: int, h: int, surah_name: str, reciter_name: str,
                               bg_hex: str, alpha: float = 1.0,
                               reciter_label: str = "Okuyucu") -> Image.Image:
    """reciter_label: "Okuyucu" etiketinin gösterileceği metin. Varsayılan
    Türkçe'dir; render_mushaf_job(), seçilen arayüz diline göre çevrilmiş
    (translate_ui_dict()'ten gelen ui_dict["reciter"]) değeri buraya geçirir
    — böylece açılış ekranındaki bu etiket de UI çevirisinin geri kalanıyla
    (Oynat/Durdur/Ayarlar vb.) TUTARLI biçimde hedef dilde görünür. Önceden
    burada sabit "Okuyucu" gömülüydü ve dil ne olursa olsun ASLA çevrilmiyordu."""
    img = Image.new("RGB", (w, h), _hex(bg_hex))
    draw = ImageDraw.Draw(img, "RGBA")
    a = max(0.0, min(1.0, alpha))

    surah_font = get_font("Amiri", max(30, int(w * 0.115)))
    label_font = get_font("Poppins", max(16, int(w * 0.042)))
    reciter_font = get_font("Poppins", max(18, int(w * 0.05)))

    surah_color = (255, 255, 255, int(255 * a))
    gold_color = (212, 175, 55, int(255 * a))

    bbox = draw.textbbox((0, 0), surah_name, font=surah_font)
    sw, sh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((w - sw) / 2 - bbox[0], h / 2 - sh - 18 - bbox[1]), surah_name,
              font=surah_font, fill=surah_color)

    label = reciter_label or "Okuyucu"
    lbbox = draw.textbbox((0, 0), label, font=label_font)
    lw, lh = lbbox[2] - lbbox[0], lbbox[3] - lbbox[1]
    label_y = h / 2 + 24
    draw.text(((w - lw) / 2 - lbbox[0], label_y - lbbox[1]), label, font=label_font, fill=gold_color)

    rbbox = draw.textbbox((0, 0), reciter_name, font=reciter_font)
    rw, rh = rbbox[2] - rbbox[0], rbbox[3] - rbbox[1]
    reciter_y = label_y + lh + 12
    draw.text(((w - rw) / 2 - rbbox[0], reciter_y - rbbox[1]), reciter_name,
              font=reciter_font, fill=surah_color)

    return img


def _render_mushaf_title_clip(job_dir: Path, w: int, h: int, fps: int, preset: str, crf: int,
                               surah_name: str, reciter_name: str, bg_hex: str,
                               duration: float, reciter_label: str = "Okuyucu") -> tuple[Path, float]:
    clip_dir = job_dir / "mushaf_title"
    clip_dir.mkdir(exist_ok=True)
    n_frames = max(1, int(duration * fps))
    fade_frames = max(1, int(0.4 * fps))
    for fi in range(n_frames):
        alpha = min(1.0, (fi + 1) / fade_frames)
        frame = render_mushaf_title_frame(w, h, surah_name, reciter_name, bg_hex, alpha,
                                           reciter_label=reciter_label)
        frame.save(clip_dir / f"{fi:05d}.png")

    audio_path = clip_dir / "audio.mp3"
    run_ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", f"{duration:.3f}",
                "-q:a", "9", str(audio_path)])

    clip_path = clip_dir / "clip.mp4"
    run_ffmpeg([
        "-framerate", str(fps), "-i", str(clip_dir / "%05d.png"),
        "-i", str(audio_path),
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        "-r", str(fps), str(clip_path),
    ])
    return clip_path, duration


_mushaf_static_cache: dict = {}
_mushaf_static_lock = threading.Lock()


def _load_mushaf_static_data() -> dict:
    with _mushaf_static_lock:
        if _mushaf_static_cache:
            return _mushaf_static_cache
        data_dir = MUSHAF_VIEWER_DIR / "data" / "QuranData"
        with open(data_dir / "pages.json", encoding="utf-8") as f:
            pages_raw = json.load(f)
        with open(data_dir / "qpc-v1-glyph-codes-wbw.json", encoding="utf-8") as f:
            words_raw = json.load(f)
        with open(data_dir / "page_start.json", encoding="utf-8") as f:
            page_start_raw = json.load(f)

        words_by_id = {w["id"]: w for w in words_raw.values()}
        page_start = {p["page"]: {"surah": int(p["surah"]), "ayah": int(p["ayah"])} for p in page_start_raw}
        page_end: dict[int, dict] = {}
        for ln in pages_raw:
            if ln.get("line_type") != "ayah" or ln.get("first_word_id") in ("", None):
                continue
            last_w = words_by_id.get(ln["last_word_id"]) or words_by_id.get(ln["first_word_id"])
            if not last_w:
                continue
            page_end[ln["page_number"]] = {"surah": int(last_w["surah"]), "ayah": int(last_w["ayah"])}

        _mushaf_static_cache.update({"page_start": page_start, "page_end": page_end})
        return _mushaf_static_cache


def get_mushaf_page_range(page: int) -> dict:
    data = _load_mushaf_static_data()
    start = data["page_start"].get(page)
    end = data["page_end"].get(page)
    if not start or not end:
        raise ValueError(f"Sayfa {page} için veri bulunamadı.")
    return {"page": page, "start": start, "end": end}


# ──────────────────────────────────────────────────────────────────────────
# CTA sahnesi: App Store / Google Play rozetleri + (varsa) QR kod.
# Outro'nun (logo animasyonu) HEMEN ARDINDAN, ayrı bir mini-klip olarak
# eklenir — mevcut logo animasyonuna dokunmadan CTA'yı güçlendirir. Store
# URL'leri tanımlı değilse bu klip hiç üretilmez (render_job içinde atlanır).
# ──────────────────────────────────────────────────────────────────────────
_QR_CACHE: dict[str, Image.Image] = {}


def _make_qr_image(url: str, box_px: int) -> Optional[Image.Image]:
    """Verilen URL için kare bir QR kod görseli üretir (siyah/beyaz, kenarlıksız
    kırpılmış). qrcode kütüphanesi kurulu değilse None döner (çağıran taraf
    QR'sız devam eder)."""
    if not QRCODE_AVAILABLE or not url:
        return None
    cache_key = f"{url}::{box_px}"
    if cache_key in _QR_CACHE:
        return _QR_CACHE[cache_key]
    try:
        qr = qrcode.QRCode(border=1, box_size=10, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
        img = img.resize((box_px, box_px), Image.LANCZOS)
        _QR_CACHE[cache_key] = img
        return img
    except Exception as e:
        log.warning(f"[QR] Üretilemedi ({url[:60]}): {e}")
        return None


def _draw_store_badge(draw: ImageDraw.ImageDraw, img: Image.Image, x: int, y: int,
                       badge_w: int, badge_h: int, label_top: str, label_bottom: str):
    """Sade, markasız (App/Play Store logosu KULLANMADAN — telif/marka
    kısıtlarına takılmamak için) ama tanıdık bir "mağaza rozeti" görünümü:
    yuvarlak köşeli koyu kutu + iki satır metin (ör. "İNDİR" / "App Store")."""
    radius = int(badge_h * 0.22)
    draw.rounded_rectangle([x, y, x + badge_w, y + badge_h], radius=radius,
                            fill=(255, 255, 255, 255), outline=(255, 255, 255, 60), width=2)
    top_font = get_font("Poppins", int(badge_h * 0.26))
    bottom_font = get_font("Oswald", int(badge_h * 0.34))
    tb = draw.textbbox((0, 0), label_top, font=top_font)
    tw = tb[2] - tb[0]
    draw.text((x + (badge_w - tw) / 2 - tb[0], y + badge_h * 0.14), label_top,
               font=top_font, fill=(90, 90, 90, 255))
    bb = draw.textbbox((0, 0), label_bottom, font=bottom_font)
    bw = bb[2] - bb[0]
    draw.text((x + (badge_w - bw) / 2 - bb[0], y + badge_h * 0.46), label_bottom,
               font=bottom_font, fill=(17, 17, 17, 255))


def render_cta_frame(w: int, h: int, t: float, duration: float, bg_hex: str) -> Image.Image:
    """Mağaza rozetleri + QR kodu ortalanmış şekilde gösteren sahne. Basit bir
    fade-in ile başlar (0 -> 0.35sn), geri kalan süre sabit durur."""
    img = Image.new("RGB", (w, h), _hex(bg_hex))
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    fade_in_dur = 0.35
    alpha = min(1.0, t / fade_in_dur) if fade_in_dur > 0 else 1.0
    alpha = ease_out_cubic(alpha)

    has_ios = bool(APP_STORE_URL)
    has_android = bool(GOOGLE_PLAY_URL)
    badges = [b for b in [
        ("İNDİR", "App Store", APP_STORE_URL) if has_ios else None,
        ("İNDİR", "Google Play", GOOGLE_PLAY_URL) if has_android else None,
    ] if b]

    badge_w, badge_h = int(w * 0.60), int(h * 0.09)
    gap = int(h * 0.025)
    badges_h = len(badges) * badge_h + max(0, len(badges) - 1) * gap

    qr_url = APP_STORE_URL or GOOGLE_PLAY_URL
    qr_size = int(w * 0.30)
    qr_pad = int(w * 0.02)
    qr_gap_top = int(h * 0.035) if badges else 0
    scan_gap = int(h * 0.018)
    scan_font = get_font("Poppins", int(w * 0.038))
    scan_txt = "Taratıp hemen indir"
    has_qr = QRCODE_AVAILABLE and bool(qr_url)
    qr_block_h = (qr_gap_top + qr_size + qr_pad * 2 + scan_gap + int(h * 0.045)) if has_qr else 0

    total_h = badges_h + qr_block_h
    by = (h - total_h) // 2

    for label_top, label_bottom, _url in badges:
        bx = (w - badge_w) // 2
        _draw_store_badge(draw, layer, bx, by, badge_w, badge_h, label_top, label_bottom)
        by += badge_h + gap
    if badges:
        by = by - gap  # son rozetten sonraki fazladan gap'i geri al

    qr_img = _make_qr_image(qr_url, qr_size) if has_qr else None
    if qr_img is not None:
        qy = by + qr_gap_top
        qx = (w - qr_img.width) // 2
        draw.rounded_rectangle(
            [qx - qr_pad, qy - qr_pad, qx + qr_img.width + qr_pad, qy + qr_img.height + qr_pad],
            radius=int(qr_pad * 1.2), fill=(255, 255, 255, 255),
        )
        layer.paste(qr_img, (qx, qy), qr_img)
        sb = draw.textbbox((0, 0), scan_txt, font=scan_font)
        sw = sb[2] - sb[0]
        sy = qy + qr_img.height + qr_pad + scan_gap
        draw.text(((w - sw) / 2 - sb[0], sy), scan_txt, font=scan_font, fill=(255, 255, 255, 235))

    if alpha < 1.0:
        a_channel = layer.getchannel("A").point(lambda a: int(a * alpha))
        layer.putalpha(a_channel)
    img.paste(layer, (0, 0), layer)
    return img


def render_cta_clip(job_dir: Path, w: int, h: int, fps: int, preset: str, crf: int) -> tuple[Path, float]:
    clip_dir = job_dir / "cta"
    clip_dir.mkdir(exist_ok=True)
    n_frames = max(1, int(CTA_DURATION * fps))
    for fi in range(n_frames):
        t = fi / fps
        frame = render_cta_frame(w, h, t, CTA_DURATION, CTA_BG)
        frame.save(clip_dir / f"{fi:05d}.png")

    audio_path = clip_dir / "audio.mp3"
    run_ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", f"{CTA_DURATION:.3f}",
                "-q:a", "9", str(audio_path)])

    clip_path = clip_dir / "clip.mp4"
    run_ffmpeg([
        "-framerate", str(fps), "-i", str(clip_dir / "%05d.png"),
        "-i", str(audio_path),
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        "-r", str(fps), str(clip_path),
    ])
    return clip_path, CTA_DURATION


TTS_TIMEOUT_SECONDS = 20.0
TTS_MAX_RETRIES = 2


def _rate_to_ssml_rate(rate: Optional[float]) -> str:
    if rate is None:
        return "+0%"
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        return "+0%"
    rate = max(0.5, min(2.0, rate))
    pct = round((rate - 1.0) * 100)
    return f"{'+' if pct >= 0 else ''}{pct}%"


def _rate_to_atempo(rate: Optional[float]) -> float:
    """ElevenLabs API'si edge-tts'teki gibi bir 'konuşma hızı' parametresi
    sunmadığından, aynı 0.5x–2.0x aralığını ffmpeg'in atempo filtresiyle
    sentezleme SONRASI uygulayarak iki sağlayıcı arasında tutarlı davranış
    sağlıyoruz."""
    if rate is None:
        return 1.0
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        return 1.0
    return max(0.5, min(2.0, rate))


async def _synth_edge_tts(text: str, out_path: Path, voice_id: str, rate: Optional[float]) -> None:
    ssml_rate = _rate_to_ssml_rate(rate)
    last_err: Optional[Exception] = None
    for attempt in range(1, TTS_MAX_RETRIES + 1):
        try:
            communicate = edge_tts.Communicate(text, voice_id, rate=ssml_rate)
            await asyncio.wait_for(communicate.save(str(out_path)), timeout=TTS_TIMEOUT_SECONDS)
            if out_path.exists() and out_path.stat().st_size > 0:
                return
            last_err = RuntimeError("edge_tts boş/geçersiz ses dosyası üretti")
        except asyncio.TimeoutError:
            last_err = RuntimeError(
                f"edge_tts {TTS_TIMEOUT_SECONDS:.0f} sn içinde yanıt vermedi "
                f"(deneme {attempt}/{TTS_MAX_RETRIES}) — ağ/firewall Microsoft TTS "
                f"servisine erişimi engelliyor olabilir."
            )
        except Exception as e:
            last_err = e
        log.warning(f"[TTS/edge] Deneme {attempt}/{TTS_MAX_RETRIES} başarısız: {last_err}")

    raise RuntimeError(f"edge-tts ile seslendirme üretilemedi: {last_err}")


async def _synth_elevenlabs_tts(text: str, out_path: Path, voice_id: str, rate: Optional[float]) -> None:
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY tanımlı değil.")

    url = ELEVENLABS_TTS_URL.format(voice_id=voice_id)
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": text,
        "model_id": ELEVENLABS_MODEL_ID,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }

    raw_path = out_path.with_suffix(".raw.mp3")
    last_err: Optional[Exception] = None
    for attempt in range(1, TTS_MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=ELEVENLABS_TIMEOUT_SECONDS) as client:
                res = await client.post(url, headers=headers, json=payload)
            if res.status_code == 200 and res.content:
                raw_path.write_bytes(res.content)
                break
            last_err = RuntimeError(
                f"ElevenLabs API hatası ({res.status_code}): {res.text[:200]}"
            )
        except asyncio.TimeoutError:
            last_err = RuntimeError(
                f"ElevenLabs {ELEVENLABS_TIMEOUT_SECONDS:.0f} sn içinde yanıt vermedi "
                f"(deneme {attempt}/{TTS_MAX_RETRIES})."
            )
        except Exception as e:
            last_err = e
        log.warning(f"[TTS/elevenlabs] Deneme {attempt}/{TTS_MAX_RETRIES} başarısız: {last_err}")
    else:
        raise RuntimeError(f"ElevenLabs ile seslendirme üretilemedi: {last_err}")

    atempo = _rate_to_atempo(rate)
    try:
        if abs(atempo - 1.0) < 0.01:
            shutil.move(str(raw_path), str(out_path))
        else:
            run_ffmpeg(["-i", str(raw_path), "-filter:a", f"atempo={atempo:.3f}", str(out_path)])
    finally:
        raw_path.unlink(missing_ok=True)


async def synth_tts(text: str, out_path: Path, voice: Optional[str] = None, rate: Optional[float] = None):
    text = text.strip()
    voice_info = VOICE_MAP.get(voice) if voice in VOICE_IDS else VOICE_MAP.get(DEFAULT_VOICE)
    resolved_voice = voice_info["id"] if voice_info else DEFAULT_VOICE
    provider = voice_info["provider"] if voice_info else "edge"

    if not text:
        run_ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "0.4",
                    "-q:a", "9", str(out_path)])
        return

    if provider == "elevenlabs":
        await _synth_elevenlabs_tts(text, out_path, resolved_voice, rate)
    else:
        await _synth_edge_tts(text, out_path, resolved_voice, rate)


def synth_tts_sync(text: str, out_path: Path, voice: Optional[str] = None, rate: Optional[float] = None) -> None:
    """synth_tts()'i, Playwright'ın sync_api'sinin (Windows'ta arka planda
    kendi event loop'unu çalıştırabilen) mevcut thread'iyle ÇAKIŞMAYACAK
    şekilde çağırır. render_job'daki 'loop = asyncio.new_event_loop();
    loop.run_until_complete(...)' deseni, Playwright açık olan bir
    render_quiz_job thread'inde 'Cannot run the event loop while another
    loop is running' hatası veriyordu — çözüm, TTS'i tamamen İZOLE, kendi
    başına yeni bir thread'de (dolayısıyla kendi başına yeni bir event
    loop'ta) çalıştırmak. Bu thread'in mevcut thread'in asyncio durumuyla
    hiçbir teması olmadığı için çakışma imkansız hale gelir."""
    error_holder: dict = {}

    def _runner():
        try:
            asyncio.run(synth_tts(text, out_path, voice, rate))
        except Exception as e:
            error_holder["error"] = e

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join()
    if "error" in error_holder:
        raise error_holder["error"]


# ──────────────────────────────────────────────────────────────────────────
# Render job — arka plan iş kuyruğu
# ──────────────────────────────────────────────────────────────────────────
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def set_job(job_id: str, **kw):
    with JOBS_LOCK:
        JOBS[job_id].update(kw)


def render_job(job_id: str, req: RenderRequest):
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    # Bu job'da kullanılan GIF cache klasörlerinin anahtarları — video kullanıcıya
    # gönderildikten (ya da hata ile sonlandıktan) SONRA, finally bloğunda
    # gif_cache/ içinden silinecek. Böylece gif_cache/ hiçbir render sonrası
    # veri biriktirmez (disk şişmez); aynı GIF aynı render içinde birden
    # fazla sahnede kullanılsa bile indirme sırasında hâlâ önbellekten
    # paylaşılır (yalnızca render bitince temizlenir).
    _job_gif_cache_keys: set[str] = set()
    try:
        w, h = RESOLUTION_MAP[req.resolution]
        fps = SPEED_FPS[req.render_speed]
        preset = SPEED_PRESET[req.render_speed]
        crf = QUALITY_CRF[req.quality]
        safe_scale = h / 1920

        set_job(job_id, status="processing", progress=1, message="Seslendirme üretiliyor...")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        n = len(req.slides)

        # ── GIF karelerini ÖNCEDEN indir (paralel, tüm sahneler için) ──
        # Böylece kare üretimi aşamasında (senkron döngü) her sahne için
        # ağdan beklemek yerine, önceden diske inmiş kareler arasından
        # döngüsel (loop) seçim yapılır.
        set_job(job_id, progress=1, message="GIF'ler hazırlanıyor...")
        gif_urls = [s.gif_url for s in req.slides if s.gif_url]
        gif_frames_map: dict[str, list[tuple[Path, float]]] = {}
        if gif_urls:
            unique_urls = list(dict.fromkeys(gif_urls))
            _job_gif_cache_keys.update(_gif_cache_key(u) for u in unique_urls)
            results = loop.run_until_complete(asyncio.gather(
                *[_get_gif_frames_cached(u) for u in unique_urls],
                return_exceptions=True,
            ))
            for u, r in zip(unique_urls, results):
                if isinstance(r, Exception) or not r:
                    log.warning(f"[GIF] '{u[:80]}' render için hazırlanamadı, o sahne(ler) GIF'siz devam eder.")
                    continue
                gif_frames_map[u] = r

        # ── UYGULAMA EKRAN GÖRÜNTÜSÜ ── screenshot_url dolu olan sahneler için
        # gerçek PNG/JPG dosyasını, GIF pipeline'ının beklediği "kare listesi"
        # formatına (tek kare, sabit süre) sarıp gif_frames_map'e ekliyoruz.
        # Böylece render döngüsü (aşağıda) GIF ile ekran görüntüsü arasında
        # hiçbir fark gözetmeden aynı yapıştırma/ölçekleme mantığını kullanır.
        screenshot_frames_map: dict[str, list[tuple[Path, float]]] = {}
        for s in req.slides:
            su = s.screenshot_url
            if not su or su in screenshot_frames_map:
                continue
            filename = su.rsplit("/", 1)[-1]
            local_path = _resolve_screenshot_path(filename)
            if local_path:
                screenshot_frames_map[su] = [(local_path, 1_000_000.0)]
            else:
                log.warning(f"[SCREENSHOT] '{su}' render için bulunamadı, o sahne GIF'e/boş banda düşer.")

        # ══════════════════════════════════════════════════════════════
        # AŞAMA 1/2: seslendirme + ham süre hesabı
        # ══════════════════════════════════════════════════════════════
        scene_data: list[dict] = []
        for i, slide in enumerate(req.slides):
            s = slide.model_dump()
            slide_dir = job_dir / f"slide_{i:03d}"
            slide_dir.mkdir(exist_ok=True)

            narration = " ".join(l["text"] for l in s["lines"] if l.get("text"))
            scene_voice = s.get("voice") or DEFAULT_VOICE
            scene_rate = s.get("voice_rate")
            audio_path = slide_dir / "audio.mp3"
            try:
                if s.get("muted"):
                    run_ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "0.4",
                                "-q:a", "9", str(audio_path)])
                else:
                    loop.run_until_complete(synth_tts(narration, audio_path, scene_voice, scene_rate))
            except Exception as tts_err:
                log.error(f"[TTS] Sahne {i + 1} için seslendirme başarısız, sessize düşülüyor: {tts_err}")
                set_job(job_id, message=f"⚠️ Sahne {i + 1}: seslendirme başarısız (sessiz devam ediliyor)")
                run_ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "1.0",
                            "-q:a", "9", str(audio_path)])
            audio_dur = probe_duration(audio_path)
            raw_duration = max(float(s.get("duration", 2.5)), min(audio_dur, 8.0))

            gif_frames = screenshot_frames_map.get(s.get("screenshot_url") or "") or gif_frames_map.get(s.get("gif_url") or "")
            if s.get("screenshot_url") and not screenshot_frames_map.get(s.get("screenshot_url")):
                set_job(job_id, message=f"⚠️ Sahne {i + 1}: uygulama ekran görüntüsü bulunamadı, GIF'e düşülüyor")
            elif s.get("gif_url") and not gif_frames:
                # GIF istenmiş ama hazırlanamamış — sahne GIF'siz render edilecek,
                # kullanıcıyı bilgilendir ama render'ı durdurma.
                set_job(job_id, message=f"⚠️ Sahne {i + 1}: GIF yüklenemedi, GIF'siz devam ediliyor")

            scene_data.append({
                "slide": s, "slide_dir": slide_dir, "audio_path": audio_path,
                "audio_dur": audio_dur, "raw_duration": raw_duration,
                "gif_frames": gif_frames,
            })
            set_job(job_id, progress=2 + int((i + 1) / max(n, 1) * 20),
                    message=f"Sahne {i + 1}/{n} seslendirildi...")

        raw_total = sum(sd["raw_duration"] for sd in scene_data)
        scale = 1.0
        if raw_total > 0:
            if raw_total < MIN_TOTAL_VIDEO_DURATION:
                scale = MIN_TOTAL_VIDEO_DURATION / raw_total
            elif raw_total > MAX_TOTAL_VIDEO_DURATION:
                scale = MAX_TOTAL_VIDEO_DURATION / raw_total

        for sd in scene_data:
            scaled = sd["raw_duration"] * scale
            sd["final_duration"] = max(scaled, min(sd["audio_dur"], sd["raw_duration"]))

        if abs(scale - 1.0) > 0.01:
            new_total = sum(sd["final_duration"] for sd in scene_data)
            log.info(
                f"[SÜRE NORMALİZASYONU] job={job_id} ham_toplam={raw_total:.1f}sn "
                f"-> hedef_toplam≈{new_total:.1f}sn (ölçek={scale:.2f}, "
                f"aralık {MIN_TOTAL_VIDEO_DURATION:.0f}-{MAX_TOTAL_VIDEO_DURATION:.0f}sn)"
            )

        # ══════════════════════════════════════════════════════════════
        # AŞAMA 2/2: kareleri çiz (GIF varsa her karede döngüsel ilerlet),
        # sesi (gerekirse) sessizlikle tamamla, klibi üret.
        # ══════════════════════════════════════════════════════════════
        slide_clips: list[Path] = []
        durations: list[float] = []

        for i, sd in enumerate(scene_data):
            s = sd["slide"]
            slide_dir = sd["slide_dir"]
            audio_path = sd["audio_path"]
            audio_dur = sd["audio_dur"]
            duration = sd["final_duration"]
            gif_frames = sd.get("gif_frames")
            durations.append(duration)

            if audio_dur < duration - 0.05:
                padded_path = slide_dir / "audio_padded.mp3"
                run_ffmpeg([
                    "-i", str(audio_path), "-af",
                    f"apad=whole_dur={duration:.3f}", str(padded_path),
                ])
                audio_path = padded_path

            set_job(job_id, progress=22 + int(i / max(n, 1) * 60),
                    message=f"Sahne {i + 1}/{n} çiziliyor...")
            n_frames = max(1, int(duration * fps))

            # GIF'in kendi frame-süresi zaman çizelgesini takip etmek için
            # basit bir "hangi GIF karesi hangi video karesinde gösterilsin"
            # eşlemesi: GIF'in toplam süresi videodakinden kısaysa baştan
            # döngüye alınır (loop), uzunsa GIF video süresinde kesilir.
            gif_total_dur = sum(d for _, d in gif_frames) if gif_frames else 0.0

            # BUG FİX: sahne süresi GIF'in doğal süresinden kısaysa, `t % gif_total_dur`
            # hiçbir zaman gif_total_dur'a ulaşamıyordu — yani GIF ASLA döngüye
            # girmiyor, sahne boyunca sadece GIF'in ilk (genelde en durağan)
            # birkaç karesi gösteriliyordu; bu da "GIF sanki durağan bir resimmiş
            # gibi" görünmesine yol açıyordu. Çözüm: sahne, GIF'ten kısaysa zaman
            # çizelgesini hızlandır (gif_speed>1) ki tam bir döngü (ya da makul bir
            # üst sınıra kadar mümkün olduğunca fazlası) sahne süresine sığsın.
            gif_speed = 1.0
            if gif_frames and gif_total_dur > 0 and duration > 0 and gif_total_dur > duration:
                gif_speed = min(GIF_MAX_SPEEDUP, gif_total_dur / duration)

            def _gif_frame_for_time(t: float) -> Optional[Path]:
                if not gif_frames or gif_total_dur <= 0:
                    return None
                tt = (t * gif_speed) % gif_total_dur
                acc = 0.0
                for fp, fd in gif_frames:
                    acc += fd
                    if tt < acc:
                        return fp
                return gif_frames[-1][0]

            # ── HIZLANDIRMA: GIF olmayan sahnelerde, animasyon bittikten
            # sonra içerik statikleşir -> son kareyi kopyala (eskisi gibi).
            # GIF olan sahnelerde ise GIF her karede değiştiği için (metin
            # animasyonu bitmiş olsa bile) HER KARE yeniden çizilmek
            # zorunda — bu sahnelerde hızlandırma uygulanmaz.
            last_frame_path: Optional[Path] = None
            for fi in range(n_frames):
                t = fi / fps
                frame_path = slide_dir / f"{fi:05d}.png"
                if gif_frames:
                    gif_frame = _gif_frame_for_time(t)
                    frame = render_slide_frame(s, w, h, t, safe_scale, gif_frame_path=gif_frame)
                    frame.save(frame_path)
                elif t <= ANIM_DUR or last_frame_path is None:
                    frame = render_slide_frame(s, w, h, t, safe_scale)
                    frame.save(frame_path)
                    last_frame_path = frame_path
                else:
                    shutil.copyfile(last_frame_path, frame_path)

            clip_path = slide_dir / "clip.mp4"
            run_ffmpeg([
                "-framerate", str(fps), "-i", str(slide_dir / "%05d.png"),
                "-i", str(audio_path),
                "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                "-r", str(fps), str(clip_path),
            ])
            slide_clips.append(clip_path)

        set_job(job_id, progress=85, message="Kapanış logosu ekleniyor...")
        try:
            outro_clip_path, outro_duration = render_outro_clip(job_dir, w, h, fps, preset, crf)
            slide_clips.append(outro_clip_path)
            durations.append(outro_duration)
            has_outro = True
        except Exception as outro_err:
            log.error(f"[OUTRO] Kapanış logosu render hatası, outro'suz devam ediliyor: {outro_err}")
            set_job(job_id, message="⚠️ Kapanış logosu eklenemedi, video onsuz devam ediyor")
            has_outro = False

        # ── CTA (mağaza rozetleri + QR) ── Outro logosunun HEMEN ARDINDAN
        # eklenir. Yalnızca en az bir mağaza URL'i (.env: APP_STORE_URL /
        # GOOGLE_PLAY_URL) tanımlıysa render edilir; tanımlı değilse video
        # eskisi gibi sadece logo outro'suyla biter (davranış bozulmaz).
        has_cta = False
        if has_outro and (APP_STORE_URL or GOOGLE_PLAY_URL):
            try:
                cta_clip_path, cta_duration = render_cta_clip(job_dir, w, h, fps, preset, crf)
                slide_clips.append(cta_clip_path)
                durations.append(cta_duration)
                has_cta = True
            except Exception as cta_err:
                log.error(f"[CTA] Mağaza rozeti/QR render hatası, CTA'sız devam ediliyor: {cta_err}")
                set_job(job_id, message="⚠️ Mağaza rozetleri eklenemedi, video onsuz devam ediyor")
                has_cta = False

        outro_slide = SlideIO(transition="fade")
        cta_slide = SlideIO(transition="fade")
        transition_slides = list(req.slides)
        if has_outro:
            transition_slides = transition_slides + [outro_slide]
        if has_cta:
            transition_slides = transition_slides + [cta_slide]

        set_job(job_id, progress=90, message="Sahneler birleştiriliyor (geçişler)...")
        final_path = _concat_with_transitions(job_dir, slide_clips, durations, transition_slides, fps)

        out_name = f"reel_{job_id}.mp4"
        out_path = OUT_DIR / out_name
        shutil.copy(final_path, out_path)

        set_job(job_id, status="done", progress=100, message="Tamamlandı", file=f"/output/{out_name}")
    except Exception as e:
        log.error("Render hatası: %s", traceback.format_exc())
        set_job(job_id, status="error", message=str(e))
    finally:
        # ── work/ ve gif_cache/ GENEL TEMİZLİĞİ ──
        # Kullanıcı isteği: bu job bittiğinde (video output/'a kopyalandığı
        # ya da hata ile sonlandığı an) sadece kendi job klasörünü değil,
        # work/ altındaki TÜM eski/artık job klasörlerini de temizle — ve
        # gif_cache/'i tamamen boşalt (sadece bu job'ın anahtarlarını değil).
        # Şu an hâlâ render edilmekte olan (queued/running) başka job'ların
        # klasörlerine dokunulmaz; JOBS'taki status alanına bakılır.
        with JOBS_LOCK:
            active_job_ids = {
                jid for jid, info in JOBS.items()
                if info.get("status") not in ("done", "error")
            }
        try:
            for entry in WORK_DIR.iterdir():
                if entry.name == "previews":
                    continue  # TTS önizleme dosyaları burada tutulur, ayrı yönetiliyor
                if entry.is_dir() and entry.name in active_job_ids:
                    continue  # hâlâ render edilen başka bir job — dokunma
                shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() else entry.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        try:
            for entry in GIF_CACHE_DIR.iterdir():
                shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() else entry.unlink(missing_ok=True)
        except FileNotFoundError:
            pass


TRANSITION_MAP = {
    "none": "fade",
    "fade": "fade",
    "slide_up": "slideup",
    "slide_left": "slideleft",
    "zoom_in": "zoomin",
}


def _concat_with_transitions(job_dir: Path, clips: list[Path], durations: list[float],
                              slides: list[SlideIO], fps: int) -> Path:
    if len(clips) == 1:
        return clips[0]

    inputs = []
    for c in clips:
        inputs += ["-i", str(c)]

    v_chain = "[0:v]"
    a_chain = "[0:a]"
    filter_parts = []
    cum_dur = durations[0]
    for i in range(1, len(clips)):
        trans_name = TRANSITION_MAP.get(slides[i].transition, "fade")
        is_hard_cut = slides[i].transition in ("none", None)
        d = 0.12 if is_hard_cut else min(TRANS_DUR_MAX, durations[i - 1] * 0.4, durations[i] * 0.4)
        d = max(0.06, d)
        offset = max(0.0, cum_dur - d)
        v_out = f"v{i}"
        a_out = f"a{i}"
        filter_parts.append(
            f"{v_chain}[{i}:v]xfade=transition={trans_name}:duration={d:.3f}:offset={offset:.3f}[{v_out}]"
        )
        filter_parts.append(f"{a_chain}[{i}:a]acrossfade=d={d:.3f}[{a_out}]")
        v_chain, a_chain = f"[{v_out}]", f"[{a_out}]"
        cum_dur = cum_dur + durations[i] - d

    filter_complex = ";".join(filter_parts)
    out_path = job_dir / "final.mp4"
    run_ffmpeg([
        *inputs, "-filter_complex", filter_complex,
        "-map", v_chain, "-map", a_chain,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-r", str(fps), str(out_path),
    ])
    return out_path




# ──────────────────────────────────────────────────────────────────────────
# Mushaf arayüzü çevirisi (Oynat/Durdur, Sayfa, Cüz vb.) — mushaf_viewer/
# reels-render.html içindeki I18N_TR sözlüğüyle KEY'LERİ birebir aynı olmak
# ZORUNDA (değerler farklı olabilir, TR kaynak metin burada da referans
# amaçlı tutuluyor). "tr" dışında bir dil istendiğinde bu sözlük Gemini'ye
# tek seferde (tüm key'ler birlikte) gönderilir, JSON obje olarak çeviri
# alınır; sonuç i18n_cache/{lang}.json dosyasına yazılır ki aynı dil için
# sonraki render'larda tekrar Gemini çağrısı yapılmasın.
_MUSHAF_I18N_TR = {
    "play": "Oynat", "stop": "Durdur", "list": "Liste", "settings": "Ayarlar",
    "reciter": "Okuyucu", "reverseMode": "Ters Terte Modu",
    "listenFromLastVerse": "Son Ayetten Dinle",
    "reverseOnDesc": "Açık — Sayfa sonu → sayfa başı",
    "reverseOffDesc": "Kapalı — Normal sırayla dinleniyor",
    "playbackSpeed": "Oynatma Hızı", "repeatCount": "Tekrar Sayısı",
    "repeatMode": "Tekrar Modu",
    "rangeHint": "Mushaf'ta başlangıç ayetine (yeşil), bitiş ayetine (kırmızı) dokunun. Oynat'a basınca sadece o aralık tekrarlanır. Bir ayete uzun basarsanız (mavi), yalnızca o ayet sonsuz döngüde çalınır.",
    "pageBackground": "Sayfa Arkaplan", "goToPage": "Sayfaya Git",
    "surah": "Sure", "juz": "Cüz", "page": "Sayfa", "searchSurah": "Sure ara…",
    "off": "Kapalı", "verse": "Ayet", "rangeRepeat": "Aralık Tekrarı", "range": "Aralık",
    # NOT: Bu 11 key mushaf-render.html'deki I18N_TR'ye eklenmişti ama buraya
    # hiç eklenmemişti — bu yüzden Gemini'ye hiç gitmiyor, çeviri sözlüğünde
    # yer almıyorlardı ve frontend'de sessizce TR fallback'e düşüyorlardı
    # (dil seçilse bile "Cüz X · Sayfa Y", "Sonuç bulunamadı" vb. hep TR
    # kalıyordu). Frontend I18N_TR ile BİREBİR aynı key setini burada da
    # tutmak zorunludur.
    "juzPageFormat": "Cüz {juz} · Sayfa {page}",
    "surahJuzFormat": "{surah} · Cüz {juz}",
    "juzPageShortFormat": "Cüz {juz} · {page}/{total}",
    "surahFallback": "Sure {n}",
    "wordTooltip": "Sure {surah}, Ayet {ayah}, Kelime {word}",
    "noResults": "Sonuç bulunamadı.",
    "pageBadgeFormat": "Sayfa {page}",
    "ayahRangeFormat": "{start} - {end}. Ayet",
    "verseKeyRangeFormat": "{start} – {end}",
    "prevPage": "Önceki sayfa", "nextPage": "Sonraki sayfa",
}
I18N_CACHE_DIR = BASE_DIR / "i18n_cache"
I18N_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────
# Sure isimleri çevirisi — mushaf-render.html'deki Translatesurah.js sadece
# 3 dil içeriyor (tr/en/ar, bkz. LANG_NAME_INDEX). Kullanıcı MUSHAF_LANGS'ta
# (insta.tsx) sunulan 21 dilden ur/id/ms/fa/bn/fr/de/ru/es/ha/sw/so/ps/bs/sq/
# az/ug/ku gibi birini seçtiğinde sure isimleri için HİÇBİR kaynak yoktu —
# bu yüzden dil ne olursa olsun Türkçe (ya da Arapça'ya fallback) kalıyordu.
# Bu sözlük insta.tsx'teki MUSHAF_SURAHS listesindeki Türkçe isimlerin
# birebir aynısıdır (id -> isim); translate_surah_names() bunu hedef dile
# çevirip i18n_cache/surah_names_{lang}.json içinde cache'ler, sonra
# render_mushaf_job() bunu window.__mushafAPI.setTranslations()'a üçüncü
# argüman olarak geçer.
SURAH_NAMES_TR = {
    1: "Fatiha", 2: "Bakara", 3: "Al-i İmran", 4: "Nisa", 5: "Maide", 6: "En'am", 7: "A'raf", 8: "Enfal",
    9: "Tevbe", 10: "Yunus", 11: "Hud", 12: "Yusuf", 13: "Ra'd", 14: "İbrahim", 15: "Hicr", 16: "Nahl",
    17: "İsra", 18: "Kehf", 19: "Meryem", 20: "Taha", 21: "Enbiya", 22: "Hac", 23: "Mü'minun", 24: "Nur",
    25: "Furkan", 26: "Şuara", 27: "Neml", 28: "Kasas", 29: "Ankebut", 30: "Rum", 31: "Lokman", 32: "Secde",
    33: "Ahzab", 34: "Sebe", 35: "Fatır", 36: "Yasin", 37: "Saffat", 38: "Sad", 39: "Zümer", 40: "Mü'min (Gafir)",
    41: "Fussilet", 42: "Şura", 43: "Zuhruf", 44: "Duhan", 45: "Casiye", 46: "Ahkaf", 47: "Muhammed", 48: "Fetih",
    49: "Hucurat", 50: "Kaf", 51: "Zariyat", 52: "Tur", 53: "Necm", 54: "Kamer", 55: "Rahman", 56: "Vakıa",
    57: "Hadid", 58: "Mücadele", 59: "Haşr", 60: "Mümtehine", 61: "Saf", 62: "Cuma", 63: "Münafikun", 64: "Tegabün",
    65: "Talak", 66: "Tahrim", 67: "Mülk", 68: "Kalem", 69: "Hakka", 70: "Mearic", 71: "Nuh", 72: "Cin",
    73: "Müzzemmil", 74: "Müddessir", 75: "Kıyame", 76: "İnsan", 77: "Mürselat", 78: "Nebe", 79: "Naziat", 80: "Abese",
    81: "Tekvir", 82: "İnfitar", 83: "Mutaffifin", 84: "İnşikak", 85: "Büruc", 86: "Tarık", 87: "Ala", 88: "Gaşiye",
    89: "Fecr", 90: "Beled", 91: "Şems", 92: "Leyl", 93: "Duha", 94: "İnşirah", 95: "Tin", 96: "Alak",
    97: "Kadir", 98: "Beyyine", 99: "Zilzal", 100: "Adiyat", 101: "Karia", 102: "Tekasür", 103: "Asr", 104: "Hümeze",
    105: "Fil", 106: "Kureyş", 107: "Maun", 108: "Kevser", 109: "Kafirun", 110: "Nasr", 111: "Tebbet", 112: "İhlas",
    113: "Felak", 114: "Nas",
}


def translate_surah_names(lang: str, model_key: str = DEFAULT_MODEL) -> dict:
    """SURAH_NAMES_TR'yi verilen dil koduna çevirir. 'tr'/'en'/'ar' için boş
    dict döner (frontend zaten Translatesurah.js'teki names[0/1/2] ile bu
    3 dili karşılıyor — gereksiz Gemini çağrısından kaçınılır). Sonuç
    i18n_cache/surah_names_{lang}.json içinde saklanır. Başarısız olursa
    boş dict döner (frontend statik TRANSLATE dizisine / TR fallback'e
    düşer — video render'ı asla bu yüzden durmaz)."""
    lang = (lang or "tr").strip().lower()
    if lang in ("tr", "en", "ar"):
        return {}

    cache_path = I18N_CACHE_DIR / f"surah_names_{lang}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.keys() == SURAH_NAMES_TR.keys():
                return cached
        except Exception:
            pass

    if not GEMINI_API_KEY:
        log.warning(f"[I18N] GEMINI_API_KEY tanımlı değil, sure isimleri '{lang}' diline çevrilemiyor.")
        return {}

    prompt = (
        "Aşağıdaki JSON, Kur'an-ı Kerim'in 114 suresinin Türkçe isimlerini "
        f"içeriyor (key: sure numarası, value: Türkçe isim). Bunu '{lang}' "
        "dil koduna çevir — yani her sureyi o dilde/o dildeki Müslüman "
        "topluluğunun kullandığı YAYGIN, standart isimle karşıla (harf "
        "çevirisi/transliterasyon olabilir, serbest çeviri değil). "
        "SADECE değerleri değiştir, key'lere (sure numaralarına) DOKUNMA. "
        "Çıktı SADECE geçerli JSON objesi olsun, başka açıklama ekleme.\n\n"
        f"{json.dumps(SURAH_NAMES_TR, ensure_ascii=False)}"
    )
    model_id = MODELS.get(model_key, MODELS[DEFAULT_MODEL])["id"]
    url = f"https://generativelanguage.googleapis.com/v1beta/{model_id}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3},
    }
    try:
        with httpx.Client(timeout=60) as client:
            res = client.post(url, params={"key": GEMINI_API_KEY}, json=body)
        res.raise_for_status()
        data = res.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        text = re.sub(r"^```json\s*|\s*```$", "", text.strip())
        translated = json.loads(text)
        if not isinstance(translated, dict):
            raise ValueError("Gemini yanıtı JSON obje değil.")
        merged = dict(SURAH_NAMES_TR)
        merged.update({k: v for k, v in translated.items() if k in SURAH_NAMES_TR})
        cache_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        return merged
    except Exception as e:
        log.error(f"[I18N] Sure isimleri '{lang}' çevirisi başarısız: {e}")
        return {}


def translate_ui_dict(lang: str, model_key: str = DEFAULT_MODEL) -> dict:
    """_MUSHAF_I18N_TR sözlüğünü verilen dil koduna (ör. 'en', 'ur', 'id')
    çevirir. 'tr' için hiçbir şey yapmadan kaynak sözlüğü döndürür. Sonuçlar
    i18n_cache/{lang}.json içinde saklanır; dosya varsa Gemini'ye HİÇ
    gidilmez. Çeviri herhangi bir nedenle başarısız olursa (API anahtarı
    yok, ağ hatası, bozuk JSON) TR sözlüğe sessizce geri düşülür — arayüz
    çevrilmeden de her zaman çalışır durumda kalır."""
    lang = (lang or "tr").strip().lower()
    if lang == "tr":
        return dict(_MUSHAF_I18N_TR)

    cache_path = I18N_CACHE_DIR / f"{lang}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.keys() == _MUSHAF_I18N_TR.keys():
                return cached
        except Exception:
            pass  # bozuk cache dosyası -> yeniden çevir

    if not GEMINI_API_KEY:
        log.warning(f"[I18N] GEMINI_API_KEY tanımlı değil, '{lang}' çevirisi atlanıyor (TR ile devam).")
        return dict(_MUSHAF_I18N_TR)

    prompt = (
        "Aşağıdaki JSON, bir Kur'an-ı Kerim Mushaf okuma uygulamasının "
        f"arayüz metinlerini (Türkçe) içeriyor. Bunu '{lang}' dil koduna "
        "çevir. Kurallar:\n"
        "- SADECE değerleri çevir, key'lere DOKUNMA (aynı key isimleriyle geri dön).\n"
        "- Dini/İslami bağlama uygun, doğal ve kısa ifadeler kullan "
        "(bir mobil uygulama arayüzünde görünecek kadar kısa).\n"
        "- Bazı değerler süslü parantez içinde yer tutucular içerir "
        "(örn. {juz}, {page}, {n}, {surah}, {ayah}, {word}, {start}, {end}, "
        "{total}). Bunları harfi harfine, DEĞİŞTİRMEDEN ve ÇEVİRMEDEN aynen "
        "koru; sadece etraflarındaki metni hedef dile çevir.\n"
        "- Çıktı SADECE geçerli JSON objesi olsun, başka hiçbir açıklama ekleme.\n\n"
        f"{json.dumps(_MUSHAF_I18N_TR, ensure_ascii=False)}"
    )
    model_id = MODELS.get(model_key, MODELS[DEFAULT_MODEL])["id"]
    url = f"https://generativelanguage.googleapis.com/v1beta/{model_id}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3},
    }
    try:
        with httpx.Client(timeout=60) as client:
            res = client.post(url, params={"key": GEMINI_API_KEY}, json=body)
        res.raise_for_status()
        data = res.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        text = re.sub(r"^```json\s*|\s*```$", "", text.strip())
        translated = json.loads(text)
        if not isinstance(translated, dict):
            raise ValueError("Gemini yanıtı JSON obje değil.")
        # Eksik key kalmasın diye TR üzerine sadece gelen çevirileri bindir.
        merged = dict(_MUSHAF_I18N_TR)
        merged.update({k: v for k, v in translated.items() if k in _MUSHAF_I18N_TR})
        cache_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        return merged
    except Exception as e:
        log.error(f"[I18N] '{lang}' çevirisi başarısız, TR ile devam ediliyor: {e}")
        return dict(_MUSHAF_I18N_TR)


# ──────────────────────────────────────────────────────────────────────────
# "Kur'an — Mushaf Okuması" render'ı — diğer temalardan TAMAMEN farklı bir
# motor kullanır: Pillow ile kare çizmek yerine, deneme.html'de zaten çalışan
# GERÇEK Mushaf sayfasını (glyph bazlı QCF fontlar, kelime kelime dizilim)
# Playwright ile headless tarayıcıda açıp ayet ayet ekran görüntüsü alır.
# Ses tamamen ayrı: her ayetin gerçek okuyucu mp3'ü indirilir, süresi
# ffprobe ile ölçülür, o kareyi o kadar süre ekranda tutacak şekilde ffmpeg
# concat demuxer'ına verilir; sonunda tüm ayet mp3'leri de sırayla
# birleştirilip görüntüyle muxlanır. Bu sayede tarayıcıda GERÇEK zamanlı
# sesli oynatmaya (autoplay kısıtlamaları, zamanlama kaymaları) hiç
# bağımlı olunmaz — ayet.py'daki "görsel kare + ayrı ses" render mantığıyla
# birebir aynı yaklaşım.
def _mushaf_verse_keys(surah: int, ayah_start: int, ayah_end: int) -> List[str]:
    # v1 kapsamı: TEK sure içinde bir ayet aralığı. Sure sınırını aşan bir
    # aralık (ör. bir sonraki sureye taşma) şu an desteklenmiyor — frontend
    # zaten ayet inputlarını seçili surenin ayet sayısına clamp'liyor.
    return [f"{surah}:{a}" for a in range(ayah_start, ayah_end + 1)]


# mushaf_viewer/index.html (getVerseAudioUrl) artık her okuyucuyu KLASÖR
# ADIYLA çözüyor (bkz. deneme.html), ama yalnızca 'Alafasy' tarayıcıda
# bizzat doğrulandı — diğer klasör adları bilinen adlandırma kalıbına göre
# eklendi ve TEK TEK doğrulanmadı. O yüzden burada da (Python tarafında)
# aynı deneme.html mantığıyla bir yedek/retry var: seçilen okuyucunun
# klasörü 404 verirse, render'ı çökertmek yerine sessizce doğrulanmış
# Alafasy adresine düşüyoruz.
_MUSHAF_FALLBACK_RECITER_FOLDER = "Alafasy"


def _mushaf_fallback_audio_url(url: str) -> Optional[str]:
    """`url`'deki verses.quran.com klasör segmentini Alafasy ile değiştirir.

    Şema: https://verses.quran.com/{Folder}/mp3/{sssaaa}.mp3
    Zaten Alafasy ise (yedeğin kendisi de başarısız olduysa) None döner —
    sonsuz döngüye/gereksiz ikinci denemeye girilmez.
    """
    m = re.match(r"^(https://verses\.quran\.com/)([^/]+)(/mp3/.+)$", url)
    if not m or m.group(2) == _MUSHAF_FALLBACK_RECITER_FOLDER:
        return None
    return f"{m.group(1)}{_MUSHAF_FALLBACK_RECITER_FOLDER}{m.group(3)}"


def _mushaf_download_verse_audio(client: "httpx.Client", url: str, dest: Path) -> None:
    try:
        with client.stream("GET", url, timeout=30) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
    except httpx.HTTPStatusError as exc:
        fallback_url = _mushaf_fallback_audio_url(url)
        if fallback_url is None:
            raise
        log.warning(
            "Mushaf ayet sesi indirilemedi (%s), doğrulanmış yedek okuyucuyla "
            "tekrar deneniyor: %s -> %s",
            exc.response.status_code, url, fallback_url,
        )
        with client.stream("GET", fallback_url, timeout=30) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)


def render_mushaf_job(job_id: str, req: MushafRenderRequest):
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError(
                "Playwright kurulu değil. Sunucuda çalıştır: "
                "'pip install playwright && playwright install chromium'"
            )

        set_job(job_id, status="running", progress=3, message="Mushaf görüntüleyici açılıyor")

        frame_durations: List[tuple[Path, float]] = []
        verse_audio_paths: List[Path] = []

        with httpx.Client(follow_redirects=True) as http_client, sync_playwright() as pw:
            browser = pw.chromium.launch()
            # 540x960 @ device_scale_factor=2 -> tam 1080x1920 (nihai video
            # çözünürlüğüyle BİREBİR aynı oran, 9:16). Eski 480x960 oranı
            # (1:2) hedef 1080x1920 (9:16) ile örtüşmediği için ffmpeg
            # kareleri ortalayıp yanlara siyah şerit ekliyordu; bu da
            # mobil görünümü gerçek boyutundan daha küçük gösteriyordu.
            # Artık render doğrudan hedef oranda olduğu için şerit yok ve
            # metin/surah header'lar tam boyutunda kalıyor.
            page = browser.new_page(viewport={"width": 540, "height": 960}, device_scale_factor=2)
            try:
                page.goto(MUSHAF_VIEWER_URL, wait_until="networkidle", timeout=60000)
                page.wait_for_function(
                    "window.__mushafAPI && window.__mushafAPI.ready === true", timeout=30000
                )

                # Arayüz dili — "tr" değilse önce Gemini ile çevrilmiş (ya da
                # cache'den okunan) sözlük Playwright üzerinden sayfaya
                # enjekte edilir; bundan sonra alınan TÜM ekran görüntüleri
                # (Oynat/Durdur, Sayfa, Cüz, Ayarlar vb.) hedef dilde olur.
                # ui_dict, "tr" durumunda da (aşağıda açılış ekranındaki
                # "Okuyucu" etiketi için) HER ZAMAN dolu tutulur — önceden
                # bu blok sadece lang != "tr" iken çalıştığı için "Okuyucu"
                # etiketi ayrı bir yerde sabit metin olarak kalmış ve dil
                # değişse bile hiç çevrilmiyordu.
                if req.lang and req.lang.strip().lower() != "tr":
                    target_lang = req.lang.strip().lower()
                    set_job(job_id, message=f"Arayüz '{req.lang}' diline çevriliyor")
                    ui_dict = translate_ui_dict(req.lang)
                    # tr/en/ar dışındaki diller için sure isimlerinin de
                    # (Fatiha, Bakara, ...) hedef dile çevrilmesi gerekiyor —
                    # bkz. translate_surah_names() üstündeki not.
                    surah_names = translate_surah_names(req.lang)
                    # DİKKAT: setTranslations(dict, langCode, surahNames)
                    # ikinci ve üçüncü parametreleri de bekliyor —
                    # CURRENT_LANG_CODE ve SURAH_NAME_OVERRIDE sadece bu
                    # parametreler verilirse güncelleniyor. Önceden sadece
                    # ui_dict gönderiliyordu; bu yüzden Oynat/Durdur/Ayarlar
                    # gibi arayüz metinleri çevrilse bile sure isimleri
                    # HER ZAMAN Türkçe kalıyordu.
                    page.evaluate(
                        "(args) => window.__mushafAPI.setTranslations(args.dict, args.langCode, args.surahNames)",
                        {"dict": ui_dict, "langCode": target_lang, "surahNames": surah_names},
                    )
                else:
                    ui_dict = dict(_MUSHAF_I18N_TR)

                # Sayfa modu: kullanıcı sure/ayet yerine doğrudan bir Mushaf
                # sayfa numarası seçtiyse, o sayfanın gerçek ilk ayetini
                # (surah/ayah) __mushafAPI.gotoPage üzerinden öğren — sayfa
                # -> ayet eşleme tablosunu backend'de tekrar tutmaya gerek
                # kalmadan, aynı veri kaynağını (mushaf_viewer'daki
                # PAGE_START) tek yerden kullanmış oluyoruz.
                surah = req.surah
                ayah_start = req.ayah_start
                ayah_end = req.ayah_end
                vk_list: List[str]
                if req.page_start is not None:
                    page_result = page.evaluate(
                        "(p) => window.__mushafAPI.gotoPage(p)", req.page_start
                    )
                    surah = page_result["surah"]
                    ayah_start = page_result["ayah"]
                    set_job(job_id, message=f"Sayfa {req.page_start} -> {surah}:{ayah_start} ile başlanıyor")

                    # Sayfa modunda kullanıcıdan ARTIK "kaç ayet okunsun" diye
                    # sayaç istemiyoruz — o sayfada gerçekten kaç ayet varsa
                    # hepsi okunur. gotoPage bizi o sayfaya götürüyor;
                    # gotoVerse'in döndürdüğü verseKeys, o sayfada görünen
                    # TÜM ayetlerin listesi (birden fazla sureye ait olabilir,
                    # ör. sayfa bir sureyle bitip diğeriyle başlıyorsa). Bu
                    # listeyi (surah, ayah) sırasına göre sıralayıp doğrudan
                    # okunacak ayet dizisi olarak kullanıyoruz.
                    page_verses_result = page.evaluate(
                        "([s,a]) => window.__mushafAPI.gotoVerse(s,a)", [surah, ayah_start]
                    )
                    current_page_verses = set(page_verses_result["verseKeys"])

                    def _vk_sort_key(vk: str) -> tuple[int, int]:
                        s, a = map(int, vk.split(":"))
                        return (s, a)

                    vk_list = sorted(current_page_verses, key=_vk_sort_key)
                    if not vk_list:
                        raise RuntimeError("Sayfada okunacak ayet bulunamadı.")
                else:
                    if surah is None or ayah_start is None or ayah_end is None:
                        raise RuntimeError("surah/ayah_start/ayah_end belirlenemedi.")

                    vk_list = _mushaf_verse_keys(surah, ayah_start, ayah_end)
                    if not vk_list:
                        raise RuntimeError("Ayet aralığı boş.")

                    s0, a0 = map(int, vk_list[0].split(":"))
                    result = page.evaluate(
                        "([s,a]) => window.__mushafAPI.gotoVerse(s,a)", [s0, a0]
                    )
                    current_page_verses = set(result["verseKeys"])

                surah_name_localized = page.evaluate(
                    "(s) => window.__mushafAPI.getSurahName(s)", surah
                )

                # ── Aralık seçimi ÖNİZLEMESİ ────────────────────────────
                # Kullanıcı elle tıklamış gibi: ÖNCE başlangıç ayeti yeşil
                # (tek başına), SONRA bitiş ayeti kırmızı eklenerek iki ayrı
                # kare halinde gösterilir — deneme.html'deki gerçek dokunuş
                # akışının (yeşil → kırmızı) video karşılığı. Bu iki kare
                # sırasında ses YOK; birazdan audio_concat_list'e eşit
                # süreli bir sessizlik eklenerek video/ses zamanlaması
                # korunacak (bkz. aşağıdaki intro_silence_duration).
                INTRO_GREEN_ONLY_S = 0.55
                INTRO_GREEN_RED_S  = 0.70
                intro_silence_duration = INTRO_GREEN_ONLY_S + INTRO_GREEN_RED_S

                page.evaluate(
                    "([s]) => window.__mushafAPI.setRangeSelection(s, null)",
                    [vk_list[0]],
                )
                page.wait_for_timeout(160)  # selected-start CSS geçişinin oturması
                intro1_path = job_dir / "intro_0_green.png"
                appRootEl = page.query_selector("#appRoot")
                if not appRootEl:
                    raise RuntimeError("#appRoot elementi bulunamadı — mushaf_viewer/index.html güncel mi?")
                appRootEl.screenshot(path=str(intro1_path))
                frame_durations.append((intro1_path, INTRO_GREEN_ONLY_S))

                page.evaluate(
                    "([s,e]) => window.__mushafAPI.setRangeSelection(s,e)",
                    [vk_list[0], vk_list[-1]],
                )
                page.wait_for_timeout(160)  # selected-end CSS geçişinin oturması
                intro2_path = job_dir / "intro_1_green_red.png"
                appRootEl = page.query_selector("#appRoot")
                appRootEl.screenshot(path=str(intro2_path))
                frame_durations.append((intro2_path, INTRO_GREEN_RED_S))

                # Okuma başlıyor → üst navbar "Oynat" yerine "Durdur" göstersin
                # (gerçek uygulamada okuma başladığında olduğu gibi).
                page.evaluate("(v) => window.__mushafAPI.setPlayingUI(v)", True)

                for idx, vk in enumerate(vk_list):
                    if vk not in current_page_verses:
                        s, a = map(int, vk.split(":"))
                        result = page.evaluate(
                            "([s,a]) => window.__mushafAPI.gotoVerse(s,a)", [s, a]
                        )
                        current_page_verses = set(result["verseKeys"])

                    page.evaluate("(vk) => window.__mushafAPI.highlightVerse(vk)", vk)
                    page.wait_for_timeout(120)  # vurgu CSS geçişinin oturması için kısa bekleme

                    png_path = job_dir / f"frame_{idx:04d}.png"
                    # Yalnızca kart değil, üst navbar (Navbar.tsx) ve alt
                    # bar dahil TÜM mobil ekran görünümü (#appRoot) alınır
                    # — deneme.html'de tarayıcıda görülen mobil görünümle
                    # birebir aynı olsun diye.
                    el = page.query_selector("#appRoot")
                    if not el:
                        raise RuntimeError("#appRoot elementi bulunamadı — mushaf_viewer/index.html güncel mi?")
                    el.screenshot(path=str(png_path))

                    audio_url = page.evaluate(
                        "([f,vk]) => window.__mushafAPI.getVerseAudioUrl(f,vk)",
                        [req.reciter_folder, vk],
                    )
                    mp3_path = job_dir / f"audio_{idx:04d}.mp3"
                    _mushaf_download_verse_audio(http_client, audio_url, mp3_path)
                    dur = probe_duration(mp3_path)
                    if dur <= 0:
                        dur = 3.0  # indirilemeyen/bozuk ses için güvenli varsayılan

                    frame_durations.append((png_path, dur))
                    verse_audio_paths.append(mp3_path)

                    pct = 5 + int(70 * (idx + 1) / len(vk_list))
                    set_job(job_id, progress=pct, message=f"{vk} render edildi ({idx + 1}/{len(vk_list)})")
            finally:
                browser.close()

        set_job(job_id, progress=78, message="Kareler videoya birleştiriliyor")
        concat_list = job_dir / "frames.txt"
        with open(concat_list, "w", encoding="utf-8") as f:
            for png_path, dur in frame_durations:
                f.write(f"file '{png_path.as_posix()}'\n")
                f.write(f"duration {dur:.3f}\n")
            # ffmpeg concat demuxer kuralı: son kareyi süresiz saymaması için
            # aynı dosyayı bir kez daha (süresiz) yazmak gerekiyor.
            f.write(f"file '{frame_durations[-1][0].as_posix()}'\n")

        video_silent = job_dir / "video_silent.mp4"
        run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,"
                   "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black",
            "-r", "30", "-pix_fmt", "yuv420p", str(video_silent),
        ])

        set_job(job_id, progress=88, message="Ses birleştiriliyor")

        # Video, önizleme karesi süresi kadar (intro_silence_duration) sessiz
        # başlıyor (yeşil→kırmızı önizleme). Ses akışının da aynı miktarda
        # sessizlikle başlaması gerekiyor, aksi halde -shortest sondaki
        # okumayı keser / ses ile görüntü kayar.
        intro_silence_path = job_dir / "intro_silence.mp3"
        run_ffmpeg([
            "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
            "-t", f"{intro_silence_duration:.3f}",
            "-q:a", "9", str(intro_silence_path),
        ])

        audio_concat_list = job_dir / "audio.txt"
        with open(audio_concat_list, "w", encoding="utf-8") as f:
            f.write(f"file '{intro_silence_path.as_posix()}'\n")
            for mp3_path in verse_audio_paths:
                f.write(f"file '{mp3_path.as_posix()}'\n")
        audio_concat = job_dir / "audio_concat.mp3"
        run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(audio_concat_list),
            "-c", "copy", str(audio_concat),
        ])

        set_job(job_id, progress=95, message="Son video hazırlanıyor")
        mushaf_body_path = job_dir / "mushaf_body.mp4"
        run_ffmpeg([
            "-i", str(video_silent), "-i", str(audio_concat),
            "-c:v", "copy", "-c:a", "aac", "-shortest", str(mushaf_body_path),
        ])

        fps = 30
        final_parts: List[Path] = []
        try:
            reciter_display_name = MUSHAF_RECITER_NAMES.get(req.reciter_folder, req.reciter_folder)
            # Manuel override: kullanıcı intro_title/intro_subtitle girdiyse
            # otomatik üretilen sure adı / okuyucu adının YERİNE bu geçer.
            intro_surah_text = (req.intro_title or "").strip() or surah_name_localized
            intro_reciter_text = (req.intro_subtitle or "").strip() or reciter_display_name
            # "Okuyucu" etiketi artık seçilen arayüz diline göre çevrilir
            # (ui_dict, lang="tr" iken de _MUSHAF_I18N_TR ile dolu tutulur).
            reciter_label = ui_dict.get("reciter", "Okuyucu")
            intro_clip_path, _ = _render_mushaf_title_clip(
                job_dir, 1080, 1920, fps, "veryfast", 23,
                intro_surah_text, intro_reciter_text, INTRO_BG, 2.2,
                reciter_label=reciter_label,
            )
            final_parts.append(intro_clip_path)
        except Exception as intro_err:
            log.error(f"[MUSHAF INTRO] Açılış ekranı render hatası, introsuz devam ediliyor: {intro_err}")
            set_job(job_id, message="⚠️ Açılış ekranı eklenemedi, video onsuz devam ediyor")

        final_parts.append(mushaf_body_path)

        try:
            # Manuel override: kullanıcı outro_text girdiyse kapanış
            # logosunun altındaki uygulama adının YERİNE bu gösterilir.
            outro_clip_path, _ = render_outro_clip(job_dir, 1080, 1920, fps, "veryfast", 23,
                                                    display_text=req.outro_text)
            final_parts.append(outro_clip_path)
        except Exception as outro_err:
            log.error(f"[MUSHAF OUTRO] Kapanış logosu render hatası, outrosuz devam ediliyor: {outro_err}")
            set_job(job_id, message="⚠️ Kapanış logosu eklenemedi, video onsuz devam ediyor")

        out_path = OUT_DIR / f"{job_id}.mp4"
        if len(final_parts) == 1:
            shutil.copy(final_parts[0], out_path)
        else:
            final_concat_list = job_dir / "final_concat.txt"
            with open(final_concat_list, "w", encoding="utf-8") as f:
                for p in final_parts:
                    f.write(f"file '{p.as_posix()}'\n")
            run_ffmpeg([
                "-f", "concat", "-safe", "0", "-i", str(final_concat_list),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-r", str(fps), str(out_path),
            ])

        set_job(job_id, status="done", progress=100, message="Tamamlandı", file=f"/output/{out_path.name}")
    except Exception as e:
        logging.exception("Mushaf render hatası")
        set_job(job_id, status="error", progress=0, message=str(e))
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


# ──────────────────────────────────────────────────────────────────────────
# "Kur'an — Bil Bakalım Quiz" render'ı — mushaf render'ıyla AYNI mimari
# (Playwright + gerçek HTML/JS motoru + ffmpeg concat), ama quiz.html'in
# kendi soru üretim/kontrol mantığını (__quizAPI, bkz. quiz_viewer/quiz.html)
# kullanır. Her soru için üç aşama vardır:
#   1) SORU  — quiz.html o sorunun ekranını (isAnswered=false) gösterirken,
#      TTS ile Türkçe soru metni (surah+ayet+talimat) seslendirilir; bu
#      sesin süresi kadar aynı kare video akışına yazılır.
#   2) DÜŞÜNME — TTS bittikten sonra, sessiz olarak think_seconds kadar
#      (varsayılan 10 sn) aynı soru ekranı ekranda kalmaya devam eder —
#      istekte belirtilen "10 sn düşünme payı".
#      3) CEVAP — __quizAPI.revealAnswer() ile doğru cevap otomatik
#      işaretlenip aynı feedback ekranı (yeşil "Doğru!" banner'ı — gerçek
#      kullanıcı deneyimindeki GÖRSEL, sadece programatik tetiklenmiş)
#      answer_seconds kadar gösterilir.
def render_quiz_job(job_id: str, req: QuizRenderRequest):
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError(
                "Playwright kurulu değil. Sunucuda çalıştır: "
                "'pip install playwright && playwright install chromium'"
            )

        set_job(job_id, status="running", progress=3, message="Quiz motoru açılıyor")

        frame_durations: List[tuple[Path, float]] = []
        audio_paths: List[Path] = []  # sırayla birleştirilecek TÜM ses parçaları (tts + sessizlik)

        def add_silence(dur: float) -> Path:
            p = job_dir / f"silence_{len(audio_paths):04d}.mp3"
            run_ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                        "-t", f"{max(dur, 0.05):.3f}", "-q:a", "9", str(p)])
            return p

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 540, "height": 960}, device_scale_factor=2)
            try:
                page.goto(QUIZ_VIEWER_URL, wait_until="networkidle", timeout=60000)
                page.wait_for_function(
                    "window.__quizAPI && window.__quizAPI.ready === true", timeout=30000
                )
                # Sadece bu Playwright oturumuna özel: font/UI büyütme CSS'ini
                # devreye sokar (bkz. quiz.html -> body.render-mode kuralları).
                # Gerçek kullanıcı tarayıcısında hiç tetiklenmez.
                page.evaluate("() => window.__quizAPI.enterRenderMode()")

                setup = {
                    "mode": req.mode, "difficulty": req.difficulty,
                    "selectionMode": req.selection_mode,
                    "juzRange": {"start": req.juz_start, "end": req.juz_end},
                    "surahSelection": req.surah_selection,
                    "pageRange": {"start": req.page_start, "end": req.page_end},
                    "questionCount": req.question_count,
                }
                page.evaluate("(s) => window.__quizAPI.configure(s)", setup)

                appRootEl = page.query_selector("#appRoot")
                if not appRootEl:
                    raise RuntimeError("#appRoot elementi bulunamadı — mushaf_viewer/quiz.html güncel mi?")

                # ── 0) WELCOME EKRANI — intro logosundan sonra, ilk sorudan
                # önce quiz.html'in kendi welcomeScreenHtml()'ini gösterir;
                # TTS ile kısa bir tanıtım seslendirilir, o süre boyunca bu
                # ekran videoda sabit kalır.
                set_job(job_id, progress=4, message="Karşılama ekranı hazırlanıyor")
                welcome_info = page.evaluate("(s) => window.__quizAPI.getWelcomeInfo(s)", setup)
                page.evaluate("(s) => window.__quizAPI.renderWelcomeScreen(s)", setup)
                page.wait_for_timeout(100)
                welcome_png_path = job_dir / "welcome.png"
                appRootEl = page.query_selector("#appRoot")
                appRootEl.screenshot(path=str(welcome_png_path))

                welcome_audio_path = job_dir / "welcome_audio.mp3"
                try:
                    synth_tts_sync(welcome_info.get("ttsText") or "Hoş geldiniz!", welcome_audio_path, req.voice, req.voice_rate)
                except Exception as tts_err:
                    log.error(f"[QUIZ TTS] Welcome seslendirme hatası, sessize düşülüyor: {tts_err}")
                    welcome_audio_path = add_silence(2.5)
                welcome_dur = max(probe_duration(welcome_audio_path), 0.5)
                frame_durations.append((welcome_png_path, welcome_dur))
                audio_paths.append(welcome_audio_path)

                set_job(job_id, progress=6, message="Sorular hazırlanıyor")
                start_result = page.evaluate("() => window.__quizAPI.start()")
                if not start_result or not start_result.get("ok"):
                    raise RuntimeError(
                        "Seçilen alanda soru üretilemedi. Lütfen farklı bir "
                        "sure/cüz/sayfa aralığı seçin."
                    )
                total_q = start_result.get("versesCount") or req.question_count

                appRootEl = page.query_selector("#appRoot")
                if not appRootEl:
                    raise RuntimeError("#appRoot elementi bulunamadı — quiz_viewer/quiz.html güncel mi?")

                q_idx = 0
                while not page.evaluate("() => window.__quizAPI.isFinished()"):
                    q_idx += 1
                    info = page.evaluate("() => window.__quizAPI.getCurrentQuestionInfo()")
                    if not info:
                        break
                    tts_text = info.get("ttsText") or f"Soru {q_idx}."

                    # ── 1) SORU sesi + ekranı ──
                    q_audio_path = job_dir / f"q_{q_idx:03d}_audio.mp3"
                    try:
                        synth_tts_sync(tts_text, q_audio_path, req.voice, req.voice_rate)
                    except Exception as tts_err:
                        log.error(f"[QUIZ TTS] Soru {q_idx} seslendirme hatası, sessize düşülüyor: {tts_err}")
                        set_job(job_id, message=f"⚠️ Soru {q_idx}: seslendirme başarısız (sessiz devam)")
                        q_audio_path = add_silence(2.0)
                    q_audio_dur = max(probe_duration(q_audio_path), 0.5)

                    q_png_path = job_dir / f"q_{q_idx:03d}_question.png"
                    appRootEl = page.query_selector("#appRoot")
                    appRootEl.screenshot(path=str(q_png_path))
                    # Soru ekranı: TTS süresi kadar (sesli, timer henüz
                    # görünmez — soru okunurken sayaç dikkat dağıtmasın) +
                    # think_seconds kadar (sessiz, istekte belirtilen "10 sn
                    # düşünme payı") ekranda kalır.
                    frame_durations.append((q_png_path, q_audio_dur))
                    audio_paths.append(q_audio_path)

                    # ── DÜŞÜNME SÜRESİ — timer'ı saniye saniye azaltarak
                    # birden fazla ekran görüntüsü alır (tek statik kare
                    # yerine gerçek bir geri sayım hissi verir). Her saniye
                    # ayrı bir PNG + o saniyelik sessizlik parçası olarak
                    # video/ses akışına eklenir.
                    think_total = max(1, round(req.think_seconds))
                    for sec_left in range(think_total, 0, -1):
                        page.evaluate(
                            "([r,t]) => window.__quizAPI.setTimerValue(r,t)",
                            [sec_left, think_total],
                        )
                        think_png_path = job_dir / f"q_{q_idx:03d}_think_{sec_left:02d}.png"
                        appRootEl = page.query_selector("#appRoot")
                        appRootEl.screenshot(path=str(think_png_path))
                        frame_durations.append((think_png_path, 1.0))
                        audio_paths.append(add_silence(1.0))
                    page.evaluate("() => window.__quizAPI.clearTimer()")

                    # ── 2) CEVABI göster ──
                    revealed = page.evaluate("() => window.__quizAPI.revealAnswer()")
                    if revealed:
                        page.wait_for_timeout(150)  # feedback banner CSS geçişi
                        a_png_path = job_dir / f"q_{q_idx:03d}_answer.png"
                        appRootEl = page.query_selector("#appRoot")
                        appRootEl.screenshot(path=str(a_png_path))
                        frame_durations.append((a_png_path, req.answer_seconds))
                        audio_paths.append(add_silence(req.answer_seconds))

                    pct = 6 + int(80 * q_idx / max(total_q, 1))
                    set_job(job_id, progress=min(pct, 88), message=f"Soru {q_idx}/{total_q} render edildi")

                    # ── 3) sıradaki soruya geç ──
                    page.evaluate("() => window.__quizAPI.nextQuestion()")

                if not frame_durations:
                    raise RuntimeError("Hiç soru render edilemedi.")
            finally:
                browser.close()

        set_job(job_id, progress=90, message="Kareler videoya birleştiriliyor")
        concat_list = job_dir / "frames.txt"
        with open(concat_list, "w", encoding="utf-8") as f:
            for png_path, dur in frame_durations:
                f.write(f"file '{png_path.as_posix()}'\n")
                f.write(f"duration {dur:.3f}\n")
            f.write(f"file '{frame_durations[-1][0].as_posix()}'\n")

        video_silent = job_dir / "video_silent.mp4"
        run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,"
                   "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black",
            "-r", "30", "-pix_fmt", "yuv420p", str(video_silent),
        ])

        set_job(job_id, progress=93, message="Ses birleştiriliyor")
        audio_concat_list = job_dir / "audio.txt"
        with open(audio_concat_list, "w", encoding="utf-8") as f:
            for p in audio_paths:
                f.write(f"file '{p.as_posix()}'\n")
        audio_concat = job_dir / "audio_concat.mp3"
        run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(audio_concat_list),
            "-c:a", "libmp3lame", "-q:a", "4", str(audio_concat),
        ])

        set_job(job_id, progress=96, message="Son video hazırlanıyor")
        quiz_body_path = job_dir / "quiz_body.mp4"
        run_ffmpeg([
            "-i", str(video_silent), "-i", str(audio_concat),
            "-c:v", "copy", "-c:a", "aac", "-shortest", str(quiz_body_path),
        ])

        # ── Logo intro/outro (mushaf temasıyla aynı yaklaşım) ──
        # Manuel override: kullanıcı intro_text/outro_text girdiyse logonun
        # altındaki uygulama adının YERİNE bu metin(ler) gösterilir.
        fps = 30
        final_parts: List[Path] = []
        try:
            intro_clip_path, _ = render_intro_clip(job_dir, 1080, 1920, fps, "veryfast", 23,
                                                    display_text=req.intro_text)
            final_parts.append(intro_clip_path)
        except Exception as intro_err:
            log.error(f"[QUIZ INTRO] Açılış logosu render hatası, introsuz devam ediliyor: {intro_err}")
            set_job(job_id, message="⚠️ Açılış logosu eklenemedi, video onsuz devam ediyor")

        final_parts.append(quiz_body_path)

        try:
            outro_clip_path, _ = render_outro_clip(job_dir, 1080, 1920, fps, "veryfast", 23,
                                                    display_text=req.outro_text)
            final_parts.append(outro_clip_path)
        except Exception as outro_err:
            log.error(f"[QUIZ OUTRO] Kapanış logosu render hatası, outrosuz devam ediliyor: {outro_err}")
            set_job(job_id, message="⚠️ Kapanış logosu eklenemedi, video onsuz devam ediyor")

        out_path = OUT_DIR / f"{job_id}.mp4"
        if len(final_parts) == 1:
            shutil.copy(final_parts[0], out_path)
        else:
            final_concat_list = job_dir / "final_concat.txt"
            with open(final_concat_list, "w", encoding="utf-8") as f:
                for p in final_parts:
                    f.write(f"file '{p.as_posix()}'\n")
            run_ffmpeg([
                "-f", "concat", "-safe", "0", "-i", str(final_concat_list),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-r", str(fps), str(out_path),
            ])

        set_job(job_id, status="done", progress=100, message="Tamamlandı", file=f"/output/{out_path.name}")
    except Exception as e:
        logging.exception("Quiz render hatası")
        set_job(job_id, status="error", progress=0, message=str(e))
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


@app.post("/api/render")
def api_render(req: RenderRequest):
    if not req.slides:
        raise HTTPException(400, "Sahne listesi boş.")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "progress": 0, "message": "Kuyruğa alındı"}
    thread = threading.Thread(target=render_job, args=(job_id, req), daemon=True)
    thread.start()
    return {"job_id": job_id}


@app.get("/api/render/{job_id}")
def api_render_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job_id bulunamadı.")
    return job


@app.get("/api/mushaf-page-info/{page}")
def api_mushaf_page_info(page: int):
    if page < 1 or page > 604:
        raise HTTPException(400, "Sayfa 1-604 aralığında olmalı.")
    try:
        return get_mushaf_page_range(page)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/render-mushaf")
def api_render_mushaf(req: MushafRenderRequest):
    if req.page_start is not None:
        # Sayfa modu: surah/ayah_start/ayah_end render_mushaf_job içinde
        # sayfanın gerçek verseKeys listesinden çözülüyor — kullanıcıdan
        # ayrıca "kaç ayet" istenmiyor, bu yüzden burada ayah_end'e dair
        # bir üst sınır kontrolüne gerek yok (bir Mushaf sayfası zaten
        # birkaç düzine ayeti geçmez).
        pass
    else:
        if req.surah is None or req.ayah_start is None or req.ayah_end is None:
            raise HTTPException(400, "surah, ayah_start ve ayah_end gerekli (ya da page_start gönderin).")
        if req.ayah_end < req.ayah_start:
            raise HTTPException(400, "Bitiş ayeti başlangıçtan küçük olamaz.")
        if req.ayah_end - req.ayah_start + 1 > 60:
            raise HTTPException(400, "Tek seferde en fazla 60 ayet render edilebilir.")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "progress": 0, "message": "Kuyruğa alındı"}
    thread = threading.Thread(target=render_mushaf_job, args=(job_id, req), daemon=True)
    thread.start()
    return {"job_id": job_id}


@app.post("/api/render-quiz")
def api_render_quiz(req: QuizRenderRequest):
    if req.selection_mode == "juz" and req.juz_end < req.juz_start:
        raise HTTPException(400, "Bitiş cüzü başlangıçtan küçük olamaz.")
    if req.selection_mode == "surah" and not req.surah_selection:
        raise HTTPException(400, "En az bir sure seçilmeli.")
    if req.selection_mode == "page" and req.page_end < req.page_start:
        raise HTTPException(400, "Bitiş sayfası başlangıçtan küçük olamaz.")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "progress": 0, "message": "Kuyruğa alındı"}
    thread = threading.Thread(target=render_quiz_job, args=(job_id, req), daemon=True)
    thread.start()
    return {"job_id": job_id}


@app.post("/api/tts-preview")
def api_tts_preview(req: TTSPreviewRequest):
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "Önizlenecek metin boş.")
    if len(text) > 500:
        raise HTTPException(400, "Önizleme metni çok uzun (maks. 500 karakter).")

    preview_dir = WORK_DIR / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    out_path = preview_dir / f"preview_{uuid.uuid4().hex[:10]}.mp3"

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(synth_tts(text, out_path, req.voice, req.voice_rate))
    except Exception as e:
        loop.close()
        raise HTTPException(502, f"Seslendirme önizlemesi üretilemedi: {e}")
    loop.close()

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise HTTPException(502, "Seslendirme önizlemesi üretilemedi (boş dosya).")

    return FileResponse(
        str(out_path), media_type="audio/mpeg", filename="preview.mp3",
        background=BackgroundTask(lambda: out_path.unlink(missing_ok=True)),
    )


@app.get("/")
def root():
    return {
        "ok": True,
        "themes": len(THEMES),
        "models": list(MODELS.keys()),
        "gif_enabled": bool(KLIPY_API_KEY),
        "elevenlabs_enabled": bool(ELEVENLABS_API_KEY),
    }