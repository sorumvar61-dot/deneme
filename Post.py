import os
import json
import datetime
import re
import hashlib
import base64
import random
from pathlib import Path
from typing import Optional

# Header ve Depends eklendi
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from google import genai
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

app = FastAPI(title="ReisulQurra Post API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# ══════════════════════════════════════════════════════════════════════════════
#  API KEY KORUMASI
# ══════════════════════════════════════════════════════════════════════════════

API_SECRET = os.getenv("API_SECRET", "benim-cok-gizli-123456")

def verify_api_key(x_api_key: str = Header(None)):
    if x_api_key != API_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

# ══════════════════════════════════════════════════════════════════════════════
#  APP FEATURES LOADER & DEFAULTS
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_APP_FEATURES = {
  "uygulama": "ReisulQurra",
  "versiyon": "1.0",
  "ozellikler": {
    "temel": {
      "baslik": "Temel Özellikler",
      "ozellikler": [
        {
          "id": "ezber_takip",
          "ad": "Ezber Takip Sistemi",
          "aciklama": "Öğrencilerin ezber durumlarını sayfa sayfa takip etmesini sağlar.",
          "teknik": "Veritabanı kayıtları"
        },
        {
          "id": "sesli_dinleme",
          "ad": "Sesli Dinleme ve Tekrar",
          "aciklama": "Hafızlardan ayetleri tekrar tekrar dinleme imkanı.",
          "teknik": "Medya oynatıcı"
        }
      ]
    }
  }
}

def _load_app_features() -> tuple[str, dict, list[dict]]:
    json_path = Path(__file__).parent / "app-ozellik.json"
    if not json_path.exists():
        # Dosya yoksa varsayılanı oluştur
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_APP_FEATURES, f, ensure_ascii=False, indent=2)

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

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
    stats = data.get("istatistikler", {})
    if stats:
        lines.append("\n### İstatistikler")
        for k, v in stats.items():
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            lines.append(f"  {k}: {v}")

    return "\n".join(lines), data, flat


APP_FEATURES_TEXT, APP_FEATURES_DATA, APP_FEATURES_FLAT = _load_app_features()

# ══════════════════════════════════════════════════════════════════════════════
#  MODEL CATALOG
# ══════════════════════════════════════════════════════════════════════════════

MODELS: dict[str, dict] = {
    "gemini-3.1-flash-lite": {"id": "models/gemini-3.1-flash-lite", "label": "Gemini 3.1 Flash Lite", "rpm": 15, "tpm": 250000, "emoji": "⚡"},
    "gemini-2.5-flash-lite": {"id": "models/gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash Lite", "rpm": 10, "tpm": 250000, "emoji": "⚖️"},
    "gemini-2.5-flash":      {"id": "models/gemini-2.5-flash",      "label": "Gemini 2.5 Flash",      "rpm": 5,  "tpm": 250000, "emoji": "🧠"},
    "gemini-3-flash":        {"id": "models/gemini-3-flash",        "label": "Gemini 3 Flash",        "rpm": 5,  "tpm": 250000, "emoji": "🔥"},
    "gemini-3.5-flash":      {"id": "models/gemini-3.5-flash",      "label": "Gemini 3.5 Flash",      "rpm": 5,  "tpm": 250000, "emoji": "🚀"},
}

DEFAULT_MODEL_KEY = "gemini-3.1-flash-lite"

DEFAULT_SYSTEM_PROMPTS: dict[str, str] = {
    "gemini-3.1-flash-lite": "Sen hızlı ve öz cevaplar veren yardımcı bir asistansın. Türkçe konuş.",
    "gemini-2.5-flash-lite": "Sen dengeli, net ve yardımcı bir asistansın. Türkçe konuş.",
    "gemini-2.5-flash":      "Sen derin analizler yapabilen zeki bir asistansın. Türkçe konuş.",
    "gemini-3-flash":        "Sen yeni nesil, yaratıcı bir asistansın. Türkçe konuş.",
    "gemini-3.5-flash":      "Sen en gelişmiş AI asistansın. Türkçe konuş.",
}

_CUSTOM_SLIDES_PROMPT: Optional[str] = None
_CUSTOM_REVIEW_PROMPT: Optional[str] = None

# ══════════════════════════════════════════════════════════════════════════════
#  PYDANTIC SCHEMAS
# ══════════════════════════════════════════════════════════════════════════════

