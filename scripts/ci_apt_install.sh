#!/usr/bin/env bash
# Install Ubuntu packages on a CI runner without failing the job the moment
# the package archive cannot be reached.
#
#     scripts/ci_apt_install.sh PACKAGE...
#
# `apt-get update && apt-get install` gives up on its first failure, and takes
# minutes over it when name resolution hangs. On 11 November 2026 the runners
# could not resolve Ubuntu's archive hosts for seven minutes or more, and four
# jobs failed before a test had run.
#
# Here every attempt has a time limit, attempts are repeated until a deadline,
# and when the runner's own mirrors fail a mirror outside ubuntu.com is tried.
# Whichever mirror answers, apt checks the index against Ubuntu's archive key
# and each package against the index, so a mirror can fail to serve a package
# but cannot substitute one.
#
# Nothing downloaded is kept between runs. apt accepts a file already in its
# archive directory when the size is right, without checking the hash, so a
# cache of packages would have to be verified here before apt saw it.
set -euo pipefail

if (($# == 0)); then
    echo "usage: ${0##*/} PACKAGE..." >&2
    exit 2
fi
packages=("$@")

# Seconds to keep trying, and the limits on one index refresh and one download.
patience=${CI_APT_PATIENCE:-300}
update_limit=${CI_APT_UPDATE_LIMIT:-45}
fetch_limit=${CI_APT_FETCH_LIMIT:-90}
# Tried when the configured mirrors fail. Empty to try none.
fallback_mirror=${CI_APT_FALLBACK_MIRROR-https://mirrors.edge.kernel.org/ubuntu}
keyring=${CI_APT_KEYRING:-/usr/share/keyrings/ubuntu-archive-keyring.gpg}

# Wait for another package operation on the runner instead of failing on its lock.
apt=(apt-get -q -o DPkg::Lock::Timeout=120)

as_root() {
    if ((EUID == 0)); then
        "$@"
    else
        sudo "$@"
    fi
}

installed() {
    local package status
    for package in "${packages[@]}"; do
        status=$(dpkg-query --show --showformat='${db:Status-Status}' "$package" 2>/dev/null) || return 1
        [[ $status == installed ]] || return 1
    done
}

# One attempt to download the packages, with any extra apt options given.
# The index refresh may fail: the index already on the runner still works
# while the mirror holds the versions it names.
fetch() {
    as_root timeout "$update_limit" "${apt[@]}" "$@" update ||
        echo "The package index was not refreshed; trying the one already here."
    as_root timeout "$fetch_limit" "${apt[@]}" "$@" install -y --download-only "${packages[@]}"
}

# apt options that read the fallback mirror alone, with an index of its own so
# that the runner's index is left as it was.
fallback=()
prepare_fallback() {
    local work codename suite
    [[ -n $fallback_mirror && -r $keyring && $(dpkg --print-architecture) == amd64 ]] || return 1
    if ((${#fallback[@]} == 0)); then
        # Left for the runner to clear with the rest of its temporary files.
        work=$(mktemp -d)
        chmod 755 "$work"
        mkdir -p "$work/lists/partial"
        codename=$(. /etc/os-release && echo "$VERSION_CODENAME")
        for suite in "$codename" "$codename-updates" "$codename-security"; do
            echo "deb [signed-by=$keyring] $fallback_mirror $suite main restricted universe multiverse"
        done >"$work/sources.list"
        fallback=(
            -o "Dir::Etc::sourcelist=$work/sources.list"
            -o "Dir::Etc::sourceparts=-"
            -o "Dir::State::lists=$work/lists"
            -o "Dir::Cache::pkgcache=$work/pkgcache.bin"
            -o "Dir::Cache::srcpkgcache=$work/srcpkgcache.bin"
        )
    fi
}

if installed; then
    echo "Already installed: ${packages[*]}"
    exit 0
fi

# The options of the attempt that worked, so the install reads the same index.
source_options=()
deadline=$((SECONDS + patience))
pause=15
attempt=1
while :; do
    if fetch; then
        break
    fi
    echo "::warning::Attempt $attempt: the runner's package mirrors did not supply ${packages[*]}."
    if prepare_fallback; then
        if fetch "${fallback[@]}"; then
            source_options=("${fallback[@]}")
            echo "Downloaded from $fallback_mirror instead."
            break
        fi
        echo "::warning::Attempt $attempt: $fallback_mirror did not supply ${packages[*]} either."
    fi
    if ((SECONDS >= deadline)); then
        echo "::error::Could not download ${packages[*]} after $SECONDS seconds of trying. No test has run."
        exit 1
    fi
    echo "Trying again in $pause seconds."
    sleep "$pause"
    pause=$((pause < 60 ? pause * 2 : 60))
    attempt=$((attempt + 1))
done

# Everything is on disk now, so this needs no network and has no time limit:
# interrupting dpkg would leave packages half configured.
as_root "${apt[@]}" "${source_options[@]}" install -y "${packages[@]}"
if ! installed; then
    echo "::error::apt reported success but ${packages[*]} is not installed."
    exit 1
fi
