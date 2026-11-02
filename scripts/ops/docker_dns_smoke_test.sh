#!/usr/bin/env bash
set -euo pipefail

cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"

if [[ ! -f .env ]]; then
    echo "── Ensuring .env is available ──"
    poetry run python scripts/encryption/env_crypto.py ensure
fi

targets=("$@")
if [[ ${#targets[@]} -eq 0 ]]; then
    targets=(
        api.phylopic.org
        service.pdok.nl
        media.birdweather.com
        cds.climate.copernicus.eu
    )
fi

python_snippet=$'import socket\nimport sys\nhost = sys.argv[1]\ninfos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)\naddresses = sorted({item[4][0] for item in infos})\nprint(host + " -> " + ", ".join(addresses))'

for host in "${targets[@]}"; do
    echo "── Resolving ${host} ──"
    bash infrastructure/docker/compose.sh run --rm --no-deps \
        --entrypoint python ingestion -c "$python_snippet" "$host"
done