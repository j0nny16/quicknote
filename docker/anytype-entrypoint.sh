#!/bin/sh
# Workaround for anyproto/anytype-cli#59: the headless CLI never sets
# PreferYamuxTransport, so anytype-heart prefers QUIC. On network paths where
# QUIC stalls, every operation that fetches from the network -- notably
# `space join` -- dies with "DeadlineExceeded ... RST_STREAM CANCEL" while
# TCP-based calls keep working, which makes it look like a permissions problem.
#
# The node dials only what its nodeconf lists, so removing the quic:// entries
# forces TCP/yamux. heart re-fetches the nodeconf from the coordinator, hence
# the periodic re-strip. Drop this once the upstream fix ships.
strip_quic() {
    find /data -path '*/nodeconf/*.yml' -exec sed -i '/quic:\/\//d' {} + 2>/dev/null || true
}

strip_quic
(while :; do sleep 15; strip_quic; done) &

exec anytype serve --listen-address 0.0.0.0:31012 --no-update-check
