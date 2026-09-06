#!/bin/sh
# Run csprpc without installing anything.
#   ./csprpc.sh doctor
#   ./csprpc.sh run
cd "$(dirname "$0")" || exit 1
exec /usr/bin/env python3 -m csprpc "$@"
