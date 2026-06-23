FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VERSION=1.8.4 \
    POETRY_VIRTUALENVS_CREATE=false

WORKDIR /app

RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}"

COPY pyproject.toml poetry.lock* ./
RUN poetry install --no-interaction --no-ansi --only main --no-root

COPY roboto_guilliman ./roboto_guilliman
COPY whatsapp-integration ./whatsapp-integration
COPY discord-integration ./discord-integration
RUN poetry install --no-interaction --no-ansi --only main

EXPOSE 8080
CMD ["poetry", "run", "serve"]
