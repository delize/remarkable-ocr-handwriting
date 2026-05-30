# docker-compose examples

Pick the one that matches your setup, copy `.env.example` to `.env` next to it
(`cp ../.env.example .env`), and run with `-f`. Each file is self-contained.

| File | Use when | Vault mount | Ollama reached via |
|---|---|---|---|
| [`compose.ghcr.yml`](compose.ghcr.yml) | **Default.** Pull the prebuilt image, transcripts to a separate volume | `:ro` | shared `ai` network |
| [`compose.build.yml`](compose.build.yml) | Dev / air-gapped — build the image locally | `:ro` | shared `ai` network |
| [`compose.alongside.yml`](compose.alongside.yml) | You want each `.md` next to its source PDF | `:rw` | shared `ai` network |
| [`compose.hostport.yml`](compose.hostport.yml) | Ollama isn't on a shared Docker network | `:ro` | `host.docker.internal:11434` |

Common to all:

```bash
# create the host dirs the volumes point at
mkdir -p /mnt/docker/rm-ocr/out /mnt/docker/rm-ocr/state
cp ../.env.example .env        # then edit paths/model if needed

docker compose -f compose.ghcr.yml up -d
docker compose -f compose.ghcr.yml logs -f rm-ocr
```

Update to the newest published image:

```bash
docker compose -f compose.ghcr.yml pull && docker compose -f compose.ghcr.yml up -d
```

The repo root also ships a ready-to-edit [`../docker-compose.yml`](../docker-compose.yml)
(the GHCR-pull variant) if you'd rather not use the `-f examples/...` form.
See the [main README](../README.md) for the full env-var reference and the three
output modes.
