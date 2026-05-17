"""
BIST 30 Sinyal Botu
===================
- Her sabah 09:30'da BIST 30 hisseleri için AL/SAT/BEKLE sinyali
- KAP haberlerini anlık takip, önemli haberlerde anında bildirim
- %2 stop-loss alarmı
- Claude AI ile haber + teknik analiz yorumlama
- Telegram'a otomatik mesaj

Kurulum:
    pip install -r requirements_sinyal.txt

Ayarlar:
    BOT_TOKEN     → @BotFather'dan
    CHAT_ID       → @userinfobot'tan
    CLAUDE_API_KEY → console.anthropic.com'dan
"""

import asyncio
import logging
import requests
import pandas as pd
import numpy as np
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import yfinance as yf
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import anthropic

# ─────────────────────────────────────────
# ⚙️  AYARLAR
# ─────────────────────────────────────────

BOT_TOKEN      = "8412980491:AAFQdm_A8OWOpf70JwlKKHYN_QDe4IVsygw"
CHAT_ID        = "628255204"
CLAUDE_API_KEY = "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"  # console.anthropic.com

TZ = ZoneInfo("Europe/Istanbul")

# BIST 30 hisseleri
BIST30 = [
    "AKBNK", "AKSEN", "ARCLK", "ASELS", "BIMAS",
    "EKGYO", "EREGL", "FROTO", "GARAN", "GUBRF",
    "HALKB", "ISCTR", "KCHOL", "KOZAA", "KOZAL",
    "KRDMD", "MGROS", "ODAS", "OYAKC", "PGSUS",
    "SAHOL", "SASA",  "SISE",  "TAVHL", "TCELL",
    "THYAO", "TKFEN", "TOASO", "TTKOM", "YKBNK"
]

# Aktif pozisyonlar: {"GARAN": {"giris": 130.5, "sinyal": "AL"}}
pozisyonlar: dict = {}

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# 📊  TEKNİK ANALİZ
# ─────────────────────────────────────────

def rsi_hesapla(fiyatlar: pd.Series, periyot: int = 14) -> float:
    delta = fiyatlar.diff()
    kazan = delta.clip(lower=0)
    kayip = -delta.clip(upper=0)
    ort_kazan = kazan.rolling(periyot).mean()
    ort_kayip = kayip.rolling(periyot).mean()
    rs = ort_kazan / ort_kayip
    rsi = 100 - (100 / (1 + rs))
    return round(rsi.iloc[-1], 1)


def macd_hesapla(fiyatlar: pd.Series):
    ema12 = fiyatlar.ewm(span=12).mean()
    ema26 = fiyatlar.ewm(span=26).mean()
    macd  = ema12 - ema26
    sinyal = macd.ewm(span=9).mean()
    return round(macd.iloc[-1], 3), round(sinyal.iloc[-1], 3)


