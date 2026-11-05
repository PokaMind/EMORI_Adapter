"""EMORI superadmin service — standalone on-robot management surface.

Runs as its own process (``emori-admin`` console script, systemd unit
``emori-admin.service``) so it survives when the hostapp/kiosk it manages
goes down. See docs/robot-admin.md.
"""
