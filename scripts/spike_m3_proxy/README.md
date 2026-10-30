# M3 spike harness (Codex base URL)

Fake OpenAI-compatible proxy + notes for verifying how `@openai/codex@0.164.0` (pinned in `Dockerfile.review`) reaches a custom base URL. **No real model key required.**

See [FINDINGS.md](FINDINGS.md).

## Quick re-run

```bash
docker network create opr-spike-m3 || true
docker rm -f opr-fake-proxy 2>/dev/null || true
docker run -d --name opr-fake-proxy --network opr-spike-m3 -p 18080:18080 \
  -v "$PWD/scripts/spike_m3_proxy/fake_proxy.py:/fake_proxy.py:ro" \
  python:3.12-slim python /fake_proxy.py

# Example: custom provider (recommended)
docker run --rm --network opr-spike-m3 --user 0 \
  -e CODEX_API_KEY=spike-job-token -e OPENAI_API_KEY=spike-job-token \
  -v "$PWD/scripts/spike_m3_proxy/workspace:/checkout:rw" \
  -v "$PWD/scripts/spike_m3_proxy/workspace/home_provider/config.toml:/opt/spike-config.toml:ro" \
  --entrypoint bash opr-review:local -c \
  'mkdir -p /var/tmp/codex-home && cp /opt/spike-config.toml /var/tmp/codex-home/config.toml &&
   export CODEX_HOME=/var/tmp/codex-home HOME=/var/tmp && cd /checkout &&
   timeout 60 codex --ask-for-approval never exec --sandbox danger-full-access --ephemeral \
     --skip-git-repo-check -m gpt-4.1-mini -c "approval_policy=\"never\"" - <<< "ok"'

docker logs opr-fake-proxy
docker exec opr-fake-proxy cat /tmp/fake_proxy_requests.jsonl
```

Configs: `workspace/home_env` (baseline), `home_cfg` (`openai_base_url`), `home_provider` (custom provider).