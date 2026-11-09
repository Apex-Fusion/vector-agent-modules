#!/usr/bin/env sh
# Check the vendored identity library of the jury contract.
#
# Usage:
#   scripts/check-vendored.sh [CONTRACTS_DIR [MODULE1_CONTRACTS_DIR]]
#       Check CONTRACTS_DIR (default: Module-1/jury/contracts) against the
#       Module-1 copy in MODULE1_CONTRACTS_DIR (default: Module-1/contracts).
#   scripts/check-vendored.sh --self-test
#       Copy the files to a temporary directory and show that the check
#       passes on the copy and fails when a file changes, disappears, differs
#       from the Module-1 copy, or when an unlisted file appears in a vendored
#       directory.
#
# CONTRACTS_DIR/VENDORED.md holds a fenced block with the info string
# `sha256`. Each line is in `sha256sum` format: "<sha256>  <path relative to
# CONTRACTS_DIR>". The check fails when
#   - a listed file does not match its checksum,
#   - a listed file differs from the file with the same path in
#     MODULE1_CONTRACTS_DIR (the Module-1 copy),
#   - a vendored directory (a listed directory named `vector_identity`) holds
#     a file or a symbolic link that the list does not name.
#
# Exit status: 0 when the check (or the self-test) passes, 1 otherwise.
set -eu

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
jury_dir=$(dirname -- "$script_dir")
default_dir="$jury_dir/contracts"
default_ref="$(dirname -- "$jury_dir")/contracts"

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

# check DIR REF: check the vendored files of DIR against VENDORED.md and
# against the Module-1 copy in REF.
check() {
  dir=$1
  ref=$2
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
  for path in $paths; do
    if [ ! -f "$ref/$path" ]; then
      echo "check-vendored: the Module-1 copy $ref/$path is missing" >&2
      status=1
    elif [ ! -f "$dir/$path" ] || ! cmp -s "$dir/$path" "$ref/$path"; then
      echo "check-vendored: $path differs from the Module-1 copy $ref/$path" >&2
      status=1
    fi
  done
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

# copy_files FROM TO: copy VENDORED.md (when present) and the listed files.
copy_files() {
  from=$1
  to=$2
  list_from=$3
  if [ -f "$from/VENDORED.md" ]; then
    cp "$from/VENDORED.md" "$to/VENDORED.md"
  fi
  for path in $(listed_paths "$list_from"); do
    mkdir -p "$to/$(dirname -- "$path")"
    cp "$from/$path" "$to/$path"
  done
}

self_test() {
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT INT TERM
  mkdir -p "$tmp/jury" "$tmp/module1"
  failures=0

  expect() {
    want=$1
    label=$2
    if check "$tmp/jury" "$tmp/module1" >/dev/null 2>&1; then got=pass; else got=fail; fi
    if [ "$got" = "$want" ]; then
      echo "self-test: $label: $got (expected)"
    else
      echo "self-test: $label: $got, expected $want" >&2
      failures=$((failures + 1))
    fi
  }

  copy_files "$default_dir" "$tmp/jury" "$default_dir"
  copy_files "$default_ref" "$tmp/module1" "$default_dir"
  rm -f "$tmp/module1/VENDORED.md"
  expect pass "unchanged copy"

  first=$(listed_paths "$default_dir" | head -n 1)
  printf ' ' >>"$tmp/jury/$first"
  expect fail "modified $first"
  cp "$default_dir/$first" "$tmp/jury/$first"
  expect pass "restored $first"

  rm "$tmp/jury/$first"
  expect fail "missing $first"
  cp "$default_dir/$first" "$tmp/jury/$first"

  printf ' ' >>"$tmp/module1/$first"
  expect fail "Module-1 copy of $first changed"
  cp "$default_ref/$first" "$tmp/module1/$first"
  expect pass "Module-1 copy restored"

  vdir=$(vendored_dirs "$default_dir" | head -n 1)
  if [ -n "$vdir" ]; then
    printf 'pub const extra: Int = 1\n' >"$tmp/jury/$vdir/extra.ak"
    expect fail "unlisted file $vdir/extra.ak"
    rm "$tmp/jury/$vdir/extra.ak"
    printf 'pub const extra: Int = 1\n' >"$tmp/extra-target.ak"
    ln -s "$tmp/extra-target.ak" "$tmp/jury/$vdir/extra-link.ak"
    expect fail "unlisted symbolic link $vdir/extra-link.ak"
    rm "$tmp/jury/$vdir/extra-link.ak" "$tmp/extra-target.ak"
  fi
  expect pass "final copy"

  [ "$failures" -eq 0 ]
}

case "${1:-}" in
  --self-test)
    self_test
    ;;
  -h | --help)
    sed -n '2,24p' "$0"
    ;;
  *)
    dir=${1:-$default_dir}
    ref=${2:-$default_ref}
    if check "$dir" "$ref"; then
      echo "check-vendored: vendored files match VENDORED.md and the Module-1 copy"
    else
      echo "check-vendored: vendored files do not match VENDORED.md or the Module-1 copy" >&2
      exit 1
    fi
    ;;
esac
