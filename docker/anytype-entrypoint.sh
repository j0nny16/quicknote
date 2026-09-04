#!/bin/sh
# Workaround for anyproto/anytype-cli#59: the headless CLI never sets
# PreferYamuxTransport, so anytype-heart prefers QUIC. Where QUIC stalls, every
# network fetch -- notably `space join` -- dies with "DeadlineExceeded" while
# TCP calls keep working, which makes it look like a permissions problem.
#
# Two layers, because editing the config alone loses a race: heart re-fetches
# the node list from the coordinator during login, so there is a window where
# QUIC is back before the next strip.
#
#   1. Drop outbound QUIC at the firewall. This holds at every instant, so the
#      transport choice cannot land on QUIC no matter what the config says.
#   2. Strip quic:// from the node list anyway, so heart does not waste time
#      dialling addresses that can never answer.
#
# Drop both once the upstream fix ships.

if iptables -A OUTPUT -p udp --dport 5430 -j REJECT 2>/dev/null; then
    # any-sync QUIC also negotiates on 443/udp with some nodes
    iptables -A OUTPUT -p udp --dport 443 -j REJECT 2>/dev/null || true
    echo "[entrypoint] outbound QUIC blocked (udp/5430, udp/443)"
else
    echo "[entrypoint] WARNING: could not add iptables rule -- is cap_add NET_ADMIN set?"
    echo "[entrypoint] falling back to config stripping only; 'space join' may time out"
fi

strip_quic() {
    find /data -path '*/nodeconf/*.yml' -exec sed -i '/quic:\/\//d' {} + 2>/dev/null || true
}

strip_quic
(while :; do sleep 15; strip_quic; done) &

exec anytype serve --listen-address 0.0.0.0:31012 --no-update-check
