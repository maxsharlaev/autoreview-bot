# Admin control UI

Static Next.js export served by the API at `/control` when `ADMIN_UI_ENABLED=true`.

```bash
npm ci
npm run build   # writes ./out
```

Local UI against a running API on one origin (cookie `SameSite=Strict`):

```bash
# terminal 1: API with ADMIN_API_ENABLED=true ADMIN_UI_ENABLED=true ADMIN_COOKIE_SECURE=false
# terminal 2:
npm run dev
# Prefer a reverse proxy that serves both /control and /api on one host/port.
```

Do not point the browser at `:3000` for login against `:8000` — the session cookie will not be sent.
