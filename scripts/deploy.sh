#!/usr/bin/env bash
# Build, push, and deploy the intent-router-hybrid to GKE with Helm.
#
# Usage:
#   PROJECT=my-project REGION=us-central1 AR_REPO=intent-router-hybrid \
#     TAG=v0.1.0 VALUES_FILE=helm/intent-router-hybrid/values-example.yaml \
#     ./scripts/deploy.sh
#
# Secrets are injected via --set (never committed):
#   EXTRA_SET_ARGS="--set secrets.typesafeApiKey=$TYPESAFE_API_KEY" ./scripts/deploy.sh
set -euo pipefail

PROJECT="${PROJECT:-my-gcp-project}"
REGION="${REGION:-us-central1}"
AR_REPO="${AR_REPO:-intent-router-hybrid}"
TAG="${TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M)}"
VALUES_FILE="${VALUES_FILE:-helm/intent-router-hybrid/values-example.yaml}"
IMAGE_REPO="$REGION-docker.pkg.dev/$PROJECT/$AR_REPO/intent-router-hybrid"
IMAGE="$IMAGE_REPO:$TAG"

echo "==> Building image ${IMAGE}"
docker build -t "$IMAGE" .

echo "==> Configuring docker auth for Artifact Registry"
gcloud auth configure-docker "$REGION-docker.pkg.dev" --quiet

echo "==> Pushing image"
docker push "$IMAGE"

echo "==> Deploying with Helm (release: intent-router-hybrid)"
# shellcheck disable=SC2086
helm upgrade --install intent-router-hybrid ./helm/intent-router-hybrid \
  --values "$VALUES_FILE" \
  --set "image.repository=$IMAGE_REPO" \
  --set "image.tag=$TAG" \
  ${EXTRA_SET_ARGS:-} \
  --wait

echo ""
echo "Deploy complete. Check rollout:"
echo "  kubectl get pods -l app.kubernetes.io/name=intent-router-hybrid"
echo "  kubectl port-forward svc/intent-router-hybrid 8080:8080"
