"""
BIST 100 Paper Trading Botu + Web Dashboard
"""

import asyncio
import logging
import os
import threading
import requests
import pandas as pd
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import yfinance as yf
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import anthropic
from flask import Flask, render_template_string

# ─────────────────────────────────────────
# ⚙️  AYARLAR
# ─────────────────────────────────────────

BOT_TOKEN      = os.environ.get("BOT_TOKEN")
CHAT_ID        = os.environ.get("CHAT_ID")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
PORT           = int(os.environ.get("PORT", 8080))

TZ = ZoneInfo("Europe/Istanbul")

BASLANGIC_BAKIYE = 50_000.0
KOMISYON_ORANI   = 0.001
MAX_POZISYON_PCT = 0.10
TARAMA_ARALIK    = 15 * 60

# ─────────────────────────────────────────
# 📋  BIST 100
# ─────────────────────────────────────────

BIST100 = list(dict.fromkeys([
    "AEFES","AGESA","AKBNK","AKFEN","AKGRT","AKSEN","ALARK","ALBRK",
    "ASELS","ASTOR","AVGYO","AYDEM","BIMAS","BRSAN","BRYAT","BTCIM","BUCIM","CIMSA",
    "CWENE","DOAS","DOHOL","DYNMO","ECILC","EKGYO","ENJSA","ENKAI",
    "EREGL","FROTO","GARAN","GESAN","GLYHO","HALKB","HEKTS",
    "ISCTR","ISDMR","ISGYO","KARSN","KCHOL","KONTR","KONYA","KORDS",
    "KOZAA","KOZAL","KRDMD","LOGO","MAVI","MGROS","MPARK","NETAS","ODAS",
    "OTKAR","PETKM","PGSUS","POLHO","SAHOL","SASA",
    "SISE","SKBNK","SOKM","TAVHL","TCELL","THYAO",
    "TKFEN","TOASO","TRALT","TTKOM","TTRAK","TUPRS",
    "ULKER","VAKBN","VESBE",
]))

# ─────────────────────────────────────────
# 💼  GLOBAL STATE
# ─────────────────────────────────────────

portfoy = {
    "bakiye":           BASLANGIC_BAKIYE,
    "baslangic":        BASLANGIC_BAKIYE,
    "pozisyonlar":      {},
    "baslangic_tarihi": datetime.now(TZ).strftime("%d.%m.%Y %H:%M"),
}
islem_gecmisi = []

# BIST100 cache — arka planda 15dk'da bir güncellenir
bist100_cache = []      # [{"sembol":..,"fiyat":..,"degisim":..,"rsi":..,"sinyal":..}]
cache_guncelleme = "—"  # Son güncelleme zamanı

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# 🛡️  YARDIMCI
# ─────────────────────────────────────────

def hafta_ici_mi():
    return datetime.now(TZ).weekday() < 5

def borsa_acik_mi():
    if not hafta_ici_mi():
        return False
    simdi = datetime.now(TZ).time()
    return dtime(10, 0) <= simdi <= dtime(18, 0)

def simdi_str():
    return datetime.now(TZ).strftime("%d.%m.%Y %H:%M")

def son_fiyat_al(sembol):
    try:
        df = yf.Ticker(f"{sembol}.IS").history(period="5d", interval="1d")
        return float(df["Close"].iloc[-1]) if not df.empty else None
    except:
        return None
def portfoy_degeri():
    toplam = portfoy["bakiye"]
    for sembol, poz in portfoy["pozisyonlar"].items():
        fiyat = son_fiyat_al(sembol)
        toplam += poz["adet"] * (fiyat if fiyat else poz["maliyet"])
    return toplam

# ─────────────────────────────────────────
# 📊  TEKNİK ANALİZ
# ─────────────────────────────────────────

ALPHA_KEY = os.environ.get("ALPHA_VANTAGE_KEY")

