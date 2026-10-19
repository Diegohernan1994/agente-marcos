import os
import sys
import json
import time
import re
import base64
import shutil
import asyncio
import threading
import subprocess
import urllib.request
import urllib.parse
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from PIL import Image, ImageDraw, ImageFont
import speech_recognition as sr
import edge_tts

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)
STATE_FILE = os.path.join(BASE_DIR, "state.json")
PUBLIC_DIR = os.path.join(BASE_DIR, "public")
REELS_DIR = os.path.join(BASE_DIR, "reels")
IMAGES_DIR = os.path.join(BASE_DIR, "images")
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads_reales")
VOICES_DIR = os.path.join(BASE_DIR, "voices")
TEMP_DIR = os.path.join(BASE_DIR, "temp_build")

os.makedirs(PUBLIC_DIR, exist_ok=True)
os.makedirs(REELS_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(VOICES_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# Copiar imágenes de catálogo iniciales a uploads_reales si está vacío
src_imgs = os.path.join(PARENT_DIR, "public", "images")
if os.path.exists(src_imgs) and not os.listdir(UPLOADS_DIR):
    for f in os.listdir(src_imgs):
        if f.lower().endswith((".jpg", ".jpeg", ".png")):
            shutil.copy2(os.path.join(src_imgs, f), os.path.join(UPLOADS_DIR, f))

src_reels = os.path.join(PARENT_DIR, "reels_publicidad")
reels_init_flag = os.path.join(REELS_DIR, ".initialized")
if os.path.exists(src_reels) and not os.path.exists(reels_init_flag):
    for f in os.listdir(src_reels):
        if f.lower().endswith(".mp4"):
            dest_f = os.path.join(REELS_DIR, f)
            if not os.path.exists(dest_f):
                shutil.copy2(os.path.join(src_reels, f), dest_f)
    with open(reels_init_flag, "w") as flag_f:
        flag_f.write("1")

state_lock = threading.Lock()
telegram_status = {"running": False, "botUsername": None, "lastError": None}

def load_state():
    with state_lock:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

def save_state(data):
    with state_lock:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

# ==============================================================================
# CÁLCULOS FINANCIEROS, DEVOLUCIONES Y CPA MÁXIMO DE META ADS
# ==============================================================================
def calculate_product_metrics(product, biz):
    price = float(product.get("price", 0))
    cost = float(product.get("cost", 0))
    pkg = float(product.get("packagingCost", 500))
    disc_pct = float(biz.get("transferDiscountPercent", 15))
    ret_pct = float(biz.get("returnReservePercent", 2.0))

    transfer_price = round(price * (1 - disc_pct / 100.0))
    return_reserve = round(price * (ret_pct / 100.0))
    total_direct_cost = cost + pkg + return_reserve

    net_margin_regular = price - total_direct_cost
    net_margin_transfer = transfer_price - total_direct_cost

    cpa_max = max(0, round(net_margin_transfer * 0.85))
    cpa_ideal = max(0, round(net_margin_transfer * 0.30))

    return {
        "id": product["id"],
        "name": product["name"],
        "price": int(price),
        "transferPrice": int(transfer_price),
        "cost": int(cost),
        "packagingCost": int(pkg),
        "returnReserve": int(return_reserve),
        "netMarginRegular": int(net_margin_regular),
        "netMarginTransfer": int(net_margin_transfer),
        "cpaIdeal": int(cpa_ideal),
        "cpaMax": int(cpa_max)
    }

def compute_store_analytics(state):
    sa = state.get("storeAnalytics", {})
    page_views = int(sa.get("pageViews", 148))
    unique_visitors = int(sa.get("uniqueVisitors", 112))
    total_clicks = int(sa.get("totalClicks", 684))
    sessions_dur = max(1, int(sa.get("totalSessionsForDuration", unique_visitors or 1)))
    total_dur = int(sa.get("totalDurationSeconds", 10752))
    avg_dur_sec = round(total_dur / sessions_dur)
    mins = avg_dur_sec // 60
    secs = avg_dur_sec % 60
    avg_dur_str = f"{mins}m {secs:02d}s" if mins > 0 else f"{secs}s"

    add_to_cart = int(sa.get("addToCartCount", 39))
    wa_clicks = int(sa.get("whatsappCheckoutClicks", 18))
    abandoned_count = int(sa.get("abandonedCartsCount", 21))
    abandoned_val = int(sa.get("abandonedCartsValue", 1084500))

    avg_clicks = round(total_clicks / max(1, unique_visitors), 1)
    conv_rate = round((wa_clicks / max(1, unique_visitors)) * 100, 1)
    abandon_rate = round((abandoned_count / max(1, add_to_cart)) * 100, 1)

    pstats = sa.get("productStats", {})
    top_products = []
    for pid, pdata in pstats.items():
        top_products.append({
            "id": pid,
            "name": pdata.get("name", pid),
            "views": int(pdata.get("views", 0)),
            "addedToCart": int(pdata.get("addedToCart", 0)),
            "whatsappOrders": int(pdata.get("whatsappOrders", 0)),
            "abandoned": int(pdata.get("abandoned", 0))
        })
    top_products.sort(key=lambda x: x["views"], reverse=True)

    ad_stats_map = sa.get("adStats", {})
    top_ads = []
    for akey, adata in ad_stats_map.items():
        a_views = int(adata.get("views", 0))
        a_sessions = max(1, int(adata.get("sessions", a_views or 1)))
        a_dur_total = int(adata.get("totalDurationSeconds", 0))
        a_avg_sec = round(a_dur_total / a_sessions) if a_views > 0 else 0
        a_min = a_avg_sec // 60
        a_sec = a_avg_sec % 60
        a_dur_str = f"{a_min}m {a_sec:02d}s" if a_min > 0 else f"{a_sec}s"
        a_orders = int(adata.get("whatsappOrders", 0))
        a_conv = round((a_orders / max(1, a_views)) * 100, 1) if a_views > 0 else 0.0
        top_ads.append({
            "id": akey,
            "adName": adata.get("adName", akey),
            "source": adata.get("source", "Meta Ads"),
            "views": a_views,
            "clicks": int(adata.get("clicks", 0)),
            "avgDurationFormatted": a_dur_str,
            "addedToCart": int(adata.get("addedToCart", 0)),
            "whatsappOrders": a_orders,
            "abandoned": int(adata.get("abandoned", 0)),
            "conversionRate": a_conv,
            "revenue": int(adata.get("revenue", 0))
        })
    top_ads.sort(key=lambda x: (x["whatsappOrders"], x["views"]), reverse=True)

    recent_ev = sa.get("recentEvents", [])[:12]

    return {
        "pageViews": page_views,
        "uniqueVisitors": unique_visitors,
        "totalClicks": total_clicks,
        "avgClicksPerVisitor": avg_clicks,
        "avgDurationSeconds": avg_dur_sec,
        "avgDurationFormatted": avg_dur_str,
        "addToCartCount": add_to_cart,
        "addToCartClicks": add_to_cart,
        "whatsappCheckoutClicks": wa_clicks,
        "abandonedCartsCount": abandoned_count,
        "abandonedCartsValue": abandoned_val,
        "conversionRatePct": conv_rate,
        "conversionRate": conv_rate,
        "abandonmentRatePct": abandon_rate,
        "abandonmentRate": abandon_rate,
        "topProducts": top_products,
        "topAds": top_ads,
        "recentEvents": recent_ev,
        "recentSessions": recent_ev
    }

# ==============================================================================
# MOTOR DE EDICIÓN REAL CON FFMPEG (CLIPS DE VIDEO Y FOTOS REALES DEL CELULAR)
# 0% IA GENERATIVA DE IMAGEN: RESPETA 100% EL PRODUCTO Y LA TEXTURA ORIGINAL
# ==============================================================================
def get_font(size, bold=True):
    candidates = [
        "arialbd.ttf" if bold else "arial.ttf",
        "seguiemb.ttf" if bold else "segoeui.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "arial.ttf"
    ]
    for c in candidates:
        try:
            return ImageFont.truetype(c, size)
        except Exception:
            continue
    return ImageFont.load_default()

def create_overlay_frame_rgba(badge, headline, subline, cta_text, out_png):
    """
    Crea un marco transparente (RGBA 1080x1920) en tonos pastel cálidos (crema, salvia suave y grafito, cero flúor/neón),
    dejando una ventana central 100% transparente (x=60..1020, y=350..1270) donde se ve
    el clip de video real o la foto real del celular SIN TOCAR NI ALTERAR LA TEXTURA DEL PRODUCTO.
    """
    w, h = 1080, 1920
    im = Image.new("RGBA", (w, h), (246, 243, 236, 255))
    draw = ImageDraw.Draw(im)

    for y in range(h):
        r = int(246 - (246 - 237) * (y / h))
        g = int(243 - (243 - 233) * (y / h))
        b = int(236 - (236 - 224) * (y / h))
        draw.line([(0, y), (w, y)], fill=(r, g, b, 255))

    # Ventana transparente para el video/foto original (960x920)
    vx, vy, vw, vh = 60, 350, 960, 920
    draw.rectangle([vx, vy, vx + vw, vy + vh], fill=(0, 0, 0, 0))
    draw.rounded_rectangle([vx - 2, vy - 2, vx + vw + 2, vy + vh + 2], radius=16, outline=(196, 189, 176, 255), width=4)

    font_brand = get_font(30, True)
    draw.text((60, 75), "ALMOHADAS VISCOELÁSTICAS CÓRDOBA", fill=(92, 122, 103, 255), font=font_brand)

    if badge:
        font_badge = get_font(26, True)
        draw.rounded_rectangle([60, 135, 580, 198], radius=12, fill=(214, 229, 218, 255))
        draw.text((80, 150), badge[:26].upper(), fill=(47, 72, 56, 255), font=font_badge)

    font_head = get_font(40, True)
    draw.text((60, 228), headline[:34].upper(), fill=(44, 48, 46, 255), font=font_head)

    font_sub = get_font(33, True)
    draw.text((60, 1315), subline, fill=(92, 74, 62, 255), font=font_sub)

    if cta_text:
        py = 1580
        draw.rounded_rectangle([70, py, 1010, py + 115], radius=22, fill=(116, 148, 127, 255))
        font_cta = get_font(32, True)
        draw.text((100, py + 36), cta_text[:32].upper(), fill=(255, 255, 255, 255), font=font_cta)

    font_foot = get_font(24, True)
    draw.text((70, 1750), "100% Producto Real • Entregas en Córdoba • tienda-marcos.pages.dev", fill=(115, 119, 116, 255), font=font_foot)
    im.save(out_png)

def render_segment_from_real_media(media_path, badge, headline, subline, cta_text, out_seg_mp4, duration=5):
    """
    Toma un clip de video real (.mp4/.mov/.webm) o una foto real (.jpg/.png/.webp) subida por el usuario,
    y genera un segmento MP4 1080x1920 a 30fps sin voz, respetando el 100% de la imagen/video original.
    """
    ext = os.path.splitext(media_path)[1].lower()
    overlay_png = out_seg_mp4.replace(".mp4", "_overlay.png")
    create_overlay_frame_rgba(badge, headline, subline, cta_text, overlay_png)

    is_video = ext in (".mp4", ".mov", ".webm", ".avi", ".mkv")

    if is_video:
        # Escalar y encuadrar el clip de video real en la ventana de 960x920 (x=60, y=350) sin deformar proporción
        filter_complex = (
            "[0:v]scale=960:920:force_original_aspect_ratio=increase,crop=960:920,setsar=1,fps=30[vid];"
            "color=c=#F6F3EC:s=1080x1920:r=30[bg];"
            "[bg][vid]overlay=60:350:shortest=1[tmp];"
            "[tmp][1:v]overlay=0:0"
        )
        cmd = [
            "ffmpeg", "-y",
            "-i", media_path,
            "-i", overlay_png,
            "-t", str(duration),
            "-filter_complex", filter_complex,
            "-an",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
            out_seg_mp4
        ]
    else:
        # Foto real del celular/cámara: encuadrar intacta en 960x920 sin alterar texturas ni aplicar filtros IA
        slide_jpg = out_seg_mp4.replace(".mp4", "_static.jpg")
        with Image.open(overlay_png) as ov_im:
            base_im = Image.new("RGB", (1080, 1920), (255, 255, 255))
            with Image.open(media_path) as pim:
                pim = pim.convert("RGB")
                cw, ch = 960, 920
                scale = max(cw / pim.width, ch / pim.height)
                sw, sh = int(pim.width * scale), int(pim.height * scale)
                resized = pim.resize((sw, sh), Image.Resampling.LANCZOS)
                ox, oy = (sw - cw) // 2, (sh - ch) // 2
                cropped = resized.crop((ox, oy, ox + cw, oy + ch))
                base_im.paste(cropped, (60, 350))
            base_im.paste(ov_im, (0, 0), ov_im)
            base_im.save(slide_jpg, quality=95)

        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-t", str(duration), "-i", slide_jpg,
            "-an",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
            out_seg_mp4
        ]

    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def list_uploaded_media():
    valid_exts = (".mp4", ".mov", ".webm", ".jpg", ".jpeg", ".png", ".webp")
    items = []
    if os.path.exists(UPLOADS_DIR):
        for fname in sorted(os.listdir(UPLOADS_DIR), key=lambda x: os.path.getmtime(os.path.join(UPLOADS_DIR, x)), reverse=True):
            if fname.lower().endswith(valid_exts):
                ext = os.path.splitext(fname)[1].lower()
                mtype = "video" if ext in (".mp4", ".mov", ".webm") else "image"
                items.append({"name": fname, "type": mtype, "url": f"/uploads/{fname}"})
    return items

def generate_visual_reel(angle="cervical", custom_title=None, selected_files=None):
    state = load_state()
    biz = state.get("business", {})
    products = state.get("products", [])
    p_pack_cerv = next((p for p in products if p["id"] == "pack-2-cervicales"), products[0])
    p_cerv = next((p for p in products if p["id"] == "cervical-individual"), products[0])

    disc = biz.get("transferDiscountPercent", 15)
    pack_transf = round(p_pack_cerv["price"] * (1 - disc / 100.0))
    cerv_transf = round(p_cerv["price"] * (1 - disc / 100.0))

    # Obtener los archivos reales subidos por el usuario (clips de video o fotos reales)
    uploaded = list_uploaded_media()
    if selected_files and len(selected_files) > 0:
        media_paths = [os.path.join(UPLOADS_DIR, f) for f in selected_files if os.path.exists(os.path.join(UPLOADS_DIR, f))]
    else:
        # Usar los últimos 3 archivos subidos (priorizando clips de video/fotos reales recién subidas)
        media_paths = [os.path.join(UPLOADS_DIR, item["name"]) for item in uploaded[:3]]

    if not media_paths:
        raise RuntimeError("No hay fotos ni clips cargados en la galería de archivos reales.")

    # Si subió 1 o 2 archivos, repetirlos para cubrir las 3 escenas del Reel
    while len(media_paths) < 3:
        media_paths.append(media_paths[-1])

    ts = datetime.now().strftime("%H%M%S")
    angle_key = angle.lower()

    if "pack" in angle_key or "oferta" in angle_key or "2" in angle_key:
        reel_name = f"REEL_REAL_PACK2_{ts}.mp4"
        scenes_text = [
            ("PROMO PACK X2", custom_title or "PACK X2 VISCOELÁSTICAS", f"PRECIO LISTA: ${p_pack_cerv['price']:,}\nCON TRANSFERENCIA: ${pack_transf:,}".replace(",", "."), f"{disc}% OFF EXTRA TRANSFERENCIA"),
            ("CALIDAD REAL", "ESPUMA CON MEMORIA REAL", "2 ALMOHADAS DE 60x40 CM\nCON FUNDA CON CIERRE LAVABLE", "NO SE DEFORMA CON EL USO"),
            ("ENVÍOS EN CÓRDOBA", "RECIBILO HOY EN TU CASA", f"DENTRO DE CIRCUNVALACIÓN ${biz.get('shippingInsideRing', 3000):,}\nENVÍO GRATIS +$100.000".replace(",", "."), "PEDÍ DIRECTO POR WHATSAPP")
        ]
    elif "rebote" in angle_key or "memoria" in angle_key or "demo" in angle_key:
        reel_name = f"REEL_REAL_REBOTE_{ts}.mp4"
        scenes_text = [
            ("PRODUCTO 100% REAL", custom_title or "MIRÁ LA DENSIDAD REAL", "ESPUMA VISCOELÁSTICA TERMOSENSIBLE\nRECUPERA SU FORMA AL INSTANTE", "SIN RELLENOS QUE SE APLASTAN"),
            ("DETALLES DE CALIDAD", "FUNDA SUAVE CON CIERRE", f"DESDE ${cerv_transf:,} ABONANDO\nCON {disc}% OFF TRANSFERENCIA".replace(",", "."), "DESMONTABLE Y LAVABLE"),
            ("CÓRDOBA CAPITAL", "ENTREGA RÁPIDA EN EL DÍA", "VERIFICÁS LA CALIDAD AL RECIBIR\nATENCIÓN DIRECTA EN WHATSAPP", "TOCÁ EL LINK DEL CATÁLOGO")
        ]
    else:
        reel_name = f"REEL_REAL_CERVICAL_{ts}.mp4"
        scenes_text = [
            ("ALIVIO CERVICAL", custom_title or "¿DOLOR DE CUELLO AL DORMIR?", "DISEÑO ANATÓMICO DE DOBLE ALTURA\nALINEA CUELLO Y COLUMNA", "ESPUMA CON MEMORIA REAL"),
            ("PRECIO DIRECTO", "INDIVIDUAL O PACK X2 AHORRO", f"INDIVIDUAL: ${cerv_transf:,} C/ TRANSF.\nPACK X2: ${pack_transf:,} C/ TRANSF.".replace(",", "."), f"{disc}% OFF POR TRANSFERENCIA"),
            ("CÓRDOBA CAPITAL", "ENVÍOS RÁPIDOS A DOMICILIO", "DENTRO DE CIRCUNVALACIÓN $3.000\nFUERA DE CIRCUNVALACIÓN $5.000", "PEDÍ LA TUYA POR WHATSAPP")
        ]

    max_clips = min(len(media_paths), 8)
    seg_dur = 4 if max_clips > 3 else 5
    seg_files = []
    for idx in range(max_clips):
        mpath = media_paths[idx]
        b_txt, h_txt, s_txt, c_txt = scenes_text[idx % len(scenes_text)]
        seg_mp4 = os.path.join(TEMP_DIR, f"seg_{ts}_{idx}.mp4")
        render_segment_from_real_media(mpath, b_txt, h_txt, s_txt, c_txt, seg_mp4, duration=seg_dur)
        seg_files.append(seg_mp4)

    concat_txt = os.path.join(TEMP_DIR, f"concat_segs_{ts}.txt")
    with open(concat_txt, "w", encoding="utf-8") as f:
        for sf in seg_files:
            f.write(f"file '{sf}'\n")

    out_mp4 = os.path.join(REELS_DIR, reel_name)
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", concat_txt,
        "-an",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
        out_mp4
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return reel_name

# ==============================================================================
# MOTOR DE VOZ DEL AGENTE: ESCUCHAR AUDIOS (STT) Y RESPONDER CON VOZ (TTS)
# (Los Reels MP4 siguen 100% sin voz; esto es solo para hablar con el Agente)
# ==============================================================================
def clean_text_for_speech(text):
    """Limpia markdown, emojis y símbolos para que la nota de voz en español argentino suene 100% natural."""
    s = re.sub(r"`[^`]+`", "", text)
    s = s.replace("**", "").replace("*", "").replace(">", "")
    s = s.replace("tienda-marcos.pages.dev", "tu tienda online")
    s = re.sub(r"\$(\d{1,3}(?:\.\d{3})+|\d+)", r"\1 pesos", s)
    s = s.replace("CPA", "costo por compra").replace("CPC", "costo por clic")
    s = s.replace("15% OFF", "15 por ciento de descuento").replace("Transf.", "con transferencia")
    s = re.sub(r"[^\w\s,.:;¿?¡!%+-áéíóúÁÉÍÓÚñÑüÜ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > 700:
        s = s[:700].rsplit(" ", 1)[0] + ". Te dejé el detalle completo por escrito en el chat."
    return s

def generate_agent_voice_note(reply_text):
    """
    Genera una nota de voz en español argentino (es-AR-TomasNeural) tanto en .mp3 (para el panel web)
    como en .ogg Opus (para enviar como Nota de Voz nativa en Telegram con sendVoice).
    """
    spoken = clean_text_for_speech(reply_text)
    if not spoken:
        return None, None
    ts = datetime.now().strftime("%H%M%S_%f")[:10]
    mp3_name = f"voz_agente_{ts}.mp3"
    ogg_name = f"voz_agente_{ts}.ogg"
    mp3_path = os.path.join(VOICES_DIR, mp3_name)
    ogg_path = os.path.join(VOICES_DIR, ogg_name)

    async def _tts():
        comm = edge_tts.Communicate(spoken, "es-AR-TomasNeural", rate="+6%")
        await comm.save(mp3_path)

    asyncio.run(_tts())
    subprocess.run(
        ["ffmpeg", "-y", "-i", mp3_path, "-c:a", "libopus", "-b:a", "48k", "-vbr", "on", ogg_path],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    return mp3_name, ogg_path

def transcribe_voice_file(audio_path):
    """
    Convierte un audio entrante de Telegram (.ogg/.oga) o del navegador (.webm/.wav/.mp3)
    a WAV 16kHz mono y lo transcribe a texto en español argentino (es-AR).
    """
    wav_path = audio_path + "_16k.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", audio_path, "-ar", "16000", "-ac", "1", wav_path],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    recognizer = sr.Recognizer()
    with sr.AudioFile(wav_path) as src_audio:
        audio_data = recognizer.record(src_audio)
    try:
        os.remove(wav_path)
    except Exception:
        pass
    return recognizer.recognize_google(audio_data, language="es-AR")

# ==============================================================================
# CEREBRO DEL AGENTE: PROCESA ÓRDENES DESDE EL PANEL WEB O TELEGRAM
# ==============================================================================
def execute_agent_instruction(user_text, source="panel", selected_files=None, generate_voice=False, is_voice_input=False):
    state = load_state()
    biz = state.get("business", {})
    products = state.get("products", [])
    text_lower = user_text.lower().strip()
    generated_reel = None

    # 1. INTENCIÓN: CREAR UN REEL CON LOS CLIPS / FOTOS REALES SUBIDOS
    if any(k in text_lower for k in ["reel", "video", "crear video", "armame", "creame", "/reel", "unir"]):
        if "pack" in text_lower or "oferta" in text_lower or "2" in text_lower:
            angle = "pack"
            angle_desc = "Oferta Pack x2 Ahorro"
        elif "rebote" in text_lower or "memoria" in text_lower or "demo" in text_lower:
            angle = "rebote"
            angle_desc = "Demostración Real de Espuma y Rebote"
        else:
            angle = "cervical"
            angle_desc = "Alivio Cervical y Postura"

        try:
            generated_reel = generate_visual_reel(angle=angle, selected_files=selected_files)
            reply = f"🎬 Listo Marcos, acá tenés tu Reel (**{angle_desc}**):"
        except Exception as e:
            reply = f"⚠️ Ocurrió un error al ensamblar el Reel con tus archivos: {e}"

    # 2. INTENCIÓN: CAMBIAR / ACTUALIZAR PRECIO O COSTO DE UN PRODUCTO
    elif any(k in text_lower for k in ["cambia el precio", "cambiar precio", "subi el", "subí el", "actualizar precio", "pone el", "poné el", "/precio"]):
        numbers = [int(n.replace(".", "")) for n in re.findall(r"\$?\s*(\d{2,3}(?:\.\d{3})|\d{4,6})", user_text)]
        target_prod = None
        for p in products:
            if p["id"].replace("-", " ") in text_lower or p["name"].lower() in text_lower:
                target_prod = p
                break
        if not target_prod:
            if "pack" in text_lower and "cervical" in text_lower:
                target_prod = next((p for p in products if p["id"] == "pack-2-cervicales"), None)
            elif "pack" in text_lower and ("clasica" in text_lower or "clásica" in text_lower):
                target_prod = next((p for p in products if p["id"] == "pack-2-clasicas"), None)
            elif "cervical" in text_lower:
                target_prod = next((p for p in products if p["id"] == "cervical-individual"), None)
            elif "gel" in text_lower:
                target_prod = next((p for p in products if p["id"] == "gel-fresh"), None)
            elif "funda" in text_lower:
                target_prod = next((p for p in products if p["id"] == "funda-repuesto"), None)
            elif "clasica" in text_lower or "clásica" in text_lower:
                target_prod = next((p for p in products if p["id"] == "clasica-individual"), None)

        if target_prod and numbers:
            new_val = numbers[-1]
            if "costo" in text_lower:
                old_val = target_prod["cost"]
                target_prod["cost"] = new_val
                save_state(state)
                m = calculate_product_metrics(target_prod, biz)
                reply = (
                    f"✅ **Costo actualizado para {target_prod['name']}**\n"
                    f"• Costo anterior: ${old_val:,} ➔ **Nuevo costo: ${new_val:,}**\n"
                    f"• **Ganancia limpia (c/ transf.):** ${m['netMarginTransfer']:,} | **CPA Máx Ads:** ${m['cpaMax']:,}"
                ).replace(",", ".")
            else:
                old_val = target_prod["price"]
                target_prod["price"] = new_val
                save_state(state)
                m = calculate_product_metrics(target_prod, biz)
                reply = (
                    f"✅ **Precio actualizado para {target_prod['name']}**\n"
                    f"• Anterior: ${old_val:,} ➔ **Nuevo: ${new_val:,}** (**${m['transferPrice']:,}** c/ 15% Transf.)\n"
                    f"• **Ganancia limpia:** ${m['netMarginTransfer']:,} | **CPA Máx Ads:** ${m['cpaMax']:,}"
                ).replace(",", ".")
        else:
            reply = (
                "Decime el producto y el monto. Por ejemplo: *\"Cambiá el precio del Pack x2 Cervicales a 68000\"*"
            )

    # 3. INTENCIÓN: CALCULAR COSTOS, DEVOLUCIONES Y CPA MÁXIMO
    elif any(k in text_lower for k in ["costo", "margen", "ganancia", "cpa", "devolucion", "devolución", "rentabilidad", "/costos"]):
        lines = [
            "📊 **Resumen de Costos, Ganancia Limpia y CPA Máximo:**\n"
        ]
        for p in products:
            m = calculate_product_metrics(p, biz)
            lines.append(
                f"🔹 **{m['name']}**: Venta **${m['price']:,}** (${m['transferPrice']:,} Transf.) • Ganancia Limpia: **+${m['netMarginTransfer']:,}** • CPA Máx: **${m['cpaMax']:,}**"
            )
        reply = "\n".join(lines).replace(",", ".")

    # 4. INTENCIÓN: ANALÍTICA DE LA TIENDA WHATSAPP (VISITAS, ORIGEN DE ANUNCIOS, CARRITOS ABANDONADOS, CLICS, PERMANENCIA, PRODUCTOS MÁS VISTOS)
    elif any(k in text_lower for k in ["analitica", "analítica", "visitas", "visualizaciones", "carrito", "abandonado", "permanencia", "mas vistos", "más vistos", "estadistica", "estadística", "origen", "anuncio especifico", "anuncio específico", "que anuncio", "qué anuncio", "/analitica"]):
        sa = compute_store_analytics(state)
        ad_lines = []
        for idx, ad in enumerate(sa.get("topAds", [])[:3], 1):
            ad_lines.append(
                f"   {idx}. **{ad['adName']}** ({ad['source']}): {ad['views']} vis. • 📲 **{ad['whatsappOrders']} pedidos ({ad['conversionRate']}%)** • 🛒 {ad['abandoned']} aband."
            )
        ads_section = ("\n\n📣 **Por Anuncio Específico:**\n" + "\n".join(ad_lines)) if ad_lines else ""
        reply = (
            f"📈 **Resumen de tu Tienda (`tienda-marcos.pages.dev`):**\n"
            f"• 👁️ **Visitas:** **{sa['pageViews']}** ({sa['uniqueVisitors']} únicos) • ⏱️ **Permanencia:** **{sa['avgDurationFormatted']}**\n"
            f"• 📲 **Pedidos WA:** **{sa['whatsappCheckoutClicks']}** ({sa['conversionRatePct']}% conv.) • 🛒 **Abandonados:** **{sa['abandonedCartsCount']}** (${sa['abandonedCartsValue']:,})"
            f"{ads_section}"
        ).replace(",", ".")

    # 4.5 INTENCIÓN: APRENDER / GUARDAR EN MEMORIA PERMANENTE DE MARCOS
    elif any(k in text_lower for k in ["acordate que", "acordate de que", "aprende que", "aprendé que", "guarda en memoria", "guardá en memoria", "recorda que", "recordá que", "anota que", "anotá que", "tene en cuenta que", "tené en cuenta que", "/aprender"]):
        cleaned_rule = re.sub(
            r"^(?:marcos\s*,?\s*)?(?:por favor\s*)?(?:acordate(?: de)? que|aprend[eé] que|guard[aá] en memoria(?: que)?|record[aá] que|anot[aá] que|ten[eé] en cuenta que|/aprender)\s*",
            "",
            user_text,
            flags=re.IGNORECASE
        ).strip()
        if not cleaned_rule:
            cleaned_rule = user_text.strip()
        mem = state.setdefault("agentMemory", {"learnedRules": [], "campaignHistory": []})
        rules = mem.setdefault("learnedRules", [])
        new_item = {
            "id": f"mem-{int(time.time()*1000)}",
            "date": datetime.now().strftime("Hoy %H:%M"),
            "text": cleaned_rule[0].upper() + cleaned_rule[1:] if cleaned_rule else cleaned_rule
        }
        rules.insert(0, new_item)
        mem["learnedRules"] = rules[:30]
        save_state(state)
        reply = f"🧠 **Aprendido y guardado en mi memoria permanente, Marcos:**\n> *\"{new_item['text']}\"*"

    elif any(k in text_lower for k in ["que sabes", "qué sabés", "que aprendiste", "qué aprendiste", "tu memoria", "mi memoria", "/memoria"]):
        mem = state.get("agentMemory", {})
        rules = mem.get("learnedRules", [])
        if not rules:
            reply = "🧠 Todavía no me pediste que guarde reglas específicas. Decime por ejemplo: *\"Acordate que los jueves reparto gratis en Nueva Córdoba\"*."
        else:
            r_lines = [f"• {r['text']} *({r.get('date', '')})*" for r in rules[:8]]
            reply = "🧠 **Marcos, esto es lo que tengo aprendido en mi memoria permanente:**\n\n" + "\n".join(r_lines)

    # 5. INTENCIÓN: AUDITAR CAMPAÑA DE META ADS (EL SEMÁFORO + APRENDIZAJE HISTÓRICO)
    elif any(k in text_lower for k in ["gaste", "gasté", "audita", "auditá", "campaña", "presupuesto", "meseta", "semaforo", "semáforo", "/auditar"]):
        nums = [int(n.replace(".", "")) for n in re.findall(r"\d+(?:\.\d+)?", user_text)]
        spend = 10000
        clicks = 35
        sales = 1
        if len(nums) >= 2:
            spend = nums[0] if nums[0] > 500 else nums[0] * 1000
            clicks = nums[1]
            sales = nums[2] if len(nums) >= 3 else 0

        cpc = round(spend / max(1, clicks))
        cpa_real = round(spend / sales) if sales > 0 else spend
        scaled_budget = round(spend * 1.20)

        if sales == 0 and spend >= 12000:
            verdict = "ROJO"
            semaforo = "🔴 SEMÁFORO ROJO: PAUSAR O CAMBIAR ANUNCIO"
            accion = (
                f"• Gastaste **${spend:,}** ({clicks} clics, 0 ventas). Apagá ese anuncio y probá un Reel nuevo apuntando a `tienda-marcos.pages.dev`."
            )
        elif sales > 0 and cpa_real <= 7500:
            verdict = "VERDE"
            semaforo = "🟢 SEMÁFORO VERDE: CAMPAÑA RENTABLE"
            accion = (
                f"• Gastaste **${spend:,}** con **{sales} venta(s)** (CPA: **${cpa_real:,}**). Si querés escalar, subí máximo +20% cada 48 hs (**${scaled_budget:,}/día**)."
            )
        else:
            verdict = "AMARILLO"
            semaforo = "🟡 SEMÁFORO AMARILLO: MANTENER Y CERRAR EN WHATSAPP"
            accion = (
                f"• Gastaste **${spend:,}** ({clicks} clics, {sales} venta(s), CPA: ${cpa_real:,}). Mantené el presupuesto 48 hs más y hacé seguimiento a las consultas abiertas."
            )

        mem = state.setdefault("agentMemory", {"learnedRules": [], "campaignHistory": []})
        chist = mem.setdefault("campaignHistory", [])
        chist.insert(0, {
            "date": datetime.now().strftime("Hoy %H:%M"),
            "spend": spend,
            "clicks": clicks,
            "sales": sales,
            "cpa": cpa_real,
            "verdict": verdict
        })
        mem["campaignHistory"] = chist[:20]
        save_state(state)

        reply = f"🚦 **{semaforo}**\n{accion}".replace(",", ".")

    # 6. INTENCIÓN: GUIONES DE CIERRE DE WHATSAPP / ANTI-VISTO / COPYS
    elif any(k in text_lower for k in ["visto", "whatsapp", "cliente", "responder", "respuesta", "objecion", "copy", "/cierre"]):
        reply = (
            "💬 **Guiones Probados de Cierre para WhatsApp (Copiar y Pegar):**\n\n"
            "1️⃣ **Cuando preguntan y clavan el visto (Mandar a las 4 o 24 hs):**\n"
            "> *\"¡Hola! ¿Cómo estás? Te escribo cortito porque hoy a las 15hs sale el cadete a repartir por tu zona en Córdoba. Me quedan los últimos 2 Packs x2 con el 15% OFF por transferencia ($55.165 las dos almohadas). ¿Querés que te separe uno para llevártelo hoy?\"*\n\n"
            "2️⃣ **Cuando dicen \"Lo consulto con mi pareja / Lo pienso y te aviso\":**\n"
            "> *\"¡Dale, genial! Comentale que el Pack x2 Cervicales les queda en $55.165 finales con transferencia (es decir, $27.580 cada almohada viscoelástica de alta densidad, menos de la mitad que en cualquier colchonería) y lo reciben en el día en Córdoba. Avisame antes de las 16hs así llego a incluirlos en el reparto de hoy.\"* \n\n"
            "3️⃣ **Cuando preguntan \"¿Tienen local para ir a verlas?\":**\n"
            "> *\"Trabajamos directo de depósito con entregas a domicilio en toda Córdoba Capital para poder mantenerte el precio de fábrica ($55.165 el Pack x2 con transferencia). Vienen en su bolsa cerrada con garantía y podés verificar la densidad y calidad en la puerta de tu casa al recibirlas.\"* "
        )

    # 7. RESPUESTA INTELIGENTE GENERAL / CONEXIÓN CON API EXTERNA + MEMORIA APRENDIDA
    else:
        ai_cfg = state.get("aiConfig", {})
        if ai_cfg.get("apiKey"):
            try:
                learned_rules = state.get("agentMemory", {}).get("learnedRules", [])
                mem_context = "\n".join([f"- {r.get('text', '')}" for r in learned_rules])
                sys_prompt = (
                    f"Sos el Asistente Comercial personal de Marcos ('{biz.get('name')}'). "
                    f"Vendés almohadas viscoelásticas en {biz.get('city')}. "
                    f"Descuento transferencia: {biz.get('transferDiscountPercent')}%. "
                    f"Reglas aprendidas en memoria:\n{mem_context}\n"
                    f"Respondé corto, directo, práctico y en español argentino hablándole a Marcos."
                )
                payload = json.dumps({
                    "model": ai_cfg.get("model", "gpt-4o-mini"),
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_text}
                    ]
                }).encode("utf-8")
                req = urllib.request.Request(
                    ai_cfg.get("apiUrl", "https://api.openai.com/v1/chat/completions"),
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {ai_cfg['apiKey']}"
                    }
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    res_data = json.loads(resp.read().decode("utf-8"))
                    reply = res_data["choices"][0]["message"]["content"]
            except Exception as e:
                reply = f"⚠️ Error al consultar la API externa ({e})."
        else:
            reply = (
                "🤖 **Estoy listo para ejecutar tu orden.** Podés:\n\n"
                "• 📎 **Subir clips de video o fotos reales de tu celular** (tocando el botón *'Subir Clips / Fotos Reales'* abajo) y decirme: *\"Armame un Reel con mis archivos\"*.\n"
                "• 🧮 **Calcular Costos, Devoluciones y CPA:** Escribí *\"Calculame los costos y ganancias\"*.\n"
                "• 💲 **Cambiar un precio o costo:** Escribí *\"Cambiá el precio del Pack x2 Cervicales a 66900\"*.\n"
                "• 🚦 **Auditar Meta Ads:** Escribí *\"Gasté 10000, tuve 35 clics y 1 venta, auditá la campaña\"*.\n"
                "• 💬 **Cerrar ventas en WhatsApp:** Escribí *\"Dame respuestas para clientes que clavan el visto\"*."
            )

    voice_mp3 = None
    voice_ogg = None
    if generate_voice:
        try:
            voice_mp3, voice_ogg = generate_agent_voice_note(reply)
        except Exception as e:
            print(f"Error generando voz: {e}", flush=True)

    now_str = datetime.now().strftime("%H:%M")
    state = load_state()
    hist = state.get("chatHistory", [])
    user_label = f"🎙️ [Orden por Voz] {user_text}" if is_voice_input else user_text
    hist.append({"role": "user", "source": source, "timestamp": now_str, "text": user_label})
    msg_obj = {"role": "assistant", "source": source, "timestamp": now_str, "text": reply}
    if generated_reel:
        msg_obj["reel"] = f"/reels/{generated_reel}"
    if voice_mp3:
        msg_obj["voice"] = f"/voices/{voice_mp3}"
    hist.append(msg_obj)
    state["chatHistory"] = hist[-50:]
    save_state(state)

    return {
        "reply": reply,
        "reel": generated_reel,
        "voiceUrl": f"/voices/{voice_mp3}" if voice_mp3 else None,
        "voiceOggPath": voice_ogg,
        "transcribedText": user_text
    }

