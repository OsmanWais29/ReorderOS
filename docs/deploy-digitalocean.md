# Deploying ReorderOS to DigitalOcean

This is the **only** path to a live ReorderOS API. After you finish this doc,
the GitHub Actions workflow `.github/workflows/deploy.yml` deploys on every push
to `main` and proves the deploy worked by curling `/health/live`,
`/health/ready`, and `/version` against the live URL.

> Cost estimate (pilot): **~$32/mo** — App ($5) + Managed PG dev ($15) +
> Spaces ($5) + small overhead. Scale up later.

---

## Prereqs

- A DigitalOcean account with billing enabled.
- `doctl` installed locally (only needed for the **first** deploy).
- Owner access to the GitHub repo.

```bash
# macOS
brew install doctl
# or: https://github.com/digitalocean/doctl/releases

doctl auth init     # paste a DO API token (Account → API → Generate New Token, scope: read+write)
```

---

## Step 1 — Create the Managed Postgres cluster

```bash
doctl databases create reorderos-dev-pg \
  --engine pg \
  --version 16 \
  --region tor1 \
  --size db-s-1vcpu-1gb \
  --num-nodes 1
```

Wait until status is `online`:

```bash
doctl databases list
```

Capture the cluster id (you'll need it once, for trusted-source firewall):

```bash
DB_ID=$(doctl databases list --format ID,Name --no-header | awk '/reorderos-dev-pg/{print $1}')
echo "$DB_ID"
```

---

## Step 2 — Create the Spaces bucket

Spaces is **not** available in `tor1` yet, so we use `nyc3` (closest US region).
Switch to `sfo3`/`fra1`/`syd1` later if you want a different region.

In the DO console: **Spaces → Create Bucket → name: `reorderos-receipts`,
region: `nyc3`, file listing: Restricted**.

Then mint an access key:
**API → Spaces Keys → Generate New Key → name: `reorderos-app-platform`**.
Copy the **access key** and **secret** — the secret is shown once.

---

## Step 3 — First-time app create

The app should live in the same DigitalOcean project as the database
("ReOrderOS"), so capture that project's id first:

```bash
PROJECT_ID=$(doctl projects list --format ID,Name --no-header \
  | awk '/ReOrderOS/{print $1}')
echo "$PROJECT_ID"

cd /path/to/ReorderOS
doctl apps create --spec .do/app.yaml --project-id "$PROJECT_ID"
```

This:

- Pulls the GitHub repo (`OsmanWais29/ReorderOS`, branch `main`).
- Builds `backend/Dockerfile`.
- Binds the `reorderos-dev-pg` cluster you created (the spec references it
  by `cluster_name`); since the cluster is in `default-tor1` VPC and the app
  is also deployed to `tor1`, traffic is private — no public ingress on the DB.
- Boots one `basic-xxs` instance behind a `*.ondigitalocean.app` URL.

Capture the **App ID** (you need it for CI):

```bash
APP_ID=$(doctl apps list --format ID,Spec.Name --no-header | awk '/reorderos-api/{print $1}')
echo "$APP_ID"
```

Capture the live URL:

```bash
doctl apps get "$APP_ID" --format DefaultIngress --no-header
# e.g. https://reorderos-api-abc12.ondigitalocean.app
```

---

## Step 4 — Fill the secret env vars

The app spec declares these as `type: SECRET` with empty values; the first
deploy will boot but Sprint 6 (receipts) and Sprint 2 (auth) need them.

In the App Platform console: **App → Settings → App-Level Environment
Variables → Edit**. Set:

| Var                    | Value                                 |
|------------------------|---------------------------------------|
| `CLERK_JWKS_URL`       | from your Clerk dashboard             |
| `CLERK_ISSUER`         | from your Clerk dashboard             |
| `DO_SPACES_KEY`        | from Step 2                           |
| `DO_SPACES_SECRET`     | from Step 2                           |

Save → triggers a redeploy automatically.

---

## Step 5 — Wire GitHub Actions for hands-off deploys

In the GitHub repo: **Settings → Secrets and variables → Actions**:

| Secret                       | Value                                        |
|------------------------------|----------------------------------------------|
| `DIGITALOCEAN_ACCESS_TOKEN`  | the token from `doctl auth init`             |
| `DIGITALOCEAN_APP_ID`        | the `$APP_ID` you captured in Step 3         |

From this point on, every push to `main` that touches `backend/**` or
`.do/app.yaml` will trigger `.github/workflows/deploy.yml`, which:

1. Validates the app spec.
2. Pushes the spec → `doctl apps update` → waits for the deployment to go
   `ACTIVE` (max 15 min).
3. Resolves the live URL.
4. `curl /health/live`, `/health/ready`, `/version` — fails the workflow if
   any check fails.
5. Posts a deploy summary on the GitHub run.

**That GitHub Actions run is the proof the API is live and connected to your
Postgres cluster.** Specifically `/health/ready` returning `{"status":"ok",
"db":"ok"}` is end-to-end proof that:

- the App Platform service is up,
- the bound managed Postgres cluster is reachable from the app's VPC,
- async SQLAlchemy + asyncpg are working over TLS,
- the DO-injected `${reorderos-dev-pg.DATABASE_URL}` was correctly normalized
  by `app.core.config` from `postgresql://` → `postgresql+asyncpg://`.

---

## Verifying manually any time

```bash
URL=$(doctl apps get "$APP_ID" --format DefaultIngress --no-header)

curl -i "$URL/health/live"      # 200 {"status":"ok"}
curl -i "$URL/health/ready"     # 200 {"status":"ok","db":"ok","select_1":1}
curl -i "$URL/version"          # 200 {"version":"0.1.0"}
curl -i "$URL/api/v1/openapi.json" | head
```

Live runtime logs:

```bash
doctl apps logs "$APP_ID" --type=run --follow
```

You should see the structured-JSON startup line:

```json
{"env":"production","version":"0.1.0","event":"startup","level":"info","timestamp":"..."}
```

---

## Troubleshooting

| Symptom                                         | Likely cause                                                                                       | Fix                                                                                       |
|-------------------------------------------------|----------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------|
| `/health/ready` returns 503 with `db:unreachable` | App not in PG trusted sources                                                                      | App Platform binds these automatically when the DB is declared in `app.yaml`. Check the Networking tab on the cluster. |
| Build fails with `pip wheel` error              | Python deps mismatch                                                                               | Check the Dockerfile build log; try `pip install -e ".[dev]"` locally to reproduce.       |
| Deploy succeeds but `/version` 404s             | Service didn't bind to `$PORT`                                                                     | Logs will show uvicorn error. Confirm `PORT` env var isn't overridden.                    |
| `doctl apps update` succeeds but app doesn't change | App spec hasn't changed                                                                          | DO no-ops if the spec is byte-identical. Force a deploy with `doctl apps create-deployment <APP_ID>`. |
| Workflow fails on `Push spec → trigger deployment` with empty APP_ID | First deploy not done yet                                                       | Run Step 3 manually once. Then add the GitHub secret.                                     |

---

## Rolling back

```bash
doctl apps list-deployments "$APP_ID"          # find the previous good deployment id
doctl apps create-deployment "$APP_ID" --rebuild=false --force-rebuild=false
```

Or revert the offending commit on `main` — the deploy workflow will re-deploy
the previous good state.
