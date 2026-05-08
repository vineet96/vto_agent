"""Deploy the image-studio agent to Vertex AI Agent Engine.
 
Usage:
    # one-time setup
    gcloud auth application-default login
    gcloud services enable aiplatform.googleapis.com storage.googleapis.com
    gsutil mb -l us-central1 gs://YOUR_PROJECT_ID-agent-staging   # any name
 
    # required env
    export GOOGLE_CLOUD_PROJECT=your-project-id
    export GOOGLE_CLOUD_LOCATION=us-central1
    export STAGING_BUCKET=gs://YOUR_PROJECT_ID-agent-staging
 
    # deploy (run from the repo root, the dir that contains vto_agent/)
    python -m deploy.deploy_to_agent_engine
 
The script prints the deployed agent's resource name. Save it - the web app
needs it as the AGENT_ENGINE_RESOURCE_NAME env var.
"""

from __future__ import annotations

import os
import sys

import vertexai
from vertexai import agent_engines

# Import the App we already built. `app` has the agent + the
# SaveFilesAsArtifactsPlugin wired in.
from vto_agent.agent import root_agent

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
STAGING_BUCKET = os.environ.get("STAGING_BUCKET")
NANO_BANANA_MODEL = os.environ.get("NANO_BANANA_MODEL", "gemini-2.5-flash-image")
DISPLAY_NAME = os.environ.get("AGENT_DISPLAY_NAME", "Virtual Tryon Agent")
OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET")

def main() -> int:
    missing = [
        n
        for n, v in (
            ("GOOGLE_CLOUD_PROJECT", PROJECT),
            ("STAGING_BUCKET", STAGING_BUCKET),
        )
        if not v
    ]
    if missing:
        sys.stderr.write(f"Missing required env vars: {', '.join(missing)}\n")
        return 1

    print(f"Initialising Vertex client: project={PROJECT}, location={LOCATION}")
    client = vertexai.Client(project=PROJECT, location=LOCATION)

    # Wrap our root_agent in an AdkApp at deploy time. (We could also pass
    # `app` directly, but constructing AdkApp here means deployments don't
    # need the SaveFilesAsArtifactsPlugin to be registered as a plugin on
    # the App object - we can still pass plugins to AdkApp directly. But
    # for simplicity, plugins on the App work on Agent Engine via the new
    # path: importing `app` from vto_agent.agent below.)
    from vto_agent.agent import app as vto_agent_app  # noqa: F401

    # AdkApp is the canonical wrapper for ADK agents on Agent Engine.
    adk_app = agent_engines.AdkApp(agent=root_agent, enable_tracing=True)

    print(f"Creating reasoningEngine '{DISPLAY_NAME}' in {LOCATION}...")
    remote = client.agent_engines.create(
        agent=adk_app,
        config={
            "display_name": DISPLAY_NAME,
            "description": (
                "vto_agent_app: virtual try-on + Nano Banana editing."
            ),
            "requirements": [
                "google-cloud-aiplatform[agent_engines,adk]>=1.112",
                "google-adk>=1.15.0",
                "google-genai>=1.0.0",
                "google-cloud-storage>=2.16",
                "pydantic>=2.0",
                "cloudpickle>=3.0",
            ],
            "extra_packages": ["./vto_agent"],
            "staging_bucket": STAGING_BUCKET,
            "env_vars": {
                "NANO_BANANA_MODEL": NANO_BANANA_MODEL,
                # OUTPUT_BUCKET is where the tools publish generated images
                # so the web app can show them. If unset, the agent still
                # works but the web UI won't be able to render results.
                **({"OUTPUT_BUCKET": OUTPUT_BUCKET} if OUTPUT_BUCKET else {}),
            },
        },
    )

    print()
    print("=" * 70)
    print("DEPLOY SUCCESSFUL")
    print("=" * 70)
    print(f"Resource name: {remote.api_resource.name}")
    print()
    print("Save this for the web app:")
    print(f"  export AGENT_ENGINE_RESOURCE_NAME={remote.api_resource.name}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())