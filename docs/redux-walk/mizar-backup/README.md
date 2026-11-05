# mizar-backup

## Decisions (2026-11-05)

- mizar does not back up to chinook. Chinook is a lab resource, and Tony's private data stays off it. Tony: "We should also not do number 1 since Chinook is a lab resource and I don't want to push my private data onto the lab drive."
- The two mira `.orig` files with plain-text secrets are now mode 600 (Tony: "Everything else looks good").
