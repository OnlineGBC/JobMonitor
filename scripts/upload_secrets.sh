#!/bin/bash
# Reads .env and uploads every key=value to Google Secret Manager.
# Skips comments and blank lines.
# If the secret already exists, adds a new version.

ENV_FILE=".env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Run this script from the JobMonitor root folder."
  exit 1
fi

echo "Reading $ENV_FILE and uploading to Secret Manager..."
echo ""

while IFS= read -r line; do
  # Skip blank lines and comments
  [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue

  # Split on first '='
  key="${line%%=*}"
  value="${line#*=}"

  # Skip if key is empty
  [[ -z "$key" ]] && continue

  echo -n "  $key ... "

  # Try to create; if it already exists, add a new version instead
  if echo -n "$value" | gcloud secrets create "$key" --data-file=- --quiet 2>/dev/null; then
    echo "created"
  elif echo -n "$value" | gcloud secrets versions add "$key" --data-file=- --quiet 2>/dev/null; then
    echo "updated (new version)"
  else
    echo "FAILED"
  fi

done < "$ENV_FILE"

echo ""
echo "Done. Verify at: https://console.cloud.google.com/security/secret-manager"