def teknik_analiz(sembol: str) -> dict | None:
    try:
        ticker = yf.Ticker(f"{sembol}.IS")
        df = ticker.history(period="3mo", interval="1d")
        if df.empty or len(df) < 30:
            return None

        kapanis = df["Close"]
        son_fiyat = round(kapanis.iloc[-1], 2)
        onceki    = round(kapanis.iloc[-2], 2)
        degisim   = round((son_fiyat - onceki) / onceki * 100, 2)

        rsi  = rsi_hesapla(kapanis)
        macd, macd_sig = macd_hesapla(kapanis)
        ma20 = round(kapanis.rolling(20).mean().iloc[-1], 2)
        ma50 = round(kapanis.rolling(50).mean().iloc[-1], 2)

        # Hacim trendi
        hacim_ort = df["Volume"].rolling(10).mean().iloc[-1]
        son_hacim = df["Volume"].iloc[-1]
        hacim_guc = "yüksek" if son_hacim > hacim_ort * 1.2 else "normal"

        # Destek / Direnç (son 20 günün min/max)
        destek   = round(kapanis.tail(20).min(), 2)
        direnc   = round(kapanis.tail(20).max(), 2)

        # Sinyal skoru hesapla (0-10)
        skor = 0
        yorumlar = []

        if rsi < 35:
            skor += 3
            yorumlar.append(f"RSI aşırı satım ({rsi})")
        elif rsi < 50:
            skor += 1
            yorumlar.append(f"RSI ({rsi}) nötr-zayıf")
        elif rsi > 65:
            skor -= 2
            yorumlar.append(f"RSI aşırı alım ({rsi})")
        else:
            yorumlar.append(f"RSI ({rsi}) nötr")

        if macd > macd_sig:
            skor += 2
            yorumlar.append("MACD pozitif")
        else:
            skor -= 1
            yorumlar.append("MACD negatif")

        if son_fiyat > ma20:
            skor += 1
            yorumlar.append("MA20 üstünde")
        else:
            skor -= 1
            yorumlar.append("MA20 altında")

        if son_fiyat > ma50:
            skor += 2
            yorumlar.append("MA50 üstünde (güçlü trend)")
        else:
            skor -= 1
            yorumlar.append("MA50 altında")

        if hacim_guc == "yüksek" and degisim > 0:
            skor += 1
            yorumlar.append("Yüksek hacimli yükseliş")

        # Nihai sinyal
        if skor >= 5:
            sinyal = "AL"
        elif skor <= 1:
            sinyal = "SAT"
        else:
            sinyal = "BEKLE"

        return {
            "sembol":   sembol,
            "fiyat":    son_fiyat,
            "degisim":  degisim,
            "rsi":      rsi,
            "macd":     macd,
            "macd_sig": macd_sig,
            "ma20":     ma20,
            "ma50":     ma50,
            "destek":   destek,
            "direnc":   direnc,
            "hacim":    hacim_guc,
            "skor":     skor,
            "sinyal":   sinyal,
            "yorumlar": yorumlar,
        }
    except Exception as e:
        log.warning(f"{sembol} teknik analiz hatası: {e}")
        return None


# ─────────────────────────────────────────
# 📰  KAP HABERLERİ
# ─────────────────────────────────────────

def kap_haberleri_getir(sembol: str, limit: int = 3) -> list[str]:
    """KAP'tan son bildirimleri çeker."""
    try:
        url = f"https://www.kap.org.tr/tr/api/memberDisclosureQuery/members/{sembol}/disclosures"
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return []
        data = r.json()
        haberler = []
        for item in data[:limit]:
            baslik = item.get("disclosureClass", "") + " - " + item.get("subject", "")
            tarih  = item.get("publishDate", "")[:10]
            haberler.append(f"{tarih}: {baslik}")
        return haberler
    except Exception as e:
        log.warning(f"KAP haber hatası ({sembol}): {e}")
        return []


def kap_son_bildirimler(limit: int = 10) -> list[dict]:
    """Tüm BIST için son bildirimleri getirir."""
    try:
        url = "https://www.kap.org.tr/tr/api/disclosures/last"
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return []
        return r.json()[:limit]
    except Exception as e:
        log.warning(f"KAP genel haber hatası: {e}")
        return []


# ─────────────────────────────────────────
# 🤖  CLAUDE AI ANALİZ
# ─────────────────────────────────────────

def claude_analiz(sembol: str, teknik: dict, haberler: list[str]) -> str:
    """Claude AI ile teknik veri + haberleri birleştirip yorum üretir."""
    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

        haber_metni = "\n".join(haberler) if haberler else "Son bildirim yok."
        teknik_metni = (
            f"Fiyat: {teknik['fiyat']} TL ({teknik['degisim']:+.2f}%)\n"
            f"RSI: {teknik['rsi']}\n"
            f"MACD: {teknik['macd']} / Sinyal: {teknik['macd_sig']}\n"
            f"MA20: {teknik['ma20']} | MA50: {teknik['ma50']}\n"
            f"Destek: {teknik['destek']} | Direnç: {teknik['direnc']}\n"
            f"Hacim: {teknik['hacim']}\n"
            f"Teknik Sinyal: {teknik['sinyal']} (Skor: {teknik['skor']}/10)\n"
            f"Yorumlar: {', '.join(teknik['yorumlar'])}"
        )

        prompt = f"""
{sembol} hissesi için kısa yatırım analizi yap.

TEKNİK VERİLER:
{teknik_metni}

SON KAP BİLDİRİMLERİ:
{haber_metni}

Lütfen şunları söyle:
1. Genel değerlendirme (2-3 cümle)
2. Risk faktörleri
3. Net karar: AL / SAT / BEKLE ve kısa gerekçe

Türkçe, kısa ve net yaz. BIST'in spekülatif yapısını göz önünde bulundur.
"""
        mesaj = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        return mesaj.content[0].text
    except Exception as e:
        log.warning(f"Claude analiz hatası ({sembol}): {e}")
        return f"AI analiz yapılamadı: {e}"


