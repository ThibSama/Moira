#!/bin/sh
# Package 7q Lintian gate (acceptance criterion 12).
#
# Runs Lintian against the repository's own .deb in a PINNED disposable
# container and requires ZERO tags. "An unavailable local command is not
# a passing Debian gate" — the gate does not depend on a host lintian
# install; the environment is the pinned Debian bookworm image below.
#
# Reproducible from the repository:
#   1. the .deb is built by the repo's own scripts/build-deb.sh with a
#      PINNED SOURCE_DATE_EPOCH (the reproducibility gate's value);
#   2. the lintian environment is the pinned image
#      debian:bookworm-slim (lintian 2.116.3+deb12u1, recorded in the
#      gate log);
#   3. the mount directory lives under the repository ($HOME is required
#      for snap-docker bind mounts — /tmp mounts appear empty).
#
# Usage:
#   ./scripts/lintian-gate.sh            # docker from PATH
#   DOCKER=... ./scripts/lintian-gate.sh # custom docker command
#   sg docker -c './scripts/lintian-gate.sh'   # group-docker hosts
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
docker_cmd=${DOCKER:-docker}

# Pinned lintian environment. The image tag is fixed so the gate is
# reproducible; the installed lintian version is recorded in the log.
image="debian:bookworm-slim"
expected_lintian="2.116.3"

# Pinned build epoch: identical source + identical SOURCE_DATE_EPOCH →
# byte-identical .deb (same value as the reproducibility gate).
source_date_epoch="${SOURCE_DATE_EPOCH:-1754380800}"

version=$(sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' "$project_dir/src/moira/__init__.py")
case "$version" in
  [0-9]*.[0-9]*.[0-9]*)
    ;;
  *)
    echo "lintian-gate: cannot derive a valid version from src/moira/__init__.py" >&2
    exit 1
    ;;
esac

deb="$project_dir/dist/moira_${version}_all.deb"
mount_dir="$project_dir/dist/.lintian"
log="$project_dir/dist/lintian.log"

rm -rf "$mount_dir"
mkdir -p "$mount_dir"

echo "lintian-gate: building $deb (SOURCE_DATE_EPOCH=$source_date_epoch)"
SOURCE_DATE_EPOCH="$source_date_epoch" MOIRA_OUTPUT_DIR="$project_dir/dist" \
  "$project_dir/scripts/build-deb.sh" >/dev/null
[ -f "$deb" ] || {
  echo "lintian-gate: build produced no .deb at $deb" >&2
  exit 1
}
cp "$deb" "$mount_dir/moira.deb"

echo "lintian-gate: running lintian in pinned image $image"
set +e
"$docker_cmd" run --rm \
  -v "$mount_dir:/deb:ro" \
  "$image" sh -c '
    apt-get update -qq >/dev/null 2>&1 &&
    apt-get install -y -qq --no-install-recommends lintian >/dev/null 2>&1 &&
    lintian --version &&
    lintian /deb/moira.deb' > "$log" 2>&1
rc=$?
set -e

echo "----- lintian.log -----"
cat "$log"
echo "-----------------------"

if [ "$rc" -ne 0 ]; then
  echo "lintian-gate: FAILED — the pinned lintian environment did not run cleanly (exit $rc)" >&2
  exit 1
fi
if ! grep -q "$expected_lintian" "$log"; then
  echo "lintian-gate: FAILED — lintian version mismatch (expected $expected_lintian)" >&2
  exit 1
fi
if grep -Eq '^[EWIP]: ' "$log"; then
  echo "lintian-gate: FAILED — lintian reported tags (zero tags required)" >&2
  exit 1
fi

echo "lintian-gate: PASS — zero lintian tags (lintian $expected_lintian)"
rm -rf "$mount_dir"
