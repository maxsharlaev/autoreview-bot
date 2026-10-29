# Seccomp profile provenance

`default.json` is the Docker Engine default seccomp profile vendored from
[`moby/profiles`](https://github.com/moby/profiles) tag **`seccomp/v0.2.4`**
(commit `e491ad972b4de5830f17b9b89dfea5a73f51f62d`, object tag annotated;
file path `seccomp/default.json`).

Matched to the Docker Engine version on the development host when M2 landed
(**29.8.0** / Docker Desktop). Re-vendor when upgrading the pinned Engine
baseline.

`review-bwrap.json` is derived from that default with the minimal additions
needed for Codex/bubblewrap user namespaces inside the review container
(see comments inside the JSON). It must **not** set `seccomp=unconfined`.
