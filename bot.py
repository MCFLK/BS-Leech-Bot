import os
import re
import time
import glob
import math
import uuid
import shutil
import asyncio

import aria2p
from aiohttp import web
from pyrogram import Client, filters, idle
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import filelink
import convert

# ───────────────────────── CONFIG ─────────────────────────
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNERS = {int(x) for x in os.environ.get("OWNER_IDS", "").replace(" ", "").split(",") if x}
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "telegram")

DL_DIR = os.environ.get("DL_DIR", "/app/downloads")
SPLIT = 1950 * 1024 * 1024          # Telegram 2GB limit (non-premium)
MARGIN = 150 * 1024 * 1024          # always keep this much disk free
META_WAIT = 300                     # seconds to wait for magnet metadata
SKIP_EXT = (".txt", ".nfo", ".url", ".html", ".htm")

app = Client("leechbot", api_id=API_ID, api_hash=API_HASH,
             bot_token=BOT_TOKEN, in_memory=True)
aria = aria2p.API(aria2p.Client(host="http://localhost", port=6800, secret=""))

QUEUE = asyncio.Semaphore(1)        # one job at a time
TASKS = {}


class Cancelled(Exception):
    pass


class Task:
    def __init__(self, tid):
        self.id = tid
        self.cancelled = False
        self.gid = None


# ───────────────────────── HELPERS ─────────────────────────
def bar(p, n=12):
    p = max(0, min(100, p))
    f = int(p / 100 * n)
    return "█" * f + "░" * (n - f)


def hsize(b):
    b = float(b)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024 or u == "TB":
            return f"{b:.2f} {u}"
        b /= 1024


def hdur(s):
    s = int(s)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}h {m}m {sec}s" if h else f"{m}m {sec}s"


def authorized(uid):
    return uid in OWNERS


def free_bytes():
    return shutil.disk_usage(DL_DIR).free


def needed(size):
    # file itself + one split part (if it will be split) + safety margin
    return size + (SPLIT if size > SPLIT else 0) + MARGIN


def check_cancel(task):
    if task.cancelled:
        raise Cancelled()


def cancel_kb(tid):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{tid}")]])


async def safe_edit(msg, text, kb=None):
    try:
        await msg.edit_text(text, reply_markup=kb)
    except Exception:
        pass


def drop_result(gid):
    try:
        aria.client.remove_download_result(gid)
    except Exception:
        pass


def kill(task):
    if not task.gid:
        return
    try:
        aria.remove([aria.get_download(task.gid)], force=True)
    except Exception:
        pass
    drop_result(task.gid)


def purge(keep=None):
    """Delete everything in DL_DIR (optionally keeping one file)."""
    keep = os.path.abspath(keep) if keep else None
    for name in os.listdir(DL_DIR):
        p = os.path.join(DL_DIR, name)
        if keep and os.path.abspath(p) == keep:
            continue
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
        except Exception:
            pass


# ───────────────────────── /start + buttons ─────────────────────────
START_TEXT = (
    "👋 **Hello {name}!**\n\n"
    "Mama Magnet leech + File-to-Link bot.\n\n"
    "🧲 `/leech magnet:?xt=...` - magnet download & upload\n"
    "🔗 File ekakata reply karala `/genlink` - direct link\n"
    "🎞 Video ekakata reply karala `/changeformat` - MP4 ekata convert"
)


def start_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Help", callback_data="help"),
         InlineKeyboardButton("🔵 About", callback_data="about")],
        [InlineKeyboardButton("🟠 Owner", url=f"https://t.me/{OWNER_USERNAME}"),
         InlineKeyboardButton("🔴 Close", callback_data="close")],
    ])


def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back")]])


@app.on_message(filters.command("start"))
async def start(_, m):
    await m.reply_text(START_TEXT.format(name=m.from_user.mention), reply_markup=start_kb())


@app.on_message(filters.command("id"))
async def my_id(_, m):
    await m.reply_text(f"Your ID: `{m.from_user.id}`")


@app.on_message(filters.command("disk"))
async def disk(_, m):
    if not authorized(m.from_user.id):
        return
    t, u, f = shutil.disk_usage(DL_DIR)
    await m.reply_text(f"💽 Total: {hsize(t)}\nUsed: {hsize(u)}\nFree: {hsize(f)}")


@app.on_callback_query()
async def cb(_, q):
    d = q.data
    if d == "help":
        await q.answer()
        await q.message.edit_text(
            "📖 **Help**\n\n"
            "`/leech <magnet>` - download & upload\n"
            "Aliases: /qbleech1, /magent, /magnet\n"
            "`/genlink` - file ekakata reply karanna, direct link ekak denawa\n"
            "`/changeformat` - video ekakata reply karanna, MP4 ekata convert karanawa\n"
            "`/setposter` - photo ekakata reply karanna, video thumbnail eka widihata\n"
            "`/delposter` - poster eka ain karanna\n"
            "`/disk` - free disk space\n`/id` - your Telegram ID\n\n"
            "Split una files join karanna: `cat file.mkv.* > file.mkv`",
            reply_markup=back_kb())
    elif d == "about":
        await q.answer()
        await q.message.edit_text("🤖 Python + Pyrogram + aria2\nHosted on Koyeb",
                                  reply_markup=back_kb())
    elif d == "back":
        await q.answer()
        await q.message.edit_text(START_TEXT.format(name=q.from_user.mention),
                                  reply_markup=start_kb())
    elif d == "close":
        await q.message.delete()
    elif d.startswith("cancel:"):
        if not authorized(q.from_user.id):
            return await q.answer("Not allowed", show_alert=True)
        task = TASKS.get(d.split(":", 1)[1])
        if not task:
            return await q.answer("Task eka natha", show_alert=True)
        task.cancelled = True
        kill(task)
        await q.answer("Cancelled ❌", show_alert=True)


