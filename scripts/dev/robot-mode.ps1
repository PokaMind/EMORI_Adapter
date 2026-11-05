# Flip the Reachy Mini between EMORI kiosk mode and stock daemon/app mode, from the dev box.
#
#   .\scripts\dev\robot-mode.ps1 emori     # EMORI kiosk appliance owns camera/mic
#   .\scripts\dev\robot-mode.ps1 daemon    # stock daemon owns camera/mic for its app/dashboard
#   .\scripts\dev\robot-mode.ps1 status    # just show the current mode
#
# Thin wrapper over the on-robot `robot-mode` command (deploy/robot/robot-mode.sh).
param(
    [ValidateSet('emori', 'daemon', 'app', 'status')]
    [string]$Mode = 'status'
)
ssh pollen@reachy-mini.local "robot-mode $Mode"
