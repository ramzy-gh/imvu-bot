#!/bin/sh
set -eu
mkdir -p /app/data/ramsei
chown bot:bot /app/data/ramsei
chmod 700 /app/data/ramsei
exec setpriv --reuid=bot --regid=bot --init-groups "$@"
