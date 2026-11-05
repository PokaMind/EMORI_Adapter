"""EMORI host app: the robot-side "partner" launcher.

Runs on the robot's CM4 (or any companion device). Responsibilities:

1. serve a kiosk **launcher** page (identify -> start) for the touchscreen / tablet;
2. mint a Pokamind **partner JWT-SSO** token server-side, signed with THIS robot's
   own Pokamind token (the key never reaches the browser), and build the SSO URL;
3. serve a **session** page that embeds Pokamind in an iframe and relays each
   per-cycle ``pokamind_analysis_update`` postMessage to a local WebSocket;
4. act as the relay **hub** that the adapter's ``PokamindBridgeSource`` reads from.

This is the connective tissue between "a learner starts a training" and "the robot
reacts". See docs/adr-001-onrobot-screenless.md.
"""

from __future__ import annotations

from emori_adapter.hostapp.jwt_sso import build_sso_url, mint_robot_sso_jwt

__all__ = ["mint_robot_sso_jwt", "build_sso_url"]
