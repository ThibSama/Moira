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

# Policy files that must exist; the build fails closed when any is absent.
policy_sources="packaging/control packaging/copyright packaging/changelog \
packaging/man/moira.1 packaging/man/moira-claude-statusline.1 packaging/man/moira-agent-hook.1"
for source in $policy_sources; do
  [ -f "$project_dir/$source" ] || {
    echo "build-deb: missing required packaging source '$source'" >&2
    exit 1
  }
done

stage="$project_dir/build/deb-root"
# Output directory is overridable so artifact tests can build into an
# isolated directory; the default is the repository-local dist/.
output_dir="${MOIRA_OUTPUT_DIR:-$project_dir/dist}"
output="$output_dir/moira_${version}_all.deb"

# Reproducibility: identical source + identical SOURCE_DATE_EPOCH must
# produce byte-identical .deb files. dpkg-deb clamps every staged file
# mtime and the ar member timestamps to SOURCE_DATE_EPOCH; the gzip
# members are built timestamp-free with -n. Without the variable the
# build still succeeds (using the current time) but is not reproducible.
source_date_epoch="${SOURCE_DATE_EPOCH:-$(date +%s)}"
export SOURCE_DATE_EPOCH="$source_date_epoch"

rm -rf "$stage"
mkdir -p "$stage/DEBIAN" "$stage/usr/bin" "$stage/usr/lib/moira" \
  "$stage/usr/share/applications" "$stage/usr/share/icons/hicolor/scalable/apps" \
  "$stage/usr/share/metainfo" "$stage/usr/share/doc/moira" "$stage/usr/share/man/man1" \
  "$output_dir"
cp "$project_dir/packaging/control" "$stage/DEBIAN/control"
cp -R "$project_dir/src/moira" "$stage/usr/lib/moira/"
find "$stage/usr/lib/moira" -type d -name __pycache__ -prune -exec rm -rf {} +
install -m 0755 "$project_dir/packaging/moira-launcher" "$stage/usr/bin/moira"
install -m 0755 "$project_dir/packaging/moira-claude-statusline" "$stage/usr/bin/moira-claude-statusline"
install -m 0755 "$project_dir/packaging/moira-agent-hook" "$stage/usr/bin/moira-agent-hook"
install -m 0644 "$project_dir/data/io.github.moira.QuotaMonitor.desktop" "$stage/usr/share/applications/"
install -m 0644 "$project_dir/data/io.github.moira.QuotaMonitor.svg" "$stage/usr/share/icons/hicolor/scalable/apps/"
install -m 0644 "$project_dir/data/io.github.moira.QuotaMonitor.metainfo.xml" "$stage/usr/share/metainfo/"
# Debian policy documentation: machine-readable copyright and a
# deterministic, timestamp-free gzip changelog.
install -m 0644 "$project_dir/packaging/copyright" "$stage/usr/share/doc/moira/copyright"
gzip -n -9 -c "$project_dir/packaging/changelog" > "$stage/usr/share/doc/moira/changelog.gz"
# Man pages (policy: gzip-compressed under /usr/share/man/man1).
for page in moira.1 moira-claude-statusline.1 moira-agent-hook.1; do
  gzip -n -9 -c "$project_dir/packaging/man/$page" > "$stage/usr/share/man/man1/$page.gz"
done
find "$stage" -type d -exec chmod 0755 {} +
# Normalize every regular file to 0644, then restore the launcher
# executability (documentation and man pages must be 0644, never
# umask-dependent).
find "$stage" -type f -exec chmod 0644 {} +
chmod 0755 "$stage/usr/bin/moira" "$stage/usr/bin/moira-claude-statusline" "$stage/usr/bin/moira-agent-hook"
# Normalize every staged file mtime (files and directories) so the
# control/data archives embed no build-time variation.
find "$stage" -exec touch -h -d "@$source_date_epoch" {} +
dpkg-deb --build --root-owner-group -Zxz -z9 "$stage" "$output"
echo "$output"