def teknik_analiz(sembol):
    try:
        import time
        time.sleep(1)
        yf.set_tz_cache_location("/tmp")
        ticker = yf.Ticker(f"{sembol}.IS")
        ticker._session = requests.Session()
        ticker._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        df = ticker.history(period="6mo", interval="1d")
        if df.empty or len(df) < 20:
            return None
        kapanis = df["Close"]
        son_fiyat = round(float(kapanis.iloc[-1]), 2)
        onceki = round(float(kapanis.iloc[-2]), 2)
        degisim = round((son_fiyat - onceki) / onceki * 100, 2)

        delta = kapanis.diff()
        rs = delta.clip(lower=0).rolling(14).mean() / (-delta.clip(upper=0)).rolling(14).mean()
        rsi = round(float((100 - 100/(1+rs)).iloc[-1]), 1)

        ema12 = kapanis.ewm(span=12).mean()
        ema26 = kapanis.ewm(span=26).mean()
        macd = round(float((ema12-ema26).iloc[-1]), 3)
        msig = round(float((ema12-ema26).ewm(span=9).mean().iloc[-1]), 3)
        ma20 = round(float(kapanis.rolling(20).mean().iloc[-1]), 2)
        ma50 = round(float(kapanis.rolling(50).mean().iloc[-1]), 2)

        hacim_ort = float(df["Volume"].rolling(10).mean().iloc[-1])
        son_hacim = float(df["Volume"].iloc[-1])

        skor = 0
        if rsi < 35:         skor += 3
        elif rsi < 50:       skor += 1
        elif rsi > 65:       skor -= 2
        if macd > msig:      skor += 2
        else:                skor -= 1
        if son_fiyat > ma20: skor += 1
        else:                skor -= 1
        if son_fiyat > ma50: skor += 2
        else:                skor -= 1
        if son_hacim > hacim_ort * 1.2 and degisim > 0:
            skor += 1

        sinyal = "AL" if skor >= 5 else ("SAT" if skor <= 1 else "BEKLE")
        return {
            "sembol": sembol, "fiyat": son_fiyat, "degisim": degisim,
            "rsi": rsi, "macd": macd, "macd_sig": msig,
            "ma20": ma20, "ma50": ma50, "skor": skor, "sinyal": sinyal,
        }
    except Exception as e:
        log.error(f"${sembol}.IS hata: {e}")
        return None

# ─────────────────────────────────────────
# 🔄  BIST100 CACHE GÜNCELLEME
# ─────────────────────────────────────────

def bist100_cache_guncelle():
    global bist100_cache, cache_guncelleme
    log.info("BIST100 cache güncelleniyor...")
    yeni_cache = []
    for sembol in BIST100:
        t = teknik_analiz(sembol)
        if t:
            yeni_cache.append(t)
    bist100_cache = yeni_cache
    cache_guncelleme = simdi_str()
    log.info(f"Cache güncellendi: {len(yeni_cache)} hisse")

def cache_thread_baslat():
    """Arka planda 15 dakikada bir cache günceller."""
    import time
    while True:
        try:
            bist100_cache_guncelle()
        except Exception as e:
            log.error(f"Cache güncelleme hatası: {e}")
        time.sleep(TARAMA_ARALIK)

# ─────────────────────────────────────────
# 💰  SANAL İŞLEMLER
# ─────────────────────────────────────────

def sanal_al(sembol, fiyat):
    if sembol in portfoy["pozisyonlar"]:
        return None
    max_tutar = portfoy["bakiye"] * MAX_POZISYON_PCT
    adet      = int((max_tutar * (1 - KOMISYON_ORANI)) / fiyat)
    if adet < 1:
        return None
    komisyon = adet * fiyat * KOMISYON_ORANI
    toplam   = adet * fiyat + komisyon
    if toplam > portfoy["bakiye"]:
        return None
    portfoy["bakiye"] -= toplam
    portfoy["pozisyonlar"][sembol] = {
        "adet": adet, "maliyet": fiyat,
        "tarih": simdi_str(), "toplam": toplam,
        "tarih_dt": datetime.now(TZ),
        "komisyon_al": round(komisyon, 2),
    }
    islem_gecmisi.append({
        "tip": "AL", "sembol": sembol, "alis_fiyat": fiyat,
        "satis_fiyat": None, "adet": adet,
        "tarih": simdi_str(), "tutar": toplam,
        "komisyon": round(komisyon, 2),
        "kar_zarar": None, "kar_pct": None, "sure": None,
    })
    return (f"🟢 *SANAL AL* — {sembol}\n"
            f"💰 {adet} adet @ {fiyat} ₺\n"
            f"💸 Komisyon: {komisyon:.2f} ₺\n"
            f"🏦 Kalan: {portfoy['bakiye']:.2f} ₺")

