FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PAPER_READER_LIBRARY_DIR=/data/library \
    PAPER_READER_HOST=0.0.0.0 \
    PAPER_READER_SECURE_COOKIES=1 \
    PORT=8080

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml ./
COPY paper_reader ./paper_reader

RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir .

RUN mkdir -p /data/library \
    && useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /data /app

USER appuser

EXPOSE 8080

# HTML/EPUB work out of the box. LaTeX needs latexml on the host.
# PDF: Docling (local) or MinerU Cloud (set MINERU_API_TOKEN).
CMD ["sh", "-c", "exec paper-reader --library --foreground --no-browser --host 0.0.0.0 --port ${PORT:-8080}"]
