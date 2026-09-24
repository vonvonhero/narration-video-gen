# Custom hardware profiles

Copy an example YAML to a directory outside the checkout, give it a unique
`id`, and load it without changing the published catalog:

```bash
./bin/narration-video-gen --profile-dir /path/to/my-profiles \
  plan --profile custom-wan21-720p-face-detailer-384-bs30
```

The Face Detailer controls are hardware-profile settings:

```yaml
settings:
  face_detailer_enabled: true
  face_detailer_size: 384
  face_detailer_blocks_to_swap: 30
```

`face_detailer_size` is the square VACE activation plane, not the source-video
resolution or detected mask size. It must be a positive multiple of 16.
`face_detailer_blocks_to_swap` is from 0 through 40. The `plan` wizard can
override `face_detailer_enabled` for the saved plan without editing the YAML.
