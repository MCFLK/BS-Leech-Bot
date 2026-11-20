import os, re, time, asyncio, shutil, subprocess
import aria2p
from aiohttp import web
from pyrogram import Client, filters, idle
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNERS = [int(x) for x in os.environ.get("OWNER_IDS", "0").split(",")]
DL_DIR = "/app/downloads"
SPLIT = 1950 * 1024 * 1024  # Telegram 2GB limit

app = Client("leechbot", api_id=API_ID, api_hash=API_HASH,
             bot_token=BOT_TOKEN, in_memory=True)
aria = aria2p.API(aria2p.Client(host="http://localhost", port=6800, secret=""))


def bar(p, n=12):
    f = int(p / 100 * n)
    return "█" * f + "░" * (n - f)


def hsize(b):
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.2f} {u}"
        b /= 1024


def authorized(uid):
    return uid in OWNERS or OWNERS == [0]


@app.on_message(filters.command("start"))
async def start(_, m):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Help", callback_data="help"),
         InlineKeyboardButton("🔵 About", callback_data="about")],
        [InlineKeyboardButton("🟠 Owner", url="https://t.me/your_username"),
         InlineKeyboardButton("🔴 Close", callback_data="close")],
    ])
    await m.reply_text(
        f"👋 **Hello {m.from_user.mention}!**\n\n"
        "Mama Torrent/Magnet leech bot. Magnet ekak evanna:\n"
        "`/leech magnet:?xt=...`",
        reply_markup=kb)


@app.on_callback_query()
async def cb(_, q):
    d = q.data
    if d == "help":
        await q.answer()
        await q.message.edit_text(
            "📖 **Help**\n\n`/leech <magnet>` - download & upload\n"
            "Aliases: /qbleech1, /magent, /magnet",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back", callback_data="back")]]))
    elif d == "about":
        await q.answer()
        await q.message.edit_text(
            "🤖 Python + Pyrogram + aria2\nHosted on Koyeb",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back", callback_data="back")]]))
    elif d == "back":
        await q.answer()
        await q.message.edit_text("Use `/leech <magnet>`")
    elif d == "close":
        await q.message.delete()
    elif d.startswith("cancel:"):
        gid = d.split(":")[1]
        try:
            aria.remove([aria.get_download(gid)], force=True, files=True)
        except Exception:
            pass
        CANCELLED.add(gid)
        await q.answer("Cancelled ❌", show_alert=True)


CANCELLED = set()


async def safe_edit(msg, text, kb=None):
    try:
        await msg.edit_text(text, reply_markup=kb)
    except Exception:
        pass


@app.on_message(filters.command(["leech", "qbleech1", "magent", "magnet"]))
async def leech(_, m):
    if not authorized(m.from_user.id):
        return await m.reply_text("⛔ Not authorized.")
    text = m.text or ""
    if m.reply_to_message and m.reply_to_message.text:
        text += " " + m.reply_to_message.text
    found = re.search(r"magnet:\?\S+", text)
    if not found:
        return await m.reply_text("Magnet link ekak denna.")
    magnet = found.group(0)

    status = await m.reply_text("⏳ Metadata ganna...")
    dl = aria.add_magnet(magnet, options={"dir": DL_DIR})
    gid = dl.gid
    kb = lambda g: InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{g}")]])

    last = 0
    while True:
        await asyncio.sleep(3)
        if gid in CANCELLED:
            return await safe_edit(status, "❌ Cancelled.")
        try:
            d = aria.get_download(gid)
        except Exception:
            return await safe_edit(status, "❌ Download removed/failed.")
        if d.followed_by_ids:  # magnet metadata -> real download
            gid = d.followed_by_ids[0]
            continue
        if d.has_failed:
            return await safe_edit(status, f"❌ Error: {d.error_message}")
        if d.is_complete:
            break
        if time.time() - last > 5:
            last = time.time()
            await safe_edit(
                status,
                f"📥 **Downloading**\n`{d.name}`\n\n"
                f"[{bar(d.progress)}] {d.progress:.1f}%\n"
                f"📦 {d.completed_length_string()} / {d.total_length_string()}\n"
                f"⚡ {d.download_speed_string()}  ⏱ {d.eta_string()}\n"
                f"🌱 Seeders: {d.num_seeders} | Connections: {d.connections}",
                kb(gid))

    files = [f.path for f in d.files]
    await safe_edit(status, "✅ Download done. Uploading...")
    for path in files:
        path = str(path)
        parts = await split_if_needed(path)
        for p in parts:
            await upload(m, status, p)
    shutil.rmtree(os.path.join(DL_DIR, d.name), ignore_errors=True)
    for f in files:
        try:
            os.remove(f)
        except Exception:
            pass
    await safe_edit(status, "🎉 Complete!")


async def split_if_needed(path):
    if os.path.getsize(path) <= SPLIT:
        return [path]
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: subprocess.run(
        ["split", "-b", str(SPLIT), "-d", path, path + ".part"], check=True))
    os.remove(path)
    d = os.path.dirname(path)
    base = os.path.basename(path) + ".part"
    return sorted(os.path.join(d, x) for x in os.listdir(d) if x.startswith(base))


async def upload(m, status, path):
    name = os.path.basename(path)
    start_t = time.time()
    last = [0]

    async def prog(cur, total):
        if time.time() - last[0] < 5:
            return
        last[0] = time.time()
        pct = cur * 100 / total
        speed = cur / max(time.time() - start_t, 1)
        eta = int((total - cur) / max(speed, 1))
        await safe_edit(
            status,
            f"📤 **Uploading**\n`{name}`\n\n[{bar(pct)}] {pct:.1f}%\n"
            f"📦 {hsize(cur)} / {hsize(total)}\n⚡ {hsize(speed)}/s  ⏱ {eta}s")

    await m.reply_document(path, caption=f"`{name}`", progress=prog)


async def health(_):
    return web.Response(text="OK")


async def main():
    await app.start()
    web_app = web.Application()
    web_app.router.add_get("/", health)
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8000))).start()
    await idle()
    await app.stop()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
