"""
BIST 100 Paper Trading Botu
============================
- BIST 100 hisselerini sürekli tarar
- AL sinyalinde sanal alım, SAT sinyalinde sanal satım yapar
- 50.000₺ sanal başlangıç bakiyesi
- %0.1 komisyon (gerçekçilik için)
- Telegram üzerinden portföy takibi
- Hafta sonu çalışmaz

Komutlar:
    /portfoy     → Anlık portföy durumu
    /islemler    → Son işlemler
    /performans  → Genel performans özeti
    /sinyal GARAN → Tek hisse analiz
    /rapor       → Tüm BIST100 özet
    /sifirla     → Portföyü sıfırla (dikkat!)
"""

import asyncio
import logging
import os
import json
import requests
import pandas as pd
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import yfinance as yf
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import anthropic

# ─────────────────────────────────────────
# ⚙️  AYARLAR
# ─────────────────────────────────────────

BOT_TOKEN      = os.environ.get("BOT_TOKEN")
CHAT_ID        = os.environ.get("CHAT_ID")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")

TZ = ZoneInfo("Europe/Istanbul")

BASLANGIC_BAKIYE = 50_000.0   # Sanal başlangıç bakiyesi (₺)
KOMISYON_ORANI   = 0.001       # %0.1 komisyon
MAX_POZISYON_PCT = 0.10        # Tek hisseye max %10 sermaye
TARAMA_ARALIK    = 15 * 60     # Her 15 dakikada bir tara (saniye)

# ─────────────────────────────────────────
# 📋  BIST 100 LİSTESİ
# ─────────────────────────────────────────

BIST100 = [
    "AEFES", "AGESA", "AKBNK", "AKFEN", "AKGRT", "AKSEN", "ALARK", "ALBRK", "ALFAS", "ARCLK",
    "ASELS", "ASTOR", "AVGYO", "AYDEM", "BIMAS", "BRSAN", "BRYAT", "BTCIM", "BUCIM", "CIMSA",
    "CWENE", "DOAS", "DOHOL", "DSTKF", "DYNMO", "ECILC", "EGEEN", "EKGYO", "ENJSA", "ENKAI",
    "EREGL", "FROTO", "GARAN", "GESAN", "GLYHO", "GUBRF", "GWIND", "HALKB", "HEKTS", "IPEKE",
    "ISCTR", "ISDMR", "ISGYO", "ISFIN", "ISMEN", "KARSN", "KCHOL", "KONTR", "KONYA", "KORDS",
    "KOZAA", "KOZAL", "KRDMD", "KTLEV", "LOGO", "MAVI", "MGROS", "MPARK", "NETAS", "ODAS",
    "OTKAR", "OYAKC", "PETKM", "PGSUS", "POLHO", "PRKAB", "QUAGR", "REEDR", "SAHOL", "SASA",
    "SELEC", "SILVR", "SISE", "SKBNK", "SMRTG", "SNGYO", "SOKM", "TAVHL", "TCELL", "THYAO",
    "TKFEN", "TKNSA", "TOASO", "TRGYO", "TRALT", "TTKOM", "TTRAK", "TUKAS", "TUPRS", "TURSG",
    "ULKER", "USDTR", "VAKBN", "VESBE", "VESTL", "YKBNK", "YATAS", "YEOTK", "ZOREN", "AKBNK",
]
BIST100 = list(dict.fromkeys(BIST100))  # Tekrarları kaldır

# ─────────────────────────────────────────
# 💼  SANAL PORTFÖY
# ─────────────────────────────────────────

portfoy = {
    "bakiye":     BASLANGIC_BAKIYE,
    "baslangic":  BASLANGIC_BAKIYE,
    "pozisyonlar": {},
    # {"GARAN": {"adet": 100, "maliyet": 130.5, "tarih": "..."}}
}

islem_gecmisi = []
# [{"tip": "AL", "sembol": "GARAN", "adet": 100, "fiyat": 130.5, "tarih": "..."}]

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────
# 🛡️  YARDIMCI FONKSİYONLAR
# ─────────────────────────────────────────

def hafta_ici_mi() -> bool:
    return datetime.now(TZ).weekday() < 5

