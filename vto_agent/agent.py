"""
ADK agent wrapping two Google image models as tools:

  * `virtual_try_on`  - Vertex AI `virtual-try-on-001`. Person + product
                        photo -> person wearing product.
  * `edit_image`      - Vertex AI Gemini "Nano Banana" image editing.
                        Image + natural-language instruction -> edited
                        image.

Both tools accept `gs://...` URIs OR artifact filenames OR "auto":
  * URIs are preferred (avoids the 8 MiB request-body limit on
    Agent Engine when running over the wire).
  * Artifact filenames work when uploaded via ADK Web (local dev).
  * "auto" walks recent artifacts as a fallback.

Run from the directory ABOVE this `vto_agent/` folder:
    adk web              # browser UI at http://localhost:8000
    adk run vto_agent    # CLI
"""

from __future__ import annotations

import os
import re
import sys
import traceback
import uuid
from typing import Optional

# ---------------------------------------------------------------------------
# Imports - wrapped so import-time errors surface (ADK's loader otherwise
# masks them as "no root_agent found").
# ---------------------------------------------------------------------------
try:
    from google import genai
    from google.genai import types
    from google.genai.types import (
        Image,
        Part,
        ProductImage,
        RecontextImageSource,
    )

    from google.adk.agents import Agent
    from google.adk.apps import App
    from google.adk.plugins.save_files_as_artifacts_plugin import (
        SaveFilesAsArtifactsPlugin,
    )
    from google.adk.tools import ToolContext
except Exception:
    traceback.print_exc()
    raise


# ---------------------------------------------------------------------------
# Model IDs
# ---------------------------------------------------------------------------
NANO_BANANA_MODEL = os.environ.get("NANO_BANANA_MODEL", "gemini-2.5-flash-image")


# ---------------------------------------------------------------------------
# Lazy Vertex client.
# ---------------------------------------------------------------------------
_genai_client: Optional["genai.Client"] = None


def _get_client() -> "genai.Client":
    global _genai_client
    if _genai_client is None:
        if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() not in (
            "true",
            "1",
            "yes",
        ):
            raise RuntimeError(
                "GOOGLE_GENAI_USE_VERTEXAI must be set to 'True'."
            )
        if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
            raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set.")
        _genai_client = genai.Client()
    return _genai_client


def _format_model_error(err: Exception, model_id: str) -> str:
    msg = str(err)
    if "was not found" in msg or "does not have access" in msg:
        return (
            f"Your GCP project doesn't have access to '{model_id}'. "
            f"This is usually because the model is allowlist-only "
            f"(preview). Switch to the GA model by setting "
            f"NANO_BANANA_MODEL=gemini-2.5-flash-image. "
            f"(Underlying error: {type(err).__name__}: {err})"
        )
    if "exceeds the limit" in msg or "payload size" in msg:
        return (
            "The image is too large to send inline. The web app should be "
            "uploading to GCS and passing gs:// URIs - check that the "
            "UPLOAD_BUCKET env var is set on the web tier. "
            f"(Underlying error: {type(err).__name__}: {err})"
        )
    return f"{type(err).__name__}: {err}"


# ---------------------------------------------------------------------------
# Helpers: resolve images from gs:// URIs OR artifacts.
# ---------------------------------------------------------------------------
_GS_URI_RE = re.compile(r"gs://[^\s,;'\"<>]+")


def _scan_gs_uris(text: str) -> list[str]:
    return _GS_URI_RE.findall(text or "")


async def _list_user_image_artifacts(
    tool_context: ToolContext,
) -> list[tuple[str, types.Part]]:
    """Non-result image artifacts in the session, newest first."""
    filenames = await tool_context.list_artifacts() or []
    images: list[tuple[str, types.Part]] = []
    for name in reversed(filenames):
        if name.startswith(("vto_result_", "edit_result_")):
            continue
        part = await tool_context.load_artifact(name)
        if part is None or part.inline_data is None:
            continue
        mime = (part.inline_data.mime_type or "").lower()
        if mime.startswith("image/"):
            images.append((name, part))
    return images


