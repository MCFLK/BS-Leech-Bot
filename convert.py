import os
import json
import time
import uuid
import shutil
import asyncio
from asyncio.subprocess import PIPE

from pyrogram import filters
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

POSTER_DIR = os.environ.get("POSTER_DIR", "/app/posters")
SPLIT = 1950 * 1024 * 1024
MARGIN = 150 * 1024 * 1024
VIDEO_EXT = (".mkv", ".mp4", ".avi", ".mov", ".webm", ".flv", ".ts", ".m4v", ".wmv", ".mpg", ".mpeg")

JOBS = {}


class Cancelled(Exception):
    pass


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


def cancel_kb(tid):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data=f"ccancel:{tid}")]])


def poster_path(uid):
    return os.path.join(POSTER_DIR, f"{uid}.img")


async def run(*args):
    p = await asyncio.create_subprocess_exec(*args, stdout=PIPE, stderr=PIPE)
    out, err = await p.communicate()
    return p.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")


async def probe(path):
    rc, out, err = await run("ffprobe", "-v", "error", "-print_format", "json",
                             "-show_streams", "-show_format", path)
    if rc != 0:
        raise RuntimeError("ffprobe failed: " + err[-200:])
    return json.loads(out)


def register(app, authorized, lock, dl_dir):
    async def edit(msg, text, kb=None):
        try:
            await msg.edit_text(text, reply_markup=kb)
        except Exception:
            pass

    # ───────────── /setposter  /delposter ─────────────
    async def setposter(_, m):
        if not m.from_user or not authorized(m.from_user.id):
            return
        src = m if (m.photo or m.document) else m.reply_to_message
        img = None
        if src:
            if src.photo:
                img = src
            elif src.document and (src.document.mime_type or "").startswith("image/"):
                img = src
        if not img:
            return await m.reply_text(
                "🖼 Photo ekakata reply karala `/setposter` danna "
                "(nathnam photo ekak caption eka `/setposter` karala yawanna).")
        os.makedirs(POSTER_DIR, exist_ok=True)
        await img.download(file_name=poster_path(m.from_user.id))
        await m.reply_text(
            "✅ Poster saved. Dan /changeformat karana videos walata me image eka thumbnail eka widihata yanawa.\n"
            "Ain karanna `/delposter`.")

    async def delposter(_, m):
        if not m.from_user or not authorized(m.from_user.id):
            return
        try:
            os.remove(poster_path(m.from_user.id))
            await m.reply_text("🗑 Poster removed.")
        except FileNotFoundError:
            await m.reply_text("Poster ekak set karala na.")

    app.add_handler(MessageHandler(setposter, filters.command("setposter")))
    app.add_handler(MessageHandler(delposter, filters.command("delposter")))

    # ───────────── cancel button ─────────────
    async def cancel_cb(_, q):
        if not authorized(q.from_user.id):
            return await q.answer("Not allowed", show_alert=True)
        job = JOBS.get(q.data.split(":", 1)[1])
        if not job:
            return await q.answer("Task eka natha", show_alert=True)
        job["cancel"] = True
        proc = job.get("proc")
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
        await q.answer("Cancelled ❌", show_alert=True)

    app.add_handler(CallbackQueryHandler(cancel_cb, filters.regex(r"^ccancel:")), group=1)

    # ───────────── /changeformat ─────────────
    async def changeformat(_, m):
        if not m.from_user:
            return
        if not authorized(m.from_user.id):
            return await m.reply_text("⛔ Not authorized.")
        r = m.reply_to_message
        media = r and (r.video or r.document or r.animation)
        if media:
            name = getattr(media, "file_name", None) or ""
            mime = getattr(media, "mime_type", None) or ""
            if r.document and not (mime.startswith("video/") or name.lower().endswith(VIDEO_EXT)):
                media = None
        if not media:
            return await m.reply_text("Video (mkv, avi wage) file ekakata reply karala `/changeformat` danna.")

        size = media.file_size or 0
        if size * 2 + MARGIN > shutil.disk_usage(dl_dir).free:
            return await m.reply_text("💽 Disk space madi na meka convert karanna (file size eka 2x ona).")

        tid = uuid.uuid4().hex[:8]
        job = {"cancel": False, "proc": None}
        JOBS[tid] = job
        status = await m.reply_text(
            "🕐 Queue eke inna..." if lock.locked() else "⏳ Starting...",
            reply_markup=cancel_kb(tid))

        async with lock:
            work = os.path.join(dl_dir, f"conv_{tid}")
            os.makedirs(work, exist_ok=True)
            try:
                await do_convert(m, r, status, job, tid, work)
            except Cancelled:
                await edit(status, "❌ Cancelled.")
            except Exception as e:
                await edit(status, f"❌ Error: `{e}`")
            finally:
                shutil.rmtree(work, ignore_errors=True)
                JOBS.pop(tid, None)

    async def do_convert(m, r, status, job, tid, work):
        def chk():
            if job["cancel"]:
                raise Cancelled()

        chk()
        # 1) download
        t0 = time.time()
        last = [0.0]

        async def dprog(cur, total):
            if job["cancel"]:
                app.stop_transmission()
            now = time.time()
            if now - last[0] < 4 and cur < total:
                return
            last[0] = now
            pct = cur * 100 / total
            sp = cur / max(now - t0, 1)
            await edit(status,
                       f"📥 **Downloading**\n\n[{bar(pct)}] {pct:.1f}%\n"
                       f"📦 {hsize(cur)} / {hsize(total)}\n⚡ {hsize(sp)}/s  ⏱ {hdur((total - cur) / max(sp, 1))}",
                       cancel_kb(tid))

        src = await r.download(file_name=work + "/", progress=dprog)
        chk()
        if not src:
            raise RuntimeError("Download fail una")

        # 2) decide copy vs re-encode
        await edit(status, "🔍 Analysing video...", cancel_kb(tid))
        info = await probe(src)
        streams = info.get("streams", [])
        v = next((s for s in streams if s.get("codec_type") == "video"), None)
        a = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if not v:
            raise RuntimeError("Video stream ekak hambune na")
        duration = float(info.get("format", {}).get("duration") or 0)

        v_copy = v.get("codec_name") == "h264" and v.get("pix_fmt") == "yuv420p"
        a_copy = (a is None) or a.get("codec_name") == "aac"
        mode = "Remuxing (quality loss na)" if (v_copy and a_copy) else "Re-encoding (x264/AAC)"

        base = os.path.splitext(os.path.basename(src))[0]
        out_path = os.path.join(work, base + ".mp4")
        cmd = ["ffmpeg", "-y", "-i", src, "-map", "0:v:0"]
        if a:
            cmd += ["-map", "0:a:0"]
        cmd += ["-c:v"] + (["copy"] if v_copy else
                           ["libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p"])
        if a:
            cmd += ["-c:a"] + (["copy"] if a_copy else ["aac", "-b:a", "192k", "-ac", "2"])
        cmd += ["-sn", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", out_path]

        # 3) convert
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
        job["proc"] = proc
        err_task = asyncio.create_task(proc.stderr.read())
        last_edit = 0.0
        t1 = time.time()
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            s = line.decode(errors="ignore").strip()
            if s.startswith("out_time_us=") or s.startswith("out_time_ms="):
                try:
                    us = int(s.split("=", 1)[1])
                except ValueError:
                    continue
                now = time.time()
                if now - last_edit > 5:
                    last_edit = now
                    done = us / 1e6
                    pct = min(done / duration * 100, 100) if duration else 0
                    speed = done / max(now - t1, 1)
                    eta = (duration - done) / max(speed, 0.01) if duration else 0
                    await edit(status,
                               f"⚙️ **{mode}**\n\n[{bar(pct)}] {pct:.1f}%\n"
                               f"🎞 {hdur(done)} / {hdur(duration)}\n⚡ {speed:.2f}x  ⏱ {hdur(eta)}",
                               cancel_kb(tid))
        await proc.wait()
        err = (await err_task).decode(errors="ignore")
        chk()
        if proc.returncode != 0 or not os.path.exists(out_path):
            raise RuntimeError("ffmpeg fail: " + err[-300:])

        out_size = os.path.getsize(out_path)
        if out_size > SPLIT:
            raise RuntimeError(
                f"Output file eka {hsize(out_size)}, Telegram 2GB limit eka wada loku. "
                "Quality adu karanna (crf eka 21 → 24) one.")

        # 4) thumbnail: poster if set, else a frame from the video
        thumb = os.path.join(work, "thumb.jpg")
        pp = poster_path(m.from_user.id)
        if os.path.exists(pp):
            await run("ffmpeg", "-y", "-i", pp, "-vf",
                      "scale=320:320:force_original_aspect_ratio=decrease", "-q:v", "5", thumb)
        if not os.path.exists(thumb):
            ts = str(min(5, duration / 2)) if duration else "1"
            await run("ffmpeg", "-y", "-ss", ts, "-i", out_path, "-frames:v", "1", "-vf",
                      "scale=320:320:force_original_aspect_ratio=decrease", "-q:v", "5", thumb)
        if not os.path.exists(thumb):
            thumb = None

        oinfo = await probe(out_path)
        ov = next((s for s in oinfo["streams"] if s.get("codec_type") == "video"), {})
        w, h = int(ov.get("width") or 0), int(ov.get("height") or 0)
        odur = int(float(oinfo.get("format", {}).get("duration") or duration or 0))

        # 5) upload
        t2 = time.time()
        last = [0.0]
        mp4name = os.path.basename(out_path)

        async def uprog(cur, total):
            if job["cancel"]:
                app.stop_transmission()
            now = time.time()
            if now - last[0] < 4 and cur < total:
                return
            last[0] = now
            pct = cur * 100 / total
            sp = cur / max(now - t2, 1)
            await edit(status,
                       f"📤 **Uploading**\n`{mp4name}`\n\n[{bar(pct)}] {pct:.1f}%\n"
                       f"📦 {hsize(cur)} / {hsize(total)}\n⚡ {hsize(sp)}/s  ⏱ {hdur((total - cur) / max(sp, 1))}",
                       cancel_kb(tid))

        await m.reply_video(out_path, caption=f"`{mp4name}`", duration=odur,
                            width=w, height=h, thumb=thumb,
                            supports_streaming=True, progress=uprog)
        chk()

        # 6) subtitles (text based only) -> .vtt
        if any(s.get("codec_type") == "subtitle" for s in streams):
            vtt = os.path.join(work, base + ".vtt")
            rc, _, _ = await run("ffmpeg", "-y", "-i", src, "-map", "0:s:0", vtt)
            if rc == 0 and os.path.exists(vtt) and os.path.getsize(vtt) > 0:
                await m.reply_document(vtt, caption="💬 Subtitles (.vtt)")

        await edit(status, "✅ Done! MP4 eka `/genlink` ekakin link ekak karaganna puluwan.")

    app.add_handler(MessageHandler(changeformat, filters.command(["changeformat", "convert", "mp4"])))
