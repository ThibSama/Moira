#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

# Authoritative version source: src/moira/__init__.py::__version__.
# The artifact version is derived from it and never hardcoded here.
version=$(sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' "$project_dir/src/moira/__init__.py")
case "$version" in
  [0-9]*.[0-9]*.[0-9]*)
    ;;
  *)
    echo "build-deb: cannot derive a valid version from src/moira/__init__.py (got '$version')" >&2
    exit 1
    ;;
esac

# Static metadata must agree with the authoritative version; fail closed on
# any mismatch so a stale literal can never produce a mislabeled artifact.
pyproject_version=$(sed -n 's/^version = "\([^"]*\)"$/\1/p' "$project_dir/pyproject.toml")
control_version=$(sed -n 's/^Version: \(.*\)$/\1/p' "$project_dir/packaging/control")
if [ "$pyproject_version" != "$version" ]; then
  echo "build-deb: pyproject.toml version '$pyproject_version' != '__version__' '$version'" >&2
  exit 1
fi
if [ "$control_version" != "$version" ]; then
  echo "build-deb: packaging/control version '$control_version' != '__version__' '$version'" >&2
  exit 1
fi

stage="$project_dir/build/deb-root"
# Output directory is overridable so artifact tests can build into an
# isolated directory; the default is the repository-local dist/.
output_dir="${MOIRA_OUTPUT_DIR:-$project_dir/dist}"
output="$output_dir/moira_${version}_all.deb"

rm -rf "$stage"
mkdir -p "$stage/DEBIAN" "$stage/usr/bin" "$stage/usr/lib/moira" \
  "$stage/usr/share/applications" "$stage/usr/share/icons/hicolor/scalable/apps" \
  "$stage/usr/share/metainfo" "$output_dir"
cp "$project_dir/packaging/control" "$stage/DEBIAN/control"
cp -R "$project_dir/src/moira" "$stage/usr/lib/moira/"
find "$stage/usr/lib/moira" -type d -name __pycache__ -prune -exec rm -rf {} +
install -m 0755 "$project_dir/packaging/moira-launcher" "$stage/usr/bin/moira"
install -m 0755 "$project_dir/packaging/moira-claude-statusline" "$stage/usr/bin/moira-claude-statusline"
install -m 0755 "$project_dir/packaging/moira-agent-hook" "$stage/usr/bin/moira-agent-hook"
install -m 0644 "$project_dir/data/io.github.moira.QuotaMonitor.desktop" "$stage/usr/share/applications/"
install -m 0644 "$project_dir/data/io.github.moira.QuotaMonitor.svg" "$stage/usr/share/icons/hicolor/scalable/apps/"
install -m 0644 "$project_dir/data/io.github.moira.QuotaMonitor.metainfo.xml" "$stage/usr/share/metainfo/"
find "$stage" -type d -exec chmod 0755 {} +
find "$stage/usr/lib/moira" -type f -exec chmod 0644 {} +
dpkg-deb --root-owner-group --build "$stage" "$output"
echo "$output"
