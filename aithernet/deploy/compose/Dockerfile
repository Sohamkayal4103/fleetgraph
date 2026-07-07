# =============================================================================
# Aithernet hosted application image (control plane + ingestion + aithernet-hosted CLI).
# Tracked, maintained, multi-stage. Build context is the repository root.
#   docker build -f deploy/compose/Dockerfile -t aithernet/control-plane:latest .
# Installs Aithernet from the repository's package metadata (pyproject) plus the
# required PostgreSQL driver. Injects NO secrets at build time.
# =============================================================================

FROM python:3.12-slim AS build
WORKDIR /app
# Copy only what the build needs (package metadata + sources) for cache friendliness.
COPY pyproject.toml README.md ./
COPY src ./src
COPY services ./services
# The managed-component specs are force-included into the aithernet wheel (pyproject
# [tool.hatch.build.targets.wheel.force-include]); the wheel build needs them present.
COPY components ./components
# Build wheels for the app + its deps + the PostgreSQL driver into /wheels.
RUN pip install --no-cache-dir --upgrade pip wheel \
    && pip wheel --no-cache-dir --wheel-dir /wheels . psycopg2-binary

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# Dedicated unprivileged user + state dirs (matching the node/state layout).
RUN useradd --system --no-create-home --shell /usr/sbin/nologin aithernet \
    && mkdir -p /var/lib/aithernet/blobs /var/lib/aithernet/ingest-blobs /backups \
    && chown -R aithernet:aithernet /var/lib/aithernet /backups
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
USER aithernet
WORKDIR /app
EXPOSE 8100 8090
# Default entrypoint is the control plane; compose overrides `command:` for
# ingestion / aithernet-hosted CLI one-shots (migrate / bootstrap / backup).
CMD ["python", "-m", "services.control_plane"]
