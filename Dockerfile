FROM python:3.11-slim

RUN pip install uv

WORKDIR /app

# Copy dependency files first for layer caching
COPY pyproject.toml .
COPY uv.lock* .

# Install dependencies only — skip building the lloyd package (source not copied yet)
RUN uv sync --no-dev --no-install-project

# Now copy source and install the project
COPY lloyd/ ./lloyd/
RUN uv sync --no-dev

EXPOSE 8080

CMD ["uv", "run", "lloyd", "run"]
