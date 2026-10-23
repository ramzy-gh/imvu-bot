#!/bin/sh
set -eu
mkdir -p /app/data/ramsei
chown bot:bot /app/data/ramsei
chmod 700 /app/data/ramsei
# Repair the old host configuration rather than continuing to save in /tmp.
case "${PERMISSIONS_PATH:-}" in
  /tmp/ramsei/permissions.json|/app/data/permissions.json)
    export PERMISSIONS_PATH=/app/data/ramsei/permissions.json ;;
esac
case "${SESSION_PATH:-}" in
  /tmp/ramsei/imvu-session.json|/app/data/imvu-session.json)
    export SESSION_PATH=/app/data/ramsei/imvu-session.json ;;
esac
exec setpriv --reuid=bot --regid=bot --init-groups "$@"
