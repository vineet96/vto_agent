FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Build context is the webapp/ directory itself (set by deploy_webapp.sh),
# so paths here are relative to webapp/ - NOT the repo root.
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY . /app/webapp

ENV PORT=8080
EXPOSE 8080

# Cloud Run sets $PORT; uvicorn picks it up.
CMD exec uvicorn webapp.main:app --host 0.0.0.0 --port ${PORT}