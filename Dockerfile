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
COPY toledo toledo_mcp.py toledo_server.py ./
COPY static/ static/

# Ensure toledo script is executable
USER root
RUN chmod +x toledo
USER toledo

# Default environment for data persistence
# (Home directory will be /home/toledo)
ENV HOME=/home/toledo

# Default command - can be overridden in docker-compose.yml
CMD ["python", "toledo_server.py"]