def sanal_sat(sembol, fiyat):
    if sembol not in portfoy["pozisyonlar"]:
        return None
    poz        = portfoy["pozisyonlar"].pop(sembol)
    adet       = poz["adet"]
    komisyon   = adet * fiyat * KOMISYON_ORANI
    net_gelir  = adet * fiyat - komisyon
    kar_zarar  = net_gelir - poz["toplam"]
    kar_pct    = (kar_zarar / poz["toplam"]) * 100
    sure_dk    = int((datetime.now(TZ) - poz["tarih_dt"]).total_seconds() / 60)
    sure_str   = f"{sure_dk//60}s {sure_dk%60}dk" if sure_dk >= 60 else f"{sure_dk}dk"
    toplam_komisyon = round(poz["komisyon_al"] + komisyon, 2)

    portfoy["bakiye"] += net_gelir

    # İşlem geçmişindeki AL kaydını güncelle
    for i in reversed(islem_gecmisi):
        if i["sembol"] == sembol and i["tip"] == "AL" and i["satis_fiyat"] is None:
            i["satis_fiyat"] = fiyat
            i["komisyon"]    = toplam_komisyon
            i["kar_zarar"]   = round(kar_zarar, 2)
            i["kar_pct"]     = round(kar_pct, 1)
            i["sure"]        = sure_str
            break

    islem_gecmisi.append({
        "tip": "SAT", "sembol": sembol, "alis_fiyat": poz["maliyet"],
        "satis_fiyat": fiyat, "adet": adet,
        "tarih": simdi_str(), "tutar": round(net_gelir, 2),
        "komisyon": round(komisyon, 2),
        "kar_zarar": round(kar_zarar, 2),
        "kar_pct": round(kar_pct, 1),
        "sure": sure_str,
    })
    emoji = "📈" if kar_zarar >= 0 else "📉"
    return (f"🔴 *SANAL SAT* — {sembol}\n"
            f"💰 {adet} adet @ {fiyat} ₺\n"
            f"⏱️ Portföyde: {sure_str}\n"
            f"💸 Toplam komisyon: {toplam_komisyon} ₺\n"
            f"{emoji} K/Z: {kar_zarar:+.2f} ₺ ({kar_pct:+.1f}%)\n"
            f"🏦 Bakiye: {portfoy['bakiye']:.2f} ₺")

# ─────────────────────────────────────────
# 🌐  WEB DASHBOARD
# ─────────────────────────────────────────

flask_app = Flask(__name__)