def borsa_acik_mi() -> bool:
    """BIST 10:00-18:00 arası açık (hafta içi)."""
    if not hafta_ici_mi():
        return False
    simdi = datetime.now(TZ).time()
    return dtime(10, 0) <= simdi <= dtime(18, 0)

def simdi_str() -> str:
    return datetime.now(TZ).strftime("%d.%m.%Y %H:%M")

def son_fiyat_al(sembol: str) -> float | None:
    try:
        ticker = yf.Ticker(f"{sembol}.IS")
        df = ticker.history(period="1d", interval="1m")
        if df.empty:
            return None
        return float(df["Close"].iloc[-1])
    except Exception as e:
        log.warning(f"Fiyat alınamadı ({sembol}): {e}")
        return None


# ─────────────────────────────────────────
# 📊  TEKNİK ANALİZ
# ─────────────────────────────────────────

def rsi_hesapla(fiyatlar: pd.Series, periyot: int = 14) -> float:
    delta = fiyatlar.diff()
    kazan = delta.clip(lower=0)
    kayip = -delta.clip(upper=0)
    rs = kazan.rolling(periyot).mean() / kayip.rolling(periyot).mean()
    return round(float(100 - (100 / (1 + rs)).iloc[-1]), 1)

def macd_hesapla(fiyatlar: pd.Series):
    ema12  = fiyatlar.ewm(span=12).mean()
    ema26  = fiyatlar.ewm(span=26).mean()
    macd   = ema12 - ema26
    sinyal = macd.ewm(span=9).mean()
    return round(float(macd.iloc[-1]), 3), round(float(sinyal.iloc[-1]), 3)

def teknik_analiz(sembol: str) -> dict | None:
    try:
        df = yf.Ticker(f"{sembol}.IS").history(period="3mo", interval="1d")
        if df.empty or len(df) < 30:
            return None

        kapanis   = df["Close"]
        son_fiyat = round(float(kapanis.iloc[-1]), 2)
        onceki    = round(float(kapanis.iloc[-2]), 2)
        degisim   = round((son_fiyat - onceki) / onceki * 100, 2)

        rsi            = rsi_hesapla(kapanis)
        macd, macd_sig = macd_hesapla(kapanis)
        ma20           = round(float(kapanis.rolling(20).mean().iloc[-1]), 2)
        ma50           = round(float(kapanis.rolling(50).mean().iloc[-1]), 2)

        hacim_ort = float(df["Volume"].rolling(10).mean().iloc[-1])
        son_hacim = float(df["Volume"].iloc[-1])
        hacim_guc = "yüksek" if son_hacim > hacim_ort * 1.2 else "normal"

        skor = 0
        if rsi < 35:   skor += 3
        elif rsi < 50: skor += 1
        elif rsi > 65: skor -= 2

        if macd > macd_sig: skor += 2
        else:               skor -= 1

        if son_fiyat > ma20: skor += 1
        else:                skor -= 1

        if son_fiyat > ma50: skor += 2
        else:                skor -= 1

        if hacim_guc == "yüksek" and degisim > 0:
            skor += 1

        if skor >= 5:   sinyal = "AL"
        elif skor <= 1: sinyal = "SAT"
        else:           sinyal = "BEKLE"

        return {
            "sembol": sembol, "fiyat": son_fiyat, "degisim": degisim,
            "rsi": rsi, "macd": macd, "macd_sig": macd_sig,
            "ma20": ma20, "ma50": ma50, "hacim": hacim_guc,
            "skor": skor, "sinyal": sinyal,
        }
    except Exception as e:
        log.warning(f"{sembol} analiz hatası: {e}")
        return None


# ─────────────────────────────────────────
# 💰  SANAL İŞLEM FONKSİYONLARI
# ─────────────────────────────────────────

