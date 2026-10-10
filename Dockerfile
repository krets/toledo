# Stamp stage: read the commit out of the checkout's .git and note the build
# time, so a plain `docker compose up --build` records them without any
# wrapper script. It only reads .git/HEAD and the ref it points at.
FROM alpine:3.20 AS stamp
COPY .git /git
RUN mkdir /out && \
    head="$(cat /git/HEAD 2>/dev/null || true)" && \
    case "$head" in \
      "ref: "*) ref="${head#ref: }"; \
                sha="$(cat "/git/$ref" 2>/dev/null || grep " $ref\$" /git/packed-refs 2>/dev/null | cut -d' ' -f1)" ;; \
      *) sha="$head" ;; \
    esac && \
    printf '%s\n%s\n' "$(echo "$sha" | cut -c1-7)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > /out/release

FROM python:3.11-slim

# Set up a non-root user
RUN useradd -m toledo
USER toledo
WORKDIR /home/toledo/app

# Pre-create configuration and task directories
RUN mkdir -p /home/toledo/.toledo/tasks

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY toledo toledo_db.py toledo_mcp.py toledo_server.py ./
COPY brief_collect.py brief_gcal.py brief_weather.py brief_state.py brief_build.py brief_scheduler.py ./
COPY static/ static/
COPY --from=stamp /out/release .release

# Ensure toledo script is executable
USER root
RUN chmod +x toledo
USER toledo

# Optional overrides for the commit + build time shown in the web UI and MCP
# instructions (scripts/release.sh sets them). Left empty, the stamp stage's
# .release file is used.
ARG TOLEDO_COMMIT=
ARG TOLEDO_BUILD_TIME=
ENV TOLEDO_COMMIT=$TOLEDO_COMMIT
ENV TOLEDO_BUILD_TIME=$TOLEDO_BUILD_TIME

# Default environment for data persistence
# (Home directory will be /home/toledo)
ENV HOME=/home/toledo

# Default command - can be overridden in docker-compose.yml
CMD ["python", "toledo_server.py"]
