# Security

## Reporting

Report vulnerabilities privately through GitHub (**Security > Report a
vulnerability**). Please do not open a public issue.

## Security properties

* **ComfyUI listens on 127.0.0.1 only.** It has no authentication, so anyone who
  can reach its port can run code on your GPU. Use an SSH tunnel for remote
  access instead of changing the binding.
* **No privileged containers, Docker socket mounts or host networking.** GPU
  access comes from the NVIDIA container runtime only.
* **Read-only mounts.** `models/`, `assets/`, `recipes/`, `profiles/` and
  `scripts/` are mounted read-only; only `outputs/` and `cache/` are writable.
* **Host changes need confirmation.** Setup asks before installing drivers or
  packages and before changing swap, `/etc/fstab`, `.wslconfig` or running
  `wsl --shutdown`.
* **The narration page is local by default.** LAN access (`tts web --lan`)
  requires a password and uses plain HTTP; use it only on a trusted network.

## Supply chain

The base image is pinned by digest, and ComfyUI and every custom node are pinned
by commit in `manifests/containers.lock.yaml`. The build fails if a pin is
missing or a dependency fails to install. ComfyUI-Manager is not installed.

Model weights are pinned by URL, size and SHA-256 in
`manifests/models.lock.yaml`. Run `narration-video-gen hashes` to verify
downloaded files.

## Secrets

This repository contains no credentials. `.gitignore` excludes `.env*`, keys and
certificates. `report` writes `results/<run-id>/run.json`, which contains your
platform, GPU, memory and file paths; review it before sharing.

## Generated media

Synthesised narration carries a SilentCipher watermark. The narration model's
terms prohibit imitating a real person's voice without their explicit consent
and prohibit deceptive use. See `THIRD_PARTY_NOTICES.md`.
