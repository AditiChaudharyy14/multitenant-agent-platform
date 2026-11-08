FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

# Only the dependencies are installed into the image; the source is bind-mounted in
# development, so copying pyproject.toml alone keeps this layer cached across edits.
COPY pyproject.toml ./
RUN pip install --no-cache-dir ".[dev]"

# UID 1000 matches the usual WSL2 user, so generated files stay owned by the host user.
# /models is where the fastembed_cache named volume mounts; Docker copies this directory's
# ownership into the volume when it is first created, so uid 1000 can write the model there.
RUN useradd --uid 1000 --create-home app && mkdir /models && chown app:app /app /models
USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