class Message(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    message: str
    history: Optional[list[Message]] = []
    model_key: Optional[str] = DEFAULT_MODEL_KEY
    system_prompt: Optional[str] = None

class UpdatePromptRequest(BaseModel):
    model_key: str
    system_prompt: str

class UpdatePromptResponse(BaseModel):
    model_key: str
    system_prompt: str
    message: str

class UpdateFeaturesRequest(BaseModel):
    features_json: dict

class UpdateFeaturesResponse(BaseModel):
    message: str
    total_features: int

class SlidePromptsResponse(BaseModel):
    slides_system: str
    review_system: str

class SlidePromptsUpdateRequest(BaseModel):
    slides_system: Optional[str] = None
    review_system: Optional[str] = None

class SlidePromptsUpdateResponse(BaseModel):
    message: str
    slides_system: str
    review_system: str

class GeneratePostRequest(BaseModel):
    input: str
    tone: Optional[str] = "bilgilendirici"
    model_key: Optional[str] = DEFAULT_MODEL_KEY
    feature_id: Optional[str] = "random"
    slide_count: Optional[int] = 5  # YENİ: Dinamik, 5-10 arası
    # ── 3 MOD SİSTEMİ ────────────────────────────────────────────────────────
    #  "topic"  -> input alanı bir konu/başlık, AI onu genişletir (eski davranış)
    #  "ai"     -> input boş bırakılabilir, AI kendi konusunu kendi seçer
    #  "hybrid" -> input konu, raw_content ham metin/makale; AI ikisini birlikte okuyup üretir
    mode: Optional[str] = "topic"
    raw_content: Optional[str] = None

class RegenerateSlideRequest(BaseModel):
    slide: dict
    instruction: str
    topic: Optional[str] = ""
    tone: Optional[str] = "bilgilendirici"
    model_key: Optional[str] = DEFAULT_MODEL_KEY

class ImageRequest(BaseModel):
    title: str
    body: Optional[str] = ""
    hashtag: str
    year: Optional[str] = None
    card_type: str = "cover"
    icon_url: Optional[str] = None

class TranslateSlidesRequest(BaseModel):
    slides: list[dict]
    meta: Optional[dict] = None
    target_language: str          # ISO kodu: "az", "en", "ar" vb.
    target_language_name: str     # Türkçe adı: "Azərbaycanca"
    native_name: Optional[str] = None   # Kendi dilindeki adı
    model_key: Optional[str] = DEFAULT_MODEL_KEY

# ══════════════════════════════════════════════════════════════════════════════
#  HTML RENDERER & CSS
# ══════════════════════════════════════════════════════════════════════════════

CARD_W, CARD_H = 405, 506
_FONT_FAMILY = "'Montserrat', 'Inter', 'Segoe UI', Arial, sans-serif"

def _get_bg_style() -> str:
    img_path = Path(__file__).parent / "insta.png"
    if img_path.exists():
        with open(img_path, "rb") as f:
            b64_img = base64.b64encode(f.read()).decode("utf-8")
        return f"background-image: url('data:image/png;base64,{b64_img}'); background-size: cover; background-position: center;"
    return "background-color: #F3EAE1;"

def _get_base_css() -> str:
    bg_style = _get_bg_style()
    return f"""
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    html, body {{
        width:{CARD_W}px; height:{CARD_H}px; overflow:hidden;
        font-family:{_FONT_FAMILY}; position:relative;
        {bg_style}
    }}
    .top-bar, .bottom-bar {{
        position:absolute; width:100%; padding:40px 45px;
        display:flex; justify-content:space-between; align-items:center;
        z-index:10; box-sizing:border-box;
    }}
    .top-bar  {{ top:0; }}
    .bottom-bar {{ bottom:0; }}
    .brand    {{ font-size:14px; font-weight:500; color:#333; letter-spacing:0.5px; }}
    .hashtag  {{ font-size:14px; font-weight:800; color:#111; }}
    .username {{ font-size:14px; font-weight:500; color:#333; }}
    .year     {{ font-size:14px; font-weight:800; color:#111; }}
    .c-icon {{ width: 50px; height: 50px; margin-bottom: 12px; opacity: 0.9; filter: invert(36%) sepia(21%) saturate(1008%) hue-rotate(63deg) brightness(97%) contrast(85%); }}
    """

def _bars(hashtag: str, year: str) -> str:
    return (
        f'<div class="top-bar">'
        f'<span class="brand">REİSUL QURRA</span>'
        f'<span class="hashtag">#{hashtag}</span>'
        f'</div>'
        f'<div class="bottom-bar">'
        f'<span class="username">@reisulsqurra</span>'
        f'<span class="year">{year}</span>'
        f'</div>'
    )

def _title_lines(title: str) -> str:
    return "<br>".join(l.upper() for l in title.replace("\\n", "\n").split("\n"))

def _html_cover(title: str, hashtag: str, year: str) -> str:
    return (
        f'<!DOCTYPE html><html><head><style>'
        f'{_get_base_css()}'
        f'.baslik {{ position:absolute; top:50%; left:45px; right:45px; transform:translateY(-50%); '
        f'z-index:10; text-align:center; display:flex; flex-direction:column; align-items:center; }}'
        f'.baslik h1 {{ font-weight:800; font-size:46px; line-height:1.1; letter-spacing:1px; color:#111; margin:0; }}'
        f'.brush {{ margin-top:15px; width:220px; height:auto; display:block; }}'
        f'</style></head><body>'
        f'{_bars(hashtag, year)}'
        f'<div class="baslik">'
        f'<h1>{_title_lines(title)}</h1>'
        f'<svg class="brush" viewBox="0 0 300 20" preserveAspectRatio="none">'
        f'<path d="M5,10 Q150,0 295,12 Q150,5 5,10 Z" fill="#436D2E"/>'
        f'</svg></div></body></html>'
    )

def _html_body(title: str, body: str, hashtag: str, year: str, icon_url: str = None) -> str:
    parsed = re.sub(r'\*\*(.*?)\*\*', r'<mark>\1</mark>', body)
    raw_lines = [l for l in parsed.replace("\\n", "\n").split("\n") if l.strip()]
    spans = "".join(f"<span>{l}</span><br><br>" for l in raw_lines[:5])
    
    icon_html = f'<img class="c-icon" src="{icon_url}">' if icon_url else ''

    return (
        f'<!DOCTYPE html><html><head><style>'
        f'{_get_base_css()}'
        f'.icerik {{ position:absolute; top:120px; left:45px; right:45px; z-index:10; text-align:center; }}'
        f'.icerik h1 {{ font-weight:800; font-size:32px; line-height:1.1; color:#111; margin-bottom: 25px; }}'
        f'.text-highlight {{ font-size:20px; font-weight:500; line-height:1.5; color:#333; }}'
        f'.text-highlight span mark {{ background-color:transparent; color:#436D2E; font-weight:800; }}' 
        f'</style></head><body>'
        f'{_bars(hashtag, year)}'
        f'<div class="icerik">{icon_html}<h1>{_title_lines(title)}</h1>'
        f'<p class="text-highlight">{spans}</p>'
        f'</div></body></html>'
    )

def _html_cta(title: str, hashtag: str, year: str) -> str:
    return (
        f'<!DOCTYPE html><html><head><style>'
        f'{_get_base_css()}'
        f'.cta {{ position:absolute; top:50%; left:0; width:100%; '
        f'transform:translateY(-50%); text-align:center; z-index:10; }}'
        f'.cta h1 {{ font-weight:800; font-size:52px; line-height:1.2; letter-spacing:2px; color:#2A1F14; margin:0; }}'
        f'</style></head><body>'
        f'{_bars(hashtag, year)}'
        f'<div class="cta"><h1>{_title_lines(title)}</h1></div>'
        f'</body></html>'
    )

def _build_html(req: ImageRequest, year: str) -> str:
    if req.card_type == "body" and req.body and req.body.strip():
        return _html_body(req.title, req.body, req.hashtag, year, req.icon_url)
    if req.card_type == "cta":
        return _html_cta(req.title, req.hashtag, year)
    return _html_cover(req.title, req.hashtag, year)

async def _render_png(html: str) -> bytes:
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=["--no-sandbox", "--disable-setuid-sandbox"])
            page = await browser.new_page(viewport={"width": CARD_W, "height": CARD_H})
            await page.set_content(html, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(300)
            png = await page.screenshot(
                clip={"x": 0, "y": 0, "width": CARD_W, "height": CARD_H},
                type="png",
            )
            await browser.close()
        return png
    except Exception as e:
        raise RuntimeError(f"Playwright render hatası: {e}") from e

# ══════════════════════════════════════════════════════════════════════════════
#  SLIDES SYSTEM PROMPT - DİNAMİK SLAYT SAYISI DESTEĞİ (5-10)
# ══════════════════════════════════════════════════════════════════════════════

def _build_slide_flow(slide_count: int, has_feature: bool = True) -> tuple[str, str]:
    flow_lines = [
        f"    TAM {slide_count} SLAYTLIK KESİN AKIŞ ŞEMASI:",
        "    1. Slayt (Kanca): Şok Giriş (Cover - Merak uyandır)",
        "    2. Slayt (Sorun): Kullanıcının yaşadığı problem (Empati kur)",
    ]
    
    schema_slides = [
        """        { "slide_number": 1, "badge": "Giriş", "card_type": "cover", "title": "...", "body": "", "hashtag": "sorun", "icon_keyword": null }""",
        """        { "slide_number": 2, "badge": "Problem", "card_type": "body", "title": "...", "body": "...", "hashtag": "hafızlık", "icon_keyword": "brain" }"""
    ]

    for i in range(3, slide_count):
        if i == slide_count - 1 and has_feature:
            # Sondan bir önceki slayt her zaman özellik slaytı olur
            flow_lines.append(f"    {i}. Slayt (Uygulama Özelliği): ReisulQurra ile Çözüm. ODAK ÖZELLİK: {{feature_info}}.")
            schema_slides.append(f"""        {{ "slide_number": {i}, "badge": "Özellik", "card_type": "body", "title": "...", "body": "...", "hashtag": "uygulama", "icon_keyword": "phone" }}""")
        else:
            # Ara slaytlar veya serbest stil çözüm aşamaları
            flow_lines.append(f"    {i}. Slayt (Çözüm/Bilgi): Çözüm Yolu ve Detaylar (Bilgi ver, derinleştir)")
            schema_slides.append(f"""        {{ "slide_number": {i}, "badge": "Çözüm", "card_type": "body", "title": "...", "body": "...", "hashtag": "odak", "icon_keyword": "target" }}""")

    # Son slayt her zaman CTA
    flow_lines.append(f"    {slide_count}. Slayt (CTA): Harekete Geçirici Mesaj. 'takip et', 'indir', 'kaydet', 'paylaş' içermeli.")
    schema_slides.append(f"""        {{ "slide_number": {slide_count}, "badge": "Aksiyon", "card_type": "cta", "title": "FARKLI\\nBİR\\nCTA YAZ", "body": "", "hashtag": "indir", "icon_keyword": null }}""")

    schema_json = "    {\n      \"slides\": [\n" + ",\n".join(schema_slides) + "\n      ]\n    }"
    return "\n".join(flow_lines), schema_json


def _get_slides_system(feature_info: str, slide_count: int = 5, has_feature: bool = True) -> str:
    if _CUSTOM_SLIDES_PROMPT:
        return _CUSTOM_SLIDES_PROMPT.replace("{feature_info}", feature_info)

    flow, schema = _build_slide_flow(slide_count, has_feature)
    flow = flow.replace("{feature_info}", feature_info)

    return f"""
    Sen ReisulQurra için Instagram carousel içerikleri üreten uzman bir sosyal medya stratejistisin.
    Amacın: Gerçek bir değer sunmak ve fark ettirmeden app pazarlaması yapmak. SADECE GEÇERLİ JSON DÖNDÜR.

    MARKA: ReisulQurra — Kuran-ı Kerim ezber ve öğrenme uygulaması

{flow}

    ÖNEMLİ: Son slayt (CTA) başlığında HEP AYNI METNİ KULLANMA. Konuya uygun yaratıcı bir başlık üret
    (örn: "TAKİP ET\\nİNDİR\\nBAŞLA", "PROFİLDEN\\nUYGULAMAYA\\nGİT", "KAYDET\\nVE\\nPAYLAŞ" vb.)

    BAŞLIK (title):
      - Max 3 satır, her satır max 3-4 kelime
      - Satırları \\n ile ayır (örn: "EZBER\\nNEDEN\\nZOR?")

    BODY (body) — ZORUNLU (1. ve son slayt hariç):
      - Sadece 1 veya 2 vurucu cümle yaz.
      - Sadece en kritik 1 veya 2 kelimeyi **kelime** ile işaretle.
      - Body slaytları için içeriği temsil eden TEK BİR İNGİLİZCE KELİME bul ve 'icon_keyword' alanına yaz. 1. ve son slayta null bırak.

    HASHTAG:
      - Her slayt için TEK Türkçe kelime, sadece harflerden oluşmalı, # işareti kullanma.

    JSON ŞEMASI (Örnek Yapı — SADECE {slide_count} SLAYT):
{schema.replace('}', '},').replace('],', ']')}
      "meta": {{ "topic": "konu", "caption": "Instagram caption metni", "global_hashtags": ["#hafızlık", "#reisulqurra"] }}
    }}
    """

_FALLBACK_HASHTAGS = [
    "ezber", "tecvid", "hafızlık", "kuran", "streak",
    "motivasyon", "sabır", "niyet", "sure", "dua",
    "hedef", "başarı", "öğrenme", "islam", "gelişim"
]

_CTA_FALLBACKS = [
    "TAKİP ET\nİNDİR\nBAŞLA",
    "PROFİLDEN\nUYGULAMAYA\nGİT",
    "KAYDET\nVE\nPAYLAŞ",
    "HEMEN\nİNDİR\nDENE",
]

# ══════════════════════════════════════════════════════════════════════════════
#  "TECRÜBELİ ABİ" KALİTE KONTROL / REKLAMCI REVİZYON PROMPTU
# ══════════════════════════════════════════════════════════════════════════════

def _get_review_system() -> str:
    if _CUSTOM_REVIEW_PROMPT:
        return _CUSTOM_REVIEW_PROMPT
    return """
    Sen, hafızlığını tamamlamış ve 30 yıldır sosyal medya reklamcılığı/içerik stratejisi
    yapan tecrübeli bir "abi"sin. Hem dini hassasiyeti hem de viral içerik refleksini
    aynı anda taşıyorsun. Sana ReisulQurra (Kuran ezber/öğrenme uygulaması) için
    hazırlanmış bir Instagram carousel JSON'u verilecek.

    GÖREVİN — SON KALİTE KONTROL VE GÜÇLENDİRME:
      1. Kancayı (1. slayt) gerçekten durdurucu mu diye değerlendir; zayıfsa,
         aynı konuyu koruyarak daha çarpıcı, merak uyandıran bir versiyonla değiştir.
      2. Sorun/Çözüm/Özellik slaytlarındaki metinleri oku: klişe, tekrar eden
         veya etkisiz ifadeleri tecrübenle daha güçlü, daha "insan gibi" ve daha
         ikna edici hale getir. **kelime** işaretlemelerini anlamlı tut.
      3. CTA slaytını kontrol et; "indir/takip et/kaydet/paylaş" gibi bir aksiyon
         net olmalı ve klişe değilse dokunma, klişeyse konuya özel yaratıcı bir
         versiyonla değiştir.
      4. Hiçbir slaytta yanlış, abartılı veya dini açıdan hassasiyetsiz bir ifade
         kullanma. Ton her zaman samimi, saygılı ve güven verici olmalı.
      5. meta.caption alanını da gözden geçir; gerekirse daha akıcı ve davetkâr hale getir.

    KESİNLİKLE DEĞİŞTİRME:
      - JSON'un alan adları, slayt sayısı, slide_number, badge ve card_type değerleri.
      - icon_keyword alanlarının anlamı (gerekirse aynı kalitede başka bir İngilizce
        kelimeyle değiştirebilirsin ama boş bırakma).
      - hashtag formatı: yine TEK Türkçe kelime, sadece harflerden oluşmalı, # kullanma.

    SADECE GEÇERLİ JSON DÖNDÜR. Açıklama, yorum, markdown kod bloğu YOK.
    Aynı şemayı (slides + meta) birebir koruyarak, sadece içerik kalitesini artırılmış
    şekilde geri döndür.
    """


async def _review_slides(model_id: str, data: dict, input_text: str, tone: str) -> dict:
    system_prompt = _get_review_system()
    current_json = json.dumps(data, ensure_ascii=False)
    user_msg = (
        f"Konu/İçerik: {input_text}\n"
        f"Ton: {tone}\n"
        f"Gözden geçirilecek carousel JSON:\n{current_json}\n\n"
        f"Yukarıdaki JSON'u 30 yıllık tecrüben ve hafızlık geçmişinle gözden geçir, "
        f"şemayı birebir koruyarak içerik kalitesini güçlendirip geri döndür."
    )

    try:
        response = await client.aio.models.generate_content(
            model=model_id,
            contents=user_msg,
            config=genai.types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.7,
                max_output_tokens=3000,
                response_mime_type="application/json",
            ),
        )
        reviewed = json.loads(_clean_json(response.text))

        original_slides = data.get("slides", [])
        reviewed_slides = reviewed.get("slides", [])
        if not reviewed_slides or len(reviewed_slides) != len(original_slides):
            return data

        reviewed.setdefault("meta", data.get("meta", {}))
        for k, v in data.get("meta", {}).items():
            reviewed["meta"].setdefault(k, v)

        reviewed["feature_used"] = data.get("feature_used")
        return reviewed
    except Exception:
        return data


