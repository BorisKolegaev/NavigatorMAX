FROM python:3.11-slim-bookworm

LABEL org.opencontainers.image.title="NavigatorMAX" \
      org.opencontainers.image.source="https://github.com/BorisKolegaev/NavigatorMAX"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/configuration \
    && chown app:app /app/configuration

# Include the project, database and configuration with credentials as provided.
COPY --chown=app:app . .

USER app

CMD ["python", "bot.py"]
