"""Cloud Run web app: thin chat UI in front of the deployed Agent Engine.

The browser POSTs to /api/chat with text + uploaded files. The server:

  1. Optionally downscales each upload to keep things fast.
  2. Uploads each upload to a GCS bucket (UPLOAD_BUCKET).
  3. Sends the agent a TEXT-ONLY message with the gs:// URIs embedded
     plus the user's prompt. This keeps the request well under the 8 MiB
     Agent Engine streamQuery payload limit.
  4. Streams the agent's events back as SSE; image artifacts (inline_data
     parts in the response) become `image` SSE events with data URLs.

Environment:
  GOOGLE_CLOUD_PROJECT          - required
  GOOGLE_CLOUD_LOCATION         - required (e.g. us-central1)
  AGENT_ENGINE_RESOURCE_NAME    - required, full
                                  projects/.../reasoningEngines/<id>
  UPLOAD_BUCKET                 - required, e.g. "my-project-uploads"
                                  (do NOT include the gs:// prefix)
  MAX_UPLOAD_DIM                - optional, default 1536. Larger images
                                  are downscaled in-memory before upload.
  PORT                          - injected by Cloud Run, default 8080
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import vertexai
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from google.cloud import storage

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webapp")

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
RESOURCE = os.environ.get("AGENT_ENGINE_RESOURCE_NAME")
UPLOAD_BUCKET = os.environ.get("UPLOAD_BUCKET")
MAX_DIM = int(os.environ.get("MAX_UPLOAD_DIM", "1536"))

_agent = None
_storage: Optional[storage.Client] = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _agent, _storage
    missing = [
        k
        for k, v in (
            ("GOOGLE_CLOUD_PROJECT", PROJECT),
            ("AGENT_ENGINE_RESOURCE_NAME", RESOURCE),
            ("UPLOAD_BUCKET", UPLOAD_BUCKET),
        )
        if not v
    ]
    if missing:
        raise RuntimeError(f"Missing env vars: {', '.join(missing)}")

    log.info("Connecting to Agent Engine: %s", RESOURCE)
    client = vertexai.Client(project=PROJECT, location=LOCATION)
    _agent = client.agent_engines.get(name=RESOURCE)
    _storage = storage.Client(project=PROJECT)
    log.info(
        "Ready. Bucket=%s MaxDim=%s Operations=%s",
        UPLOAD_BUCKET, MAX_DIM, _agent.operation_schemas(),
    )
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
def healthz():
    return {"ok": True, "agent": RESOURCE, "bucket": UPLOAD_BUCKET}


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static/index.html"))


app.mount(
    "/static",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
    name="static",
)


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")


# ---------------------------------------------------------------------------
# GCS upload helper
# ---------------------------------------------------------------------------
def _maybe_downscale(raw: bytes, max_dim: int) -> tuple[bytes, str]:
    """Downscale to fit max_dim on the longest side. Always returns JPEG
    if the input was a JPEG, PNG otherwise. Falls back to the original
    bytes if Pillow can't open the file."""
    try:
        from PIL import Image as PILImage  # local import: optional dep
    except ImportError:
        return raw, ""

    try:
        img = PILImage.open(io.BytesIO(raw))
    except Exception:
        return raw, ""

    fmt = (img.format or "PNG").upper()
    out_mime = "image/jpeg" if fmt in ("JPEG", "JPG") else "image/png"
    if max(img.size) <= max_dim:
        return raw, out_mime

    img.thumbnail((max_dim, max_dim))
    buf = io.BytesIO()
    if out_mime == "image/jpeg":
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=88, optimize=True)
    else:
        img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), out_mime