def _get_slide_regen_system(card_type: str) -> str:
    if card_type == "cover":
        ozel_kural = """
    Bu bir KAPAK (cover) slaytıdır:
      - SADECE "title" alanını üret. "body" her zaman boş string "" olmalı, "icon_keyword" her zaman null olmalı.
      - title: max 3 satır, her satır max 3-4 kelime, satırları \\n ile ayır (örn: "EZBER\\nNEDEN\\nZOR?").
      - Amaç: Merak uyandıran, "kanca" niteliğinde şok edici/dikkat çekici bir giriş başlığı.
        """
    elif card_type == "cta":
        ozel_kural = """
    Bu bir HAREKETE GEÇİRİCİ (cta - kapanış) slaytıdır:
      - SADECE "title" alanını üret. "body" her zaman boş string "" olmalı, "icon_keyword" her zaman null olmalı.
      - title: max 3 satır, her satır max 3-4 kelime, satırları \\n ile ayır.
      - İçinde "takip et", "indir", "kaydet", "paylaş", "başla" gibi bir harekete geçirme kelimesi geçmeli.
      - Standart/klişe ifadelerden kaçın, yaratıcı ve konuya özel bir CTA üret.
        """
    else:
        ozel_kural = """
    Bu bir İÇERİK (body) slaytıdır:
      - "title": max 3 satır, her satır max 3-4 kelime, satırları \\n ile ayır.
      - "body": Sadece 1 veya 2 vurucu cümle. İçindeki en kritik 1-2 kelimeyi **kelime** şeklinde işaretle.
      - "icon_keyword": body içeriğini temsil eden TEK BİR İNGİLİZCE KELİME (örn: 'brain', 'clock', 'book', 'shield').
        """

    return f"""
    Sen ReisulQurra için Instagram carousel içeriği üreten uzman bir sosyal medya stratejistisin.
    SADECE GEÇERLİ JSON DÖNDÜR — başında, sonunda veya içinde hiçbir açıklama, markdown ya da yorum olmasın.

    MARKA: ReisulQurra — Kuran-ı Kerim ezber ve öğrenme uygulaması

    Sana TEK BİR SLAYTIN mevcut hali (JSON) ve kullanıcının bu slayt için istediği değişiklik talimatı verilecek.
    Görevin: SADECE bu slaytı, kullanıcının talimatına göre güncellemek. Diğer slaytları ve genel akışı düşünme.

    KURALLAR:
      - "card_type" alanı KESİNLİKLE DEĞİŞTİRİLEMEZ, mevcut değer "{card_type}" olarak kalmalı.
      - "slide_number" ve "badge" alanlarını mevcut slayttaki değerlerle aynı bırak (değiştirme).
      - "hashtag": TEK Türkçe kelime, sadece harflerden oluşmalı, # işareti kullanma.
{ozel_kural}

    JSON ŞEMASI (TEK SLAYT):
    {{
      "slide_number": {{mevcut_ile_aynı}},
      "badge": "{{mevcut_ile_aynı}}",
      "card_type": "{card_type}",
      "title": "...",
      "body": "...",
      "hashtag": "...",
      "icon_keyword": "..." veya null
    }}
    """


