#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -f .env ]; then echo 'Create .env from .env.example and configure LOCALSTACK_AUTH_TOKEN first.' >&2; exit 1; fi
docker compose up -d --build localstack ollama ocr
(cd frontend && npm ci && npm run build)
docker compose build api web
docker compose run --rm --no-deps -v "$PWD:/workspace" -w /workspace api sh -c 'pip install --no-cache-dir awscli && python bin/provision.py'
docker compose exec ollama ollama pull nomic-embed-text
docker compose exec ollama ollama pull qwen2.5:7b
docker compose up -d --build api web
