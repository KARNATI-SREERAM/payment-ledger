# Image for the FastAPI ledger service. Only requirements.txt and app/ go in (see .dockerignore).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Dependencies first: this layer is cached and only rebuilt when requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .
EXPOSE 8000
# ONE worker process on purpose: the rate limiter keeps its counters in this process's memory.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
