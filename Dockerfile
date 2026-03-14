FROM python:3.11-slim

RUN pip install uv

WORKDIR /app

COPY pyproject.toml .
COPY uv.lock* .

RUN uv sync --no-dev

COPY lloyd/ ./lloyd/

EXPOSE 8080

CMD ["uv", "run", "lloyd", "run"]