# ─────────────────────────────────────────
# 📨  MESAJ FORMATLAMA
# ─────────────────────────────────────────

def sinyal_emoji(sinyal: str) -> str:
    return {"AL": "🟢", "SAT": "🔴", "BEKLE": "🟡"}.get(sinyal, "⚪")


def hisse_rapor_olustur(sembol: str, claude_yorum: bool = True) -> str:
    """Tek hisse için tam rapor üretir."""
    teknik = teknik_analiz(sembol)
    if not teknik:
        return f"⚠️ {sembol}: veri alınamadı"

    haberler = kap_haberleri_getir(sembol)
    emoji = sinyal_emoji(teknik["sinyal"])

    rapor = (
        f"{emoji} *{sembol}* — {teknik['sinyal']}\n"
        f"💰 {teknik['fiyat']} ₺ ({teknik['degisim']:+.2f}%)\n"
        f"📊 RSI: {teknik['rsi']} | MACD: {'↑' if teknik['macd'] > teknik['macd_sig'] else '↓'}\n"
        f"📈 MA20: {teknik['ma20']} | MA50: {teknik['ma50']}\n"
        f"🎯 Destek: {teknik['destek']} | Direnç: {teknik['direnc']}\n"
    )

    if haberler:
        rapor += "📰 *Son KAP:*\n" + "\n".join(f"• {h}" for h in haberler) + "\n"

    if claude_yorum:
        yorum = claude_analiz(sembol, teknik, haberler)
        rapor += f"\n🤖 *AI Yorum:*\n{yorum}"

    return rapor


# ─────────────────────────────────────────
# ⏰  OTOMATİK GÖREVLER
# ─────────────────────────────────────────

async def sabah_raporu(app: Application):
    """Her sabah 09:30'da BIST 30 özet raporu gönderir."""
    simdi = datetime.now(TZ).strftime("%d.%m.%Y %H:%M")
    log.info("Sabah raporu hazırlanıyor...")

    baslik = f"🌅 *BIST 30 Sabah Raporu — {simdi}*\n\n"

    # Tüm hisseleri analiz et, AL sinyali verenleri öne al
    al_sinyaller   = []
    sat_sinyaller  = []
    bekle_sinyaller = []

    for sembol in BIST30:
        teknik = teknik_analiz(sembol)
        if not teknik:
            continue
        satir = f"{sinyal_emoji(teknik['sinyal'])} *{sembol}* {teknik['fiyat']} ₺ ({teknik['degisim']:+.2f}%) — {teknik['sinyal']}"
        if teknik["sinyal"] == "AL":
            al_sinyaller.append((teknik["skor"], satir, sembol, teknik))
        elif teknik["sinyal"] == "SAT":
            sat_sinyaller.append(satir)
        else:
            bekle_sinyaller.append(satir)

    # Skora göre sırala
    al_sinyaller.sort(reverse=True)

    mesaj = baslik

    if al_sinyaller:
        mesaj += "🟢 *AL Sinyalleri:*\n"
        for _, satir, _, _ in al_sinyaller:
            mesaj += satir + "\n"
        mesaj += "\n"

    if sat_sinyaller:
        mesaj += "🔴 *SAT Sinyalleri:*\n"
        mesaj += "\n".join(sat_sinyaller) + "\n\n"

    if bekle_sinyaller:
        mesaj += "🟡 *Bekle:*\n"
        mesaj += "\n".join(bekle_sinyaller) + "\n\n"

    mesaj += "⚠️ _Bu sinyaller bilgi amaçlıdır, yatırım tavsiyesi değildir._"

    # Uzunsa böl
    for parca in [mesaj[i:i+4000] for i in range(0, len(mesaj), 4000)]:
        try:
            await app.bot.send_message(chat_id=CHAT_ID, text=parca, parse_mode="Markdown")
        except Exception as e:
            log.error(f"Sabah raporu gönderilemedi: {e}")

    # En iyi AL sinyali için detaylı Claude analizi gönder
    if al_sinyaller:
        _, _, en_iyi_sembol, en_iyi_teknik = al_sinyaller[0]
        haberler = kap_haberleri_getir(en_iyi_sembol)
        yorum = claude_analiz(en_iyi_sembol, en_iyi_teknik, haberler)
        detay = f"🔍 *En Güçlü Sinyal: {en_iyi_sembol}*\n\n{yorum}"
        try:
            await app.bot.send_message(chat_id=CHAT_ID, text=detay, parse_mode="Markdown")
        except Exception as e:
            log.error(f"Detay mesajı gönderilemedi: {e}")


