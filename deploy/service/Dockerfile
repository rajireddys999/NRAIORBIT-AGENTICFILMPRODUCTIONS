# The always-on runner: `nraiorbit service tick` as a Cloud Run job (CPU only; the paid models are APIs).
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libegl1 libgles2 libgl1 curl gnupg ca-certificates \
    && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" > /etc/apt/sources.list.d/google-cloud-sdk.list \
    && apt-get update && apt-get install -y --no-install-recommends google-cloud-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY library ./library
COPY projects/_template ./projects/_template
RUN pip install --no-cache-dir -e ".[google,lipsync,publish]" requests

# the Telugu card font
RUN mkdir -p /usr/local/share/fonts/noto-telugu && cd /usr/local/share/fonts/noto-telugu \
    && curl -fsSL "https://fonts.googleapis.com/css2?family=Noto+Serif+Telugu:wght@700" -A "Mozilla/5.0" \
       | grep -o "https://[^)]*\.ttf" | head -1 | xargs -r curl -fsSL -o NotoSerifTelugu-Bold.ttf || true

ENV PYTHONUNBUFFERED=1 NRAIORBIT_PROJECTS=/work/projects
VOLUME ["/work"]
ENTRYPOINT ["nraiorbit", "service", "tick"]
