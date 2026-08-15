"""
Ortak Giriş Noktası (main.py)
-----------------------------
ayet.py (Kuran Reels render backend), Post.py (ReisulQurra Post/Carousel
API'si) ve insta.py (Hafızlık Reels Studio backend'i) birbirinden tamamen
bağımsız üç FastAPI uygulaması. Üçünü de aynı sunucuda, tek bir uvicorn
process'i ile ayakta tutmak için burada sub-application olarak farklı path
prefix'leri altında mount ediyoruz.

Neden mount, neden merge değil:
- Üçünün de kendi CORS middleware'i, kendi route'ları (bazıları aynı isimli,
  örn. GET "/") var. mount() her sub-app'i kendi izole ASGI uygulaması gibi
  çalıştırır, isim çakışması/route çakışması olmaz.
- ayet.py / insta.py kendi StaticFiles mount'larını ("/output",
  "/qul_downloads", "/screenshots", "/assets") ve BASE_DIR = Path(__file__).parent
  kullanımını, modül olarak import edildiğinde bile kendi dosyalarının
  bulunduğu klasöre göre doğru çözer — davranış değişmez.

Sonuç path'ler:
  /ayet/...    -> eskiden ayet.py'de "/" olan her şey (örn. /ayet/api/render)
  /post/...    -> eskiden Post.py'de "/" olan her şey (örn. /post/generate-post)
  /insta/...   -> eskiden insta.py'de "/" olan her şey (örn. /insta/api/render,
                  /insta/api/generate, /insta/api/models, /insta/output/...)

Çalıştırma:
    pip install -r requirements.txt --break-system-packages   (gerekirse)
    export GEMINI_API_KEY=xxx PEXELS_API_KEY=xxx API_SECRET=xxx
    uvicorn main:app --host 0.0.0.0 --port 8000

ayet.py, Post.py ve insta.py bu dosyayla AYNI klasörde olmalı, ayrıca
her birinin ihtiyaç duyduğu fonts/, qul_downloads/, screenshots/, assets/,
ffmpeg/ffprobe de aynı yerde olmalı.
"""

import os

from fastapi import FastAPI

from ayet import app as ayet_app
from Post import app as post_app
from insta import app as insta_app

app = FastAPI(title="ReisulQurra — Ortak API Gateway")

# ══════════════════════════════════════════════════════════════════════════════
#  API_SECRET DOĞRULAMA / DEBUG
# ══════════════════════════════════════════════════════════════════════════════
# Post.py kendi API_SECRET'ini os.getenv("API_SECRET", "benim-cok-gizli-123456")
# ile ayrı ayrı okuyor. Burada aynı değeri tekrar okuyup başlangıçta MASKELENMİŞ
# halini logluyoruz ki mobil uygulamadaki (post.tsx) x-api-key ile sunucudaki
# değerin eşleşip eşleşmediğini kolayca kontrol edebilesin (401 Unauthorized
# hatalarının en sık nedeni bu ikisinin farklı olması).
API_SECRET = os.getenv("API_SECRET", "benim-cok-gizli-123456")


def _masked(secret: str) -> str:
    if len(secret) <= 4:
        return "*" * len(secret)
    return secret[:2] + "*" * (len(secret) - 4) + secret[-2:]


print(f"[main.py] API_SECRET yüklendi -> {_masked(API_SECRET)} (uzunluk={len(API_SECRET)})")
print("[main.py] Mobil uygulamadaki (Ayarlar > API Key) değerin bununla birebir "
      "aynı olduğundan emin ol, aksi halde /post/... isteklerinde 401 alırsın.")


@app.get("/")
async def root():
    return {
        "status": "ok",
        "message": "ReisulQurra ortak sunucu ayakta.",
        "services": {
            "ayet (Kuran Reels)": "/ayet",
            "post (Instagram Carousel)": "/post",
            "insta (Hafızlık Reels Studio)": "/insta",
        },
    }


# Her üç alt uygulama da kendi CORS/route/static ayarlarıyla birlikte
# olduğu gibi mount ediliyor — davranışları tek tek çalıştırıldıklarındakiyle
# birebir aynı, sadece path'lerinin başına prefix ekleniyor.
app.mount("/ayet", ayet_app)
app.mount("/post", post_app)
app.mount("/insta", insta_app)