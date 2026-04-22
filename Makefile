# Convenience targets for local dev and Azure Container Registry pushes.
#
# Required env vars for push:
#   ACR_REGISTRY   e.g. myorg.azurecr.io
#   IMAGE_TAG      e.g. v1, v1.0.0, $(git rev-parse --short HEAD)

ACR_REGISTRY ?= acraianalytic.azurecr.io
IMAGE_TAG    ?= latest

.PHONY: help up down build push release logs test

help:
	@echo "Targets:"
	@echo "  up        Start local stack (postgres + api)"
	@echo "  down      Stop local stack"
	@echo "  build     Build api image as $$ACR_REGISTRY/rag-for-2tb:$$IMAGE_TAG"
	@echo "  push      Push built api image to ACR (requires az acr login first)"
	@echo "  release   build + push in one go"
	@echo "  logs      Tail api logs"
	@echo "  test      Run pytest against the local codebase"
	@echo ""
	@echo "Example:"
	@echo "  az acr login --name myorg"
	@echo "  ACR_REGISTRY=myorg.azurecr.io IMAGE_TAG=v1 make release"

up:
	docker compose up --build

down:
	docker compose down

build:
	ACR_REGISTRY=$(ACR_REGISTRY) IMAGE_TAG=$(IMAGE_TAG) docker compose build api

push:
	ACR_REGISTRY=$(ACR_REGISTRY) IMAGE_TAG=$(IMAGE_TAG) docker compose push api

release: build push
	@echo "Pushed $(ACR_REGISTRY)/rag-for-2tb:$(IMAGE_TAG)"

logs:
	docker compose logs -f api

test:
	python -m pytest tests/ -v
