# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately, in one of two ways:

- through GitHub's **private vulnerability reporting** on this repository
  (the "Report a vulnerability" button under the Security tab), or
- by email to **dev@pokamind.com**.

Do not open a public issue, pull request or discussion for a security problem. Include
what is affected, how to reproduce it, and the impact you expect. You will get an
acknowledgement within 5 working days, and we will keep you informed while we fix it.

## Scope

This policy covers the robot-side code in this repository: the hostapp, the admin
service, the kiosk, the ROS 2 packages, the FIWARE integration and the deploy kit.
Problems in the Pokamind service itself go to the same contact.

## Supported versions

Security fixes are made on the latest commit of the default branch. Deploy from it.

## Robot tokens

Each robot authenticates to Pokamind with its own token (`pkr1.<robot-id>.<key>`, an
Ed25519 seed). Only Pokamind issues, rotates and revokes tokens; Pokamind keeps only the
public key. If a token leaks, ask Pokamind to revoke it and issue a new one, then install
the new one on the robot (see [docs/deploy-a-new-robot.md](docs/deploy-a-new-robot.md),
"Replace, rotate, revoke"). Never post a full token anywhere; the masked form
(`pkr1.<robot-id>.…abcd`) is enough to identify it.

## Keeping secrets out of the repository

Never commit `.env`, `robot.local.env`, anything under `secrets/`, `configs/roster.yaml`
(the live badge roster, which holds personal data) or `configs/site/` (the robot's saved
settings, its Pokamind scope snapshot and its menus). All five are in `.gitignore`. The only
full token in the repository is the golden test vector `pkr1.test-robot-01.…`, a public
test fixture and not a credential.

## Deployment hardening

- **Keep the admin PIN.** The phone admin page (`:8070`) and its actions are protected
  by `EMORI_ADMIN_PIN`; `install.sh` (or Deploy-to-Robot) generates one and shows it
  once. Change it on the admin page (Settings -> Credentials).
- **Set a dashboard password.** The management dashboard (`:8090/dashboard`) answers 503
  until `EMORI_DASH_PASSWORD` is set; `install.sh` generates one.
- **The FIWARE stack ships local lab credentials.** `deploy/fiware` uses fixed database
  and Grafana passwords and publishes its ports on all interfaces. Change the credentials,
  and restrict the ports to the robot's subnet, before exposing it beyond a lab network
  (see [deploy/fiware/README.md](deploy/fiware/README.md)).
- **ROS 2 on the robot is local only.** `ROS_LOCALHOST_ONLY` is the only access control
  DDS offers; keep it set, and keep the default de-identification
  (`EMORI_ROS_DEIDENTIFY=1`, `EMORI_ROS_SPEECH_TEXT=0`).