HTML = """
<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="60">
<title>FurkiBot — Paper Trading</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@300;400;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0a0a0f; --card: #111118; --border: #1e1e2e;
    --green: #00ff88; --red: #ff4466; --yellow: #ffd700;
    --blue: #4488ff; --text: #e0e0f0; --muted: #666680;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:var(--bg); color:var(--text); font-family:'DM Sans',sans-serif; padding:24px; }
  .header { display:flex; justify-content:space-between; align-items:center; margin-bottom:28px; padding-bottom:16px; border-bottom:1px solid var(--border); }
  .logo { font-family:'Space Mono',monospace; font-size:18px; font-weight:700; color:var(--green); }
  .logo span { color:var(--muted); }
  .live { display:flex; align-items:center; gap:8px; font-size:12px; color:var(--muted); font-family:'Space Mono',monospace; }
  .dot { width:8px; height:8px; background:var(--green); border-radius:50%; animation:pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
  .grid4 { display:grid; grid-template-columns:repeat(4,1fr); gap:16px; margin-bottom:20px; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:20px; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:20px; }
  .card-label { font-size:11px; text-transform:uppercase; letter-spacing:1.5px; color:var(--muted); margin-bottom:8px; font-family:'Space Mono',monospace; }
  .card-value { font-size:22px; font-weight:600; font-family:'Space Mono',monospace; line-height:1; }
  .card-sub { font-size:12px; color:var(--muted); margin-top:6px; }
  .section-title { font-size:12px; font-family:'Space Mono',monospace; color:var(--muted); text-transform:uppercase; letter-spacing:1px; margin-bottom:14px; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th { text-align:left; padding:8px 10px; font-size:10px; text-transform:uppercase; letter-spacing:1px; color:var(--muted); font-family:'Space Mono',monospace; border-bottom:1px solid var(--border); }
  td { padding:10px; border-bottom:1px solid #15151f; font-family:'Space Mono',monospace; font-size:12px; }
  tr:last-child td { border-bottom:none; }
  tr:hover td { background:#13131e; }
  .badge { display:inline-block; padding:2px 7px; border-radius:4px; font-size:10px; font-weight:700; }
  .al  { background:rgba(0,255,136,0.15); color:var(--green); }
  .sat { background:rgba(255,68,102,0.15); color:var(--red); }
  .bekle { background:rgba(255,215,0,0.15); color:var(--yellow); }
  .green { color:var(--green); }
  .red   { color:var(--red); }
  .yellow{ color:var(--yellow); }
  .blue  { color:var(--blue); }
  .empty { text-align:center; color:var(--muted); padding:32px; font-size:12px; }
  .full { grid-column: 1 / -1; }
  @media(max-width:768px){ .grid4{grid-template-columns:repeat(2,1fr)} .grid2{grid-template-columns:1fr} }
</style>
</head>
<body>

<div class="header">
  <div class="logo">FURKI<span>BOT</span> // PAPER TRADING</div>
  <div class="live"><div class="dot"></div>{{ simdi }} · 60sn'de yenilenir</div>
</div>

<!-- ÖZET KARTLAR -->
<div class="grid4">
  <div class="card">
    <div class="card-label">Portföy Değeri</div>
    <div class="card-value">{{ toplam_deger }} ₺</div>
    <div class="card-sub">Başlangıç: {{ baslangic }} ₺</div>
  </div>
  <div class="card">
    <div class="card-label">Toplam K/Z</div>
    <div class="card-value {{ 'green' if kz_pozitif else 'red' }}">{{ toplam_kz }} ₺</div>
    <div class="card-sub {{ 'green' if kz_pozitif else 'red' }}">{{ toplam_kz_pct }}%</div>
  </div>
  <div class="card">
    <div class="card-label">Nakit Bakiye</div>
    <div class="card-value blue">{{ bakiye }} ₺</div>
    <div class="card-sub">{{ pozisyon_sayisi }} açık pozisyon</div>
  </div>
  <div class="card">
    <div class="card-label">Başarı Oranı</div>
    <div class="card-value yellow">{{ basari_orani }}%</div>
    <div class="card-sub">{{ kazanan }}/{{ toplam_satislar }} kazandı</div>
  </div>
</div>

<!-- AÇIK POZİSYONLAR + İŞLEM GEÇMİŞİ -->
<div class="grid2">
  <div class="card">
    <div class="section-title">Açık Pozisyonlar</div>
    {% if pozisyonlar %}
    <table>
      <thead><tr><th>Hisse</th><th>Adet</th><th>Alış</th><th>Anlık</th><th>Süre</th><th>K/Z</th></tr></thead>
      <tbody>
      {% for p in pozisyonlar %}
      <tr>
        <td style="font-weight:700;">{{ p.sembol }}</td>
        <td>{{ p.adet }}</td>
        <td>{{ p.maliyet }} ₺</td>
        <td>{{ p.anlik }} ₺</td>
        <td style="color:var(--muted);">{{ p.sure }}</td>
        <td class="{{ 'green' if p.kz >= 0 else 'red' }}">
          {{ '+' if p.kz >= 0 else '' }}{{ p.kz }} ₺<br>
          <span style="font-size:10px;">{{ '+' if p.kz_pct >= 0 else '' }}{{ p.kz_pct }}%</span>
        </td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}<div class="empty">Açık pozisyon yok</div>{% endif %}
  </div>

  <div class="card">
    <div class="section-title">İşlem Geçmişi (Son 15)</div>
    {% if islemler %}
    <table>
      <thead><tr><th>Hisse</th><th>Alış</th><th>Satış</th><th>Komisyon</th><th>K/Z</th><th>Süre</th></tr></thead>
      <tbody>
      {% for i in islemler %}
      {% if i.tip == 'SAT' %}
      <tr>
        <td style="font-weight:700;">{{ i.sembol }}</td>
        <td>{{ i.alis }} ₺</td>
        <td>{{ i.satis }} ₺</td>
        <td style="color:var(--muted);">{{ i.komisyon }} ₺</td>
        <td class="{{ 'green' if i.kz >= 0 else 'red' }}">
          {{ '+' if i.kz >= 0 else '' }}{{ i.kz }} ₺<br>
          <span style="font-size:10px;">{{ '+' if i.kz_pct >= 0 else '' }}{{ i.kz_pct }}%</span>
        </td>
        <td style="color:var(--muted);">{{ i.sure }}</td>
      </tr>
      {% endif %}
      {% endfor %}
      </tbody>
    </table>
    {% else %}<div class="empty">Henüz tamamlanan işlem yok</div>{% endif %}
  </div>
</div>

<!-- BIST 100 TABLOSU -->
<div class="card">
  <div class="section-title">
    BIST 100 — Anlık Durum
    <span style="color:var(--muted); font-size:10px; margin-left:12px;">Son güncelleme: {{ cache_guncelleme }}</span>
  </div>
  {% if bist100 %}
  <table>
    <thead>
      <tr>
        <th>Hisse</th><th>Fiyat</th><th>Değişim</th><th>RSI</th>
        <th>MA20</th><th>MA50</th><th>Skor</th><th>Sinyal</th>
      </tr>
    </thead>
    <tbody>
    {% for h in bist100 %}
    <tr>
      <td style="font-weight:700;">{{ h.sembol }}</td>
      <td>{{ h.fiyat }} ₺</td>
      <td class="{{ 'green' if h.degisim >= 0 else 'red' }}">
        {{ '+' if h.degisim >= 0 else '' }}{{ h.degisim }}%
      </td>
      <td class="{{ 'red' if h.rsi > 65 else ('green' if h.rsi < 35 else '') }}">
        {{ h.rsi }}
      </td>
      <td style="color:var(--muted);">{{ h.ma20 }} ₺</td>
      <td style="color:var(--muted);">{{ h.ma50 }} ₺</td>
      <td>{{ h.skor }}/10</td>
      <td><span class="badge {{ 'al' if h.sinyal == 'AL' else ('sat' if h.sinyal == 'SAT' else 'bekle') }}">{{ h.sinyal }}</span></td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="empty">⏳ BIST100 verileri yükleniyor... (İlk yükleme birkaç dakika sürebilir)</div>
  {% endif %}
</div>

</body>
</html>
"""

