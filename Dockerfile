# MenuCrous — bot Discord (menus CROUS + notes INSA)
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Europe/Paris \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
# Chromium « headless shell » (plus léger que le Chromium complet) + ses dépendances système,
# pour lire Mon Dossier Web (appli Vaadin, rendue en JavaScript).
# libatomic1 : requis par le Node embarqué de Playwright, absent de l'image slim.
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 fonts-dejavu-core \
 && pip install -r requirements.txt \
 && playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 1000 --create-home bot && mkdir -p /data && chown bot:bot /data
COPY --chown=bot:bot menucrous ./menucrous
USER bot

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=4).status==200 else 1)"

CMD ["python", "-m", "menucrous"]