def _clean_json(raw: str) -> str:
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1:
        return raw[start : end + 1]
    return raw.strip()

def _enforce(data: dict) -> dict:
    slides: list[dict] = data.get("slides", [])
    if not slides:
        return data

    # İlk slaytı cover zorla
    slides[0].update({"card_type": "cover", "body": "", "icon_keyword": None})

    # Son slaytı CTA zorla
    last = slides[-1]
    last_title = str(last.get("title", "")).strip()
    if not last_title or len(last_title) < 3:
        last_title = random.choice(_CTA_FALLBACKS)
    last.update({
        "card_type": "cta",
        "title": last_title,
        "body": "",
        "icon_keyword": None,
    })

    # Ara slaytları body yap
    for s in slides[1:-1]:
        s["card_type"] = "body"
        body = s.get("body", "")
        if not body or not body.strip():
            title_clean = s.get("title", "").replace("\\n", " ").replace("\n", " ")
            s["body"] = f"**{title_clean}** hakkında doğru adımı at."

    # Hashtag temizliği
    seen: set[str] = set()
    fb_idx = 0
    for i, s in enumerate(slides, 1):
        s["slide_number"] = i
        raw_tag = re.sub(
            r"[^a-zA-ZğüşıöçĞÜŞİÖÇ0-9]", "",
            str(s.get("hashtag", "")).replace("#", "").split()[0]
        ).lower().strip()[:20]
        if not raw_tag or raw_tag in seen:
            while fb_idx < len(_FALLBACK_HASHTAGS) and _FALLBACK_HASHTAGS[fb_idx] in seen:
                fb_idx += 1
            raw_tag = _FALLBACK_HASHTAGS[fb_idx] if fb_idx < len(_FALLBACK_HASHTAGS) else f"slayt{i}"
            fb_idx += 1
        seen.add(raw_tag)
        s["hashtag"] = raw_tag

    data["slides"] = slides
    return data


