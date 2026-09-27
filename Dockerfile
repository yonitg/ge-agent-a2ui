FROM python:3.12-slim

RUN pip install --no-cache-dir uv==0.8.13

WORKDIR /code

COPY ./pyproject.toml ./README.md ./uv.lock* ./

# Cache third-party dependencies layer
RUN uv sync --frozen --no-install-project

# Copy application source and catalog
COPY ./app ./app
COPY ./gemini_enterprise_composite_catalog.json ./

# Fast final project sync
RUN uv sync --frozen

# Run as an unprivileged user. The code and the virtualenv stay root-owned and
# read-only for the app; it only writes to /tmp (the reports file).
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app
USER 10001:10001
ENV HOME=/home/app PATH="/code/.venv/bin:$PATH"

EXPOSE 8080

# Start uvicorn from the virtualenv directly: `uv run` may try to re-sync the
# root-owned environment at startup.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
