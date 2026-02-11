FROM python:3.13-slim AS base
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=mwader/static-ffmpeg:7.1 /ffmpeg /usr/local/bin/ffmpeg
COPY --from=mwader/static-ffmpeg:7.1 /ffprobe /usr/local/bin/ffprobe
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
ENV PYTHONWARNINGS="ignore::SyntaxWarning"
COPY narrator/ narrator/
COPY poc.py .
RUN uv run python -c "import nltk; nltk.download('punkt_tab', quiet=True)"
EXPOSE 8585
ENTRYPOINT ["uv", "run", "uvicorn", "narrator.app:app", "--host", "0.0.0.0", "--port", "8585"]