def _clean_hashtag(raw: str, fallback: str) -> str:
    raw = str(raw or "").replace("#", "").strip()
    parts = raw.split()
    raw = parts[0] if parts else ""
    cleaned = re.sub(r"[^a-zA-ZğüşıöçĞÜŞİÖÇ0-9]", "", raw).lower().strip()[:20]
    return cleaned or fallback


def _enforce_single_slide(slide: dict, original: dict) -> dict:
    card_type = original.get("card_type", "body")

    cleaned: dict = {
        "slide_number": original.get("slide_number"),
        "badge": original.get("badge", slide.get("badge", "")),
        "card_type": card_type,
        "title": slide.get("title", original.get("title", "")),
        "body": slide.get("body", ""),
        "hashtag": slide.get("hashtag", original.get("hashtag", "")),
        "icon_keyword": slide.get("icon_keyword"),
    }

    if card_type in ("cover", "cta"):
        cleaned["body"] = ""
        cleaned["icon_keyword"] = None
        title = str(cleaned.get("title", "")).strip()
        if card_type == "cta" and (not title or len(title) < 3):
            title = random.choice(_CTA_FALLBACKS)
        elif not title:
            title = original.get("title", "")
        cleaned["title"] = title
    else:
        body = cleaned.get("body", "")
        if not body or not body.strip():
            title_clean = str(cleaned.get("title", "")).replace("\\n", " ").replace("\n", " ")
            cleaned["body"] = f"**{title_clean}** hakkında doğru adımı at."
        if not cleaned.get("icon_keyword"):
            cleaned["icon_keyword"] = original.get("icon_keyword")

    cleaned["hashtag"] = _clean_hashtag(cleaned.get("hashtag"), original.get("hashtag", "kuran"))
    return cleaned


