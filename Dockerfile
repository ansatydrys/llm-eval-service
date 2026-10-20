FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install .

COPY alembic.ini ./
COPY migrations ./migrations
COPY data ./data

RUN useradd --create-home --uid 10001 eval
USER eval

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
