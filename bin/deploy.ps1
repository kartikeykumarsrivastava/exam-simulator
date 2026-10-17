$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
if (!(Test-Path .env)) { throw 'Create .env from .env.example and configure your LOCALSTACK_AUTH_TOKEN first.' }
function Run-Docker { & docker @args; if ($LASTEXITCODE -ne 0) { throw "Docker command failed ($LASTEXITCODE)." } }
Run-Docker compose up -d --build localstack ollama ocr
Push-Location frontend
try {
  & npm ci
  if ($LASTEXITCODE -ne 0) { throw 'Frontend install failed.' }
  & npm run build
  if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
} finally { Pop-Location }
Run-Docker compose build api web
Run-Docker compose run --rm --no-deps -v "${PWD}:/workspace" -w /workspace api sh -c 'pip install --no-cache-dir awscli && python bin/provision.py'
Run-Docker compose exec ollama ollama pull nomic-embed-text
Run-Docker compose exec ollama ollama pull qwen2.5:7b
Run-Docker compose up -d api web
Write-Host 'Exam Studio: http://localhost:3000 | API docs: http://localhost:8000/docs'