async def _generate_slides(
    model_id: str,
    input_text: str,
    tone: str,
    feature_id: str,
    slide_count: int = 5,
) -> dict:
    # slide_count sınırla: 5 ile 10 arası
    slide_count = max(5, min(10, slide_count))

    has_feature = True
    # Özellik seçimi ve serbest stil kontrolü
    if feature_id == "none":
        has_feature = False
        selected_feature = {"ad": "Serbest Stil", "aciklama": "Sadece içerik üretimi", "teknik": ""}
        feature_info = ""
    elif feature_id == "random" or not feature_id:
        selected_feature = random.choice(APP_FEATURES_FLAT) if APP_FEATURES_FLAT else {"ad": "Genel Uygulama", "aciklama": "ReisulQurra", "teknik": ""}
        feature_info = f"Uygulama Özelliği: {selected_feature.get('ad', '')} - {selected_feature.get('aciklama', '')}"
    else:
        selected_feature = next((f for f in APP_FEATURES_FLAT if f["id"] == feature_id), None)
        if not selected_feature:
            selected_feature = random.choice(APP_FEATURES_FLAT) if APP_FEATURES_FLAT else {"ad": "Genel Uygulama", "aciklama": "ReisulQurra", "teknik": ""}
        feature_info = f"Uygulama Özelliği: {selected_feature.get('ad', '')} - {selected_feature.get('aciklama', '')}"

    system_prompt = _get_slides_system(feature_info, slide_count, has_feature)
    

    user_msg = (
        f"İçerik/Konu: {input_text}\nTon: {tone}\n"
        f"ZORUNLU: TAM {slide_count} SLAYT üret. Ne eksik ne fazla — kesinlikle {slide_count} slayt.\n"
        f"Son slayt (CTA) başlığında 'takip et', 'indir', 'kaydet' veya 'paylaş' gibi bir aksiyon kelimesi geçsin.\n"
        f"Son slayt (CTA) başlığını sakın standart bırakma, hep farklı, yaratıcı bir şeyler yaz.\n"
    )

    response = await client.aio.models.generate_content(
        model=model_id,
        contents=user_msg,
        config=genai.types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.75,
            max_output_tokens=3000,
            response_mime_type="application/json",
        ),
    )
    
    parsed_data = json.loads(_clean_json(response.text))
    parsed_data["feature_used"] = selected_feature.get("ad", "Genel")

    enforced = _enforce(parsed_data)
    reviewed = await _review_slides(model_id, enforced, input_text, tone)
    return _enforce(reviewed)

# ══════════════════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/")
def root():
    return {"status": "ok", "message": "ReisulQurra Post API"}

@app.get("/app-features")
def get_features():
    return {"flat_features": APP_FEATURES_FLAT, "raw_json": APP_FEATURES_DATA}

@app.put("/app-features", response_model=UpdateFeaturesResponse)
def update_features(req: UpdateFeaturesRequest, _: None = Depends(verify_api_key)):
    global APP_FEATURES_TEXT, APP_FEATURES_DATA, APP_FEATURES_FLAT
    json_path = Path(__file__).parent / "app-ozellik.json"
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(req.features_json, f, ensure_ascii=False, indent=2)
        APP_FEATURES_TEXT, APP_FEATURES_DATA, APP_FEATURES_FLAT = _load_app_features()
        return UpdateFeaturesResponse(message="Özellikler güncellendi ✅", total_features=len(APP_FEATURES_FLAT))
    except Exception as e:
        raise HTTPException(500, f"Güncelleme hatası: {e}")

@app.post("/app-features/reset", response_model=UpdateFeaturesResponse)
def reset_features(_: None = Depends(verify_api_key)):
    global APP_FEATURES_TEXT, APP_FEATURES_DATA, APP_FEATURES_FLAT
    json_path = Path(__file__).parent / "app-ozellik.json"
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_APP_FEATURES, f, ensure_ascii=False, indent=2)
        APP_FEATURES_TEXT, APP_FEATURES_DATA, APP_FEATURES_FLAT = _load_app_features()
        return UpdateFeaturesResponse(message="Özellikler İlk Versiyona Sıfırlandı ✅", total_features=len(APP_FEATURES_FLAT))
    except Exception as e:
        raise HTTPException(500, f"Sıfırlama hatası: {e}")

@app.get("/slides-prompts", response_model=SlidePromptsResponse)
def get_slides_prompts():
    return SlidePromptsResponse(
        slides_system=_CUSTOM_SLIDES_PROMPT if _CUSTOM_SLIDES_PROMPT else _get_slides_system("{feature_info}", 5),
        review_system=_CUSTOM_REVIEW_PROMPT if _CUSTOM_REVIEW_PROMPT else _get_review_system(),
    )

