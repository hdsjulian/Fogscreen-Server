#!/bin/bash
# toggle-wifi.sh — one-stop WiFi mode switcher for the Temple.
#
#   toggle-wifi.sh                       cycle client → ap → wlian → client
#   toggle-wifi.sh ap        (or: 1)     Temple hotspot (what the apps join)
#   toggle-wifi.sh wlian                 WLIAN hotspot
#   toggle-wifi.sh client    (or: 0)     join the home wifi
#   toggle-wifi.sh client SSID [PASS]    set/update home-wifi credentials,
#                                        then join (omit PASS for open network)
#   toggle-wifi.sh status                show current mode, SSID and IP
#
# If joining the home wifi fails, the script falls back to AP mode so the
# Pi always stays reachable over the air.
set -u

PARAM="${1:-}"

wlan_conn() {  # name of the active connection on wlan0, if any
    nmcli -t -f NAME,DEVICE connection show --active | awk -F: '$2=="wlan0"{print $1; exit}'
}

conn_ssid() {  # SSID stored in a profile
    nmcli -g 802-11-wireless.ssid connection show "$1" 2>/dev/null
}

CURRENT="$(wlan_conn)"

show_status() {
    local mode
    case "$CURRENT" in
        ap)     mode="ap (Temple hotspot)" ;;
        wlian)  mode="wlian (secondary hotspot)" ;;
        mywifi) mode="client (home wifi)" ;;
        "")     mode="disconnected" ;;
        *)      mode="$CURRENT" ;;
    esac
    echo "Mode:  $mode"
    echo "SSID:  $([ -n "$CURRENT" ] && conn_ssid "$CURRENT" || echo '—')"
    echo "IP:    $(nmcli -g IP4.ADDRESS device show wlan0 2>/dev/null | head -1)"
}

print_help() {
    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
    echo ""
    show_status
}

wait_dnsmasq_gone() {
    sudo pkill dnsmasq 2>/dev/null
    sleep 1
    while pgrep dnsmasq > /dev/null; do sleep 1; done
}

down_all() {
    sudo nmcli connection down mywifi >/dev/null 2>&1
    sudo nmcli connection down ap     >/dev/null 2>&1
    sudo nmcli connection down wlian  >/dev/null 2>&1
}

hotspot_up() {  # $1 = ap | wlian
    echo "Switching to $1 mode..."
    down_all
    if ! sudo nmcli connection up "$1"; then
        echo "ERROR: could not start the '$1' hotspot." >&2
        exit 1
    fi
    wait_dnsmasq_gone
    sudo systemctl start dnsmasq nginx
    echo "Done. Now in $1 mode — broadcasting: $(conn_ssid "$1")"
}

go_client() {  # $1 = optional new SSID, $2 = optional new password
    if [ -n "${1:-}" ]; then
        echo "Storing home-wifi credentials (SSID: $1)..."
        sudo nmcli connection modify mywifi 802-11-wireless.ssid "$1"
        if [ -n "${2:-}" ]; then
            sudo nmcli connection modify mywifi \
                wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$2"
        else
            sudo nmcli connection modify mywifi remove wifi-sec
        fi
    fi

    local ssid
    ssid="$(conn_ssid mywifi)"
    if [ -z "$ssid" ] || [ "$ssid" = "your-home-wifi" ]; then
        echo "ERROR: no real home-wifi credentials configured yet." >&2
        echo "Set them with:  $0 client \"YourSSID\" \"password\"" >&2
        exit 1
    fi

    echo "Switching to client mode (joining '$ssid')..."
    sudo systemctl stop dnsmasq nginx
    down_all

    if sudo nmcli connection up mywifi; then
        echo "Done. Now in client mode — IP: $(nmcli -g IP4.ADDRESS device show wlan0 | head -1)"
    else
        echo "FAILED to join '$ssid'." >&2
        echo "(Wrong password? Network not on 2.4 GHz? Out of range?" >&2
        echo " Scan with:  nmcli device wifi list --rescan yes)" >&2
        echo "Falling back to AP mode so the Pi stays reachable..." >&2
        sudo nmcli connection up ap >/dev/null 2>&1
        wait_dnsmasq_gone
        sudo systemctl start dnsmasq nginx
        echo "Now broadcasting: $(conn_ssid ap)" >&2
        exit 1
    fi
}

case "$PARAM" in
    help|-h|--help)
        print_help ;;
    status)
        show_status ;;
    ap|1)
        [ "$CURRENT" = "ap" ] && echo "Already in AP mode." || hotspot_up ap ;;
    wlian)
        [ "$CURRENT" = "wlian" ] && echo "Already in WLIAN mode." || hotspot_up wlian ;;
    client|0)
        if [ -n "${2:-}" ]; then
            go_client "${2}" "${3:-}"
        elif [ "$CURRENT" = "mywifi" ]; then
            echo "Already in client mode."
        else
            go_client
        fi ;;
    "")
        case "$CURRENT" in
            mywifi) hotspot_up ap ;;
            ap)     hotspot_up wlian ;;
            wlian)  go_client ;;
            *)      hotspot_up ap ;;   # disconnected/unknown → safe default
        esac ;;
    *)
        echo "Unknown mode: '$PARAM'"
        print_help
        exit 1 ;;
esac
