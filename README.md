# Image Studio — ADK Agent + Web App

An ADK agent with two image capabilities, deployed to **Vertex AI Agent
Engine** and fronted by a **Cloud Run** chat web app.

| Capability | Model | Tool |
|---|---|---|
| Virtual try-on (person + clothing → person wearing clothing) | `virtual-try-on-001` | `virtual_try_on` |
| Generative image editing (color swap, object removal, etc.) | `gemini-2.5-flash-image` (Nano Banana) | `edit_image` |

The chatbot lets users upload a photo of a person plus a photo of a
garment (or just one photo to edit), and renders the generated result
inline in the chat.

**Iterative refinement works across turns.** Each tool's success message
embeds the `gs://` URI of the generated image, so on follow-up turns
the agent can chain edits without re-uploads — *"now make the shirt
darker"*, *"change the background to a beach"*, *"try a different
color"* all work, with the previous result becoming the source for the
next operation.

## Repo layout

```
.
├── vto_agent/                      # the ADK agent (deployed package)
│   ├── __init__.py
│   ├── agent.py                    # root_agent + 2 tools
│   └── .env.example
│
├── webapp/                         # Cloud Run frontend
│   ├── main.py                     # FastAPI server
│   ├── static/index.html           # chat UI
│   ├── Dockerfile
│   └── requirements.txt
│
├── deploy/
│   ├── deploy_to_agent_engine.py   # push agent → Agent Engine
│   ├── deploy_webapp.sh            # build & deploy Cloud Run
│   └── register_with_gemini_enterprise.sh   # optional: register in GE
│
├── README.md                       # this file
├── GEMINI_ENTERPRISE.md            # GE integration guide
├── requirements.txt                # for local `adk web`
└── .gcloudignore
```

## How the pieces fit together

```
  Browser  (static/index.html)
     │   multipart upload + SSE stream
     ▼
  Cloud Run  (webapp/main.py)
     │   1. Pillow downscale (in-memory) to <= MAX_UPLOAD_DIM
     │   2. Upload bytes -> gs://${UPLOAD_BUCKET}/uploads/<uuid>.png
     │   3. async_stream_query(message="<prompt>\n- gs://.../<file>")
     ▼
  Vertex AI Agent Engine  (reasoningEngine, deployed from vto_agent/)
     │   - Agent reads gs:// URIs from the message
     │   - Calls a tool with those URIs
     ▼
  Tools call Vertex AI image models:
     • virtual-try-on-001 via client.models.recontext_image
     • gemini-2.5-flash-image via client.models.generate_content
     │
     │   Tool publishes result PNG -> gs://${OUTPUT_BUCKET}/results/<uuid>.png
     │   Tool returns {output_gs_uri: "gs://..."} in its response
     ▼
  Cloud Run watches the streamed events for `output_gs_uri` in any
  function_response, fetches the bytes from GCS, and emits them as an
  SSE `image` event with a base64 data URL. The browser renders it
  inline beneath the agent's text reply.
```

Two important consequences of this architecture:

1. **Inputs go through GCS, not inline.** Agent Engine's `streamQuery`
   endpoint has an **8 MiB request body limit**. Phone photos plus base64
   inflation easily exceed that, so the web app uploads to GCS first and
   sends only the `gs://` URI as text.
2. **Outputs come back through GCS too.** ADK puts saved artifacts on the
   `event.actions.artifact_delta`, not on the streamed agent reply as
   `inline_data` parts. To render generated images reliably the tools
   also publish a copy to `gs://${OUTPUT_BUCKET}/results/...` and return
   the URI in their response, which the web app fetches and forwards.

## End-to-end deploy

### 0. One-time GCP setup

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID

gcloud services enable \
  aiplatform.googleapis.com \
  storage.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com

# Staging bucket for Agent Engine deploys (different from upload bucket below)
gsutil mb -l us-central1 gs://YOUR_PROJECT_ID-agent-staging
```

### 1. Deploy the agent to Agent Engine

```bash
export GOOGLE_CLOUD_PROJECT=your-project-id
export GOOGLE_CLOUD_LOCATION=us-central1
export STAGING_BUCKET=gs://YOUR_PROJECT_ID-agent-staging

# Bucket where the agent publishes generated try-on / edit images so the
# web app can display them. Reuse the same bucket as UPLOAD_BUCKET below
# if you only want one bucket for both inputs and outputs.
export OUTPUT_BUCKET=YOUR_PROJECT_ID-vto-uploads

pip install \
  "google-cloud-aiplatform[agent_engines,adk]>=1.112" \
  "google-adk>=1.15.0" \
  "google-genai>=1.0.0"

python -m deploy.deploy_to_agent_engine
```

The script prints a resource name like:

```
projects/123456789/locations/us-central1/reasoningEngines/<numeric-id>
```

Save it — the next two steps need it.

### 2. Build & deploy the Cloud Run chatbot

```bash
export AGENT_ENGINE_RESOURCE_NAME=projects/.../reasoningEngines/...
export UPLOAD_BUCKET=YOUR_PROJECT_ID-vto-uploads        # any unique name
# Optional: dedicated runtime SA. Defaults to the default compute SA.
# export RUNTIME_SA=image-studio-runner@your-project.iam.gserviceaccount.com