@app.put("/slides-prompts", response_model=SlidePromptsUpdateResponse)
def update_slides_prompts(req: SlidePromptsUpdateRequest, _: None = Depends(verify_api_key)):
    global _CUSTOM_SLIDES_PROMPT, _CUSTOM_REVIEW_PROMPT
    if req.slides_system is not None:
        _CUSTOM_SLIDES_PROMPT = req.slides_system.strip() or None
    if req.review_system is not None:
        _CUSTOM_REVIEW_PROMPT = req.review_system.strip() or None
    return SlidePromptsUpdateResponse(
        message="Promptlar güncellendi ✅",
        slides_system=_CUSTOM_SLIDES_PROMPT or "(varsayılan)",
        review_system=_CUSTOM_REVIEW_PROMPT or "(varsayılan)",
    )

@app.get("/models")
def list_models():
    return {
        "models": [
            {**info, "key": k, "system_prompt": DEFAULT_SYSTEM_PROMPTS.get(k, "")}
            for k, info in MODELS.items()
        ]
    }

@app.post("/models/prompt", response_model=UpdatePromptResponse)
def update_system_prompt(req: UpdatePromptRequest, _: None = Depends(verify_api_key)):
    if req.model_key not in MODELS:
        raise HTTPException(404, f"Model bulunamadı: {req.model_key}")
    DEFAULT_SYSTEM_PROMPTS[req.model_key] = req.system_prompt
    return UpdatePromptResponse(model_key=req.model_key, system_prompt=req.system_prompt, message="Güncellendi ✅")

@app.post("/generate-post")
async def generate_post(req: GeneratePostRequest, _: None = Depends(verify_api_key)):
    if req.model_key not in MODELS:
        raise HTTPException(404, f"Model desteklenmiyor: {req.model_key}")

    info = MODELS[req.model_key]
    slide_count = max(5, min(10, req.slide_count or 5))

    # ── 3 MOD SİSTEMİ: input_text'i moda göre kur ────────────────────────────
    mode = (req.mode or "topic").strip().lower()
    user_topic = (req.input or "").strip()
    raw_content = (req.raw_content or "").strip()

    if mode == "ai":
        # Kullanıcı konu vermemiş/vermek istememiş — AI kendi konusunu seçsin.
        if user_topic:
            # Yine de bir ipucu verdiyse yönlendirme olarak kullan.
            input_text = (
                f"KONU SERBEST (yönlendirme): '{user_topic}'\n"
                f"Bu yönlendirmeyi ilham al ama birebir kopyalama, kendi özgün açını bul ve "
                f"bu konu etrafında en ilgi çekici, paylaşılabilir postu SEN kurgula."
            )
        else:
            input_text = (
                "KONU SERBEST: Kullanıcı herhangi bir konu belirtmedi. Uygulamanın özelliğiyle "
                "alakalı, hafızlık/Kuran/İslami yaşam temalı, güncel ve ilgi çekici, özgün bir "
                "konuyu SEN seç ve o konu üzerinden post üret."
            )
    elif mode == "hybrid":
        if not raw_content:
            raise HTTPException(400, "hybrid modda 'raw_content' (ham metin) boş olamaz.")
        input_text = (
            f"Konu/Başlık: {user_topic or '(belirtilmedi, aşağıdaki içerikten çıkar)'}\n\n"
            f"Ham İçerik/Kaynak Metin:\n{raw_content}\n\n"
            f"Yukarıdaki konuyu ve ham içeriği BİRLİKTE değerlendir: ham içerikteki önemli "
            f"noktaları, örnekleri ve bilgileri konuya bağlayarak post üret. Ham metni birebir "
            f"kopyalama, özünü post formatına uyarla."
        )
    else:  # "topic" — mevcut/varsayılan davranış
        if not user_topic:
            raise HTTPException(400, "topic modda 'input' (konu) boş olamaz.")
        input_text = user_topic

    try:
        data = await _generate_slides(
            model_id=info["id"],
            input_text=input_text,
            tone=req.tone or "bilgilendirici",
            feature_id=req.feature_id,
            slide_count=slide_count,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Üretim Hatası: {str(e)}")

    return {**data, "model_used": info["id"], "model_label": f"{info['emoji']} {info['label']}"}

@app.post("/regenerate-slide")
async def regenerate_slide(req: RegenerateSlideRequest, _: None = Depends(verify_api_key)):
    if req.model_key not in MODELS:
        raise HTTPException(404, f"Model desteklenmiyor: {req.model_key}")

    instruction = (req.instruction or "").strip()
    if not instruction:
        raise HTTPException(400, "Talimat boş olamaz.")

    if "card_type" not in req.slide:
        raise HTTPException(400, "Slayt verisi eksik: card_type gerekli.")

    info = MODELS[req.model_key]
    card_type = req.slide.get("card_type", "body")
    system_prompt = _get_slide_regen_system(card_type)

    current_json = json.dumps(req.slide, ensure_ascii=False)
    user_msg = (
        f"Genel konu/içerik: {req.topic or '(belirtilmedi)'}\n"
        f"Ton: {req.tone or 'bilgilendirici'}\n"
        f"Mevcut slayt (JSON): {current_json}\n"
        f"Kullanıcının bu slayt için talimatı: {instruction}\n"
        f"Sadece güncellenmiş slaytın JSON'unu döndür."
    )

    try:
        response = await client.aio.models.generate_content(
            model=info["id"],
            contents=user_msg,
            config=genai.types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.8,
                max_output_tokens=800,
                response_mime_type="application/json",
            ),
        )
        parsed = json.loads(_clean_json(response.text))
    except Exception as e:
        raise HTTPException(500, f"Slayt güncelleme hatası: {e}")

    updated = _enforce_single_slide(parsed, req.slide)
    return {"slide": updated, "model_used": info["id"], "model_label": f"{info['emoji']} {info['label']}"}

