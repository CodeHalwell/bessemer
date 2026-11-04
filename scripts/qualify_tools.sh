#!/usr/bin/env bash
# Install pinned external load tools for an explicitly requested qualification run.
set -euo pipefail

if [[ $# != 1 ]]; then
    echo 'Usage: scripts/qualify_tools.sh NEW_OUTPUT_DIRECTORY' >&2
    exit 2
fi

qualification_tools=$(realpath -m -- "$1")
if [[ -e "$qualification_tools" ]]; then
    echo 'Output directory already exists; preserve previous tool provenance.' >&2
    exit 2
fi
mkdir -p -- "$qualification_tools"

case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) ;;
    *) echo 'This pinned oha artifact supports Linux x86_64 only.' >&2; exit 2 ;;
esac

curl --fail --location --retry 3 \
    https://github.com/hatoo/oha/releases/download/v1.16.0/oha-linux-amd64 \
    --output "$qualification_tools/oha"
echo "620bb9e16fb53eabc9a3fc45f88bdb41fefa3fee5c05e75892011ce320391716  $qualification_tools/oha" \
    | sha256sum --check --status
chmod +x -- "$qualification_tools/oha"

git clone --no-checkout https://github.com/giltene/wrk2.git "$qualification_tools/wrk2-src"
git -C "$qualification_tools/wrk2-src" checkout --detach \
    44a94c17d8e6a0bac8559b53da76848e430cb7a7
make -C "$qualification_tools/wrk2-src" -j2

# The bundled LuaJIT ELF writer can truncate the embedded module's symbol with
# current toolchains. Compile its C output instead; preserve all wrk2 logic.
(
    cd -- "$qualification_tools/wrk2-src/deps/luajit/src"
    ./luajit -b -t c -n wrk "$qualification_tools/wrk2-src/src/wrk.lua" \
        "$qualification_tools/wrk-bytecode.c"
)
(
    cd -- "$qualification_tools/wrk2-src"
    cc -c -o obj/bytecode.o "$qualification_tools/wrk-bytecode.c"
    cc -no-pie -Wl,-E -Ldeps/luajit/src -o wrk obj/*.o \
        -lluajit -lpthread -lm -lcrypto -lssl -ldl
)

"$qualification_tools/oha" --version
git -C "$qualification_tools/wrk2-src" rev-parse HEAD
sha256sum "$qualification_tools/oha" "$qualification_tools/wrk2-src/wrk"