./deploy/deploy_webapp.sh
```

The script:

- Creates `gs://${UPLOAD_BUCKET}` if missing, with a **1-day lifecycle**
  so uploaded and generated images auto-delete after 24h.
- Grants the Cloud Run runtime SA `roles/aiplatform.user` (to call the
  reasoningEngine) and `objectAdmin` on the bucket.
- Grants the Vertex AI service agent (`service-<PROJECT_NUMBER>@gcp-sa-aiplatform.iam.gserviceaccount.com`)
  `objectAdmin` on the bucket — needed for the agent to both read input
  images and write generated results.
- Builds the container with Cloud Build and deploys to Cloud Run.
- Prints the public service URL.

Open the URL in a browser, attach images via the paperclip, and chat.

### 3. (Optional) Register in Gemini Enterprise

```bash
export GE_APP_ID=your-gemini-enterprise-app-id
export GE_APP_LOCATION=global   # or "us" / "eu" — must match your GE app

./deploy/register_with_gemini_enterprise.sh
```

See [GEMINI_ENTERPRISE.md](GEMINI_ENTERPRISE.md) for the full walkthrough.

## Required environment variables

Cumulative — set these before running each step.

| Step | Variable | Notes |
|---|---|---|
| All | `GOOGLE_CLOUD_PROJECT` | Your project ID. |
| All | `GOOGLE_CLOUD_LOCATION` | Region. Use `us-central1`; both VTO and Nano Banana support it. **Don't use `global`**. |
| Agent deploy | `STAGING_BUCKET` | **Full `gs://...` URL**, e.g. `gs://my-project-agent-staging`. The only variable that takes the prefix. |
| Agent deploy | `OUTPUT_BUCKET` | **Bare bucket name** (no `gs://`), where tools publish result images. |
| Agent deploy (optional) | `NANO_BANANA_MODEL` | Defaults to `gemini-2.5-flash-image` (GA). Override only if you have allowlist access to a preview model. |
| Webapp deploy | `AGENT_ENGINE_RESOURCE_NAME` | `projects/.../reasoningEngines/<id>` from step 1. |
| Webapp deploy | `UPLOAD_BUCKET` | **Bare bucket name** (no `gs://`) where the web app stages uploads. Same value as `OUTPUT_BUCKET` is fine. |
| Webapp deploy (optional) | `MAX_UPLOAD_DIM` | Pillow downscale max edge length, default `1536`. |
| Webapp deploy (optional) | `RUNTIME_SA` | Cloud Run runtime SA email. Defaults to the project's default compute SA. |
| GE registration | `GE_APP_ID` | App ID from Cloud Console → Gemini Enterprise → Apps. |
| GE registration | `GE_APP_LOCATION` | `global`, `us`, or `eu`. Most apps are `global`. |

## Local development

### Local `adk web`

```bash
pip install -r requirements.txt
cp vto_agent/.env.example vto_agent/.env   # fill in GOOGLE_CLOUD_PROJECT
adk web                                    # http://localhost:8000
```

In local mode, uploads come in via ADK's paperclip and become session
artifacts; the tools fall back to artifact-loading when no `gs://` URI
is provided. `OUTPUT_BUCKET` doesn't need to be set locally — the tools
silently skip the result-publish step and the artifact path provides
inline rendering.

### Run the web app locally against the deployed agent

```bash
pip install -r webapp/requirements.txt
export GOOGLE_CLOUD_PROJECT=...
export GOOGLE_CLOUD_LOCATION=us-central1
export AGENT_ENGINE_RESOURCE_NAME=projects/.../reasoningEngines/...
export UPLOAD_BUCKET=...
uvicorn webapp.main:app --reload --port 8080
```

## Testing the deployed agent

Quick smoke test (no UI):

```bash
python - <<'PY'
import asyncio, os, vertexai

client = vertexai.Client(
    project=os.environ["GOOGLE_CLOUD_PROJECT"],
    location=os.environ["GOOGLE_CLOUD_LOCATION"],
)
agent = client.agent_engines.get(name=os.environ["AGENT_ENGINE_RESOURCE_NAME"])

async def go():
    s = await agent.async_create_session(user_id="smoke")
    sid = s["id"] if isinstance(s, dict) else s.id
    async for ev in agent.async_stream_query(
        user_id="smoke", session_id=sid,
        message="Hello! What can you do?",
    ):
        for p in (ev.get("content") or {}).get("parts") or []:
            if p.get("text"): print(p["text"], end="")
    print()

asyncio.run(go())
PY
```

