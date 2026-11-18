FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/
COPY data/properties.db /app/data/properties.db
ENV DB_PATH=/app/data/properties.db

# Hugging Face Spaces routes to 7860; anything else can override PORT.
ENV PORT=7860
EXPOSE 7860

# Non-root, since the database is mounted read-only anyway.
RUN useradd -m runner && chown -R runner /app
USER runner

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health').read()"

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT}"]
