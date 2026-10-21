#!/bin/sh
# Run from the extracted source root. This changes no existing container/routes.
set -eu
case "$(pwd)" in /tmp/*|/opt/servicevoice/*|/home/ubuntu/*) ;; *) echo 'Use a dedicated extracted attempt directory' >&2; exit 2;; esac
[ ! -e phone-lab-out ] || { echo 'Fresh output directory required; previous evidence retained' >&2; exit 2; }
[ "$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)" -ge 768000 ] || { echo 'Insufficient headroom for bounded isolated image build' >&2; exit 2; }
nonce="$(date -u +%Y%m%d%H%M%S)-$$"
image="servicevoice-phone-lab:$nonce"
name="phone-lab-$nonce"
mkdir phone-lab-out
printf '%s\n' "$image" > phone-lab-out/image-tag.txt
printf '%s\n' "$name" > phone-lab-out/container-name.txt
cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  set +e
  python3 integrations/phone-bridge/lab/cleanup.py "$name" "$image" "$nonce" phone-lab-out
  cleaned=$?
  printf '{"command_exit":%s,"cleanup_exit":%s}\n' "$status" "$cleaned" > phone-lab-out/execution.json
  [ "$cleaned" -eq 0 ] || status=1
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM
# Legacy builder's resource flags are intentional; fail if unavailable.
DOCKER_BUILDKIT=0 timeout 300 docker build --memory=384m --memory-swap=384m --cpu-period=100000 --cpu-quota=50000 \
  -t "$image" -f integrations/phone-bridge/lab/Dockerfile . >phone-lab-out/build.log 2>&1
# No published ports, no network namespace other than loopback, no provider key.
# Keep the stopped container as evidence; exact identity cleanup also runs on failure.
timeout 65 docker run --name "$name" --label "servicevoice.phone_lab=$nonce" --network none --read-only --memory=256m --memory-swap=256m --cpus=.5 --pids-limit=128 \
  --cap-drop ALL --cap-add NET_RAW --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,noexec,size=128m --tmpfs /run:rw,nosuid,noexec,size=16m \
  -v "$(pwd)/phone-lab-out:/out:rw" "$image" >phone-lab-out/run.log 2>&1
cat phone-lab-out/result.json
