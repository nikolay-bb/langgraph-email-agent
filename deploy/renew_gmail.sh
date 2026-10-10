#!/bin/bash
# A new Gmail sign-in in the browser, then the new token goes to the server.
# Needed each 7 days while the Google OAuth app is in "Testing": Google ends
# the sign-in after 7 days. With the app "In production" this is not needed.
#
# Run it on your own computer, from the repo root, with your own values:
#   GCP_PROJECT=<project> GCP_ZONE=<zone> VM_NAME=<machine> bash deploy/renew_gmail.sh
#
# The server reads the token file at each Gmail call, so no restart is needed.
# The copy goes through Google's Identity-Aware Proxy, so the machine needs
# no SSH port open to the public internet.
set -e

: "${GCP_PROJECT:?set GCP_PROJECT to your Google Cloud project}"
: "${GCP_ZONE:?set GCP_ZONE to the zone of the machine}"
: "${VM_NAME:?set VM_NAME to the name of the machine}"
REMOTE_DIR="${REMOTE_DIR:-langgraph-email-agent}"

SECRETS=src/email_assistant/tools/gmail/.secrets

uv run python src/email_assistant/tools/gmail/setup_gmail.py

gcloud compute scp "$SECRETS/token.json" "$VM_NAME:$REMOTE_DIR/$SECRETS/token.json" \
    --project "$GCP_PROJECT" --zone "$GCP_ZONE" --tunnel-through-iap --quiet

echo "The new Gmail sign-in is on the machine."