async def stop_loss_kontrol(app: Application):
    """Aktif pozisyonlarda %2 zarar kontrolü yapar."""
    if not pozisyonlar:
        return
    for sembol, poz in list(pozisyonlar.items()):
        try:
            ticker = yf.Ticker(f"{sembol}.IS")
            fiyat = ticker.fast_info.last_price
            if not fiyat:
                continue
            giris = poz["giris"]
            degisim_pct = (fiyat - giris) / giris * 100
            if degisim_pct <= -2.0:
                mesaj = (
                    f"🚨 *STOP-LOSS UYARISI!*\n"
                    f"*{sembol}* pozisyonun -%2 seviyesine ulaştı!\n"
                    f"Giriş: {giris} ₺ | Şu an: {fiyat:.2f} ₺\n"
                    f"Değişim: {degisim_pct:.2f}%\n"
                    f"⛔ POZİSYONU KAPAT!"
                )
                await app.bot.send_message(chat_id=CHAT_ID, text=mesaj, parse_mode="Markdown")
                log.warning(f"Stop-loss tetiklendi: {sembol}")
        except Exception as e:
            log.warning(f"Stop-loss kontrol hatası ({sembol}): {e}")


async def kap_anlık_takip(app: Application):
    """Her 5 dakikada KAP'ı kontrol eder, BIST30 hisselerine ait haberleri bildirir."""
    try:
        bildirimler = kap_son_bildirimler(limit=20)
        for b in bildirimler:
            kod = b.get("memberCode", "") or b.get("member", {}).get("memberCode", "")
            if kod in BIST30:
                baslik = b.get("subject", "")
                tarih  = b.get("publishDate", "")[:16]
                mesaj = (
                    f"📢 *KAP BİLDİRİMİ — {kod}*\n"
                    f"📌 {baslik}\n"
                    f"🕐 {tarih}"
                )
                try:
                    await app.bot.send_message(chat_id=CHAT_ID, text=mesaj, parse_mode="Markdown")
                except Exception as e:
                    log.error(f"KAP bildirimi gönderilemedi: {e}")
    except Exception as e:
        log.warning(f"KAP takip hatası: {e}")


# ─────────────────────────────────────────
# 🤖  TELEGRAM KOMUTLARI
# ─────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mesaj = (
        "👋 *BIST 30 Sinyal Botu*\n\n"
        "/sinyal GARAN — Tek hisse detaylı analiz\n"
        "/rapor — Tüm BIST30 özet raporu\n"
        "/poz GARAN 130.5 — Pozisyon aç (stop-loss takibi)\n"
        "/pozlar — Açık pozisyonlar\n"
        "/pozkapat GARAN — Pozisyonu kapat\n"
        "/yardim — Bu menü\n\n"
        "🤖 Her sabah 09:30'da otomatik rapor gelir.\n"
        "⛔ %2 zarar görünce anında uyarı alırsın."
    )
    await update.message.reply_text(mesaj, parse_mode="Markdown")


async def cmd_sinyal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Kullanım: /sinyal GARAN")
        return
    sembol = ctx.args[0].upper()
    if sembol not in BIST30:
        await update.message.reply_text(f"⚠️ {sembol} BIST30 listesinde değil. Yine de analiz ediyorum...")
    await update.message.reply_text(f"⏳ {sembol} analiz ediliyor...")
    rapor = hisse_rapor_olustur(sembol, claude_yorum=True)
    await update.message.reply_text(rapor, parse_mode="Markdown")


