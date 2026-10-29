"""Windows-native media gateway for containerized Dash. NAS paths remain native Windows."""
from __future__ import annotations
import hashlib
import io
import os
import secrets
import sys
import tempfile
from pathlib import Path
from flask import Flask, Response, request, jsonify, send_file
from dotenv import load_dotenv
from waitress import serve

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT / "dashboard"))
from db import fetch_image_bytes, fetch_corpus_image_bytes  # noqa: E402

TOKEN = os.getenv("MEDIA_GATEWAY_TOKEN", "").strip()
if len(TOKEN) < 24:
    raise SystemExit("MEDIA_GATEWAY_TOKEN wajib minimal 24 karakter random di .env")
PORT = int(os.getenv("MEDIA_GATEWAY_PORT", "8052"))
THUMB_DIR = Path(os.getenv("THUMB_CACHE_DIR", str(ROOT / "thumb_cache")))
THUMB_SIZE = max(64, int(os.getenv("THUMB_MAX_SIZE", "300")))
QUALITY = min(90, max(40, int(os.getenv("THUMB_JPEG_QUALITY", "70"))))
app = Flask(__name__)

@app.before_request
def authenticate():
    if request.path == "/api/healthz":
        return None
    if not secrets.compare_digest(request.headers.get("X-GoldenEye-Gateway-Token", ""), TOKEN):
        return Response("Unauthorized", status=401)

@app.get("/api/healthz")
def healthz():
    return jsonify({"status": "UP", "service": "media-gateway"})

def missing_image():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="440"><rect width="100%" height="100%" fill="#f8fafc"/><text x="50%" y="50%" text-anchor="middle" fill="#64748b" font-family="Arial" font-size="22">Foto tidak tersedia</text></svg>'
    response = Response(svg, mimetype="image/svg+xml")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Golden-Eye-Image"] = "missing"
    return response

def thumbnail_path(key: str, kind: str):
    # include kind so corpus and reused cannot share a stale thumbnail by accident
    digest = hashlib.sha1((kind + ":" + str(key)).encode("utf-8",errors="replace")).hexdigest()
    return THUMB_DIR / (digest + ".jpg")

def serve_evidence(key: str, *, kind: str, thumbnail: bool, fetcher):
    if not key or len(key)>1024:
        return missing_image()
    path = thumbnail_path(key, kind) if thumbnail else None
    if path and path.is_file() and path.stat().st_size>0:
        return send_file(path, mimetype="image/jpeg", max_age=86400, conditional=True)
    row = fetcher(key)
    if not row:
        return missing_image()
    payload, mime, name = row
    raw = bytes(payload)
    if not thumbnail:
        response = Response(raw, mimetype=mime or "image/jpeg")
        response.headers["Cache-Control"]="private, max-age=3600"
        return response
    try:
        from PIL import Image
        image = Image.open(io.BytesIO(raw))
        image.thumbnail((THUMB_SIZE, THUMB_SIZE))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        out=io.BytesIO()
        image.save(out,format="JPEG",quality=QUALITY,optimize=True)
        data=out.getvalue()
        THUMB_DIR.mkdir(parents=True,exist_ok=True)
        # atomic writer protects against simultaneous requests for the same thumbnail
        temp_name=None
        try:
            with tempfile.NamedTemporaryFile(dir=THUMB_DIR,prefix=".tmp-ge-",suffix=".jpg",delete=False) as tmp:
                temp_name=tmp.name
                tmp.write(data)
            os.replace(temp_name,path)
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)
        response=Response(data,mimetype="image/jpeg")
        response.headers["Cache-Control"]="public,max-age=86400"
        return response
    except Exception as exc:
        print(f"[MEDIA THUMB WARN] {type(exc).__name__}",flush=True)
        return Response(raw,mimetype=mime or "image/jpeg")

@app.get("/api/image/<path:key>")
def image(key):
    return serve_evidence(key,kind="image",thumbnail=False,fetcher=fetch_image_bytes)

@app.get("/api/thumb/<path:key>")
def thumb(key):
    return serve_evidence(key,kind="thumb",thumbnail=True,fetcher=fetch_image_bytes)

@app.get("/api/corpus-thumb/<path:key>")
def corpus_thumb(key):
    return serve_evidence(key,kind="corpus-thumb",thumbnail=True,fetcher=fetch_corpus_image_bytes)

if __name__=="__main__":
    print(f"[MEDIA] Windows gateway serving port {PORT}. Access restricted by bearer token + Windows Firewall.",flush=True)
    serve(app,host="0.0.0.0",port=PORT,threads=6)
