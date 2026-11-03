#!/usr/bin/env sh
# Check the vendored identity library against the checksums in VENDORED.md.
#
# Usage:
#   scripts/check-vendored.sh [MODULE_DIR]
#       Check MODULE_DIR (default: the module directory of this script).
#   scripts/check-vendored.sh --self-test
#       Copy the vendored files to a temporary directory and show that the
#       check passes on the copy and fails when a file changes, disappears or
#       when an unlisted file appears in a vendored directory.
#
# VENDORED.md holds a fenced block with the info string `sha256`. Each line is
# in `sha256sum` format: "<sha256>  <path relative to MODULE_DIR>". A vendored
# directory is a listed directory whose name is `vector_identity`; it must hold
# only listed files.
#
# Exit status: 0 when the check (or the self-test) passes, 1 otherwise.
set -eu

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
default_dir=$(dirname -- "$script_dir")

# Print the checksum lines of the `sha256` block of VENDORED.md in $1.
checksums() {
  awk '
    /^```sha256[[:space:]]*$/ { inside = 1; next }
    /^```/ { inside = 0 }
    inside && NF { print }
  ' "$1/VENDORED.md"
}

# Print the listed paths of VENDORED.md in $1.
listed_paths() {
  checksums "$1" | awk '{ print $2 }'
}

# Print the vendored directories (listed directories named vector_identity).
vendored_dirs() {
  listed_paths "$1" | while IFS= read -r path; do
    parent=$(dirname -- "$path")
    case "$parent" in
      vector_identity | */vector_identity) printf '%s\n' "$parent" ;;
    esac
  done | sort -u
}

check() {
  dir=$1
  if [ ! -f "$dir/VENDORED.md" ]; then
    echo "check-vendored: $dir/VENDORED.md is missing" >&2
    return 1
  fi
  lines=$(checksums "$dir")
  if [ -z "$lines" ]; then
    echo "check-vendored: $dir/VENDORED.md has no sha256 block" >&2
    return 1
  fi
  status=0
  if ! (cd "$dir" && printf '%s\n' "$lines" | sha256sum --check --strict --quiet -); then
    status=1
  fi
  paths=$(listed_paths "$dir")
  for vdir in $(vendored_dirs "$dir"); do
    for file in $(cd "$dir" && find "$vdir" \( -type f -o -type l \) | sort); do
      if ! printf '%s\n' "$paths" | grep -qx -- "$file"; then
        echo "check-vendored: $file is not listed in VENDORED.md" >&2
        status=1
      fi
    done
  done
  return "$status"
}

copy_module() {
  from=$1
  to=$2
  cp "$from/VENDORED.md" "$to/VENDORED.md"
  for vdir in $(vendored_dirs "$from"); do
    mkdir -p "$to/$vdir"
    cp -R "$from/$vdir/." "$to/$vdir/"
  done
  for path in $(listed_paths "$from"); do
    mkdir -p "$to/$(dirname -- "$path")"
    cp "$from/$path" "$to/$path"
  done
}

self_test() {
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT INT TERM
  failures=0

  expect() {
    want=$1
    label=$2
    if check "$tmp" >/dev/null 2>&1; then got=pass; else got=fail; fi
    if [ "$got" = "$want" ]; then
      echo "self-test: $label: $got (expected)"
    else
      echo "self-test: $label: $got, expected $want" >&2
      failures=$((failures + 1))
    fi
  }

  copy_module "$default_dir" "$tmp"
  expect pass "unchanged copy"

  first=$(listed_paths "$default_dir" | head -n 1)
  printf ' ' >>"$tmp/$first"
  expect fail "modified $first"
  cp "$default_dir/$first" "$tmp/$first"
  expect pass "restored $first"

  rm "$tmp/$first"
  expect fail "missing $first"
  cp "$default_dir/$first" "$tmp/$first"

  vdir=$(vendored_dirs "$default_dir" | head -n 1)
  if [ -n "$vdir" ]; then
    printf 'pub const extra: Int = 1\n' >"$tmp/$vdir/extra.ak"
    expect fail "unlisted file $vdir/extra.ak"
    rm "$tmp/$vdir/extra.ak"
    printf 'pub const extra: Int = 1\n' >"$tmp/extra-target.ak"
    ln -s "$tmp/extra-target.ak" "$tmp/$vdir/extra-link.ak"
    expect fail "unlisted symbolic link $vdir/extra-link.ak"
    rm "$tmp/$vdir/extra-link.ak" "$tmp/extra-target.ak"
  fi
  expect pass "final copy"

  [ "$failures" -eq 0 ]
}

case "${1:-}" in
  --self-test)
    self_test
    ;;
  -h | --help)
    sed -n '2,18p' "$0"
    ;;
  *)
    dir=${1:-$default_dir}
    if check "$dir"; then
      echo "check-vendored: vendored files match VENDORED.md"
    else
      echo "check-vendored: vendored files do not match VENDORED.md" >&2
      exit 1
    fi
    ;;
esac
