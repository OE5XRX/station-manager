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
# autobahn (pulled by daphne/channels) builds an optional NVX C extension from
# its sdist. This slim image has no compiler and NVX has no arm64 support, so
# the build fails; recent autobahn (>=26.x) then refuses to fall back to a
# pure-Python wheel unless AUTOBAHN_USE_NVX=0 is set. Force the pure-Python wheel.
RUN export AUTOBAHN_USE_NVX=0; \
    if [ "$DEV" = "true" ]; then \
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

# Pre-create /app/oidc_keys and hand it to appuser BEFORE the USER
# switch. The directory is a Docker named volume mount in production;
# Docker's first-mount logic copies the image's existing path (and its
# ownership) into the fresh volume, so this is the only place we get
# to set the right uid/gid. Without this, the volume comes up as
# root:root and setup_oidc_keys (running as appuser) hits
# PermissionError when writing the private key.
RUN mkdir -p /app/oidc_keys \
    && chown -R appuser:appuser /app/oidc_keys \
    && chmod 0700 /app/oidc_keys

USER appuser

EXPOSE 8000

CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "config.asgi:application"]