def sanal_al(sembol: str, fiyat: float) -> str | None:
    """
    AL sinyalinde sanal alım yapar.
    Maksimum portföyün %10'u kadar pozisyon açar.
    """
    if sembol in portfoy["pozisyonlar"]:
        return None  # Zaten pozisyon var

    max_tutar  = portfoy["bakiye"] * MAX_POZISYON_PCT
    komisyon   = max_tutar * KOMISYON_ORANI
    net_tutar  = max_tutar - komisyon

    if net_tutar < fiyat:
        return None  # Yeterli bakiye yok

    adet = int(net_tutar / fiyat)
    if adet < 1:
        return None

    toplam_maliyet = adet * fiyat + (adet * fiyat * KOMISYON_ORANI)

    if toplam_maliyet > portfoy["bakiye"]:
        return None

    portfoy["bakiye"] -= toplam_maliyet
    portfoy["pozisyonlar"][sembol] = {
        "adet":    adet,
        "maliyet": fiyat,
        "tarih":   simdi_str(),
        "toplam":  toplam_maliyet,
    }

    islem_gecmisi.append({
        "tip": "AL", "sembol": sembol, "adet": adet,
        "fiyat": fiyat, "tarih": simdi_str(),
        "tutar": toplam_maliyet,
    })

    log.info(f"SANAL AL: {sembol} x{adet} @ {fiyat}₺ = {toplam_maliyet:.2f}₺")
    return (
        f"🟢 *SANAL AL* — {sembol}\n"
        f"💰 {adet} adet @ {fiyat} ₺\n"
        f"💸 Toplam: {toplam_maliyet:.2f} ₺ (komisyon dahil)\n"
        f"🏦 Kalan bakiye: {portfoy['bakiye']:.2f} ₺"
    )


def sanal_sat(sembol: str, fiyat: float) -> str | None:
    """SAT sinyalinde sanal satım yapar."""
    if sembol not in portfoy["pozisyonlar"]:
        return None  # Pozisyon yok

    poz          = portfoy["pozisyonlar"].pop(sembol)
    adet         = poz["adet"]
    maliyet      = poz["maliyet"]
    gelir        = adet * fiyat
    komisyon     = gelir * KOMISYON_ORANI
    net_gelir    = gelir - komisyon
    kar_zarar    = net_gelir - poz["toplam"]
    kar_zarar_pct = (kar_zarar / poz["toplam"]) * 100

    portfoy["bakiye"] += net_gelir

    islem_gecmisi.append({
        "tip": "SAT", "sembol": sembol, "adet": adet,
        "fiyat": fiyat, "tarih": simdi_str(),
        "tutar": net_gelir, "kar_zarar": kar_zarar,
    })

    emoji = "📈" if kar_zarar >= 0 else "📉"
    log.info(f"SANAL SAT: {sembol} x{adet} @ {fiyat}₺ | K/Z: {kar_zarar:.2f}₺")
    return (
        f"🔴 *SANAL SAT* — {sembol}\n"
        f"💰 {adet} adet @ {fiyat} ₺\n"
        f"💸 Net gelir: {net_gelir:.2f} ₺\n"
        f"{emoji} Kar/Zarar: {kar_zarar:+.2f} ₺ ({kar_zarar_pct:+.1f}%)\n"
        f"🏦 Yeni bakiye: {portfoy['bakiye']:.2f} ₺"
    )


def portfoy_degeri() -> float:
    """Mevcut pozisyonların anlık piyasa değeri + nakit bakiye."""
    toplam = portfoy["bakiye"]
    for sembol, poz in portfoy["pozisyonlar"].items():
        fiyat = son_fiyat_al(sembol)
        if fiyat:
            toplam += poz["adet"] * fiyat
        else:
            toplam += poz["toplam"]  # Fiyat alınamazsa maliyetle hesapla
    return toplam


# ─────────────────────────────────────────
# 📰  KAP HABERLERİ
# ─────────────────────────────────────────

def kap_haberleri_getir(sembol: str, limit: int = 2) -> list[str]:
    try:
        url = f"https://www.kap.org.tr/tr/api/memberDisclosureQuery/members/{sembol}/disclosures"
        r   = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        if r.status_code != 200:
            return []
        haberler = []
        for item in r.json()[:limit]:
            baslik = item.get("disclosureClass", "") + " - " + item.get("subject", "")
            tarih  = item.get("publishDate", "")[:10]
            haberler.append(f"{tarih}: {baslik}")
        return haberler
    except:
        return []


# ─────────────────────────────────────────
# 🤖  CLAUDE AI ANALİZ
# ─────────────────────────────────────────