@app.post("/generate-image")
async def generate_image(req: ImageRequest, _: None = Depends(verify_api_key)):
    year = req.year or str(datetime.date.today().year)
    try:
        html = _build_html(req, year)
        png = await _render_png(html)
    except Exception as e:
        raise HTTPException(500, f"Render hatası: {e}")

    return Response(content=png, media_type="image/png", headers={"Content-Disposition": "attachment; filename=slide.png"})

@app.post("/translate-slides")
async def translate_slides(req: TranslateSlidesRequest, _: None = Depends(verify_api_key)):
    """
    Mevcut Türkçe slaytları istenen dile çevirir.
    Sadece metin içerikleri (title, body, meta.caption) çevrilir;
    card_type, slide_number, badge, hashtag, icon_keyword dokunulmaz.
    """
    if req.model_key not in MODELS:
        raise HTTPException(404, f"Model desteklenmiyor: {req.model_key}")

    info = MODELS[req.model_key]
    lang_display = req.native_name or req.target_language_name

    system_prompt = f"""
Sen profesyonel bir çevirmensin. Kuran ezber/hafızlık konularında uzman olan
ReisulQurra uygulaması için Instagram carousel slayt içeriklerini çeviriyorsun.

HEDEF DİL: {req.target_language_name} ({lang_display}) — ISO kodu: {req.target_language}

ÇEVİRMEN KURALLARI:
  1. SADECE şu JSON alanlarını çevir:
       - her slayttaki "title"
       - her slayttaki "body"
       - meta.caption
     Diğer TÜM alanlar (card_type, slide_number, badge, hashtag,
     icon_keyword, global_hashtags) KESİNLİKLE aynen bırakılmalı.

  2. "title" alanlarındaki \\n satır bölmelerini KORU.
     Çeviri sonrasında da aynı satır sayısını koru (mümkünse).

  3. "body" içindeki **kalın** işaretlemelerini KORU.
     Örn: "**ezber** güçlenir" → "{lang_display}'de **doğru karşılık** güçlenir"

  4. Hafızlık, Kuran, namaz, dua gibi İslami terimleri
     hedef dilde yaygın ve doğru kullanılan karşılıklarıyla çevir.
     Arapça İslami terimleri (Kuran, sure, hadis vb.) hedef dilde
     nasıl yazılıyorsa öyle yaz.

  5. Kültürel uyum: çeviri kelimesi kelimesine değil, doğal ve akıcı
     olmalı. Hedef dili anadil olarak konuşan biri gibi yaz.

  6. meta.global_hashtags çevirme — aynen bırak.

  7. SADECE GEÇERLİ JSON döndür. Açıklama, markdown, yorum YOK.
     Şema: {{ "slides": [...], "meta": {{...}} }}
"""

    slides_json = json.dumps(
        {"slides": req.slides, "meta": req.meta or {}},
        ensure_ascii=False
    )

    user_msg = (
        f"Aşağıdaki JSON içeriğini {req.target_language_name} ({lang_display}) diline çevir:\n\n"
        f"{slides_json}\n\n"
        f"Sadece title, body ve meta.caption alanlarını çevir. "
        f"Diğer tüm alanları AYNEN koru. Sadece JSON döndür."
    )

    try:
        response = await client.aio.models.generate_content(
            model=info["id"],
            contents=user_msg,
            config=genai.types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.3,        # Çeviri için düşük temperature
                max_output_tokens=4000,
                response_mime_type="application/json",
            ),
        )

        translated = json.loads(_clean_json(response.text))

    except json.JSONDecodeError as e:
        raise HTTPException(500, f"Çeviri JSON parse hatası: {e}")
    except Exception as e:
        raise HTTPException(500, f"Çeviri hatası: {e}")

    # Güvenlik: orijinal slide'ların yapısal alanlarını koru
    original_slides = req.slides
    translated_slides = translated.get("slides", [])

    if len(translated_slides) != len(original_slides):
        raise HTTPException(500, "Çeviri slayt sayısı uyuşmuyor. Tekrar deneyin.")

    safe_slides = []
    for orig, trans in zip(original_slides, translated_slides):
        safe_slide = {
            # Yapısal alanlar: orijinalden al
            "slide_number": orig["slide_number"],
            "card_type":    orig["card_type"],
            "badge":        orig.get("badge", ""),
            "hashtag":      orig.get("hashtag", ""),
            "icon_keyword": orig.get("icon_keyword"),
            "icon_svg":     orig.get("icon_svg"),
            # Metin alanlar: çeviriden al (yoksa orijinale dön)
            "title": trans.get("title") or orig.get("title", ""),
            "body":  trans.get("body") or orig.get("body", ""),
        }
        safe_slides.append(safe_slide)

    # Meta birleştir: caption çevrilmiş, hashtag'ler orijinal
    orig_meta  = req.meta or {}
    trans_meta = translated.get("meta", {})
    safe_meta  = {
        **orig_meta,
        "caption": trans_meta.get("caption") or orig_meta.get("caption", ""),
        # global_hashtags çevrilmez — orijinal kalır
        "global_hashtags": orig_meta.get("global_hashtags", []),
    }

    return {
        "slides":   safe_slides,
        "meta":     safe_meta,
        "language": req.target_language,
        "language_name": req.target_language_name,
    }