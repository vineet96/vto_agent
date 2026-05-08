"""Smoke-test the deployed image-studio agent."""
import asyncio
import base64
import os
import sys

import vertexai

PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
RESOURCE = os.environ["AGENT_ENGINE_RESOURCE_NAME"]


async def main():
    client = vertexai.Client(project=PROJECT, location=LOCATION)
    agent = client.agent_engines.get(name=RESOURCE)
    print("Available operations:", agent.operation_schemas())

    session = await agent.async_create_session(user_id="smoke-test")
    sid = session["id"] if isinstance(session, dict) else session.id
    print(f"Session: {sid}\n")

    prompt = sys.argv[1] if len(sys.argv) > 1 else "Hello! What can you do?"
    print(f"USER: {prompt}\n")

    img_n = 0
    async for ev in agent.async_stream_query(
        user_id="smoke-test", session_id=sid, message=prompt,
    ):
        author = ev.get("author", "?")
        for part in (ev.get("content") or {}).get("parts") or []:
            if part.get("text"):
                print(f"[{author}] {part['text']}")
            fc = part.get("function_call")
            if fc:
                print(f"[{author}] >> tool call: {fc.get('name')}({fc.get('args')})")
            fr = part.get("function_response")
            if fr:
                resp = fr.get("response") or {}
                print(f"[{author}] << tool result: status={resp.get('status')} msg={resp.get('message')}")
            inline = part.get("inline_data")
            if inline and inline.get("data"):
                data = inline["data"]
                if isinstance(data, (bytes, bytearray)):
                    blob = bytes(data)
                else:
                    blob = base64.b64decode(data)
                fn = f"out_{img_n}.png"
                with open(fn, "wb") as f:
                    f.write(blob)
                print(f"[{author}] [image saved -> {fn} ({len(blob)} bytes)]")
                img_n += 1


if __name__ == "__main__":
    asyncio.run(main())