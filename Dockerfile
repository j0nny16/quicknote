FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    QUICKNOTES_DATA_DIR=/data \
    QUICKNOTES_CONFIG=/app/config.yaml

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && mkdir -p /data

# No EXPOSE: the bot dials out to Telegram, nothing listens.
ENTRYPOINT ["quicknotes"]
CMD ["run"]
