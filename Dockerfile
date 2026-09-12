# The HTTP transport for a hosted deployment, as scripts/deploy-gcp.sh builds it
# on Cloud Build. The stdio server does not need this: `pip install queenscoach`.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# The gcp extra adds the Firestore token store (CATS_TOKEN_STORE=firestore).
RUN pip install '.[gcp]' \
 && useradd --system --no-create-home --uid 10001 catsmcp

USER catsmcp

# Cloud Run delivers traffic to port 8080 by default. The server binds every
# interface because the platform's front end is the only way in; it still
# requires CATS_PUBLIC_URL, the Google client, and an allow list at runtime.
ENV CATS_TRANSPORT=http \
    CATS_HTTP_HOST=0.0.0.0 \
    CATS_HTTP_PORT=8080

EXPOSE 8080
CMD ["queenscoach"]
