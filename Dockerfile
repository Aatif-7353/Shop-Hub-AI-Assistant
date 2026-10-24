# syntax=docker/dockerfile:1
# ---- build stage: resolve + compile wheels once ----
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml ./
COPY common ./common
COPY store_api ./store_api
COPY orders_agent ./orders_agent
COPY orchestrator ./orchestrator
COPY chat_api ./chat_api
COPY evals ./evals
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

# ---- runtime stage: no compilers, non-root ----
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
RUN useradd --create-home --uid 10001 app
COPY --from=build /wheels /wheels
RUN pip install --no-index --find-links=/wheels shophub-assistant && rm -rf /wheels
USER app
WORKDIR /home/app
# One image, three services; compose picks the command and sets PORT.
ENV PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz',timeout=2)"
CMD ["sh", "-c", "uvicorn chat_api.main:app --host 0.0.0.0 --port ${PORT}"]
