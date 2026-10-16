FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/app/data

WORKDIR /app
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements-dev.txt

COPY collector ./collector
COPY tests ./tests

# Не запускаємо від root
RUN useradd --create-home --uid 1000 collector \
    && mkdir -p /app/data \
    && chown -R collector:collector /app
USER collector

CMD ["python", "-m", "collector", "run"]
