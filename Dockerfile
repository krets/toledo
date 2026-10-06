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

# Ensure toledo script is executable
USER root
RUN chmod +x toledo
USER toledo

# Release commit + build time shown in the web UI and MCP instructions;
# the image has no .git to read them from.
ARG TOLEDO_COMMIT=
ARG TOLEDO_BUILD_TIME=
ENV TOLEDO_COMMIT=$TOLEDO_COMMIT
ENV TOLEDO_BUILD_TIME=$TOLEDO_BUILD_TIME

# Default environment for data persistence
# (Home directory will be /home/toledo)
ENV HOME=/home/toledo

# Default command - can be overridden in docker-compose.yml
CMD ["python", "toledo_server.py"]
