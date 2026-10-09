FROM python:3.12-slim

# Keep dependency resolution and execution on uv; no pip is needed in the image.
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /uvx /bin/

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project

COPY app.py .
COPY planner ./planner
COPY scripts ./scripts
COPY supabase ./supabase
COPY data ./data
COPY templates ./templates
COPY .streamlit ./.streamlit
RUN uv sync --locked

RUN mkdir -p /app/data
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD uv run python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3)"
CMD ["uv", "run", "streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