Healthy reply should describe the two capabilities and ask for images.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `service account info is missing 'email' field` | ADC fell through to the GCE metadata server. Run `gcloud auth application-default login` and `export GOOGLE_APPLICATION_CREDENTIALS=$HOME/.config/gcloud/application_default_credentials.json`. |
| `Environment variable name 'GOOGLE_CLOUD_PROJECT' is reserved` during deploy | Agent Runtime injects `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, and `GOOGLE_GENAI_USE_VERTEXAI` automatically — they cannot be passed in the deploy `env_vars` dict. The current `deploy_to_agent_engine.py` already handles this. |
| `The following requirements are missing: {'pydantic', 'cloudpickle'}` | Agent Engine pickles your `AdkApp` client-side and unpickles in the runtime, so it requires version-aligned packages declared explicitly. The current `deploy_to_agent_engine.py` lists both — make sure your local copy is up to date. |
| `No root_agent found for 'vto_agent'` when running `adk web` | `agent.py` failed to import. Run `python -c "import vto_agent; print(vto_agent.root_agent.name)"` from the parent dir to see the real traceback. |
| `Publisher Model … was not found or your project does not have access` | Preview model used without allowlist. Set `NANO_BANANA_MODEL=gemini-2.5-flash-image` (GA) in your `.env` or shell. |
| `Request payload size exceeds the limit: 8388608 bytes` | Image bytes were sent inline instead of via GCS. Confirm the web app has `UPLOAD_BUCKET` set and Pillow is installed. |
| Chatbot replies with text but no image | The agent isn't publishing results to GCS. Check that `OUTPUT_BUCKET` was set when you ran `deploy_to_agent_engine`, and that the Vertex AI service agent has `objectAdmin` on that bucket. |
| Agent says *"I can't access the previous result, please re-upload"* on a follow-up turn | The deployed agent doesn't surface previous-result URIs in tool messages. Pull the latest `vto_agent/agent.py` (the success messages now embed the `gs://` URI explicitly) and redeploy with `python -m deploy.deploy_to_agent_engine`. |
| `Permission denied on gs://...` during VTO/edit call | The Vertex AI service agent (`service-<PROJECT_NUMBER>@gcp-sa-aiplatform.iam.gserviceaccount.com`) needs `objectAdmin` on `UPLOAD_BUCKET`. The webapp deploy script grants this — re-run if you skipped. |
| `gs://gs://...` not valid (from `gsutil mb`) | You set `UPLOAD_BUCKET` or `OUTPUT_BUCKET` with a `gs://` prefix. Drop it — those variables take **bare bucket names**. Only `STAGING_BUCKET` takes a `gs://` URL. |
| `gcloud.builds.submit: unrecognized arguments: -f` | You're running an older copy of `deploy_webapp.sh`. The current script uses `gcloud builds submit … webapp` instead of `-f webapp/Dockerfile .` (the `-f` flag is a Docker convention; `gcloud builds submit` doesn't support it). Pull the latest script. |
| `storage.objects.get access … denied` during `builds submit` | Cloud Build's default identity (the project's compute SA, since the 2024 default-SA change) lacks `roles/storage.admin` and `roles/cloudbuild.builds.builder`. The current `deploy_webapp.sh` grants these — re-run, or grant manually with `gcloud projects add-iam-policy-binding`. |
| `Repository "cloud-run-source-deploy" not found` | The Artifact Registry repo `gcloud builds submit --tag` pushes to doesn't auto-create. The current `deploy_webapp.sh` runs `gcloud artifacts repositories create` — re-pull and re-run, or run `gcloud artifacts repositories create cloud-run-source-deploy --repository-format=docker --location=$GOOGLE_CLOUD_LOCATION` once manually. |
| `.: filename argument required` after `STATUS: SUCCESS` | Your local `deploy_webapp.sh` has an orphan `.` line left over from an old version of the build command. The build ran fine; bash interpreted the lone `.` as the `source` builtin. Pull the latest script or delete the orphan line yourself. |
| `User store … does not exist` during GE registration | The Gemini Enterprise app's multi-region isn't fully provisioned. Either set up a subscription in that region, or set `GE_APP_LOCATION=global` to match where your app actually lives. See [GEMINI_ENTERPRISE.md](GEMINI_ENTERPRISE.md). |

## Notes & gotchas

- **Each agent deploy creates a NEW reasoningEngine resource**, with a new
  numeric ID. Re-export `AGENT_ENGINE_RESOURCE_NAME` and re-run the
  webapp deploy and (if applicable) the GE registration after each agent
  redeploy. Both downstream scripts are idempotent — Cloud Run updates
  the existing service in place; GE registration patches by display name.
- **All generated images carry a SynthID watermark.**
- **Uploaded and generated photos auto-delete after 1 day** via the bucket
  lifecycle policy set up by `deploy_webapp.sh`.
- **The Cloud Run service deploys with `--allow-unauthenticated`** for
  demo simplicity. For production, gate it with **Cloud IAP** or remove
  the flag and use Cloud Run's built-in IAM.
- **Nano Banana 2 (`gemini-3.1-flash-image-preview`) is allowlist-only** on
  Vertex AI. The default `gemini-2.5-flash-image` (GA) works on every
  project; only override if you've been added to the preview allowlist.