@flask_app.route("/")
def dashboard():
    toplam_deger = portfoy_degeri()
    kz           = toplam_deger - portfoy["baslangic"]
    kz_pct       = (kz / portfoy["baslangic"]) * 100

    # Açık pozisyonlar
    poz_listesi = []
    for sembol, poz in portfoy["pozisyonlar"].items():
        fiyat    = son_fiyat_al(sembol) or poz["maliyet"]
        kz_poz   = poz["adet"] * fiyat - poz["toplam"]
        kz_poz_p = (kz_poz / poz["toplam"]) * 100
        sure_dk  = int((datetime.now(TZ) - poz["tarih_dt"]).total_seconds() / 60)
        sure_str = f"{sure_dk//60}s {sure_dk%60}dk" if sure_dk >= 60 else f"{sure_dk}dk"
        poz_listesi.append({
            "sembol": sembol, "adet": poz["adet"],
            "maliyet": poz["maliyet"], "anlik": round(fiyat, 2),
            "sure": sure_str,
            "kz": round(kz_poz, 2), "kz_pct": round(kz_poz_p, 1),
        })

    # İşlem geçmişi (sadece SAT'lar, son 15)
    satislar = [i for i in islem_gecmisi if i["tip"] == "SAT"]
    islem_listesi = []
    for i in reversed(satislar[-15:]):
        islem_listesi.append({
            "tip": i["tip"], "sembol": i["sembol"],
            "alis": i["alis_fiyat"], "satis": i["satis_fiyat"],
            "komisyon": i["komisyon"],
            "kz": i["kar_zarar"], "kz_pct": i["kar_pct"],
            "sure": i["sure"],
        })

    tum_satislar = [i for i in islem_gecmisi if i["tip"] == "SAT"]
    kazananlar   = [i for i in tum_satislar if (i.get("kar_zarar") or 0) > 0]

    return render_template_string(HTML,
        simdi         = simdi_str(),
        toplam_deger  = f"{toplam_deger:,.2f}",
        baslangic     = f"{portfoy['baslangic']:,.0f}",
        toplam_kz     = f"{kz:+,.2f}",
        toplam_kz_pct = f"{kz_pct:+.2f}",
        kz_pozitif    = kz >= 0,
        bakiye        = f"{portfoy['bakiye']:,.2f}",
        pozisyon_sayisi = len(portfoy["pozisyonlar"]),
        basari_orani  = round(len(kazananlar)/len(tum_satislar)*100, 1) if tum_satislar else 0,
        kazanan       = len(kazananlar),
        toplam_satislar = len(tum_satislar),
        pozisyonlar   = poz_listesi,
        islemler      = islem_listesi,
        bist100       = bist100_cache,
        cache_guncelleme = cache_guncelleme,
    )