# ==============================================================================
# WORKER DE TELEGRAM EN SEGUNDO PLANO (RECIBE AUDIOS/VOZ, FOTOS, CLIPS Y TEXTO)
# ==============================================================================
def send_telegram_message(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    urllib.request.urlopen(req, timeout=15)

def send_telegram_voice(token, chat_id, ogg_path, caption=""):
    """Envía una Nota de Voz nativa (con ondas de audio) al chat de Telegram usando sendVoice."""
    boundary = "----WebKitFormBoundaryVoice7MA4YWxk"
    url = f"https://api.telegram.org/bot{token}/sendVoice"
    filename = os.path.basename(ogg_path)
    with open(ogg_path, "rb") as f:
        voice_bytes = f.read()

    parts = []
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode("utf-8"))
    if caption:
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption[:900]}\r\n".encode("utf-8"))
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"voice\"; filename=\"{filename}\"\r\nContent-Type: audio/ogg\r\n\r\n".encode("utf-8")
        + voice_bytes
        + f"\r\n--{boundary}--\r\n".encode("utf-8")
    )
    body = b"".join(parts)
    req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    urllib.request.urlopen(req, timeout=30)

def send_telegram_video(token, chat_id, video_path, caption=""):
    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    url = f"https://api.telegram.org/bot{token}/sendVideo"
    filename = os.path.basename(video_path)
    with open(video_path, "rb") as f:
        video_bytes = f.read()

    parts = []
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode("utf-8"))
    if caption:
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption[:900]}\r\n".encode("utf-8"))
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"video\"; filename=\"{filename}\"\r\nContent-Type: video/mp4\r\n\r\n".encode("utf-8")
        + video_bytes
        + f"\r\n--{boundary}--\r\n".encode("utf-8")
    )
    body = b"".join(parts)
    req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    urllib.request.urlopen(req, timeout=45)

