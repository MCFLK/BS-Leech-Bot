import os
import re
import asyncio
import hmac
import hashlib
import mimetypes
from urllib.parse import quote

from aiohttp import web
from pyrogram import filters
from pyrogram.handlers import MessageHandler
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

BOT_TOKEN = os.environ["BOT_TOKEN"]
BIN_CHANNEL = int(os.environ.get("BIN_CHANNEL", "0"))     # private storage channel id (-100...)
BIN_HASH = int(os.environ.get("BIN_CHANNEL_HASH", "0"))   # channel access hash (get it with /bininfo)
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")     # https://your-app.koyeb.app
PUBLIC_LINKS = os.environ.get("PUBLIC_LINKS", "0") == "1"  # 1 = anyone can use /genlink
CHUNK = 1024 * 1024                                       # Pyrogram stream_media chunk size


def sign(mid: int) -> str:
    return hmac.new(BOT_TOKEN.encode(), str(mid).encode(), hashlib.sha256).hexdigest()[:16]


def get_media(msg):
    return msg.document or msg.video or msg.audio or msg.animation or msg.voice


def register(app, web_app, authorized):
    # ───────────── /genlink ─────────────
    async def genlink(_, m):
        if not m.from_user:
            return
        if not (PUBLIC_LINKS or authorized(m.from_user.id)):
            return await m.reply_text("⛔ Not authorized.")
        if not BIN_CHANNEL or not BASE_URL:
            return await m.reply_text("⚠️ `BIN_CHANNEL` saha `BASE_URL` env variables set karanna.")

        r = m.reply_to_message
        media = get_media(r)
        if not media:
            return await m.reply_text("Document / video / audio file ekakata reply karala /genlink danna.")

        wait = await m.reply_text("⏳ Link eka hadanawa...")
        try:
            sent = await r.copy(BIN_CHANNEL)   # no re-upload, instant
        except Exception as e:
            return await wait.edit_text(
                f"❌ Storage channel ekata copy karanna beri una:\n`{e}`\n"
                "Bot eka channel eke **admin** da kiyala balanna.")

        mid = sent.id
        h = sign(mid)
        name = quote(media.file_name or f"file_{mid}")
        dl = f"{BASE_URL}/dl/{mid}/{name}?h={h}"
        st = f"{BASE_URL}/stream/{mid}/{name}?h={h}"
        size = media.file_size or 0
        await wait.edit_text(
            f"✅ **Link ready**\n\n📄 `{media.file_name or 'file'}`\n"
            f"📦 {size / 1024 / 1024:.2f} MB\n\n"
            f"⬇️ `{dl}`\n\n▶️ `{st}`",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬇️ Download", url=dl),
                InlineKeyboardButton("▶️ Stream", url=st)]]))

    app.add_handler(MessageHandler(genlink, filters.command("genlink") & filters.reply))

    async def bininfo(_, m):
        if not m.from_user or not authorized(m.from_user.id):
            return
        try:
            peer = await app.storage.get_peer_by_id(BIN_CHANNEL)
            await m.reply_text(f"`BIN_CHANNEL_HASH` = `{peer.access_hash}`")
        except KeyError:
            await m.reply_text("Channel eka cache wela na. Channel ekata message ekak danna, passe aye /bininfo.")

    app.add_handler(MessageHandler(bininfo, filters.command("bininfo")))

    # ───────────── HTTP streaming ─────────────
    async def serve(request, inline):
        try:
            mid = int(request.match_info["mid"])
        except ValueError:
            return web.Response(status=404, text="Not found")
        if not hmac.compare_digest(request.query.get("h", ""), sign(mid)):
            return web.Response(status=403, text="Invalid link")

        try:
            msg = await app.get_messages(BIN_CHANNEL, mid)
            media = get_media(msg)
        except Exception:
            media = None
        if not media:
            return web.Response(status=404, text="File not found")

        size = media.file_size
        name = media.file_name or f"file_{mid}"
        mime = media.mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"

        start, end = 0, size - 1
        rng = request.headers.get("Range")
        if rng:
            mt = re.match(r"bytes=(\d*)-(\d*)", rng)
            if mt:
                s, e = mt.groups()
                if s:
                    start = int(s)
                    end = int(e) if e else size - 1
                elif e:
                    start = max(size - int(e), 0)
        end = min(end, size - 1)
        if start > end or start >= size:
            return web.Response(status=416, headers={"Content-Range": f"bytes */{size}"})

        length = end - start + 1
        dispo = "inline" if inline else "attachment"
        headers = {
            "Content-Type": mime,
            "Content-Length": str(length),
            "Accept-Ranges": "bytes",
            "Content-Disposition": f"{dispo}; filename*=UTF-8''{quote(name)}",
        }
        status = 200
        if rng:
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"

        resp = web.StreamResponse(status=status, headers=headers)
        if request.method == "HEAD":
            await resp.prepare(request)
            return resp

        # First chunk ekak ganna try karanawa headers yawanna kalin.
        # Telegram eken error awoth browser eka 0 B/s eke hangila inna ekata wada, clear error ekak penawa.
        gen = app.stream_media(msg, offset=start // CHUNK)
        try:
            first = await asyncio.wait_for(gen.__anext__(), timeout=60)
        except Exception as e:
            print(f"[filelink] first chunk error mid={mid} size={size}: {e!r}")
            return web.Response(status=502, text=f"Telegram stream error: {e!r}")

        await resp.prepare(request)
        skip = start % CHUNK
        remaining = length

        async def chunks():
            yield first
            async for c in gen:
                yield c

        try:
            async for chunk in chunks():
                if skip:
                    chunk = chunk[skip:]
                    skip = 0
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                await resp.write(chunk)
                remaining -= len(chunk)
                if remaining <= 0:
                    break
        except (ConnectionResetError, ConnectionError):
            pass
        except Exception as e:
            print(f"[filelink] stream error mid={mid}: {e!r}")
        return resp

    async def dl_handler(request):
        return await serve(request, inline=False)

    async def stream_handler(request):
        return await serve(request, inline=True)

    web_app.router.add_get("/dl/{mid}/{name}", dl_handler)
    web_app.router.add_get("/stream/{mid}/{name}", stream_handler)


async def prepare(app):
    """Call after app.start(): make Pyrogram resolve the storage channel."""
    if not BIN_CHANNEL:
        return
    if BIN_HASH:
        try:
            await app.storage.update_peers([(BIN_CHANNEL, BIN_HASH, "channel", None, None)])
        except Exception as e:
            print(f"[filelink] hash inject una na: {e}")
    try:
        await app.get_chat(BIN_CHANNEL)
    except Exception as e:
        print(f"[filelink] BIN_CHANNEL resolve una na: {e}")
