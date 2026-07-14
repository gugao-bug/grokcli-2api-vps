# grokcli-2api — single container with optional inline Turnstile Solver
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    GROK2API_HOST=0.0.0.0 \
    GROK2API_PORT=3000 \
    GROK2API_OPEN_BROWSER=0 \
    GROK2API_STORE_BACKEND=hybrid \
    GROK2API_WORKERS=2 \
    PYTHONPATH=/app/grok-build-auth \
    HOME=/root \
    DEBIAN_FRONTEND=noninteractive \
    # Inline local captcha defaults (same container)
    GROK2API_CAPTCHA_PROVIDER=local \
    CAPTCHA_PROVIDER=local \
    GROK2API_LOCAL_SOLVER_URL=http://127.0.0.1:5072 \
    LOCAL_SOLVER_URL=http://127.0.0.1:5072 \
    GROK2API_INLINE_SOLVER=1 \
    TURNSTILE_HOST=127.0.0.1 \
    TURNSTILE_PORT=5072 \
    TURNSTILE_THREAD=3 \
    TURNSTILE_BROWSER_TYPE=camoufox \
    TURNSTILE_LAZY=1 \
    TURNSTILE_IDLE_SEC=180

WORKDIR /app

# App tools + browser runtime libs for inline Turnstile Solver (Camoufox/Firefox)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fonts-liberation \
        fonts-noto-color-emoji \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libx11-6 \
        libx11-xcb1 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        libxshmfence1 \
        libxss1 \
        libxtst6 \
        tzdata \
        xvfb \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo Asia/Shanghai > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
COPY requirements-store.txt /app/requirements-store.txt
COPY turnstile-solver/requirements.txt /app/turnstile-solver-requirements.txt
RUN python -m pip install --no-cache-dir -U pip setuptools wheel \
    && python -m pip install --no-cache-dir -r /app/requirements.txt \
    && python -m pip install --no-cache-dir -r /app/requirements-store.txt \
    && python -m pip install --no-cache-dir -r /app/turnstile-solver-requirements.txt

# Prefetch browser binaries used by inline solver
RUN python -m camoufox fetch \
    && python -m patchright install chromium || true

COPY . /app
RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /app/turnstile-solver/logs /app/turnstile-solver/keys \
    && test -f /app/grok-build-auth/xconsole_client/client.py \
    && test -f /app/grok_build_adapter.py \
    && test -f /app/turnstile-solver/api_solver.py \
    && python -c "import grok_build_adapter, app; print('build-check', app.APP_VERSION, grok_build_adapter.ADAPTER_BUILD)"

EXPOSE 3000 5072

# data/ only for optional JSON import artifacts / models cache
VOLUME ["/app/data"]

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "app.py"]