def _guess_mime_from_uri(uri: str) -> str:
    lo = uri.lower()
    if lo.endswith(".jpg") or lo.endswith(".jpeg"):
        return "image/jpeg"
    if lo.endswith(".webp"):
        return "image/webp"
    if lo.endswith(".gif"):
        return "image/gif"
    return "image/png"


async def _resolve_image_source(
    hint: str, tool_context: ToolContext
) -> tuple[Optional[str], Optional[Image]]:
    """Turn a user-supplied hint into (label, genai_types.Image).

    A hint can be:
      * a `gs://...` URI                  -> Image(gcs_uri=..., mime_type=...)
      * an artifact filename in session   -> Image(image_bytes=..., mime=...)
      * "auto" or unknown                 -> (None, None) (caller handles)
    """
    if not hint:
        return None, None
    if hint.startswith("gs://"):
        return hint, Image(gcs_uri=hint, mime_type=_guess_mime_from_uri(hint))
    if hint == "auto":
        return None, None
    all_names = await tool_context.list_artifacts() or []
    if hint in all_names:
        part = await tool_context.load_artifact(hint)
        if part is not None and part.inline_data is not None:
            return hint, Image(
                image_bytes=part.inline_data.data,
                mime_type=part.inline_data.mime_type or "image/png",
            )
    return None, None


async def _save_result_artifact(
    tool_context: ToolContext, filename: str, image_bytes: bytes, mime_type: str
) -> None:
    await tool_context.save_artifact(
        filename=filename,
        artifact=types.Part(
            inline_data=types.Blob(data=image_bytes, mime_type=mime_type),
        ),
    )


def _publish_result_to_gcs(
    image_bytes: bytes, mime_type: str, filename: str
) -> Optional[str]:
    """Upload generated image to OUTPUT_BUCKET so the front-end can fetch
    it. Returns the gs:// URI, or None if OUTPUT_BUCKET isn't configured
    (e.g. local `adk web` dev) - in which case the image is still saved
    as a session artifact and the front-end falls back to that path.
    """
    bucket = os.environ.get("OUTPUT_BUCKET")
    if not bucket:
        return None
    try:
        from google.cloud import storage

        client = storage.Client()
        blob = client.bucket(bucket).blob(f"results/{filename}")
        blob.upload_from_string(image_bytes, content_type=mime_type)
        return f"gs://{bucket}/results/{filename}"
    except Exception as e:
        # Don't fail the whole tool call just because the publish failed -
        # the artifact path still works.
        sys.stderr.write(f"WARN: result publish failed: {e}\n")
        return None


