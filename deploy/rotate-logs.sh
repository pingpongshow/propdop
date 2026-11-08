#!/bin/sh
# Cap a PropDop log file, keeping one previous generation.
#
# Copy-truncate rather than rename, because the services hold the file open: systemd's
# `append:` opens it once at unit start, so renaming it would leave them writing to a file
# nobody can find until the next restart. Truncating in place is safe precisely because
# `append:` uses O_APPEND, where every write seeks to the end first and so lands at zero
# rather than leaving a sparse hole.
#
# Usage: rotate-logs.sh LOGFILE [MAX_BYTES]
set -eu

log="${1:?usage: rotate-logs.sh LOGFILE [MAX_BYTES]}"
max="${2:-20971520}"            # 20 MiB, so two generations cost at most 40 MiB

[ -f "$log" ] || exit 0
size=$(wc -c < "$log")
[ "$size" -lt "$max" ] && exit 0

cp -f "$log" "$log.1"
: > "$log"
