# Docker Usage

How to build, load, run, and use the `doc-extract` Docker image.

## Prerequisites

- Docker installed ([get Docker](https://docs.docker.com/get-docker/))
- Port **8889** free on the host

## Two ways to get the image

### A. Build from source

Run this in the project root (the folder with [Dockerfile](Dockerfile)):

```bash
docker build -t doc-extract:latest .
```

First build takes ~2–3 minutes (LibreOffice is ~800 MB). Subsequent rebuilds are incremental and fast.

### B. Load the pre-built `.tar`

If the repo ships with [docker-image/doc-extract.tar](docker-image/doc-extract.tar):

```bash
docker load -i docker-image/doc-extract.tar
```

You should see:

```
Loaded image: doc-extract:latest
```

## Run the container

The container listens on **port 8889** internally (set in the Dockerfile's `CMD`).

Foreground (stops when you Ctrl-C):

```bash
docker run --rm -p 8889:8889 doc-extract:latest
```

Background (detached) with a name so you can manage it:

```bash
docker run -d --rm --name doc-extract -p 8889:8889 doc-extract:latest
```

Verify it's up:

```bash
curl http://localhost:8889/health
# → {"status":"ok"}
```

## Use the API

### Extract one or more documents

The form field is `files` (you can repeat it). Response is always a JSON **array** — one item per uploaded file. Per-file failures are reported **inside** the list (as an `error` field on that item) and don't fail the whole request.

```bash
# Single file → 1-item list back
curl -F 'files=@/path/to/your/report.xlsx' http://localhost:8889/extract

# Multiple files → N-item list back, processed concurrently
curl -F 'files=@a.docx' -F 'files=@b.pdf' -F 'files=@c.xlsx' http://localhost:8889/extract
```

Success response:

```json
[
  {
    "filename": "report.xlsx",
    "file_type": "xlsx",
    "plain_text": "Data\nname\tvalue\nalpha\t1\nbeta\t2"
  }
]
```

Mixed response (one bad file in a batch):

```json
[
  { "filename": "good.docx", "file_type": "docx", "plain_text": "..." },
  { "filename": "bad.zip",   "error": "Unsupported file extension: .zip" }
]
```

Works for any supported extension: `.docx`, `.xlsx`, `.pptx`, `.docm`, `.xlsm`, `.pptm`, `.doc`, `.xls`, `.ppt`, `.pdf`, `.txt`, `.md`.

### Check supported formats

```bash
curl http://localhost:8889/supported
```

### Health check

```bash
curl http://localhost:8889/health
```

## Stop / remove

```bash
# if running in background:
docker stop doc-extract

# --rm (used above) auto-removes the container on exit.
# If you ran without --rm, remove it manually:
docker rm doc-extract
```

## Inspect / debug

```bash
# Tail logs from a running container:
docker logs -f doc-extract

# Open a shell inside the running container:
docker exec -it doc-extract /bin/bash

# See the image size and layers:
docker images doc-extract
docker history doc-extract:latest
```

## Change the host port

Default runs on host port 8889. To use, say, 9000:

```bash
docker run --rm -p 9000:8889 doc-extract:latest
# → now curl http://localhost:9000/extract
```

The container listens on **8889** internally (set in the Dockerfile's `CMD`); only the left side of `-p HOST:CONTAINER` freely changes. The right side must stay `8889` unless you also change the Dockerfile and rebuild.

## Re-export the image (after rebuilding)

```bash
docker save -o docker-image/doc-extract.tar doc-extract:latest
# Or compressed (~120 MB instead of 350 MB):
docker save doc-extract:latest | gzip > docker-image/doc-extract.tar.gz
```

The `docker-image/` folder is gitignored — ship the tar out-of-band (scp, S3, release asset, etc.).

## Resource notes

| Aspect | Value |
| --- | --- |
| Image size on disk | ~1.4 GB (uncompressed) |
| Exported `.tar` | ~350 MB |
| Compressed `.tar.gz` | ~120 MB |
| Baseline memory (idle) | ~80 MB |
| Peak memory (legacy conversion) | ~500 MB — LibreOffice is launched per request |

LibreOffice is the bulk of the image. If you never need `.doc/.xls/.ppt` support you can strip it from the Dockerfile, dropping the image to ~300 MB.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `address already in use` on port 8889 | Something else is using it | `docker run -p 9000:8889 ...` or stop the other process |
| `soffice failed: ...` when uploading `.doc/.xls/.ppt` | Corrupt or password-protected file | Confirm the file opens in LibreOffice / MS Office first |
| `413 File exceeds 100 MB limit` | File larger than `MAX_UPLOAD_MB` | Edit [app/config.py](app/config.py), rebuild the image |
| Build fails on `apt-get install libreoffice-*` | Network / mirror issue | Retry; or pin a specific Debian mirror in the Dockerfile |
| `docker: permission denied` | User not in `docker` group | `sudo usermod -aG docker $USER` then re-login |