def claude_analiz(sembol: str, teknik: dict) -> str:
    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        prompt = f"""
{sembol} hissesi için kısa yatırım analizi:
Fiyat: {teknik['fiyat']} TL ({teknik['degisim']:+.2f}%)
RSI: {teknik['rsi']} | MACD: {teknik['macd']} / {teknik['macd_sig']}
MA20: {teknik['ma20']} | MA50: {teknik['ma50']}
Teknik Sinyal: {teknik['sinyal']} (Skor: {teknik['skor']}/10)

2-3 cümle değerlendirme ve AL/SAT/BEKLE karar ver. Türkçe, kısa.
"""
        mesaj = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        return mesaj.content[0].text
    except Exception as e:
        return f"AI yorum yapılamadı: {e}"


# ─────────────────────────────────────────
# ⏰  OTOMATİK TARAMA
# ─────────────────────────────────────────

async def bist100_tara(app: Application):
    """
    Her 15 dakikada BIST100'ü tarar.
    AL sinyalinde sanal alım, SAT sinyalinde sanal satım yapar.
    Borsa kapalıysa çalışmaz.
    """
    if not borsa_acik_mi():
        log.info("Borsa kapalı, tarama atlandı.")
        return

    log.info("BIST100 taraması başlıyor...")
    islem_sayisi = 0

    for sembol in BIST100:
        teknik = teknik_analiz(sembol)
        if not teknik:
            continue

        mesaj = None

        if teknik["sinyal"] == "AL":
            mesaj = sanal_al(sembol, teknik["fiyat"])

        elif teknik["sinyal"] == "SAT":
            mesaj = sanal_sat(sembol, teknik["fiyat"])

        if mesaj:
            islem_sayisi += 1
            try:
                await app.bot.send_message(chat_id=CHAT_ID, text=mesaj, parse_mode="Markdown")
                await asyncio.sleep(1)  # Telegram rate limit
            except Exception as e:
                log.error(f"Mesaj gönderilemedi: {e}")

    log.info(f"Tarama tamamlandı. {islem_sayisi} işlem yapıldı.")


async def gunluk_ozet(app: Application):
    """Her gün 18:05'te günlük özet gönderir."""
    if not hafta_ici_mi():
        return

    toplam_deger = portfoy_degeri()
    kar_zarar    = toplam_deger - portfoy["baslangic"]
    kar_zarar_pct = (kar_zarar / portfoy["baslangic"]) * 100

    bugun_islemler = [i for i in islem_gecmisi
                      if i["tarih"].startswith(datetime.now(TZ).strftime("%d.%m.%Y"))]
    bugun_kar = sum(i.get("kar_zarar", 0) for i in bugun_islemler if i["tip"] == "SAT")

    emoji = "📈" if kar_zarar >= 0 else "📉"
    mesaj = (
        f"🌆 *Günlük Özet — {datetime.now(TZ).strftime('%d.%m.%Y')}*\n\n"
        f"💼 Portföy Değeri: {toplam_deger:.2f} ₺\n"
        f"🏦 Nakit Bakiye: {portfoy['bakiye']:.2f} ₺\n"
        f"📊 Açık Pozisyon: {len(portfoy['pozisyonlar'])} hisse\n\n"
        f"{emoji} Toplam K/Z: {kar_zarar:+.2f} ₺ ({kar_zarar_pct:+.1f}%)\n"
        f"📅 Bugünkü K/Z: {bugun_kar:+.2f} ₺\n"
        f"🔢 Bugünkü İşlem: {len(bugun_islemler)}\n\n"
        f"⚠️ _Bu sanal portföydür, gerçek para değildir._"
    )
    try:
        await app.bot.send_message(chat_id=CHAT_ID, text=mesaj, parse_mode="Markdown")
    except Exception as e:
        log.error(f"Günlük özet gönderilemedi: {e}")


# ─────────────────────────────────────────
# 🤖  TELEGRAM KOMUTLARI
# ─────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mesaj = (
        "👋 *BIST 100 Paper Trading Botu*\n\n"
        "🤖 50.000₺ sanal sermaye ile otomatik al-sat yapıyorum!\n\n"
        "/portfoy — Anlık portföy durumu\n"
        "/islemler — Son 10 işlem\n"
        "/performans — Genel performans\n"
        "/sinyal GARAN — Tek hisse analiz\n"
        "/rapor — BIST100 özet tarama\n"
        "/sifirla — Portföyü sıfırla\n\n"
        "📡 Her 15 dakikada BIST100 taranır.\n"
        "⏰ Borsa saatleri: 10:00-18:00 (hafta içi)"
    )
    await update.message.reply_text(mesaj, parse_mode="Markdown")