# ---------------------------------------------------------------------------
# Tool 1: virtual try-on
# ---------------------------------------------------------------------------
async def virtual_try_on(
    person_image: str,
    product_image: str,
    tool_context: ToolContext,
) -> dict:
    """Generate a photo of the person wearing the product.

    Both args may be `gs://...` URIs (preferred when running on Agent
    Engine), artifact filenames (local ADK Web uploads), or "auto" to fall
    back to the two most recent uploaded images in the session.

    Args:
        person_image: gs:// URI, artifact name, or "auto".
        product_image: gs:// URI, artifact name, or "auto".

    Returns:
        dict with `status`, `output_artifact`, `markdown`, and `message`.
    """
    try:
        client = _get_client()

        person_label, person_img = await _resolve_image_source(
            person_image, tool_context
        )
        product_label, product_img = await _resolve_image_source(
            product_image, tool_context
        )

        # Fallback to recent artifacts (local dev).
        if person_img is None or product_img is None:
            recent = await _list_user_image_artifacts(tool_context)
            if len(recent) < 2:
                return {
                    "status": "error",
                    "message": (
                        "I need a person photo and a clothing/product photo. "
                        "Either upload both images, or include their gs:// "
                        "URIs in your message."
                    ),
                }
            (latest_name, latest_part), (prev_name, prev_part) = recent[0], recent[1]
            mk = lambda p: Image(
                image_bytes=p.inline_data.data,
                mime_type=p.inline_data.mime_type or "image/png",
            )
            if person_img is None and product_img is None:
                person_label, person_img = prev_name, mk(prev_part)
                product_label, product_img = latest_name, mk(latest_part)
            elif person_img is None:
                person_label, person_img = (
                    (prev_name, mk(prev_part))
                    if latest_name == product_label
                    else (latest_name, mk(latest_part))
                )
            elif product_img is None:
                product_label, product_img = (
                    (prev_name, mk(prev_part))
                    if latest_name == person_label
                    else (latest_name, mk(latest_part))
                )

        response = client.models.recontext_image(
            model="virtual-try-on-001",
            source=RecontextImageSource(
                person_image=person_img,
                product_images=[ProductImage(product_image=product_img)],
            ),
        )
        if not response.generated_images:
            return {
                "status": "error",
                "message": (
                    "Model returned no images. Inputs may have been blocked "
                    "by safety filters - try different photos."
                ),
            }

        result = response.generated_images[0].image
        out_filename = f"vto_result_{uuid.uuid4().hex[:8]}.png"
        out_mime = result.mime_type or "image/png"
        await _save_result_artifact(
            tool_context, out_filename, result.image_bytes, out_mime
        )
        out_gs_uri = _publish_result_to_gcs(
            result.image_bytes, out_mime, out_filename
        )
        return {
            "status": "success",
            "output_artifact": out_filename,
            "output_gs_uri": out_gs_uri,
            "markdown": f"![Try-on result]({out_filename})",
            "message": (
                f"Generated try-on image. Result available at: "
                f"{out_gs_uri or out_filename}. "
                f"Used '{person_label}' as person, '{product_label}' as "
                f"product. To edit or refine this image in a follow-up "
                f"turn, pass this URI as source_image to edit_image or as "
                f"person_image/product_image to virtual_try_on."
            ),
        }
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        return {
            "status": "error",
            "message": _format_model_error(e, "virtual-try-on-001"),
        }


# ---------------------------------------------------------------------------
# Tool 2: edit_image (Nano Banana)
# ---------------------------------------------------------------------------
async def edit_image(
    edit_instruction: str,
    source_image: str,
    tool_context: ToolContext,
) -> dict:
    """Apply a generative edit to a single uploaded image (Nano Banana).

    `source_image` may be a `gs://...` URI, an artifact filename, or
    "auto" to use the most recent uploaded image. The instruction must be
    SPECIFIC - if the user is vague, ask before calling this tool.

    Args:
        edit_instruction: Concrete edit instruction
            (e.g. "change shoes to navy blue, keep everything else").
        source_image: gs:// URI, artifact filename, or "auto".

    Returns:
        dict with `status`, `output_artifact`, `markdown`, `message`.
    """
    try:
        client = _get_client()

        if not edit_instruction or not edit_instruction.strip():
            return {
                "status": "error",
                "message": "I need a specific edit instruction. What change?",
            }

        # Build the source Part to feed Nano Banana. Prefer gs:// URI; fall
        # back to inline bytes from an artifact.
        src_part: Optional[Part] = None
        src_label: Optional[str] = None

        if source_image and source_image.startswith("gs://"):
            src_label = source_image
            src_part = Part.from_uri(
                file_uri=source_image,
                mime_type=_guess_mime_from_uri(source_image),
            )
        else:
            # Artifact path
            if source_image and source_image != "auto":
                names = await tool_context.list_artifacts() or []
                if source_image in names:
                    p = await tool_context.load_artifact(source_image)
                    if p is not None and p.inline_data is not None:
                        src_label = source_image
                        src_part = Part(
                            inline_data=types.Blob(
                                data=p.inline_data.data,
                                mime_type=p.inline_data.mime_type or "image/png",
                            )
                        )
            if src_part is None:
                recent = await _list_user_image_artifacts(tool_context)
                if not recent:
                    return {
                        "status": "error",
                        "message": (
                            "No image to edit. Upload one or include its "
                            "gs:// URI in your message."
                        ),
                    }
                src_label, p = recent[0]
                src_part = Part(
                    inline_data=types.Blob(
                        data=p.inline_data.data,
                        mime_type=p.inline_data.mime_type or "image/png",
                    )
                )

        contents = [src_part, Part.from_text(text=edit_instruction)]

        response = client.models.generate_content(
            model=NANO_BANANA_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
            ),
        )

        out_bytes: Optional[bytes] = None
        out_mime = "image/png"
        for cand in response.candidates or []:
            if not cand.content or not cand.content.parts:
                continue
            for part in cand.content.parts:
                if part.inline_data and part.inline_data.data:
                    out_bytes = part.inline_data.data
                    out_mime = part.inline_data.mime_type or "image/png"
                    break
            if out_bytes:
                break

        if out_bytes is None:
            return {
                "status": "error",
                "message": (
                    f"'{NANO_BANANA_MODEL}' returned no image. The edit may "
                    f"have been blocked by safety filters, or the prompt was "
                    f"too vague. Try a more specific instruction."
                ),
            }

        out_filename = f"edit_result_{uuid.uuid4().hex[:8]}.png"
        await _save_result_artifact(tool_context, out_filename, out_bytes, out_mime)
        out_gs_uri = _publish_result_to_gcs(out_bytes, out_mime, out_filename)

        return {
            "status": "success",
            "output_artifact": out_filename,
            "output_gs_uri": out_gs_uri,
            "markdown": f"![Edited image]({out_filename})",
            "message": (
                f"Generated edited image. Result available at: "
                f"{out_gs_uri or out_filename}. "
                f"Edited '{src_label}' with: \"{edit_instruction}\". "
                f"To make further edits in a follow-up turn, pass this URI "
                f"as source_image to edit_image."
            ),
        }
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        return {
            "status": "error",
            "message": _format_model_error(e, NANO_BANANA_MODEL),
        }


