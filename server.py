# RedzTok Web — واجهة ويب فوق الكود الأصلي (redztok.py) بدون أي تعديل عليه
import os, re, subprocess, threading, time, uuid, shutil, traceback
from flask import Flask, request, jsonify, send_file, abort

# على السيرفر ما في tkinter، فبنحط بديل فاضي عشان الملف الأصلي يتحمّل بدون تعديل
try:
    import tkinter  # noqa
except ImportError:
    import sys
    from unittest.mock import MagicMock
    for _m in ("tkinter", "tkinter.filedialog", "tkinter.messagebox", "tkinter.ttk"):
        sys.modules[_m] = MagicMock()

import redztok as core            # ← ملفك الأصلي (احفظه باسم redztok.py جنب هذا الملف)

FFMPEG = core.FFMPEG
HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "work")
os.makedirs(WORK, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_MB", "200")) * 1024 ** 2
JOBS = {}

QUALITY = {"high": ("12M", "15M", "24M"), "vhigh": ("20M", "25M", "40M"), "max": ("35M", "45M", "70M")}
SIZES = {"portrait": (1080, 1920), "landscape": (1920, 1080), "square": (1080, 1080)}


def get_duration(path):
    try:
        r = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True,
                           encoding="utf-8", errors="ignore", timeout=30)
        m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", r.stderr)
        if m:
            h, mi, s = m.groups()
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception:
        pass
    return 0


def run_job(job, o):
    log = lambda m: job["log"].append(m)
    try:
        d = job["dir"]
        src, temp1, out = job["src"], os.path.join(d, "step1_temp.mp4"), job["out"]
        dur = get_duration(src) or 60
        w, h = SIZES.get(o["aspect"], SIZES["portrait"])
        fps = o["fps"] if o["pure_frame"] else "30"

        vf = []
        if o["denoise"]: vf.append("hqdn3d=4:3:6:4.5")
        vf.append(f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=bicubic")
        vf.append(f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black")
        if o["sharpen"]: vf.append("cas=strength=0.3")
        if o["pure_frame"]:
            vf.append(f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1")
        vf.append("format=yuv420p")

        br, mx, bs = QUALITY.get(o["quality"], QUALITY["vhigh"])
        args = [FFMPEG, "-y", "-i", src, "-vf", ",".join(vf)]
        if not o["pure_frame"]: args += ["-r", fps]
        args += ["-c:v", "libx264", "-preset", "medium", "-b:v", br, "-maxrate", mx, "-bufsize", bs,
                 "-profile:v", "high", "-level", "4.2", "-pix_fmt", "yuv420p", "-color_range", "tv",
                 "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                 "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", temp1]
        args = args[:2] + ["-progress", "pipe:1", "-nostats"] + args[2:]

        log(f"FPS: {fps}  |  الأبعاد: {w}x{h}  |  الجودة: {br}")
        log("▶ FFmpeg re-encode...")
        errf = os.path.join(d, "ffmpeg_err.txt")
        with open(errf, "w") as ef:
            p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=ef, text=True, errors="ignore")
            for line in p.stdout:
                m = re.match(r"out_time_us=(\d+)", line.strip())
                if m and int(m.group(1)) > 0:
                    job["progress"] = min(int(m.group(1)) / 1e6 / dur * 85, 85)
            p.wait()
        if p.returncode != 0:
            log("✗ فشل FFmpeg")
            log(open(errf, errors="ignore").read()[-400:])
            job["state"] = "error"
            return
        log("✓ FFmpeg تم")

        if o["patch"]:
            log("▶ تطبيق الـ Patches...")
            job["progress"] = 90
            try:
                ok = core.apply_tiktok_patches(temp1, out, log_callback=log)
            except Exception as e:
                log(f"⚠ استثناء: {e}")
                ok = False
            if not ok:
                log("⚠ فشل الـ Patch — استخدام الملف الأصلي")
                shutil.copy(temp1, out)
        else:
            shutil.copy(temp1, out)

        try: os.remove(temp1)
        except Exception: pass
        log(f"✓ اكتمل — الحجم النهائي {os.path.getsize(out)/1024/1024:.1f} MB")
        job["progress"], job["state"], job["t"] = 100, "done", time.time()
    except Exception:
        log("✗ خطأ: " + traceback.format_exc()[-400:])
        job["state"] = "error"


# حماية بكلمة سر (اختيارية): حط APP_PASSWORD في Environment على Render
@app.before_request
def guard():
    pw = os.environ.get("APP_PASSWORD")
    if pw and (not request.authorization or request.authorization.password != pw):
        return ("Login required", 401, {"WWW-Authenticate": 'Basic realm="RedzTok"'})


def cleanup_loop():
    while True:
        time.sleep(600)
        for jid, j in list(JOBS.items()):
            if j["state"] != "running" and time.time() - j.get("t", 0) > 3600:
                shutil.rmtree(j["dir"], ignore_errors=True)
                JOBS.pop(jid, None)


threading.Thread(target=cleanup_loop, daemon=True).start()


@app.get("/")
def index():
    return send_file(os.path.join(HERE, "index.html"))


@app.post("/start")
def start():
    f = request.files.get("video")
    if not f or not f.filename:
        return jsonify(error="no file"), 400
    jid = uuid.uuid4().hex[:12]
    d = os.path.join(WORK, jid)
    os.makedirs(d)
    name, ext = os.path.splitext(os.path.basename(f.filename))
    src = os.path.join(d, "input" + (ext or ".mp4"))
    f.save(src)
    fm = request.form
    o = {"fps": fm.get("fps", "60") if fm.get("fps") in ("60", "120") else "60",
         "aspect": fm.get("aspect", "portrait"), "quality": fm.get("quality", "vhigh"),
         "pure_frame": fm.get("pure_frame") == "1", "sharpen": fm.get("sharpen") == "1",
         "denoise": fm.get("denoise") == "1", "patch": fm.get("patch") == "1"}
    job = {"dir": d, "src": src, "out": os.path.join(d, f"{name}_redztok.mp4"),
           "state": "running", "progress": 0, "log": ["بدأت المعالجة"], "name": f"{name}_redztok.mp4", "t": time.time()}
    JOBS[jid] = job
    threading.Thread(target=run_job, args=(job, o), daemon=True).start()
    return jsonify(id=jid)


@app.get("/status/<jid>")
def status(jid):
    j = JOBS.get(jid) or abort(404)
    return jsonify(state=j["state"], progress=j["progress"], log=j["log"])


@app.get("/download/<jid>")
def download(jid):
    j = JOBS.get(jid) or abort(404)
    if j["state"] != "done": abort(409)
    return send_file(j["out"], as_attachment=True, download_name=j["name"])


if __name__ == "__main__":
    print("افتح: http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, threaded=True)