# ───────────────────────── CORE ─────────────────────────
async def fetch_metadata(status, task, magnet):
    """Download only the .torrent metadata from the magnet."""
    d = aria.add_magnet(magnet, options={
        "dir": DL_DIR, "bt-metadata-only": "true", "bt-save-metadata": "true"})
    task.gid = d.gid
    t0 = time.time()
    while True:
        await asyncio.sleep(2)
        check_cancel(task)
        try:
            d = aria.get_download(task.gid)
        except Exception:
            check_cancel(task)
            raise RuntimeError("Metadata download removed")
        if d.has_failed:
            raise RuntimeError(d.error_message or "Metadata failed")
        if d.is_complete:
            break
        if time.time() - t0 > META_WAIT:
            kill(task)
            raise RuntimeError("Metadata timeout (peers/seeders na). Torrent eka dead wenna puluwan.")
        await safe_edit(
            status,
            f"🔎 **Getting metadata...** {int(time.time() - t0)}s\n"
            f"🔗 Connections: {d.connections}",
            cancel_kb(task.id))

    h = d.info_hash
    tpath = os.path.join(DL_DIR, f"{h}.torrent")
    if not os.path.exists(tpath):
        found = glob.glob(os.path.join(DL_DIR, "*.torrent"))
        if not found:
            raise RuntimeError(".torrent file eka save una na")
        tpath = found[0]
    drop_result(task.gid)
    task.gid = None
    return tpath


def list_files(tpath):
    """Read the file list from the .torrent (paused add, then remove)."""
    d = aria.add_torrent(tpath, options={"dir": DL_DIR, "pause": "true"})
    d = aria.get_download(d.gid)
    files = []
    for f in d.files:
        rel = os.path.relpath(str(f.path), DL_DIR)
        if "_____padding_file" in rel or rel.lower().endswith(SKIP_EXT):
            continue
        files.append((int(f.index), rel, int(f.length)))
    try:
        aria.remove([d], force=True)
    except Exception:
        pass
    drop_result(d.gid)
    purge(keep=tpath)
    return files


async def download_files(status, task, tpath, sel, title, rel=None):
    """sel = "3" (one file) or "1,2,5" (many). Returns path of the file if rel is given."""
    d = aria.add_torrent(tpath, options={
        "dir": DL_DIR, "select-file": sel,
        "seed-time": "0", "bt-stop-timeout": "900"})
    task.gid = d.gid
    last = 0
    shown = f"`{os.path.basename(rel)}`\n\n" if rel else "\n"
    while True:
        await asyncio.sleep(3)
        check_cancel(task)
        try:
            d = aria.get_download(task.gid)
        except Exception:
            check_cancel(task)
            raise RuntimeError("Download removed")
        if d.has_failed:
            raise RuntimeError(d.error_message or "Download failed")
        if d.is_complete:
            break
        if time.time() - last > 5:
            last = time.time()
            await safe_edit(
                status,
                f"📥 **{title}**\n{shown}"
                f"[{bar(d.progress)}] {d.progress:.1f}%\n"
                f"📦 {d.completed_length_string()} / {d.total_length_string()}\n"
                f"⚡ {d.download_speed_string()}  ⏱ {d.eta_string()}\n"
                f"🌱 Seeders: {d.num_seeders} | Connections: {d.connections}\n"
                f"💽 Free disk: {hsize(free_bytes())}",
                cancel_kb(task.id))

    try:
        aria.remove([d], force=True)
    except Exception:
        pass
    drop_result(task.gid)
    task.gid = None
    if not rel:
        return None
    path = os.path.join(DL_DIR, rel)
    if not os.path.exists(path):
        raise RuntimeError(f"File eka hambune na: {rel}")
    return path


async def send_doc(m, status, task, path, caption, label):
    t0 = time.time()
    last = [0.0]
    name = os.path.basename(path)

    async def prog(cur, total):
        if task.cancelled:
            app.stop_transmission()
        now = time.time()
        if now - last[0] < 4 and cur < total:
            return
        last[0] = now
        pct = cur * 100 / total
        speed = cur / max(now - t0, 1)
        eta = (total - cur) / max(speed, 1)
        await safe_edit(
            status,
            f"📤 **Uploading** {label}\n`{name}`\n\n"
            f"[{bar(pct)}] {pct:.1f}%\n"
            f"📦 {hsize(cur)} / {hsize(total)}\n"
            f"⚡ {hsize(speed)}/s  ⏱ {hdur(eta)}",
            cancel_kb(task.id))

    await m.reply_document(path, caption=caption, force_document=True, progress=prog)
    check_cancel(task)