# ---------------------------------------------------------------------------
# Agent + App
# ---------------------------------------------------------------------------
INSTRUCTION = """\
You help users with two image tasks. Inputs may arrive in three ways:
  (a) `gs://...` URIs mentioned in the user message (preferred when running
      on Agent Engine - the web app uploads to GCS and includes URIs here),
  (b) uploaded files saved as session artifacts (local ADK Web), or
  (c) implicit "the image they just uploaded".

When you see `gs://...` URIs in the conversation, pass those directly to
the tools as image arguments. Otherwise pass "auto" and the tool will pick
the most recent uploaded image.

IMPORTANT - reusing previous results: When a tool returns successfully,
its `message` field contains a `gs://...` URI for the result image. That
URI stays in your conversation context. If the user's next message asks
to modify, refine, or build on the previous result (e.g. "make it
darker", "now change the background", "try a different color"), pass
that URI as the source_image to edit_image (or as person_image /
product_image to virtual_try_on). You DO NOT need the user to re-upload
the image - the URI from the prior tool response IS the handle to it.

A) VIRTUAL TRY-ON - show a person wearing a clothing product.
   Use `virtual_try_on(person_image, product_image)`. Requires TWO images
   (a person and a clothing/product). If either is missing, ask for it.

B) IMAGE EDITING (Nano Banana) - apply a generative edit to one image.
   Use `edit_image(edit_instruction, source_image)`.

   Before calling `edit_image` make sure you have:
     1. A SPECIFIC instruction. If the user says "change the color"
        without naming one, ASK "Which color would you like?" and wait.
     2. An image to edit. This may be (i) a `gs://` URI the user just
        provided, (ii) a fresh upload, or (iii) the URI of a previous
        tool result from earlier in this conversation - all are valid
        sources. Only ask the user to upload a new image if NONE of
        these are available.

For BOTH tools: when they succeed, reply with a SHORT confirmation and
embed the generated image using the `markdown` field returned by the
tool, which looks like:  ![Edited image](edit_result_xxxxxxxx.png)

If a tool returns an error, explain it plainly and suggest a fix.
Keep replies short. Never echo base64 or raw bytes.
"""

try:
    root_agent = Agent(
        name="image_studio_agent",
        model="gemini-2.5-flash",
        description=(
            "Image studio: virtual try-on + Nano Banana editing. Accepts "
            "uploaded files or gs:// URIs."
        ),
        instruction=INSTRUCTION,
        tools=[virtual_try_on, edit_image],
    )

    app = App(
        name="image_studio_app",
        root_agent=root_agent,
        plugins=[SaveFilesAsArtifactsPlugin()],
    )
except Exception:
    traceback.print_exc()
    raise