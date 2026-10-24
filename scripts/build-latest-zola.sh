#!/usr/bin/env bash
set -euo pipefail

# Resolve the latest stable Zola release on every Cloudflare Pages build.
latest_url=$(curl --connect-timeout 15 --max-time 300 -fsSLI -o /dev/null \
  -w '%{url_effective}' \
  https://github.com/getzola/zola/releases/latest)
tag=${latest_url##*/}
asset="zola-${tag}-x86_64-unknown-linux-musl.tar.gz"
tmpdir=$(mktemp -d)

curl --connect-timeout 15 --max-time 300 --retry 3 --retry-delay 2 -fsSL \
  "https://github.com/getzola/zola/releases/download/${tag}/${asset}" \
  -o "$tmpdir/zola.tar.gz"

tar -xzf "$tmpdir/zola.tar.gz" -C "$tmpdir" zola
"$tmpdir/zola" --version
"$tmpdir/zola" build "$@"
