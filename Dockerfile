FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/ requirements/

ARG DEV=false
RUN if [ "$DEV" = "true" ]; then \
      pip install --no-cache-dir -r requirements/dev.txt; \
    else \
      pip install --no-cache-dir -r requirements/prod.txt; \
    fi

COPY . .

# collectstatic needs a SECRET_KEY but we don't want to embed one
ARG DJANGO_SECRET_KEY=build-only-dummy-key
RUN DJANGO_SECRET_KEY=${DJANGO_SECRET_KEY} \
    python manage.py collectstatic --noinput --settings=config.settings.prod

# Run as non-root user
RUN adduser --disabled-password --no-create-home appuser
USER appuser

EXPOSE 8000

CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "config.asgi:application"]
