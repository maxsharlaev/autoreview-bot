# Public release checklist

English is the primary documentation language. Existing Russian guides are available in [docs/ru](ru/README.md).

Complete these checks before a public release.

| Check | Release criterion |
| --- | --- |
| Source, docs, examples and observability assets | Use neutral names and sample data; review release files for sensitive information |
| Local configuration | Keep `.env`, `config.yaml`, private keys and local prompts out of Git and Docker build context |
| Credentials | Run a dedicated secret scanner on release files and rotate any credential found |
| Licensing | Include Apache-2.0 and review third-party dependency and asset notices |
| CI and contribution policy | Pass Ruff, tests and image build; add the checks planned in [ROADMAP.md](ROADMAP.md) |
| Production defaults | Keep database and Redis ports private, restrict metrics, and require a webhook signature before recommending direct webhook delivery |
