#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
stage="$project_dir/build/deb-root"
output="$project_dir/dist/moira_0.2.0_all.deb"

rm -rf "$stage"
mkdir -p "$stage/DEBIAN" "$stage/usr/bin" "$stage/usr/lib/moira" \
  "$stage/usr/share/applications" "$stage/usr/share/icons/hicolor/scalable/apps" \
  "$stage/usr/share/metainfo" "$project_dir/dist"
cp "$project_dir/packaging/control" "$stage/DEBIAN/control"
cp -R "$project_dir/src/moira" "$stage/usr/lib/moira/"
find "$stage/usr/lib/moira" -type d -name __pycache__ -prune -exec rm -rf {} +
install -m 0755 "$project_dir/packaging/moira-launcher" "$stage/usr/bin/moira"
install -m 0755 "$project_dir/packaging/moira-claude-statusline" "$stage/usr/bin/moira-claude-statusline"
install -m 0644 "$project_dir/data/io.github.moira.QuotaMonitor.desktop" "$stage/usr/share/applications/"
install -m 0644 "$project_dir/data/io.github.moira.QuotaMonitor.svg" "$stage/usr/share/icons/hicolor/scalable/apps/"
install -m 0644 "$project_dir/data/io.github.moira.QuotaMonitor.metainfo.xml" "$stage/usr/share/metainfo/"
find "$stage" -type d -exec chmod 0755 {} +
find "$stage/usr/lib/moira" -type f -exec chmod 0644 {} +
dpkg-deb --root-owner-group --build "$stage" "$output"
echo "$output"
