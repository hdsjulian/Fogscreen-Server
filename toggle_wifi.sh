#!/bin/bash
PARAM="${1:-}"
CURRENT=$(nmcli -t -f NAME connection show --active | head -1)
IS_AP=false
IS_WLIAN=false
[ "$CURRENT" = "ap" ] && IS_AP=true
[ "$CURRENT" = "wlian" ] && IS_WLIAN=true

print_help() {
    echo ""
    echo "Usage: $0 [mode]"
    echo ""
    echo "Modes:"
    echo "  client   Switch to WiFi client mode (connects to mywifi)"
    echo "  ap       Switch to AP hotspot mode (SSID: FogScreen or whatever your ap SSID is)"
    echo "  wlian    Switch to WLIAN hotspot mode (SSID: WLIAN)"
    echo "  help     Show this help"
    echo ""
    echo "No argument: cycles client → ap → wlian → client"
    echo ""
    echo "Current mode: $CURRENT"
    echo ""
}

wait_dnsmasq_gone() {
    sudo pkill dnsmasq 2>/dev/null
    sleep 1
    while pgrep dnsmasq > /dev/null; do sleep 1; done
}

go_ap() {
    echo "Switching to AP mode..."
    sudo nmcli connection down mywifi 2>/dev/null
    sudo nmcli connection down wlian 2>/dev/null
    sudo nmcli connection up ap
    wait_dnsmasq_gone
    sudo systemctl start dnsmasq nginx
    echo "Done. Now in AP mode."
}

go_wlian() {
    echo "Switching to WLIAN mode..."
    sudo nmcli connection down mywifi 2>/dev/null
    sudo nmcli connection down ap 2>/dev/null
    sudo nmcli connection up wlian
    wait_dnsmasq_gone
    sudo systemctl start dnsmasq nginx
    echo "Done. Now in WLIAN mode."
}

go_client() {
    echo "Switching to client mode..."
    sudo systemctl stop dnsmasq nginx
    sudo nmcli connection down ap 2>/dev/null
    sudo nmcli connection down wlian 2>/dev/null
    sudo nmcli connection up mywifi
    echo "Done. Now in client mode."
}

if [ "$PARAM" = "help" ]; then
    print_help
elif [ "$PARAM" = "ap" ]; then
    $IS_AP && echo "Already in AP mode." || go_ap
elif [ "$PARAM" = "wlian" ]; then
    $IS_WLIAN && echo "Already in WLIAN mode." || go_wlian
elif [ "$PARAM" = "client" ]; then
    ($IS_AP || $IS_WLIAN) && go_client || echo "Already in client mode."
elif [ -z "$PARAM" ]; then
    if $IS_AP; then
        go_wlian
    elif $IS_WLIAN; then
        go_client
    else
        go_ap
    fi
else
    echo "Unknown mode: '$PARAM'"
    print_help
    exit 1
fi

exit 0