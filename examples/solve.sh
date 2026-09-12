#!/bin/sh
# Example: request a Turnstile token via the relay API.
#
# Usage: ./solve.sh <url> <sitekey> [api_host]
# Example:
#   ./solve.sh https://example.com/login 0x4AAAAAAAxxxxxxxxxxxxxxxx 127.0.0.1

set -eu

URL="${1:?usage: solve.sh <url> <sitekey> [api_host]}"
SITEKEY="${2:?usage: solve.sh <url> <sitekey> [api_host]}"
API="${3:-127.0.0.1}"

curl -s -X POST "http://${API}:8081/solve" \
  -H 'Content-Type: application/json' \
  -d "{\"url\":\"${URL}\",\"sitekey\":\"${SITEKEY}\",\"timeout\":180}"
echo