def flask_thread():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False)

# ─────────────────────────────────────────
# ⏰  OTOMATİK TARAMA
# ─────────────────────────────────────────

async def bist100_tara(app):
    if not borsa_acik_mi():
        log.info("Borsa kapalı.")
        return
    log.info("BIST100 taraması + cache güncelleniyor...")
    global bist100_cache, cache_guncelleme
    yeni_cache = []
    for sembol in BIST100:
        t = teknik_analiz(sembol)
        if not t:
            continue
        yeni_cache.append(t)
        mesaj = None
        if t["sinyal"] == "AL":
            mesaj = sanal_al(sembol, t["fiyat"])
        elif t["sinyal"] == "SAT":
            mesaj = sanal_sat(sembol, t["fiyat"])
        if mesaj:
            try:
                await app.bot.send_message(chat_id=CHAT_ID, text=mesaj, parse_mode="Markdown")
                await asyncio.sleep(1)
            except Exception as e:
                log.error(f"Mesaj hatası: {e}")
    bist100_cache     = yeni_cache
    cache_guncelleme  = simdi_str()
    log.info(f"Tarama tamamlandı: {len(yeni_cache)} hisse")

async def gunluk_ozet(app):
    if not hafta_ici_mi():
        return
    deger = portfoy_degeri()
    kz    = deger - portfoy["baslangic"]
    kz_p  = (kz / portfoy["baslangic"]) * 100
    mesaj = (f"🌆 *Günlük Özet — {simdi_str()}*\n\n"
             f"💼 Portföy: {deger:,.2f} ₺\n"
             f"🏦 Nakit: {portfoy['bakiye']:,.2f} ₺\n"
             f"{'📈' if kz>=0 else '📉'} K/Z: {kz:+,.2f} ₺ ({kz_p:+.1f}%)\n"
             f"📂 Açık Pozisyon: {len(portfoy['pozisyonlar'])}\n\n"
             f"⚠️ _Sanal portföy_")
    try:
        await app.bot.send_message(chat_id=CHAT_ID, text=mesaj, parse_mode="Markdown")
    except Exception as e:
        log.error(f"Özet hatası: {e}")

# ─────────────────────────────────────────
# 🤖  TELEGRAM KOMUTLARI
# ─────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *BIST 100 Paper Trading Botu*\n\n"
        "🌐 [Dashboard → bistbot-production.up.railway.app](https://bistbot-production.up.railway.app)\n\n"
        "/portfoy — Anlık portföy\n"
        "/islemler — Son işlemler\n"
        "/performans — Genel performans\n"
        "/sinyal GARAN — Tek hisse analiz\n"
        "/sifirla — Portföyü sıfırla\n\n"
        "📡 Her 15dk BIST100 taranır (10:00-18:00)",
        parse_mode="Markdown"
    )

async def cmd_portfoy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    deger = portfoy_degeri()
    kz    = deger - portfoy["baslangic"]
    kz_p  = (kz / portfoy["baslangic"]) * 100
    satirlar = [f"💼 *Portföy — {simdi_str()}*\n", f"🏦 Nakit: {portfoy['bakiye']:,.2f} ₺"]
    for sembol, poz in portfoy["pozisyonlar"].items():
        fiyat = son_fiyat_al(sembol) or poz["maliyet"]
        kz_p2 = ((poz["adet"]*fiyat - poz["toplam"]) / poz["toplam"]) * 100
        e = "🟢" if kz_p2 >= 0 else "🔴"
        satirlar.append(f"{e} *{sembol}* {poz['adet']} adet | {kz_p2:+.1f}%")
    satirlar.append(f"\n💰 Toplam: {deger:,.2f} ₺")
    satirlar.append(f"{'📈' if kz>=0 else '📉'} K/Z: {kz:+,.2f} ₺ ({kz_p:+.1f}%)")
    await update.message.reply_text("\n".join(satirlar), parse_mode="Markdown")

