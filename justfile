# justfile — station-manager. `just --list` zeigt alle Recipes.
# Hosts/Secrets aus ungetrackter .env (Env-Override möglich).
set dotenv-load := true

cm4_host := env_var_or_default("CM4_HOST", "cm4-dev.local")

# Dev-Server hoch + Dev-Station seeden
dev-server:
    docker compose up -d web
    docker compose exec web python manage.py seed_dev_station

# Nur Dev-Station seeden (Config drucken)
seed:
    docker compose exec web python manage.py seed_dev_station

# Serial-Contract-Test gegen ein Ziel (CM4 default)
selftest host=cm4_host:
    scripts/dev-selftest.sh {{host}}

# Test-Suite
test:
    python -m pytest -q