async def cmd_rapor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ BIST30 analiz ediliyor, biraz bekle...")
    app = ctx.application
    await sabah_raporu(app)


async def cmd_poz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await update.message.reply_text("Kullanım: /poz GARAN 130.5")
        return
    sembol = ctx.args[0].upper()
    try:
        giris = float(ctx.args[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("❌ Geçersiz fiyat.")
        return
    pozisyonlar[sembol] = {"giris": giris, "tarih": datetime.now(TZ).strftime("%d.%m.%Y %H:%M")}
    stop = round(giris * 0.98, 2)
    await update.message.reply_text(
        f"✅ *{sembol}* pozisyon açıldı\n"
        f"Giriş: {giris} ₺\n"
        f"Stop-Loss: {stop} ₺ (-%2)\n"
        f"Her dakika kontrol edilecek.",
        parse_mode="Markdown"
    )


async def cmd_pozlar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not pozisyonlar:
        await update.message.reply_text("📭 Açık pozisyon yok.")
        return
    satirlar = ["📋 *Açık Pozisyonlar*\n"]
    for sembol, poz in pozisyonlar.items():
        try:
            fiyat = yf.Ticker(f"{sembol}.IS").fast_info.last_price
            degisim = (fiyat - poz["giris"]) / poz["giris"] * 100
            emoji = "🟢" if degisim >= 0 else "🔴"
            satirlar.append(
                f"{emoji} *{sembol}* | Giriş: {poz['giris']} ₺ | "
                f"Şu an: {fiyat:.2f} ₺ | {degisim:+.2f}%"
            )
        except:
            satirlar.append(f"⚠️ *{sembol}* | Giriş: {poz['giris']} ₺ | Fiyat alınamadı")
    await update.message.reply_text("\n".join(satirlar), parse_mode="Markdown")


async def cmd_pozKapat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Kullanım: /pozKapat GARAN")
        return
    sembol = ctx.args[0].upper()
    if sembol in pozisyonlar:
        poz = pozisyonlar.pop(sembol)
        await update.message.reply_text(f"✅ {sembol} pozisyonu kapatıldı. Giriş fiyatı: {poz['giris']} ₺")
    else:
        await update.message.reply_text(f"ℹ️ {sembol} için açık pozisyon yok.")


async def cmd_yardim(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


# ─────────────────────────────────────────
# 🚀  ANA FONKSİYON
# ─────────────────────────────────────────

def main():
    if "BURAYA" in BOT_TOKEN:
        print("❌ BOT_TOKEN, CHAT_ID ve CLAUDE_API_KEY değerlerini doldurun!")
        return

    log.info("BIST 30 Sinyal Botu başlatılıyor...")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("sinyal",   cmd_sinyal))
    app.add_handler(CommandHandler("rapor",    cmd_rapor))
    app.add_handler(CommandHandler("poz",      cmd_poz))
    app.add_handler(CommandHandler("pozlar",   cmd_pozlar))
    app.add_handler(CommandHandler("pozKapat", cmd_pozKapat))
    app.add_handler(CommandHandler("yardim",   cmd_yardim))

    jq = app.job_queue

    # Sabah 09:30 raporu (hafta içi)
    jq.run_daily(
        lambda ctx: asyncio.create_task(sabah_raporu(app)),
        time=dtime(9, 30, tzinfo=TZ),
        days=(0, 1, 2, 3, 4)
    )

    # Her dakika stop-loss kontrolü
    jq.run_repeating(
        lambda ctx: asyncio.create_task(stop_loss_kontrol(app)),
        interval=60, first=60
    )

    # Her 5 dakikada KAP bildirimi takibi
    jq.run_repeating(
        lambda ctx: asyncio.create_task(kap_anlık_takip(app)),
        interval=300, first=60
    )

    log.info(f"BIST30 takip listesi: {len(BIST30)} hisse")
    log.info("Bot aktif! Telegram'dan /start ile başla.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