def download_telegram_file(token, file_id, ext_hint=".jpg", target_dir=None):
    info_url = f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}"
    with urllib.request.urlopen(info_url, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
        file_path = data["result"]["file_path"]
    ext = os.path.splitext(file_path)[1] or ext_hint
    dl_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    fname = f"tg_real_{datetime.now().strftime('%H%M%S')}_{file_id[-6:]}{ext}"
    folder = target_dir or UPLOADS_DIR
    dest = os.path.join(folder, fname)
    with urllib.request.urlopen(dl_url, timeout=30) as r, open(dest, "wb") as out_f:
        out_f.write(r.read())
    return fname if target_dir is None else dest

def telegram_polling_loop():
    last_update_id = 0
    while True:
        try:
            state = load_state()
            tg = state.get("telegramConfig", {})
            token = os.environ.get("TELEGRAM_BOT_TOKEN") or tg.get("botToken", "").strip()
            if not token:
                telegram_status["running"] = False
                time.sleep(3)
                continue

            if not telegram_status["running"]:
                me_url = f"https://api.telegram.org/bot{token}/getMe"
                with urllib.request.urlopen(me_url, timeout=10) as r:
                    me_data = json.loads(r.read().decode("utf-8"))
                    if me_data.get("ok"):
                        telegram_status["running"] = True
                        telegram_status["botUsername"] = me_data["result"].get("username")
                        telegram_status["lastError"] = None

            poll_url = f"https://api.telegram.org/bot{token}/getUpdates?offset={last_update_id + 1}&timeout=15"
            with urllib.request.urlopen(poll_url, timeout=20) as r:
                updates = json.loads(r.read().decode("utf-8"))
                if updates.get("ok"):
                    for upd in updates.get("result", []):
                        last_update_id = upd["update_id"]
                        msg = upd.get("message") or upd.get("edited_message")
                        if not msg:
                            continue
                        chat_id = str(msg["chat"]["id"])

                        # 1. Si manda una NOTA DE VOZ o AUDIO por Telegram
                        voice_obj = msg.get("voice") or msg.get("audio")
                        if voice_obj:
                            fid = voice_obj["file_id"]
                            try:
                                audio_local_path = download_telegram_file(token, fid, ".ogg", target_dir=TEMP_DIR)
                                transcribed = transcribe_voice_file(audio_local_path)
                                res = execute_agent_instruction(
                                    transcribed,
                                    source="telegram",
                                    generate_voice=True,
                                    is_voice_input=True
                                )
                                clean_reply = (
                                    f"🎙️ Escuché tu orden: \"{transcribed}\"\n\n"
                                    + res["reply"].replace("**", "").replace("*", "")
                                )
                                send_telegram_message(token, chat_id, clean_reply)

                                if res.get("voiceOggPath") and os.path.exists(res["voiceOggPath"]):
                                    send_telegram_voice(token, chat_id, res["voiceOggPath"], caption="🔊 Respuesta en voz del Agente")

                                if res.get("reel"):
                                    video_full = os.path.join(REELS_DIR, res["reel"])
                                    if os.path.exists(video_full):
                                        send_telegram_video(token, chat_id, video_full, caption=f"🎬 {res['reel']} (100% producto real, sin voz)")
                            except Exception as ve:
                                send_telegram_message(token, chat_id, f"⚠️ No pude escuchar bien el audio ({ve}). ¿Podés repetir el mensaje de voz un poquito más claro?")
                            continue

                        # 2. Si manda un VIDEO corto desde el celular por Telegram
                        if "video" in msg:
                            fid = msg["video"]["file_id"]
                            saved_name = download_telegram_file(token, fid, ".mp4")
                            send_telegram_message(
                                token,
                                chat_id,
                                f"📎 ¡Clip de video real recibido ({saved_name}) y guardado intacto!\nMandame más clips/fotos o mandame un audio diciendo 'Armame el reel' para unirlos respetando 100% el producto."
                            )
                            continue

                        # 3. Si manda una FOTO real desde el celular por Telegram
                        if "photo" in msg and len(msg["photo"]) > 0:
                            fid = msg["photo"][-1]["file_id"]
                            saved_name = download_telegram_file(token, fid, ".jpg")
                            send_telegram_message(
                                token,
                                chat_id,
                                f"📷 ¡Foto real de tu celular recibida ({saved_name}) y guardada intacta!\nMandame más fotos/clips o mandame un audio diciendo 'Armame el reel' para generar el video."
                            )
                            continue

                        # 4. Si manda TEXTO por Telegram (también responde con texto + Nota de Voz)
                        user_text = msg.get("text", "").strip()
                        if not user_text:
                            continue

                        res = execute_agent_instruction(user_text, source="telegram", generate_voice=True)
                        clean_reply = res["reply"].replace("**", "").replace("*", "")
                        send_telegram_message(token, chat_id, clean_reply)

                        if res.get("voiceOggPath") and os.path.exists(res["voiceOggPath"]):
                            send_telegram_voice(token, chat_id, res["voiceOggPath"], caption="🔊 Respuesta en voz del Agente")

                        if res.get("reel"):
                            video_full = os.path.join(REELS_DIR, res["reel"])
                            if os.path.exists(video_full):
                                send_telegram_video(token, chat_id, video_full, caption=f"🎬 {res['reel']} (100% producto real, sin voz)")
        except Exception as e:
            telegram_status["running"] = False
            telegram_status["lastError"] = str(e)
            time.sleep(5)

# ==============================================================================
# SERVIDOR HTTP + API REST
# ==============================================================================
class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = False

class AgentRequestHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/state":
            state = load_state()
            biz = state.get("business", {})
            hidden_reels = set(state.get("deletedReels", []))
            for hr in list(hidden_reels):
                hr_path = os.path.join(REELS_DIR, hr)
                if os.path.exists(hr_path):
                    try:
                        os.remove(hr_path)
                    except Exception:
                        pass
            enriched_products = [calculate_product_metrics(p, biz) for p in state.get("products", [])]
            reels = sorted(
                [f for f in os.listdir(REELS_DIR) if f.endswith(".mp4") and f not in hidden_reels],
                key=lambda x: os.path.getmtime(os.path.join(REELS_DIR, x)),
                reverse=True
            )
            self._send_json({
                "business": biz,
                "products": enriched_products,
                "storeAnalytics": compute_store_analytics(state),
                "agentMemory": state.get("agentMemory", {"learnedRules": [], "campaignHistory": []}),
                "chatHistory": state.get("chatHistory", []),
                "reels": reels,
                "uploads": list_uploaded_media(),
                "telegramStatus": telegram_status,
                "telegramConfigured": bool(state.get("telegramConfig", {}).get("botToken")),
                "aiConfigured": bool(state.get("aiConfig", {}).get("apiKey"))
            })
            return

        if path.startswith("/reels/"):
            fname = os.path.basename(path)
            fpath = os.path.join(REELS_DIR, fname)
            if os.path.exists(fpath):
                with open(fpath, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition", f"inline; filename=\"{fname}\"")
                self.end_headers()
                self.wfile.write(data)
                return

        if path.startswith("/voices/"):
            fname = os.path.basename(path)
            fpath = os.path.join(VOICES_DIR, fname)
            if os.path.exists(fpath):
                ctype = "audio/ogg" if fname.endswith(".ogg") else "audio/mpeg"
                with open(fpath, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

        if path.startswith("/uploads/"):
            fname = os.path.basename(path)
            fpath = os.path.join(UPLOADS_DIR, fname)
            if os.path.exists(fpath):
                ext = os.path.splitext(fname)[1].lower()
                ctype = "video/mp4" if ext in (".mp4", ".mov", ".webm") else "image/jpeg"
                with open(fpath, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

        if path == "/" or path == "/index.html":
            index_file = os.path.join(PUBLIC_DIR, "index.html")
            if os.path.exists(index_file):
                with open(index_file, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

        self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        payload = json.loads(raw_body) if raw_body else {}

        if path == "/api/chat":
            msg = payload.get("message", "").strip()
            selected_files = payload.get("selectedFiles", None)
            gen_voice = bool(payload.get("generateVoice", False))
            is_voice_in = bool(payload.get("isVoiceInput", False))
            if not msg:
                self._send_json({"error": "Mensaje vacío"}, 400)
                return
            res = execute_agent_instruction(
                msg,
                source="panel",
                selected_files=selected_files,
                generate_voice=gen_voice,
                is_voice_input=is_voice_in
            )
            self._send_json(res)
            return

        if path == "/api/voice-order":
            # Recibe un audio grabado desde el micrófono del panel web o subido en base64, lo transcribe y responde con voz
            b64 = payload.get("audioBase64", "")
            ext = payload.get("ext", ".webm")
            selected_files = payload.get("selectedFiles", None)
            if "," in b64:
                b64 = b64.split(",", 1)[1]
            if not b64:
                self._send_json({"error": "Audio vacío"}, 400)
                return
            raw_audio = base64.b64decode(b64)
            tmp_audio = os.path.join(TEMP_DIR, f"web_mic_{datetime.now().strftime('%H%M%S_%f')}{ext}")
            with open(tmp_audio, "wb") as f:
                f.write(raw_audio)
            try:
                transcribed = transcribe_voice_file(tmp_audio)
                res = execute_agent_instruction(
                    transcribed,
                    source="panel",
                    selected_files=selected_files,
                    generate_voice=True,
                    is_voice_input=True
                )
                self._send_json(res)
            except Exception as e:
                self._send_json({"error": f"No se pudo transcribir el audio: {e}"}, 400)
            finally:
                try:
                    os.remove(tmp_audio)
                except Exception:
                    pass
            return

        if path == "/api/upload":
            files = payload.get("files", [])
            saved = []
            for fobj in files:
                raw_name = re.sub(r"[^a-zA-Z0-9._-]", "_", fobj.get("name", "archivo.jpg"))
                b64 = fobj.get("dataBase64", "")
                if "," in b64:
                    b64 = b64.split(",", 1)[1]
                raw_bytes = base64.b64decode(b64)
                final_name = f"real_{datetime.now().strftime('%H%M%S')}_{raw_name}"
                dest_path = os.path.join(UPLOADS_DIR, final_name)
                with open(dest_path, "wb") as out_f:
                    out_f.write(raw_bytes)
                saved.append(final_name)
            self._send_json({"ok": True, "saved": saved})
            return

        if path == "/api/delete-reel":
            raw_fname = urllib.parse.unquote(payload.get("filename", "")).strip()
            if not raw_fname:
                self._send_json({"error": "Archivo inválido"}, 400)
                return
            state = load_state()
            deleted_list = set(state.get("deletedReels", []))

            if raw_fname == "ALL":
                for f in os.listdir(REELS_DIR):
                    if f.endswith(".mp4"):
                        fpath = os.path.join(REELS_DIR, f)
                        try:
                            os.remove(fpath)
                        except Exception:
                            deleted_list.add(f)
                for m in state.get("chatHistory", []):
                    m.pop("reel", None)
            else:
                fname = os.path.basename(raw_fname)
                fpath = os.path.join(REELS_DIR, fname)
                if os.path.exists(fpath):
                    try:
                        os.remove(fpath)
                        deleted_list.discard(fname)
                    except Exception:
                        deleted_list.add(fname)
                else:
                    deleted_list.add(fname)
                for m in state.get("chatHistory", []):
                    if m.get("reel") == f"/reels/{fname}":
                        m.pop("reel", None)

            state["deletedReels"] = list(deleted_list)
            save_state(state)
            self._send_json({"ok": True})
            return

        if path == "/api/delete-upload":
            fname = os.path.basename(urllib.parse.unquote(payload.get("filename", "")).strip())
            if not fname:
                self._send_json({"error": "Archivo inválido"}, 400)
                return
            fpath = os.path.join(UPLOADS_DIR, fname)
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except Exception as e:
                    self._send_json({"error": str(e)}, 500)
                    return
            self._send_json({"ok": True})
            return

        if path == "/api/clear-chat":
            state = load_state()
            state["chatHistory"] = []
            save_state(state)
            self._send_json({"ok": True})
            return

        if path == "/api/build-reel":
            angle = payload.get("angle", "cervical")
            selected_files = payload.get("selectedFiles", None)
            try:
                generated_reel = generate_visual_reel(angle=angle, selected_files=selected_files)
                self._send_json({"ok": True, "reel": generated_reel})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/api/memory/add":
            rule_text = payload.get("text", "").strip()
            if not rule_text:
                self._send_json({"error": "Regla vacía"}, 400)
                return
            state = load_state()
            mem = state.setdefault("agentMemory", {"learnedRules": [], "campaignHistory": []})
            rules = mem.setdefault("learnedRules", [])
            new_rule = {
                "id": f"mem-{int(time.time())}",
                "text": rule_text,
                "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "category": payload.get("category", "Aprendido de Marcos")
            }
            rules.insert(0, new_rule)
            save_state(state)
            self._send_json({"ok": True, "agentMemory": mem})
            return

        if path == "/api/memory/delete":
            rule_id = payload.get("id", "").strip()
            state = load_state()
            mem = state.setdefault("agentMemory", {"learnedRules": [], "campaignHistory": []})
            mem["learnedRules"] = [r for r in mem.get("learnedRules", []) if r.get("id") != rule_id]
            save_state(state)
            self._send_json({"ok": True, "agentMemory": mem})
            return

        if path == "/api/products":
            state = load_state()
            biz = state.setdefault("business", {})
            if "transferDiscountPercent" in payload:
                biz["transferDiscountPercent"] = float(payload["transferDiscountPercent"])
            if "returnReservePercent" in payload:
                biz["returnReservePercent"] = float(payload["returnReservePercent"])
            if "products" in payload and isinstance(payload["products"], list):
                cleaned = []
                for idx, item in enumerate(payload["products"]):
                    pid = item.get("id") or f"prod-{idx+1}-{int(time.time())}"
                    cleaned.append({
                        "id": pid,
                        "name": str(item.get("name", "Producto")).strip() or "Producto",
                        "price": int(float(item.get("price", 0))),
                        "cost": int(float(item.get("cost", 0))),
                        "packagingCost": int(float(item.get("packagingCost", 500)))
                    })
                state["products"] = cleaned
            save_state(state)
            enriched = [calculate_product_metrics(p, biz) for p in state.get("products", [])]
            self._send_json({"ok": True, "business": biz, "products": enriched})
            return

        if path == "/api/track":
            # Recibe eventos en tiempo real desde la Tienda WhatsApp (tienda-marcos.pages.dev o local)
            state = load_state()
            sa = state.setdefault("storeAnalytics", {})
            ev_type = payload.get("event") or payload.get("eventType") or ""
            prod_id = payload.get("productId", "")
            prod_name = payload.get("productName", "")
            clicks = int(payload.get("clicks", 0))
            duration_sec = int(payload.get("durationSec", 0))
            cart_value = int(payload.get("cartValue", 0))
            cart_items = payload.get("cartItems", [])

            ad_key = payload.get("adKey") or "directo-catalogo"
            ad_name = payload.get("adName") or "Acceso Directo al Catálogo"
            ad_source = payload.get("source") or "WhatsApp / Directo"

            pstats = sa.setdefault("productStats", {})
            ad_stats = sa.setdefault("adStats", {})
            ad_entry = ad_stats.setdefault(ad_key, {
                "adName": ad_name,
                "source": ad_source,
                "views": 0,
                "clicks": 0,
                "sessions": 0,
                "totalDurationSeconds": 0,
                "addedToCart": 0,
                "whatsappOrders": 0,
                "abandoned": 0,
                "revenue": 0
            })

            if ev_type == "page_view":
                sa["pageViews"] = int(sa.get("pageViews", 0)) + 1
                ad_entry["views"] = int(ad_entry.get("views", 0)) + 1
                if payload.get("isNewSession", True) or payload.get("newVisitor", False):
                    sa["uniqueVisitors"] = int(sa.get("uniqueVisitors", 0)) + 1

            elif ev_type == "product_view":
                sa["totalClicks"] = int(sa.get("totalClicks", 0)) + 1
                ad_entry["clicks"] = int(ad_entry.get("clicks", 0)) + 1
                if prod_id:
                    ps = pstats.setdefault(prod_id, {"name": prod_name or prod_id, "views": 0, "addedToCart": 0, "whatsappOrders": 0, "abandoned": 0})
                    ps["views"] = int(ps.get("views", 0)) + 1

            elif ev_type == "add_to_cart":
                sa["totalClicks"] = int(sa.get("totalClicks", 0)) + 1
                sa["addToCartCount"] = int(sa.get("addToCartCount", 0)) + 1
                ad_entry["clicks"] = int(ad_entry.get("clicks", 0)) + 1
                ad_entry["addedToCart"] = int(ad_entry.get("addedToCart", 0)) + 1
                if prod_id:
                    ps = pstats.setdefault(prod_id, {"name": prod_name or prod_id, "views": 0, "addedToCart": 0, "whatsappOrders": 0, "abandoned": 0})
                    ps["views"] = int(ps.get("views", 0)) + 1
                    ps["addedToCart"] = int(ps.get("addedToCart", 0)) + 1

            elif ev_type == "whatsapp_order":
                if payload.get("newVisitor"):
                    sa["pageViews"] = int(sa.get("pageViews", 0)) + 1
                    sa["uniqueVisitors"] = int(sa.get("uniqueVisitors", 0)) + 1
                    sa["addToCartCount"] = int(sa.get("addToCartCount", 0)) + 1
                    ad_entry["views"] = int(ad_entry.get("views", 0)) + 1
                    ad_entry["addedToCart"] = int(ad_entry.get("addedToCart", 0)) + 1
                sa["totalClicks"] = int(sa.get("totalClicks", 0)) + max(1, clicks)
                sa["whatsappCheckoutClicks"] = int(sa.get("whatsappCheckoutClicks", 0)) + 1
                ad_entry["clicks"] = int(ad_entry.get("clicks", 0)) + max(1, clicks)
                ad_entry["whatsappOrders"] = int(ad_entry.get("whatsappOrders", 0)) + 1
                ad_entry["revenue"] = int(ad_entry.get("revenue", 0)) + cart_value
                if duration_sec > 0:
                    sa["totalSessionsForDuration"] = int(sa.get("totalSessionsForDuration", 0)) + 1
                    sa["totalDurationSeconds"] = int(sa.get("totalDurationSeconds", 0)) + duration_sec
                    ad_entry["sessions"] = int(ad_entry.get("sessions", 0)) + 1
                    ad_entry["totalDurationSeconds"] = int(ad_entry.get("totalDurationSeconds", 0)) + duration_sec
                if prod_id:
                    ps = pstats.setdefault(prod_id, {"name": prod_name or prod_id, "views": 0, "addedToCart": 0, "whatsappOrders": 0, "abandoned": 0})
                    ps["views"] = int(ps.get("views", 0)) + (1 if payload.get("newVisitor") else 0)
                    ps["addedToCart"] = int(ps.get("addedToCart", 0)) + (1 if payload.get("newVisitor") else 0)
                    ps["whatsappOrders"] = int(ps.get("whatsappOrders", 0)) + 1
                for item in cart_items:
                    iid = item.get("id", "")
                    if iid and iid in pstats and iid != prod_id:
                        pstats[iid]["whatsappOrders"] = int(pstats[iid].get("whatsappOrders", 0)) + 1
                rec = sa.setdefault("recentEvents", [])
                summary_names = ", ".join([i.get("name", "Almohada") for i in cart_items]) or (prod_name or "Pedido en Tienda")
                rec.insert(0, {
                    "time": datetime.now().strftime("Hoy %H:%M"),
                    "type": "whatsapp_order",
                    "status": "whatsapp_order",
                    "durationSec": duration_sec or 95,
                    "clicks": clicks or 6,
                    "cartValue": cart_value,
                    "topProduct": summary_names,
                    "adName": ad_name,
                    "source": ad_source,
                    "detail": f"Pidió por WhatsApp: {summary_names} (${cart_value:,})".replace(",", ".")
                })
                sa["recentEvents"] = rec[:15]

            elif ev_type == "abandoned_cart":
                if payload.get("newVisitor"):
                    sa["pageViews"] = int(sa.get("pageViews", 0)) + 1
                    sa["uniqueVisitors"] = int(sa.get("uniqueVisitors", 0)) + 1
                    sa["addToCartCount"] = int(sa.get("addToCartCount", 0)) + 1
                    ad_entry["views"] = int(ad_entry.get("views", 0)) + 1
                    ad_entry["addedToCart"] = int(ad_entry.get("addedToCart", 0)) + 1
                sa["totalClicks"] = int(sa.get("totalClicks", 0)) + max(1, clicks)
                sa["abandonedCartsCount"] = int(sa.get("abandonedCartsCount", 0)) + 1
                sa["abandonedCartsValue"] = int(sa.get("abandonedCartsValue", 0)) + cart_value
                ad_entry["clicks"] = int(ad_entry.get("clicks", 0)) + max(1, clicks)
                ad_entry["abandoned"] = int(ad_entry.get("abandoned", 0)) + 1
                if duration_sec > 0:
                    sa["totalSessionsForDuration"] = int(sa.get("totalSessionsForDuration", 0)) + 1
                    sa["totalDurationSeconds"] = int(sa.get("totalDurationSeconds", 0)) + duration_sec
                    ad_entry["sessions"] = int(ad_entry.get("sessions", 0)) + 1
                    ad_entry["totalDurationSeconds"] = int(ad_entry.get("totalDurationSeconds", 0)) + duration_sec
                if prod_id:
                    ps = pstats.setdefault(prod_id, {"name": prod_name or prod_id, "views": 0, "addedToCart": 0, "whatsappOrders": 0, "abandoned": 0})
                    ps["views"] = int(ps.get("views", 0)) + (1 if payload.get("newVisitor") else 0)
                    ps["addedToCart"] = int(ps.get("addedToCart", 0)) + (1 if payload.get("newVisitor") else 0)
                    ps["abandoned"] = int(ps.get("abandoned", 0)) + 1
                for item in cart_items:
                    iid = item.get("id", "")
                    if iid and iid in pstats and iid != prod_id:
                        pstats[iid]["abandoned"] = int(pstats[iid].get("abandoned", 0)) + 1
                rec = sa.setdefault("recentEvents", [])
                summary_names = ", ".join([i.get("name", "Almohada") for i in cart_items]) or (prod_name or "Productos en carrito")
                rec.insert(0, {
                    "time": datetime.now().strftime("Hoy %H:%M"),
                    "type": "abandoned_cart",
                    "status": "abandoned_cart",
                    "durationSec": duration_sec or 55,
                    "clicks": clicks or 4,
                    "cartValue": cart_value,
                    "topProduct": summary_names,
                    "adName": ad_name,
                    "source": ad_source,
                    "detail": f"Carrito Abandonado: {summary_names} (${cart_value:,})".replace(",", ".")
                })
                sa["recentEvents"] = rec[:15]

            elif ev_type == "session_end":
                if clicks > 0:
                    sa["totalClicks"] = int(sa.get("totalClicks", 0)) + clicks
                    ad_entry["clicks"] = int(ad_entry.get("clicks", 0)) + clicks
                if duration_sec > 0:
                    sa["totalSessionsForDuration"] = int(sa.get("totalSessionsForDuration", 0)) + 1
                    sa["totalDurationSeconds"] = int(sa.get("totalDurationSeconds", 0)) + duration_sec
                    ad_entry["sessions"] = int(ad_entry.get("sessions", 0)) + 1
                    ad_entry["totalDurationSeconds"] = int(ad_entry.get("totalDurationSeconds", 0)) + duration_sec

            save_state(state)
            self._send_json({"ok": True, "storeAnalytics": compute_store_analytics(state)})
            return

        if path == "/api/analytics/reset":
            state = load_state()
            sa = state.setdefault("storeAnalytics", {})
            sa["pageViews"] = 0
            sa["uniqueVisitors"] = 0
            sa["totalClicks"] = 0
            sa["totalSessionsForDuration"] = 0
            sa["totalDurationSeconds"] = 0
            sa["addToCartCount"] = 0
            sa["whatsappCheckoutClicks"] = 0
            sa["abandonedCartsCount"] = 0
            sa["abandonedCartsValue"] = 0
            for pid, pdata in sa.get("productStats", {}).items():
                pdata["views"] = 0
                pdata["addedToCart"] = 0
                pdata["whatsappOrders"] = 0
                pdata["abandoned"] = 0
            for akey, adata in sa.get("adStats", {}).items():
                adata["views"] = 0
                adata["clicks"] = 0
                adata["sessions"] = 0
                adata["totalDurationSeconds"] = 0
                adata["addedToCart"] = 0
                adata["whatsappOrders"] = 0
                adata["abandoned"] = 0
                adata["revenue"] = 0
            sa["recentEvents"] = []
            save_state(state)
            self._send_json({"ok": True, "storeAnalytics": compute_store_analytics(state)})
            return

        if path == "/api/config":
            state = load_state()
            if "botToken" in payload:
                state.setdefault("telegramConfig", {})["botToken"] = payload["botToken"].strip()
                telegram_status["running"] = False
            if "apiKey" in payload:
                state.setdefault("aiConfig", {})["apiKey"] = payload["apiKey"].strip()
            if "apiUrl" in payload and payload["apiUrl"].strip():
                state.setdefault("aiConfig", {})["apiUrl"] = payload["apiUrl"].strip()
            if "model" in payload and payload["model"].strip():
                state.setdefault("aiConfig", {})["model"] = payload["model"].strip()
            save_state(state)
            self._send_json({"ok": True})
            return

        self.send_error(404, "Not Found")

if __name__ == "__main__":
    t_thread = threading.Thread(target=telegram_polling_loop, daemon=True)
    t_thread.start()
    ports_to_try = [int(os.environ.get("PORT", 4567)), 5055, 3055, 8765]
    server = None
    bound_port = None
    for p in ports_to_try:
        try:
            server = ThreadedHTTPServer(("0.0.0.0", p), AgentRequestHandler)
            bound_port = p
            break
        except OSError:
            continue
    print(f"Agente Todo-en-Uno corriendo en http://localhost:{bound_port}", flush=True)
    server.serve_forever()
