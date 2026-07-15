FROM python:3.12.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/tmp/home \
    TMPDIR=/tmp \
    XDG_CACHE_HOME=/tmp/.cache

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes --requirement requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app \
    && install -d -o app -g app -m 0750 /data

COPY --chown=app:app app/ ./app/
COPY --chown=app:app migrations/ ./migrations/
COPY --chown=app:app templates/ ./templates/
COPY --chown=app:app static/ ./static/
COPY --chown=app:app alembic.ini gunicorn.conf.py run.py ./
COPY entrypoint.sh /usr/local/bin/twitch-drops-entrypoint
RUN chmod 0755 /usr/local/bin/twitch-drops-entrypoint

USER app:app

EXPOSE 5000

ENTRYPOINT ["/usr/local/bin/twitch-drops-entrypoint"]
CMD ["gunicorn", "--config", "gunicorn.conf.py", "run:app"]
