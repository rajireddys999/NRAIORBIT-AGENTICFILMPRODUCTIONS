# The control room: `nraiorbit ui` as a Cloud Run service behind Identity-Aware Proxy (deploy/ui/README.md).
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN pip install --no-cache-dir -e ".[google,publish,ui]"

ENV PYTHONUNBUFFERED=1 UI_AUTH=on
EXPOSE 8080
CMD ["nraiorbit", "ui"]
