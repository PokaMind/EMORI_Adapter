#!/bin/sh
# pw-record for the hub image, ahead of the real one on the path.
#
# The voice tap asks for `--raw -`, which the host's PipeWire needs to stream headerless samples
# to stdout. This image has an older PipeWire, whose pw-record refuses the flag and exits, and
# which already writes headerless samples to `-`. So the flag is dropped here, and the tap runs
# the same command in both places.
for word in "$@"; do
    shift
    [ "$word" = --raw ] || set -- "$@" "$word"
done
exec /usr/bin/pw-record "$@"
