FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-core \
        libreoffice-writer \
        libreoffice-calc \
        libreoffice-impress \
        fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt ./
# rapidocr-onnxruntime depends on opencv-python, which needs GUI system
# libraries (libGL, libglib, ...) we don't ship. Swap it for the headless
# variant — same `cv2` module, no GUI deps. Done as a post-install step
# because pip resolves the dep transitively from rapidocr.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y opencv-python \
    && pip install --no-cache-dir opencv-python-headless

# Warm RapidOCR's ONNX models at build time. The wheel ships the models
# bundled, but instantiating once here surfaces any install/runtime issue
# during build instead of on first ingest.
RUN python -c "from rapidocr_onnxruntime import RapidOCR; RapidOCR()"

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
COPY scripts ./scripts
RUN mkdir -p /srv/storage/uploads /srv/storage/logs /srv/db && chmod +x /srv/scripts/entrypoint.sh

EXPOSE 8889

ENTRYPOINT ["/srv/scripts/entrypoint.sh"]
