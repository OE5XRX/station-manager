FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# TARGETARCH is set automatically by BuildKit (amd64/arm64). The kernel
# package name + cosign binary suffix both follow that convention.
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev \
        libguestfs-tools \
        "linux-image-${TARGETARCH}" \
        ca-certificates \
        curl \
    && curl -fsSL "https://github.com/sigstore/cosign/releases/latest/download/cosign-linux-${TARGETARCH}" \
        -o /usr/local/bin/cosign \
    && chmod +x /usr/local/bin/cosign \
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

# Run as non-root user. Home dir is required because cosign writes its
# TUF trust-root cache under $HOME/.sigstore/root at first verify call.
RUN adduser --disabled-password --gecos '' appuser
USER appuser

EXPOSE 8000

CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "config.asgi:application"]
