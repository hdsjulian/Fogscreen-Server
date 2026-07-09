#!/bin/bash
# toggle-wifi.sh — switch the Pi between the Temple's Wi-Fi modes.
#
#   ap                    Pi is the access point "The Temple of Digital Oblivion"
#                         (static 192.168.4.1, serves DHCP + the captive portal).
#                         This is the network the apps join.
#   client [PASSWORD]     Pi joins the "WLIAN" Wi-Fi as a client (DHCP).
#                         PASSWORD sets/updates the WLIAN key.
#   join SSID [PASSWORD]  Pi joins any Wi-Fi network you name, as a client (DHCP).
#   status                show current mode, SSID and IP.
#   (no argument)         toggle between ap and client (WLIAN).
#
# Passwords are stored in NetworkManager, never in this file.
# If joining a network fails, it falls back to AP mode so the Pi stays reachable.
set -u

SELF="$(readlink -f "$0")"
LOG=/tmp/toggle-wifi.log

AP_PROFILE=ap          # NetworkManager profile for the Temple AP (SSID already set)
CLIENT_PROFILE=wlian   # profile for the fixed "WLIAN" client network
JOIN_PROFILE=custom    # profile reused for `join SSID` (arbitrary network)
CLIENT_SSID=WLIAN

# Run as root up front — prompting on the still-live terminal if needed — so the
# switch, which we relaunch detached below, never blocks on a sudo prompt once
# the connection is gone.
if [ "$(id -u)" -ne 0 ]; then
    exec sudo -- "$SELF" "$@"
fi

# A switch tears down the network this script may be running over (SSH on the
# AP). Relaunched detached (below) so the drop can't interrupt it; ignore
# SIGHUP too as a backstop.
trap '' HUP

PARAM="${1:-}"

active_conn() {   # name of the active connection on wlan0, if any
    nmcli -t -f NAME,DEVICE connection show --active | awk -F: '$2=="wlan0"{print $1; exit}'
}

active_ssid() {
    [ -n "$CURRENT" ] && nmcli -g 802-11-wireless.ssid connection show "$CURRENT" 2>/dev/null
}

wlan_ip() {
    nmcli -g IP4.ADDRESS device show wlan0 2>/dev/null | head -1
}

CURRENT="$(active_conn)"

current_mode() {
    case "$CURRENT" in
        "$AP_PROFILE")     echo ap ;;
        "$CLIENT_PROFILE") echo client ;;
        "$JOIN_PROFILE")   echo join ;;
        "")                echo disconnected ;;
        *)                 echo "$CURRENT" ;;
    esac
}

show_status() {
    echo "Mode: $(current_mode)"
    echo "SSID: $(active_ssid)"
    echo "IP:   $(wlan_ip)"
}

print_help() {
    cat <<'EOF'
toggle-wifi.sh — switch the Pi between the Temple's Wi-Fi modes.

  ap                    Pi is the AP "The Temple of Digital Oblivion"
                        (192.168.4.1, serves DHCP + portal). The apps join this.
  client [PASSWORD]     Pi joins the "WLIAN" Wi-Fi as a client (DHCP).
  join SSID [PASSWORD]  Pi joins any network you name, as a client (DHCP).
  status                show current mode, SSID and IP.
  (no argument)         toggle between ap and client.

If joining fails, falls back to AP mode so the Pi stays reachable.
EOF
    echo ""
    show_status
}

wait_dnsmasq_gone() {
    pkill dnsmasq 2>/dev/null
    sleep 1
    while pgrep dnsmasq >/dev/null; do sleep 1; done
}

go_ap() {
    echo "Switching to AP mode (Temple)..."
    nmcli connection down "$CLIENT_PROFILE" >/dev/null 2>&1
    nmcli connection down "$JOIN_PROFILE"   >/dev/null 2>&1
    if ! nmcli connection up "$AP_PROFILE"; then
        echo "ERROR: could not bring up the Temple AP." >&2
        exit 1
    fi
    wait_dnsmasq_gone
    systemctl start dnsmasq nginx
    echo "Done. AP mode — broadcasting: $(nmcli -g 802-11-wireless.ssid connection show "$AP_PROFILE")"
}