async def cmd_islemler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    satislar = [i for i in islem_gecmisi if i["tip"] == "SAT"]
    if not satislar:
        await update.message.reply_text("📭 Henüz tamamlanan işlem yok.")
        return
    satirlar = ["📋 *Son 10 İşlem*\n"]
    for i in reversed(satislar[-10:]):
        e = "📈" if i["kar_zarar"] >= 0 else "📉"
        satirlar.append(
            f"{e} *{i['sembol']}* | Alış: {i['alis_fiyat']}₺ → Satış: {i['satis_fiyat']}₺\n"
            f"   K/Z: {i['kar_zarar']:+.2f}₺ ({i['kar_pct']:+.1f}%) | Süre: {i['sure']} | Komisyon: {i['komisyon']}₺"
        )
    await update.message.reply_text("\n\n".join(satirlar), parse_mode="Markdown")

async def cmd_performans(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    deger    = portfoy_degeri()
    kz       = deger - portfoy["baslangic"]
    kz_p     = (kz / portfoy["baslangic"]) * 100
    satislar = [i for i in islem_gecmisi if i["tip"] == "SAT"]
    kazanan  = [i for i in satislar if (i.get("kar_zarar") or 0) > 0]
    oran     = len(kazanan)/len(satislar)*100 if satislar else 0
    toplam_komisyon = sum(i.get("komisyon", 0) for i in islem_gecmisi)
    mesaj = (f"🏆 *Performans*\n\n"
             f"💰 Başlangıç: {portfoy['baslangic']:,.0f} ₺\n"
             f"💼 Güncel: {deger:,.2f} ₺\n"
             f"{'📈' if kz>=0 else '📉'} K/Z: {kz:+,.2f} ₺ ({kz_p:+.1f}%)\n\n"
             f"✅ Kazanan: {len(kazanan)}/{len(satislar)} ({oran:.1f}%)\n"
             f"💸 Toplam Komisyon: {toplam_komisyon:.2f} ₺\n"
             f"📂 Açık Pozisyon: {len(portfoy['pozisyonlar'])}\n"
             f"🔢 Toplam İşlem: {len(islem_gecmisi)}")
    await update.message.reply_text(mesaj, parse_mode="Markdown")

async def cmd_sinyal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Kullanım: /sinyal GARAN")
        return
    sembol = ctx.args[0].upper()
    await update.message.reply_text(f"⏳ {sembol} analiz ediliyor...")
    t = teknik_analiz(sembol)
    if not t:
        await update.message.reply_text(f"⚠️ {sembol}: veri alınamadı")
        return
    e = {"AL":"🟢","SAT":"🔴","BEKLE":"🟡"}.get(t["sinyal"],"⚪")
    await update.message.reply_text(
        f"{e} *{sembol}* — {t['sinyal']}\n"
        f"💰 {t['fiyat']} ₺ ({t['degisim']:+.2f}%)\n"
        f"📊 RSI: {t['rsi']} | MACD: {'↑' if t['macd']>t['macd_sig'] else '↓'}\n"
        f"📈 MA20: {t['ma20']} | MA50: {t['ma50']}\n"
        f"🎯 Skor: {t['skor']}/10",
        parse_mode="Markdown"
    )

async def cmd_sifirla(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    portfoy["bakiye"]      = BASLANGIC_BAKIYE
    portfoy["baslangic"]   = BASLANGIC_BAKIYE
    portfoy["pozisyonlar"] = {}
    islem_gecmisi.clear()
    await update.message.reply_text(f"♻️ Sıfırlandı! Bakiye: {BASLANGIC_BAKIYE:,.0f} ₺")

# ─────────────────────────────────────────
# 🚀  MAIN
# ─────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        print("❌ Environment variable'lar eksik!")
        return

    # Flask dashboard thread
    threading.Thread(target=flask_thread, daemon=True).start()
    log.info(f"Dashboard: http://0.0.0.0:{PORT}")

    # İlk cache yüklemesini arka planda başlat
    threading.Thread(target=bist100_cache_guncelle, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("portfoy",    cmd_portfoy))
    app.add_handler(CommandHandler("islemler",   cmd_islemler))
    app.add_handler(CommandHandler("performans", cmd_performans))
    app.add_handler(CommandHandler("sinyal",     cmd_sinyal))
    app.add_handler(CommandHandler("sifirla",    cmd_sifirla))

    jq = app.job_queue
    jq.run_repeating(
        lambda ctx: asyncio.create_task(bist100_tara(app)),
        interval=TARAMA_ARALIK, first=120
    )
    jq.run_daily(
        lambda ctx: asyncio.create_task(gunluk_ozet(app)),
        time=dtime(18, 5, tzinfo=TZ), days=(0,1,2,3,4)
    )

    log.info(f"Bot aktif! {len(BIST100)} hisse takipte.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
