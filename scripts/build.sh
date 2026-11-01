#!/bin/sh
# buildctl-daemonless.sh (ephemeral rootless buildkitd), made to survive a busy server. CI's build steps call this
# with buildctl's own arguments: scripts/build.sh build --frontend dockerfile.v0 ...
# - Waits until the host has room before starting, instead of starting a build that thrashes the whole node
#   (2026-11-01). The server also runs plenty outside Kubernetes, so the scheduler can't see this; /proc/meminfo
#   in the pod shows the host's numbers.
# - Gives buildkitd ~2.5 minutes to come up instead of upstream's ~2 seconds. If it still doesn't, upstream prints
#   buildkitd's own log.
# BusyBox sh: the build job runs in moby/buildkit's rootless image.
set -eu

need_mb=${BUILD_MIN_AVAILABLE_MB:-4096}
max_wait_s=${BUILD_MAX_WAIT_S:-2400} # inside the runner's 1 h job timeout
waited=0
while :; do
	avail_mb=$(awk '/^MemAvailable:/ { print int($2 / 1024) }' /proc/meminfo)
	[ "$avail_mb" -ge "$need_mb" ] && break
	if [ "$waited" -ge "$max_wait_s" ]; then
		echo "the server is still short of memory after ${waited}s (${avail_mb} MB available, need ${need_mb} MB)," \
			"not building; re-run this job once it has room" >&2
		exit 1
	fi
	[ $((waited % 60)) -eq 0 ] &&
		echo "waiting for memory: ${avail_mb} MB available, need ${need_mb} MB (waited ${waited}s)"
	sleep 15
	waited=$((waited + 15))
done

export BUILDCTL_CONNECT_RETRIES_MAX="${BUILDCTL_CONNECT_RETRIES_MAX:-120}"
exec buildctl-daemonless.sh "$@"