# connect_client PROFILE SSID [PASSWORD] [RETRY_HINT]
# (Re)configures PROFILE as a DHCP client of SSID and brings it up. Falls back
# to AP mode on failure so the Pi never strands itself.
connect_client() {
    local prof="$1" ssid="$2" pass="${3:-}" retry="${4:-}"

    if ! nmcli -t connection show "$prof" >/dev/null 2>&1; then
        nmcli connection add type wifi ifname wlan0 \
            con-name "$prof" ssid "$ssid" autoconnect no
    fi
    # Force client (infrastructure + DHCP); setup.sh had created 'wlian' as an AP.
    nmcli connection modify "$prof" \
        802-11-wireless.mode infrastructure \
        802-11-wireless.ssid "$ssid" \
        ipv4.method auto ipv4.addresses "" ipv4.gateway ""
    if [ -n "$pass" ]; then
        echo "Storing password for '$ssid'..."
        nmcli connection modify "$prof" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$pass"
    fi

    echo "Joining '$ssid'..."
    systemctl stop dnsmasq nginx
    nmcli connection down "$AP_PROFILE" >/dev/null 2>&1

    if nmcli connection up "$prof"; then
        echo "Done. Client mode — joined '$ssid' — IP: $(wlan_ip)"
    else
        echo "FAILED to join '$ssid'." >&2
        echo "(Wrong password? out of range? 2.4 GHz only?)" >&2
        [ -n "$retry" ] && echo " Retry with the password:  $retry" >&2
        echo "Falling back to AP mode so the Pi stays reachable..." >&2
        nmcli connection up "$AP_PROFILE" >/dev/null 2>&1
        wait_dnsmasq_gone
        systemctl start dnsmasq nginx
        exit 1
    fi
}

# status / help run inline.
case "$PARAM" in
    status)         show_status; exit 0 ;;
    help|-h|--help) print_help;  exit 0 ;;
esac

# Decide the action and gather any parameters.
ACTION="" ; JOIN_SSID="" ; SECRET=""
case "$PARAM" in
    ap)     ACTION=ap ;;
    client) ACTION=client ; SECRET="${2:-}" ;;
    join)   ACTION=join ; JOIN_SSID="${2:-}" ; SECRET="${3:-}"
            [ -z "$JOIN_SSID" ] && { echo "usage: $SELF join SSID [PASSWORD]" >&2; exit 1; } ;;
    "")     [ "$(current_mode)" = ap ] && ACTION=client || ACTION=ap ;;
    *)      echo "Unknown mode: '$PARAM'"; print_help; exit 1 ;;
esac

# Don't drop the connection just to end up where we already are (unless the user
# passed new credentials, in which case they want to (re)configure/reconnect).
CUR="$(current_mode)"
if [ -z "$SECRET" ] && [ -z "$JOIN_SSID" ] && [ "$ACTION" = "$CUR" ]; then
    echo "Already in $ACTION mode."
    exit 0
fi

# A switch kills the connection it runs over, so relaunch detached (own session,
# logging to $LOG) — the SSH drop can't interrupt it then.
if [ "${TOGGLE_WORKER:-}" != 1 ]; then
    echo "Switching to $ACTION mode in the background — this connection will drop."
    case "$ACTION" in
        ap)     echo "Reconnect to 'The Temple of Digital Oblivion', then:  ssh raspi@192.168.4.1" ;;
        client) echo "The Pi will join WLIAN; find its new IP on the WLIAN router." ;;
        join)   echo "The Pi will join '$JOIN_SSID'; find its new IP on that network's router." ;;
    esac
    echo "Progress log on the Pi:  $LOG"
    TOGGLE_WORKER=1 setsid "$SELF" "$@" >"$LOG" 2>&1 </dev/null &
    exit 0
fi

# Detached worker: perform the switch.
case "$ACTION" in
    ap)     go_ap ;;
    client) connect_client "$CLIENT_PROFILE" "$CLIENT_SSID" "$SECRET" "$SELF client \"PASSWORD\"" ;;
    join)   connect_client "$JOIN_PROFILE"   "$JOIN_SSID"   "$SECRET" "$SELF join \"$JOIN_SSID\" \"PASSWORD\"" ;;
esac
