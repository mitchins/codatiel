FROM python:3.12-slim

# git for revision-bound checkouts, gh for read-only PR discovery.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && curl -fsSL --proto '=https' https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 codatiel
WORKDIR /app
RUN chown codatiel:codatiel /app \
 && mkdir -p /data/state /data/runs /data/work /data/feedback \
 && chown -R codatiel:codatiel /data
# Locked runtime dependencies first (docker/requirements-lock.txt pins
# every deployed version; S8544). The local tree provides codatiel_i2
# itself via --no-deps so the lock stays the single version authority.
COPY docker/requirements-lock.txt ./
RUN pip install --no-cache-dir --only-binary :all: -r requirements-lock.txt && pip cache purge
COPY pyproject.toml ./
COPY codatiel_i2 ./codatiel_i2
RUN pip install --no-cache-dir --no-deps --only-binary :all: . && pip cache purge
COPY docker/entrypoint.sh /usr/local/bin/codatiel-entrypoint.sh
RUN chmod +x /usr/local/bin/codatiel-entrypoint.sh

USER codatiel
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["codatiel-entrypoint.sh"]
CMD ["watcher", "daemon"]
HEALTHCHECK --interval=5m --timeout=60s --start-period=2m --retries=2 \
  CMD codatiel-i2 watcher status || exit 1