async def cmd_portfoy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Portföy hesaplanıyor...")

    toplam_deger  = portfoy["bakiye"]
    satirlar      = [f"💼 *Portföy Durumu — {simdi_str()}*\n"]
    satirlar.append(f"🏦 Nakit: {portfoy['bakiye']:.2f} ₺\n")

    if portfoy["pozisyonlar"]:
        satirlar.append("📊 *Açık Pozisyonlar:*")
        for sembol, poz in portfoy["pozisyonlar"].items():
            fiyat = son_fiyat_al(sembol) or poz["maliyet"]
            guncel_deger = poz["adet"] * fiyat
            kar_zarar    = guncel_deger - poz["toplam"]
            kar_pct      = (kar_zarar / poz["toplam"]) * 100
            emoji        = "🟢" if kar_zarar >= 0 else "🔴"
            satirlar.append(
                f"{emoji} *{sembol}* | {poz['adet']} adet | "
                f"Maliyet: {poz['maliyet']}₺ | Şu an: {fiyat:.2f}₺ | "
                f"{kar_zarar:+.2f}₺ ({kar_pct:+.1f}%)"
            )
            toplam_deger += guncel_deger
    else:
        satirlar.append("📭 Açık pozisyon yok.")

    kar_zarar_toplam = toplam_deger - portfoy["baslangic"]
    kar_pct_toplam   = (kar_zarar_toplam / portfoy["baslangic"]) * 100
    emoji_toplam     = "📈" if kar_zarar_toplam >= 0 else "📉"

    satirlar.append(f"\n💰 *Toplam Portföy: {toplam_deger:.2f} ₺*")
    satirlar.append(f"{emoji_toplam} Başlangıçtan bu yana: {kar_zarar_toplam:+.2f} ₺ ({kar_pct_toplam:+.1f}%)")
    satirlar.append("\n⚠️ _Sanal portföy — gerçek para değil_")

    await update.message.reply_text("\n".join(satirlar), parse_mode="Markdown")


async def cmd_islemler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not islem_gecmisi:
        await update.message.reply_text("📭 Henüz işlem yapılmadı.")
        return

    son10 = islem_gecmisi[-10:][::-1]
    satirlar = ["📋 *Son İşlemler*\n"]
    for i in son10:
        emoji = "🟢" if i["tip"] == "AL" else "🔴"
        kz    = f" | {i['kar_zarar']:+.2f}₺" if "kar_zarar" in i else ""
        satirlar.append(
            f"{emoji} *{i['tip']}* {i['sembol']} — "
            f"{i['adet']} adet @ {i['fiyat']}₺{kz}\n"
            f"🕐 {i['tarih']}"
        )
    await update.message.reply_text("\n\n".join(satirlar), parse_mode="Markdown")