async def _upload_to_gcs(
    raw: bytes, content_type: str, original_name: str
) -> tuple[str, str]:
    """Upload bytes to GCS, return (gs://uri, mime_type)."""
    data, mime_after = _maybe_downscale(raw, MAX_DIM)
    mime = mime_after or content_type or "image/png"
    ext = ".jpg" if mime == "image/jpeg" else ".png"
    blob_name = f"uploads/{uuid.uuid4().hex}{ext}"
    bucket = _storage.bucket(UPLOAD_BUCKET)
    blob = bucket.blob(blob_name)
    blob.upload_from_string(data, content_type=mime)
    log.info(
        "Uploaded %s (%s) -> gs://%s/%s (%d bytes after rescale)",
        original_name, mime, UPLOAD_BUCKET, blob_name, len(data),
    )
    return f"gs://{UPLOAD_BUCKET}/{blob_name}", mime


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
@app.post("/api/session")
async def create_session(user_id: str = Form(...)):
    if _agent is None:
        raise HTTPException(503, "agent not ready")
    session = await _agent.async_create_session(user_id=user_id)
    sid = session.get("id") if isinstance(session, dict) else getattr(session, "id", None)
    return {"session_id": sid}


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
@app.post("/api/chat")
async def chat(
    user_id: str = Form(...),
    session_id: str = Form(...),
    message: str = Form(""),
    files: list[UploadFile] = File(default_factory=list),
):
    if _agent is None or _storage is None:
        raise HTTPException(503, "agent not ready")

    # 1. Upload each file to GCS, collect gs:// URIs.
    uploaded: list[tuple[str, str]] = []  # (gs_uri, original_name)
    for f in files or []:
        raw = await f.read()
        if not raw:
            continue
        gs_uri, _mime = await _upload_to_gcs(raw, f.content_type or "", f.filename or "img")
        uploaded.append((gs_uri, f.filename or "img"))

    # 2. Build a TEXT message that includes the URIs, so the agent can
    #    pass them to its tools. The full bytes never go through Agent
    #    Engine's HTTP endpoint - just short URI strings.
    if uploaded and message:
        prompt = (
            f"{message}\n\n"
            f"Uploaded images (gs:// URIs you can pass to the tools):\n"
            + "\n".join(f"- {uri}  (filename: {name})" for uri, name in uploaded)
        )
    elif uploaded:
        prompt = (
            "Uploaded images (gs:// URIs you can pass to the tools):\n"
            + "\n".join(f"- {uri}  (filename: {name})" for uri, name in uploaded)
        )
    elif message:
        prompt = message
    else:
        raise HTTPException(400, "empty message")

    log.info("Sending to agent (user=%s session=%s, %d uri(s))",
             user_id, session_id, len(uploaded))

    async def event_stream():
        emitted_uris: set[str] = set()  # avoid double-emit if URI appears twice

        def _fetch_and_emit_gs(uri: str):
            """Pull bytes from GCS and yield as SSE image event."""
            try:
                if not uri.startswith("gs://"):
                    return None
                if uri in emitted_uris:
                    return None
                emitted_uris.add(uri)
                # gs://bucket/path/to/file.png
                _, _, rest = uri.partition("gs://")
                bucket_name, _, blob_path = rest.partition("/")
                blob = _storage.bucket(bucket_name).blob(blob_path)
                raw = blob.download_as_bytes()
                mime = blob.content_type or "image/png"
                b64 = base64.b64encode(raw).decode("ascii")
                return _sse(
                    "image",
                    {
                        "mime_type": mime,
                        "data_url": f"data:{mime};base64,{b64}",
                        "source_uri": uri,
                    },
                )
            except Exception as fe:
                log.exception("failed to fetch result image %s", uri)
                return _sse(
                    "error",
                    {"message": f"Could not fetch result image: {fe}"},
                )

        try:
            async for event in _agent.async_stream_query(
                user_id=user_id,
                session_id=session_id,
                message=prompt,
            ):
                content = event.get("content") or {}
                for part in content.get("parts") or []:
                    text = part.get("text")
                    inline = part.get("inline_data")
                    fn_resp = part.get("function_response")

                    if text:
                        yield _sse(
                            "text",
                            {"author": event.get("author"), "text": text},
                        )

                    # Image arrived inline (rare but possible)
                    elif inline and inline.get("data"):
                        data = inline["data"]
                        if isinstance(data, (bytes, bytearray)):
                            data = base64.b64encode(data).decode("ascii")
                        mime = inline.get("mime_type") or "image/png"
                        yield _sse(
                            "image",
                            {
                                "mime_type": mime,
                                "data_url": f"data:{mime};base64,{data}",
                            },
                        )

                    # Tool result - check for output_gs_uri so we can fetch
                    # the generated image and stream it to the browser.
                    elif fn_resp:
                        resp = fn_resp.get("response") or {}
                        gs_uri = resp.get("output_gs_uri")
                        if gs_uri:
                            evt = _fetch_and_emit_gs(gs_uri)
                            if evt is not None:
                                yield evt
            yield _sse("done", {})
        except Exception as e:
            log.exception("stream failed")
            yield _sse("error", {"message": f"{type(e).__name__}: {e}"})

    return StreamingResponse(event_stream(), media_type="text/event-stream")