def copy_chunk(src, dst, limit):
    left = limit
    with open(dst, "wb") as out:
        while left > 0:
            buf = src.read(min(16 * 1024 * 1024, left))
            if not buf:
                break
            out.write(buf)
            left -= len(buf)


async def upload_path(m, status, task, path, n, total):
    size = os.path.getsize(path)
    base = os.path.basename(path)
    if size <= SPLIT:
        await send_doc(m, status, task, path, f"`{base}`", f"({n}/{total})")
        return
    parts = math.ceil(size / SPLIT)
    with open(path, "rb") as src:
        for i in range(1, parts + 1):
            check_cancel(task)
            part = f"{path}.{i:03d}"
            await safe_edit(status, f"✂️ Splitting part {i}/{parts}...", cancel_kb(task.id))
            await asyncio.to_thread(copy_chunk, src, part, SPLIT)
            try:
                await send_doc(m, status, task, part,
                               f"`{base}.{i:03d}`  (part {i}/{parts})",
                               f"({n}/{total}, part {i}/{parts})")
            finally:
                try:
                    os.remove(part)
                except Exception:
                    pass


async def run_task(m, status, task, magnet):
    check_cancel(task)
    purge()
    tpath = await fetch_metadata(status, task, magnet)
    files = list_files(tpath)
    if not files:
        raise RuntimeError("Torrent eke download karanna files na")

    total = len(files)
    all_size = sum(length for _, _, length in files)
    done, skipped = 0, []

    if all_size + SPLIT + MARGIN <= free_bytes():
        # Disk eka madi: full torrent ekama ekapara download, passe eka eka upload
        sel = ",".join(str(i) for i, _, _ in files)
        await download_files(status, task, tpath, sel, "Downloading all files")
        for n, (idx, rel, length) in enumerate(files, 1):
            check_cancel(task)
            path = os.path.join(DL_DIR, rel)
            if not os.path.exists(path):
                skipped.append(os.path.basename(rel))
                continue
            await upload_path(m, status, task, path, n, total)
            try:
                os.remove(path)
            except Exception:
                pass
            done += 1
    else:
        # Disk madi na: file eka eka download -> upload -> delete
        for n, (idx, rel, length) in enumerate(files, 1):
            check_cancel(task)
            if needed(length) > free_bytes():
                skipped.append(f"{os.path.basename(rel)} ({hsize(length)})")
                continue
            path = await download_files(status, task, tpath, str(idx),
                                        f"Downloading ({n}/{total})", rel)
            await upload_path(m, status, task, path, n, total)
            purge(keep=tpath)
            done += 1

    msg = f"🎉 **Complete!** {done}/{total} files uploaded."
    if skipped:
        msg += "\n\n⚠️ Skip una:\n" + "\n".join(f"• `{s}`" for s in skipped[:10])
    await safe_edit(status, msg)


@app.on_message(filters.command(["leech", "qbleech1", "magent", "magnet"]))
async def leech(_, m):
    if not m.from_user:
        return
    if not authorized(m.from_user.id):
        return await m.reply_text(
            f"⛔ Not authorized.\nYour ID: `{m.from_user.id}`\n"
            "Meka `OWNER_IDS` env variable ekata danna.")
    text = m.text or ""
    if m.reply_to_message:
        text += " " + (m.reply_to_message.text or m.reply_to_message.caption or "")
    found = re.search(r"magnet:\?\S+", text)
    if not found:
        return await m.reply_text("Magnet link ekak denna.")
    magnet = found.group(0)

    tid = uuid.uuid4().hex[:8]
    task = Task(tid)
    TASKS[tid] = task
    status = await m.reply_text(
        "🕐 Queue eke inna..." if QUEUE.locked() else "⏳ Starting...",
        reply_markup=cancel_kb(tid))

    async with QUEUE:
        try:
            await run_task(m, status, task, magnet)
        except Cancelled:
            await safe_edit(status, "❌ Cancelled.")
        except Exception as e:
            await safe_edit(status, f"❌ Error: `{e}`")
        finally:
            kill(task)
            purge()
            TASKS.pop(tid, None)


# ───────────────────────── WEB + MAIN ─────────────────────────
async def health(_):
    return web.Response(text="OK")


async def wait_for_aria():
    for _ in range(30):
        try:
            aria.get_stats()
            return
        except Exception:
            await asyncio.sleep(1)
    raise RuntimeError("aria2 RPC start una na")


async def main():
    os.makedirs(DL_DIR, exist_ok=True)
    await wait_for_aria()
    await app.start()
    await filelink.prepare(app)

    web_app = web.Application()
    web_app.router.add_get("/", health)
    filelink.register(app, web_app, authorized)
    convert.register(app, authorized, QUEUE, DL_DIR)

    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8000))).start()
    print("Bot started")
    await idle()
    await app.stop()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