async def cmd_performans(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    toplam_deger = portfoy_degeri()
    kar_zarar    = toplam_deger - portfoy["baslangic"]
    kar_pct      = (kar_zarar / portfoy["baslangic"]) * 100

    tum_satislar    = [i for i in islem_gecmisi if i["tip"] == "SAT"]
    kazanan         = [i for i in tum_satislar if i.get("kar_zarar", 0) > 0]
    kaybeden        = [i for i in tum_satislar if i.get("kar_zarar", 0) <= 0]
    toplam_kar      = sum(i.get("kar_zarar", 0) for i in kazanan)
    toplam_zarar    = sum(i.get("kar_zarar", 0) for i in kaybeden)
    basari_orani    = (len(kazanan) / len(tum_satislar) * 100) if tum_satislar else 0

    emoji = "📈" if kar_zarar >= 0 else "📉"
    mesaj = (
        f"🏆 *Performans Raporu*\n\n"
        f"💰 Başlangıç: {portfoy['baslangic']:.2f} ₺\n"
        f"💼 Güncel Değer: {toplam_deger:.2f} ₺\n"
        f"{emoji} Toplam K/Z: {kar_zarar:+.2f} ₺ ({kar_pct:+.1f}%)\n\n"
        f"📊 *İşlem İstatistikleri*\n"
        f"Toplam İşlem: {len(islem_gecmisi)}\n"
        f"Kapatılan Pozisyon: {len(tum_satislar)}\n"
        f"✅ Kazanan: {len(kazanan)} ({basari_orani:.1f}%)\n"
        f"❌ Kaybeden: {len(kaybeden)}\n"
        f"📈 Toplam Kar: {toplam_kar:+.2f} ₺\n"
        f"📉 Toplam Zarar: {toplam_zarar:+.2f} ₺\n"
        f"🏦 Nakit Bakiye: {portfoy['bakiye']:.2f} ₺\n"
        f"📂 Açık Pozisyon: {len(portfoy['pozisyonlar'])}\n\n"
        f"⚠️ _Sanal portföy — gerçek para değil_"
    )
    await update.message.reply_text(mesaj, parse_mode="Markdown")


async def cmd_sinyal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Kullanım: /sinyal GARAN")
        return
    sembol = ctx.args[0].upper()
    await update.message.reply_text(f"⏳ {sembol} analiz ediliyor...")
    teknik = teknik_analiz(sembol)
    if not teknik:
        await update.message.reply_text(f"⚠️ {sembol}: veri alınamadı")
        return

    emoji  = {"AL": "🟢", "SAT": "🔴", "BEKLE": "🟡"}.get(teknik["sinyal"], "⚪")
    yorum  = claude_analiz(sembol, teknik)
    rapor  = (
        f"{emoji} *{sembol}* — {teknik['sinyal']}\n"
        f"💰 {teknik['fiyat']} ₺ ({teknik['degisim']:+.2f}%)\n"
        f"📊 RSI: {teknik['rsi']} | MACD: {'↑' if teknik['macd'] > teknik['macd_sig'] else '↓'}\n"
        f"📈 MA20: {teknik['ma20']} | MA50: {teknik['ma50']}\n"
        f"🎯 Skor: {teknik['skor']}/10\n\n"
        f"🤖 *AI Yorum:*\n{yorum}"
    )
    await update.message.reply_text(rapor, parse_mode="Markdown")


async def cmd_rapor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ BIST100 taranıyor, biraz bekle (bu uzun sürebilir)...")
    app = ctx.application
    await bist100_tara(app)
    await update.message.reply_text("✅ Tarama tamamlandı! /portfoy ile durumu görebilirsin.")


async def cmd_sifirla(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    portfoy["bakiye"]      = BASLANGIC_BAKIYE
    portfoy["baslangic"]   = BASLANGIC_BAKIYE
    portfoy["pozisyonlar"] = {}
    islem_gecmisi.clear()
    await update.message.reply_text(
        f"♻️ Portföy sıfırlandı!\n"
        f"💰 Yeni bakiye: {BASLANGIC_BAKIYE:,.2f} ₺"
    )


async def cmd_yardim(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


# ─────────────────────────────────────────
# 🚀  ANA FONKSİYON
# ─────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN, CHAT_ID ve CLAUDE_API_KEY environment variable olarak tanımlanmalı!")
        return

    log.info("BIST 100 Paper Trading Botu başlatılıyor...")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("portfoy",    cmd_portfoy))
    app.add_handler(CommandHandler("islemler",   cmd_islemler))
    app.add_handler(CommandHandler("performans", cmd_performans))
    app.add_handler(CommandHandler("sinyal",     cmd_sinyal))
    app.add_handler(CommandHandler("rapor",      cmd_rapor))
    app.add_handler(CommandHandler("sifirla",    cmd_sifirla))
    app.add_handler(CommandHandler("yardim",     cmd_yardim))

    jq = app.job_queue

    # Her 15 dakikada BIST100 tara (borsa saatleri 10:00-18:00)
    jq.run_repeating(
        lambda ctx: asyncio.create_task(bist100_tara(app)),
        interval=TARAMA_ARALIK, first=60
    )

    # Her gün 18:05'te günlük özet
    jq.run_daily(
        lambda ctx: asyncio.create_task(gunluk_ozet(app)),
        time=dtime(18, 5, tzinfo=TZ),
        days=(0, 1, 2, 3, 4)
    )

    log.info(f"BIST100 takip listesi: {len(BIST100)} hisse")
    log.info(f"Sanal başlangıç bakiyesi: {BASLANGIC_BAKIYE:,.0f} ₺")
    log.info("Bot aktif! Telegram'dan /start ile başla.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
