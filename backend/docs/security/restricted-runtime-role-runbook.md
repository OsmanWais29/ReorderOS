# Staging Runbook — Restricted Runtime Role Cutover

**Status: DO NOT RUN.** Executable plan reviewed alongside the security PR. Execute only
after explicit founder approval **per phase** (A0, A1, B, C are separately approved; Phase
B-alt needs its own) and a staging maintenance window. Every step is idempotent or guarded;
each has a verification gate and a rollback that never drops a login role. All `doctl`
syntax was validated against `doctl version 1.155.0` `--help` / official docs
(`apps spec get --deployment` per the official spec-get reference).

**The procedure is THREE deployment events** — A0 (config-only: kill the push race),
A1 (source-only: compatibility code), C (config+source: the cutover) — plus B (role
provisioning; **proven-safe against the running deployment before any credential
change**). Each deployment's ID is captured **from the command that creates it**. **Every
wait is bounded**; every failed gate goes through `abort` (logs captured, secret-bearing
built specs unlinked, non-zero exit) — and `abort` **never deletes a rollback artifact**.

## Working directory, shell options, and shared gate helpers (establish FIRST)

All checked-in Python modules are importable **only from `backend/`**.

```bash
# [LOCAL MAC] — run once per shell; every [LOCAL MAC] block below assumes ALL of this
set -euo pipefail
REPO=/Users/bigvito/Documents/ReorderOS
cd "$REPO/backend"
PY=.venv/bin/python
APP=70e351c1-3c5f-4699-b85b-05a2346e7f84          # reorderos-staging (STAGING ONLY)
BASE=https://reorderos-staging-3ez2g.ondigitalocean.app
umask 077
mkdir -p ~/.reorderos-cutover/secrets ~/.reorderos-cutover/logs
chmod 700 ~/.reorderos-cutover ~/.reorderos-cutover/secrets ~/.reorderos-cutover/logs

# Bounded deployment waiter — the ONLY way any phase waits. ACTIVE=success;
# ERROR/CANCELED/SUPERSEDED, doctl failure, or timeout (60×15s) are hard failures.
wait_active() {  # wait_active DEP_ID [iterations]
  local dep="$1" n="${2:-60}" i phase
  for i in $(seq 1 "$n"); do
    phase=$(doctl apps get-deployment "$APP" "$dep" --format Phase --no-header) \
      || { echo "wait_active: get-deployment failed for $dep — STOP"; return 1; }
    echo "[$i/$n] $dep phase=$phase"
    case "$phase" in
      ACTIVE) return 0 ;;
      ERROR|CANCELED|SUPERSEDED) echo "wait_active: $dep ended $phase — STOP"; return 1 ;;
    esac
    sleep 15
  done
  echo "wait_active: $dep not ACTIVE after $((n*15))s — STOP"; return 1
}

# ROLLBACK ARTIFACT capture — the spec of the CURRENTLY-ACTIVE (pre-change) deployment,
# fetched with `apps spec get --deployment` so a later failed deployment can never
# contaminate it. Written once, mode 0600, and NEVER deleted by abort — only by
# cleanup_rollback_artifacts at the founder-approved final cleanup.
# The optional EXPECTED_ID (2nd arg) BINDS the capture to one specific deployment: when
# a prior gate (retire_verify) already proved a deployment, the capture must be of THAT
# deployment — if the app's active id has moved since the proof, the proof is stale and
# the capture must refuse rather than silently bless whatever is active now.
capture_rollback() {  # capture_rollback TAG [EXPECTED_ID]
  local tag="$1" expected="${2:-}" id phase current out tmp
  out=~/.reorderos-cutover/rollback_${tag}_spec.yaml
  if [ -e "$out" ]; then echo "capture_rollback: $out already exists — STOP"; return 1; fi
  tmp="${out}.tmp.$$"
  rm -f "$tmp"

  # Ask the app object for its current active deployment directly. Do not select the
  # first apparently-ACTIVE row from deployment history.
  id=$(doctl apps get "$APP" --format ActiveDeployment.ID --no-header) \
    || { echo "capture_rollback: active-deployment lookup failed — STOP"; return 1; }
  test -n "$id" \
    || { echo "capture_rollback: app reports no active deployment — STOP"; return 1; }
  if [ -n "$expected" ] && [ "$id" != "$expected" ]; then
    echo "capture_rollback: active is $id, but the verified deployment was $expected —"
    echo "the pre-capture proof is STALE; re-run the verification gate. STOP"; return 1
  fi
  phase=$(doctl apps get-deployment "$APP" "$id" --format Phase --no-header) \
    || { echo "capture_rollback: deployment lookup failed — STOP"; return 1; }
  test "$phase" = ACTIVE \
    || { echo "capture_rollback: selected deployment is $phase, not ACTIVE — STOP"; return 1; }

  # Fetch to a temporary 0600 file, parse it as a non-empty app spec, and re-check that
  # the same deployment is still active. Only then hard-link it into the sacred final
  # path; `ln` is atomic and refuses to overwrite an artifact created by another run.
  doctl apps spec get "$APP" --deployment "$id" > "$tmp" \
    || { rm -f "$tmp"; echo "capture_rollback: spec fetch failed — STOP"; return 1; }
  chmod 600 "$tmp"
  "$PY" -c 'import sys,yaml; s=yaml.safe_load(open(sys.argv[1])); assert isinstance(s,dict) and s.get("name")' "$tmp" \
    || { rm -f "$tmp"; echo "capture_rollback: fetched spec is empty/invalid — STOP"; return 1; }
  current=$(doctl apps get "$APP" --format ActiveDeployment.ID --no-header) \
    || { rm -f "$tmp"; echo "capture_rollback: active re-check failed — STOP"; return 1; }
  test "$current" = "$id" \
    || { rm -f "$tmp"; echo "capture_rollback: active deployment changed during capture — STOP"; return 1; }
  ln "$tmp" "$out" \
    || { rm -f "$tmp"; echo "capture_rollback: atomic publish failed — STOP"; return 1; }
  rm -f "$tmp"
  echo "rollback artifact captured atomically from PRE-ACTIVE deployment $id (tag $tag)"
}

# Capture per-component logs for a deployment BEFORE any rollback (forensics).
capture_logs() {  # capture_logs DEP_ID TAG
  local dep="$1" tag="$2" comp
  for comp in api inbox-worker reconciliation-worker receipt-extraction-worker \
              inbound-email-worker migrate; do
    doctl apps logs "$APP" "$comp" --type run --deployment "$dep" \
      > ~/.reorderos-cutover/logs/"$tag.$comp.log" 2>/dev/null || true
  done
  echo "logs captured under ~/.reorderos-cutover/logs/ (tag $tag)"
}

# THE abort path — every failed gate goes through this. Captures logs when a deployment
# id exists, unlinks the AGGREGATED secret-bearing WORK artifacts (built cutover spec,
# fetched live/a0/post_a0 specs), and exits non-zero. It deliberately PRESERVES:
#   - rollback_*_spec.yaml                     (the ONLY safe path back — see R3)
#   - secrets/ + *.pw                           (so a fixed re-run needs no re-entry)
# Run cleanup_secrets only after successful completion or a VERIFIED rollback.
abort() {  # abort TAG
  local tag="$1"
  [ -n "${DEP_ID:-}" ] && capture_logs "$DEP_ID" "$tag"
  rm -f ~/.reorderos-cutover/cutover_spec.yaml ~/.reorderos-cutover/live_spec.yaml \
        ~/.reorderos-cutover/a0_spec.yaml ~/.reorderos-cutover/post_a0_spec.yaml \
        ~/.reorderos-cutover/smoke_auth.header \
        ~/.reorderos-cutover/b0_components.expected \
        ~/.reorderos-cutover/b0_components.passed \
        ~/.reorderos-cutover/b0_components.passed.sorted \
        ~/.reorderos-cutover/service_role
  echo "ABORT($tag): work specs unlinked; rollback artifacts + secrets/ PRESERVED — run"
  echo "cleanup_secrets only after completion or a verified rollback. Exiting non-zero."
  exit 1
}

# SENSITIVE-MATERIAL cleanup: plaintext passwords, DSN files, and secret-bearing WORK
# files. Deliberately NEVER touches rollback_*_spec.yaml — rollback artifacts contain
# only EV[...] encrypted references (no plaintext secrets), and deleting the current
# rollback path is a SEPARATE, founder-approved decision (see cleanup_rollback_artifacts).
# Run at successful completion or after a verified rollback.
cleanup_secrets() {
  rm -f ~/.reorderos-cutover/secrets/* ~/.reorderos-cutover/*.pw \
        ~/.reorderos-cutover/cutover_spec.yaml ~/.reorderos-cutover/live_spec.yaml \
        ~/.reorderos-cutover/a0_spec.yaml ~/.reorderos-cutover/post_a0_spec.yaml \
        ~/.reorderos-cutover/smoke_auth.header \
        ~/.reorderos-cutover/b0_components.expected \
        ~/.reorderos-cutover/b0_components.passed \
        ~/.reorderos-cutover/b0_components.passed.sorted \
        ~/.reorderos-cutover/service_role
  echo "secret-bearing files unlinked (plain unlink — no secure-erasure claims on SSD);"
  echo "rollback_*_spec.yaml PRESERVED — delete only via cleanup_rollback_artifacts"
}

# FINAL rollback-artifact cleanup — a SEPARATE step requiring EXPLICIT founder approval,
# never bundled into cleanup_secrets or any abort path. Deletion is per-artifact, only
# when it is no longer the go-forward rollback (superseded by a later ACTIVE deployment,
# or a credential it references via EV[...] was rotated; founder confirms in writing).
#
# OPERATOR CONTRACT on "no wildcards": the shell expands a glob BEFORE any function
# sees its arguments, so a function cannot distinguish a wildcard expansion from
# individually typed paths. This function therefore accepts artifact TAGS ("a0", "c",
# "retire") — never paths — and builds the path itself: a glob of artifact FILES
# expands to names containing "/" and ".", which the tag check rejects structurally.
cleanup_rollback_artifacts() {  # cleanup_rollback_artifacts TAG...   e.g.: … retire
  [ "$#" -ge 1 ] || { echo "name each artifact TAG explicitly (a0 | c | retire)"; return 1; }
  local tag f
  for tag in "$@"; do
    case "$tag" in
      ""|*[!a-z0-9_]*) echo "refusing: '$tag' is not a bare artifact tag (paths/globs rejected)"; return 1 ;;
    esac
    f="$HOME/.reorderos-cutover/rollback_${tag}_spec.yaml"
    [ -e "$f" ] || { echo "no rollback artifact for tag '$tag' — nothing deleted"; return 1; }
    rm -f "$f"; echo "retired: $f"
  done
}
```

Spec paths are **relative to `backend/`**: `../.do/staging.app.yaml`. Inside
`[DO API SHELL]` the working directory is already the backend app root and the
interpreter is `python`. **The DO console shell has NO `set -e`** — every mutating or
gating command there carries its own explicit `|| { echo …; exit 1; }`.

## Shell labels (every command block is tagged — do not mix shells)
- **`[LOCAL MAC]`** — the founder's laptop shell, prepared with the preamble above.
- **`[DO API SHELL]`** — a shell inside the DigitalOcean `api` component
  (`doctl apps console "$APP" api`).

> **No `psql`.** DB administration runs via asyncpg through `python -m scripts.role_admin …`
> inside `[DO API SHELL]`. Confirm the console shell supports `read -s` (bash/zsh):
> `echo "$0"`. If plain POSIX `sh`, use `stty -echo; read PW; stty echo; echo`.

## Invariants
- **Production is frozen.** Never touched by this runbook.
- Staging only: app `$APP`, DB cluster `reorderos-staging-pg`, URL `$BASE`.
- **The rollback artifact is sacred**: captured from the PRE-ACTIVE deployment before
  each mutating phase, never deleted by `abort`, deleted only at completion or after a
  verified rollback.
- **No credential of the RUNNING deployment is ever invalidated in place.** Every
  password change is preceded by `role_admin preflight-rotate` inside **every running
  service/worker whose effective environment could contain a DB DSN**. The live-spec
  inventory in B0 defines that checklist; a one-shot PRE_DEPLOY job is not a running
  credential consumer. A role the deployment DOES use is replaced via the **versioned-role
  procedure (Phase B-alt)** — new login role first, deploy its DSN, verify. Never
  password-first. Disabling the old login afterwards (retirement) is currently
  **unsupported** — see B-alt.8; the old role stays LOGIN-enabled with its residual
  documented.
- **No command prints, echoes, logs, or histories a password, DSN, token, or EV
  reference.** Bearer tokens for smokes are read with `read -s` into shell variables.
- Role mutations are ATOMIC (single transaction: attrs + password + memberships) and
  `prove` VALIDATES the full contract in code — its exit status is the gate.
- Stop on any failed gate via `abort`. **No intentional-failure paths.**
- `deploy_on_push` stays **false** after the cutover until the deterministic workflow is
  proven; restoring it is a later, explicit founder decision.

## Deployment architecture (why each phase looks the way it does)
- Dockerfile default stays production-compatible; staging overrides it at C (Uvicorn-only
  api; migrations ONLY in the PRE_DEPLOY job as doadmin; job secrets exactly
  `{DATABASE_URL}`).
- Role assertions + per-component config gate on `RESTRICTED_RUNTIME_ROLES_ENABLED`
  (=true only at C). The expected service role name is `SERVICE_ROLE_NAME` (default
  `service_worker`) — the versioned-role rotation (B-alt) changes it in the SAME
  deployment as the DSN, so the assertion follows the credential.
- The API stays fail-closed off-head via `_assert_schema_at_head()`.
- Rotation-key safety: live `TOKEN_ENCRYPTION_KEY_PREVIOUS` is preserved on every
  token-decrypting component or the build aborts; `deploy_verify --cutover` re-checks
  the distribution all-or-none against the fresh live spec.

---

## Phase A0 — kill the push race (config-only deployment)

> **Source-preservation note:** `/version` on the currently deployed (pre-A1) build does
> NOT report a commit, so a `/version`-based SHA check here would be vacuous
> (`unknown == unknown`). A0's source preservation is proven **structurally**: the
> update passes no `--update-sources`, the pre-apply diff is machine-verified to touch
> only `deploy_on_push`, and the post-apply live spec is byte-compared to the intended
> A0 spec. Real SHA gates begin at A1.

```bash
# [LOCAL MAC]
unset DEP_ID
capture_rollback a0 || abort a0-capture-rollback     # PRE-ACTIVE spec, immutable
rm -f ~/.reorderos-cutover/live_spec.yaml ~/.reorderos-cutover/a0_spec.yaml
doctl apps spec get "$APP" > ~/.reorderos-cutover/live_spec.yaml || abort a0-spec-get
chmod 600 ~/.reorderos-cutover/live_spec.yaml

"$PY" - <<'EOF' || abort a0-edit   # flip deploy_on_push only; PROVE nothing else changed
import copy, os, yaml, pathlib
p = pathlib.Path.home()/".reorderos-cutover"
live = yaml.safe_load((p/"live_spec.yaml").read_text())
a0 = copy.deepcopy(live)
flips = 0
for kind in ("services","workers","jobs"):
    for comp in a0.get(kind) or []:
        gh = comp.get("github") or {}
        if gh.get("deploy_on_push") is True:
            gh["deploy_on_push"] = False; flips += 1
check = copy.deepcopy(a0)
for kind in ("services","workers","jobs"):
    for comp in check.get(kind) or []:
        gh = comp.get("github") or {}
        if gh.get("deploy_on_push") is False:
            gh["deploy_on_push"] = True
assert check == live, "A0 edit touched more than deploy_on_push — STOP"
out = p/"a0_spec.yaml"
if out.is_symlink() or out.exists():
    raise SystemExit(f"{out} already exists — remove it and re-run (never overwrite)")
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
data = yaml.safe_dump(a0, sort_keys=False).encode()
fd = os.open(out, flags, 0o600)
try:
    os.fchmod(fd, 0o600)
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        assert n > 0, "short write"
        view = view[n:]
    assert os.fstat(fd).st_size == len(data), "size mismatch after write"
finally:
    os.close(fd)
print(f"A0 spec written 0600: deploy_on_push flipped on {flips} component(s); no other change")
EOF

doctl apps spec validate ~/.reorderos-cutover/a0_spec.yaml || abort a0-validate

DEP_ID=$(doctl apps update "$APP" --spec ~/.reorderos-cutover/a0_spec.yaml \
  --format InProgressDeployment.ID --no-header) || abort a0-update
test -n "$DEP_ID" || abort a0-no-dep-id
echo "A0 DEP_ID=$DEP_ID"
wait_active "$DEP_ID" || abort a0-not-active
```

**Gate A0** — readiness, applied-spec equality, workers running:
```bash
# [LOCAL MAC]
test "$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/health/ready")" = 200 || abort a0-not-ready
doctl apps spec get "$APP" > ~/.reorderos-cutover/post_a0_spec.yaml || abort a0-postspec-get
chmod 600 ~/.reorderos-cutover/post_a0_spec.yaml
"$PY" - <<'EOF' || abort a0-spec-drift
import yaml, pathlib
p = pathlib.Path.home()/".reorderos-cutover"
a0 = yaml.safe_load((p/"a0_spec.yaml").read_text())
post = yaml.safe_load((p/"post_a0_spec.yaml").read_text())
assert post == a0, "post-apply live spec != intended A0 spec — STOP"
print("A0 post-apply spec equality: OK")
EOF
rm -f ~/.reorderos-cutover/post_a0_spec.yaml
for comp in inbox-worker receipt-extraction-worker inbound-email-worker; do
  doctl apps logs "$APP" "$comp" --type run --deployment "$DEP_ID" | tail -3 \
    | grep -q . || abort "a0-$comp-silent"
done
curl -sS "$BASE/health/storage"   # Spaces readiness booleans — expect unchanged
```
**Rollback A0:** `DEP_ID=$(doctl apps update "$APP" --spec
~/.reorderos-cutover/rollback_a0_spec.yaml --format InProgressDeployment.ID --no-header)
&& wait_active "$DEP_ID"` — the artifact is the PRE-ACTIVE deployment's spec fetched via
`--deployment`, so it cannot have been contaminated by any later (failed) deployment.
DigitalOcean's console deployment-rollback is the documented alternative.

---

## Phase A1 — compatibility deployment (source-only)

Commit + push this branch (founder-approved push — flag OFF, `legacy` profile, today's
behavior; migration 0035 applies via the existing live migrate job, inert under doadmin).
With `deploy_on_push` false, the push deploys NOTHING; the deployment below is the only
trigger.

```bash
# [LOCAL MAC]
git rev-parse HEAD > ~/.reorderos-cutover/expected_sha       # comparison only, never injected

unset DEP_ID
DEP_ID=$(doctl apps create-deployment "$APP" --update-sources --format ID --no-header) \
  || abort a1-create
test -n "$DEP_ID" || abort a1-no-dep-id
echo "A1 DEP_ID=$DEP_ID"
wait_active "$DEP_ID" || abort a1-not-active
```

**Gate A1** — exact SHA everywhere, migration head, existing pipelines:
```bash
# [LOCAL MAC]
EXPECTED="$(cat ~/.reorderos-cutover/expected_sha)"
API_SHA="$(curl -sS "$BASE/version" | "$PY" -c 'import sys,json;print(json.load(sys.stdin).get("commit","unknown"))')"
test "$API_SHA" = "$EXPECTED" || abort a1-api-sha

for pair in "inbox-worker:inbox_worker" \
            "receipt-extraction-worker:receipt_extraction_worker" \
            "inbound-email-worker:inbound_email_worker"; do
  comp="${pair%%:*}"; event="${pair##*:}"
  doctl apps logs "$APP" "$comp" --type run --deployment "$DEP_ID" \
    | "$PY" -c "import sys; from scripts.deploy_verify import parse_starting_commit; \
c=parse_starting_commit(sys.stdin.read(), '$event'); \
print(f'$comp {c}'); sys.exit(0 if c=='$EXPECTED' else 1)" \
    || abort "a1-$comp-sha"
done
# (reconciliation-worker joins at C — it does not exist on live staging yet.)

doctl apps logs "$APP" migrate --type run --deployment "$DEP_ID" \
  | grep -qi "0035" || abort a1-migration
# pipelines: authenticated receipt upload → 202 extract; Postmark webhook auth behavior;
# founder smokes per the Sprint-6 checklists (upload one invoice end-to-end).
```
**Rollback A1:** reset the branch and re-run `create-deployment --update-sources`
(capturing + waiting as above), or DO console rollback. No spec/roles changed here.

---

## Phase B — provision and prove the roles (no deployment; PROVEN-SAFE first)

`role_admin.py` exists in the running container **because of A1**. The api component's
`DATABASE_URL` is still the doadmin DSN — it is the admin connection.

**B0 — rotation-safety preflight (mandatory, BEFORE any credential change).**
Two halves; both must pass. There is deliberately no "the API represents everything"
shortcut: each running service/worker is checked in its own container.

*B0.1 — derive the complete running-component checklist from the fresh live spec.*
PRE_DEPLOY jobs are excluded: they are one-shot deployment tasks, not members of the
currently serving deployment whose connection pools a rotation could strand.
```bash
# [LOCAL MAC]
rm -f ~/.reorderos-cutover/live_spec.yaml
rm -f ~/.reorderos-cutover/b0_components.expected \
      ~/.reorderos-cutover/b0_components.passed \
      ~/.reorderos-cutover/b0_components.passed.sorted
doctl apps spec get "$APP" > ~/.reorderos-cutover/live_spec.yaml || abort b0-spec-get
chmod 600 ~/.reorderos-cutover/live_spec.yaml
"$PY" - <<'EOF' || abort b0-component-inventory
import os, yaml, pathlib
base = pathlib.Path.home()/".reorderos-cutover"
live = yaml.safe_load((base/"live_spec.yaml").read_text())
components = []
for kind in ("services","workers"):
    for comp in live.get(kind) or []:
        name = str(comp.get("name") or "")
        if not name:
            raise SystemExit(f"{kind}: unnamed running component — STOP")
        keys = sorted(
            str(e.get("key")) for e in comp.get("envs") or []
            if e.get("key") in ("DATABASE_URL","SERVICE_DATABASE_URL")
        )
        components.append((kind, name, keys))
if not components:
    raise SystemExit("fresh live spec contains no running services/workers — STOP")
for kind, name, keys in components:
    print(f"B0 COMPONENT {kind}/{name}: component_dsn_overrides={','.join(keys) or '(none)'}")
expected = base/"b0_components.expected"
fd = os.open(expected, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    for name in sorted(name for _, name, _ in components):
        f.write(name + "\n")
print(f"B0.1 OK: {len(components)} running component(s) inventoried; check EVERY one in B0.2")
EOF
```

*B0.2 — no effective live DSN authenticates as either role, in every listed container.*
For **each** `B0 COMPONENT services/<name>` or `workers/<name>` printed above, open that
component's console (`doctl apps console "$APP" <name>`) and run the following block.
Record a component as passed only after **both** commands exit 0. Do not include `migrate`.
```bash
# [DO COMPONENT SHELL — repeat for api AND every currently-running worker]
python -m scripts.role_admin preflight-rotate service_worker \
  || { echo "service_worker is LIVE — do NOT rotate; use Phase B-alt"; exit 1; }
python -m scripts.role_admin preflight-rotate reorderos_app \
  || { echo "reorderos_app is LIVE (unexpected pre-cutover) — STOP"; exit 1; }
# EXPECT (current staging): all present *_uses_role=False. A missing env is reported
# False; a component-level override is read from that component's own effective env.
exit
```
After each component passes both commands, return to `[LOCAL MAC]` and record **that exact
component name** (the value after `services/` or `workers/` in B0.1):
```bash
# [LOCAL MAC] — repeat once per successfully checked component
printf '%s\n' '<component-name>' >> ~/.reorderos-cutover/b0_components.passed
```
Then enforce checklist equality before B1:
```bash
# [LOCAL MAC]
sort -u ~/.reorderos-cutover/b0_components.passed \
  > ~/.reorderos-cutover/b0_components.passed.sorted \
  || abort b0-passed-list
cmp -s ~/.reorderos-cutover/b0_components.expected \
       ~/.reorderos-cutover/b0_components.passed.sorted \
  || abort b0-incomplete-component-proof
echo "B0.2 OK: every running service/worker passed both role preflights"
```
If any listed component was skipped, duplicated, or misspelled, the equality gate fails.
If any preflight reports a role in live use: **STOP**. Phase B-alt supports an in-use
`service_worker` only; an unexpected in-use `reorderos_app` requires a separately reviewed
versioned request-role plan — do not improvise one.

> **Why B0 keeps a manual console sweep while retirement (B-alt.7) does not.** B0 runs
> PRE-cutover: the flag is off and neither role is asserted at startup, so no
> execution-bound evidence class exists yet — the in-container env check is the
> strongest available evidence, and `doctl` offers no scriptable exec to automate it.
> The blast radius is also different: B0 gates rotating passwords of roles the running
> (doadmin-DSN) deployment should not reference at all, and rotation never touches the
> credentials that deployment actually uses, so a wrong answer here degrades to the
> R1/R2 rollbacks without an outage of the running deployment. The B-alt.7 evidence
> gate concerns a login that WAS recently live — it is therefore execution-bound
> (`scripts/retire_verify.py`) and accepts no manual attestation; and even its PASS
> does not authorize disabling the login (B-alt.8 is unsupported — the control plane
> offers no way to bind the checks to the DDL).

**B1/B2 — provision + rotate (atomic; `prove`'s exit code is the gate):**
```bash
# [LOCAL MAC]
openssl rand -hex 32 > ~/.reorderos-cutover/reorderos_app.pw     # mode 0600 via umask
openssl rand -hex 32 > ~/.reorderos-cutover/service_worker.pw
pbcopy < ~/.reorderos-cutover/reorderos_app.pw
doctl apps console "$APP" api
```
```bash
# [DO API SHELL]  (no set -e here — every command carries explicit failure handling)
read -s PW; export PW           # paste (Cmd-V), Enter — hidden, not in history
python -m scripts.role_admin provision-app || { echo "provision FAILED (atomic; nothing half-changed)"; exit 1; }
python -m scripts.role_admin prove reorderos_app || { echo "reorderos_app CONTRACT VIOLATION — STOP -> R1"; exit 1; }
# prove VALIDATES in code (attrs + exact memberships + admin option) and prints, e.g.:
#   role=reorderos_app rolcanlogin=True rolsuper=False rolbypassrls=False
#   rolcreatedb=False rolcreaterole=False rolreplication=False rolinherit=True
#   member_of=app_user admin_option=False
#   prove reorderos_app: CONTRACT OK
unset PW
```
```bash
# [LOCAL MAC]
pbcopy < ~/.reorderos-cutover/service_worker.pw
```
```bash
# [DO API SHELL]
read -s PW; export PW
python -m scripts.role_admin rotate service_worker || { echo "rotate FAILED (atomic; nothing half-changed)"; exit 1; }
python -m scripts.role_admin prove service_worker || { echo "service_worker CONTRACT VIOLATION — STOP -> R2"; exit 1; }
# EXPECT printed: … member_of=(none) admin_option=False … + "prove service_worker: CONTRACT OK"
unset PW
exit
```

Build the DSN files for Phase C (never printed):
```bash
# [LOCAL MAC] — cluster coordinates from the DO managed-DB connection panel (non-secret)
PGHOST=...; PGPORT=...; PGDATABASE=...
"$PY" - "$PGHOST" "$PGPORT" "$PGDATABASE" <<'EOF' || abort b-dsn-build
import sys, urllib.parse, pathlib
h,p,db = sys.argv[1:4]; base = pathlib.Path.home()/".reorderos-cutover"
sec = base/"secrets"
for role, fname in (("reorderos_app","api.DATABASE_URL"), ("service_worker","SERVICE_DATABASE_URL")):
    pw = (base/f"{role}.pw").read_text().strip()
    dsn = f"postgresql://{role}:{urllib.parse.quote(pw, safe='')}@{h}:{p}/{db}?sslmode=require"
    out = sec/fname; out.write_text(dsn); out.chmod(0o600)
EOF
```

### Phase B-alt — versioned replacement role (ONLY if a B0 preflight failed; own approval)

For a role the running deployment DOES authenticate as, **password-first rotation is
forbidden** — connection churn or a container restart during the deploy window would break
the running service. The outage-safe path is a **versioned replacement login role**,
deployed by the SAME Phase-C machinery (builder → validator → single apply → gates) with
`SERVICE_ROLE=service_worker_v2` selected once — there is no hand-edited spec anywhere.
The old `service_worker` login stays valid until rollback is explicitly retired.

**B-alt.1 — provision + prove the replacement** (parent contract is asserted in code
BEFORE anything is created; a refusal creates and changes nothing):
```bash
# [LOCAL MAC]
openssl rand -hex 32 > ~/.reorderos-cutover/service_worker_v2.pw    # mode 0600 via umask
pbcopy < ~/.reorderos-cutover/service_worker_v2.pw
doctl apps console "$APP" api
```
```bash
# [DO API SHELL]  (no set -e — every command carries explicit failure handling)
read -s PW; export PW           # paste (Cmd-V), Enter — hidden, not in history
python -m scripts.role_admin provision-worker service_worker_v2 \
  || { echo "provision-worker FAILED (parent contract or atomic step) — STOP"; exit 1; }
python -m scripts.role_admin prove service_worker_v2 \
  || { echo "service_worker_v2 CONTRACT VIOLATION — STOP"; exit 1; }
# EXPECT: rolcanlogin=True rolsuper=False rolbypassrls=False rolcreatedb=False
#   rolcreaterole=False rolreplication=False rolinherit=True
#   member_of=service_worker admin_option=False + "prove service_worker_v2: CONTRACT OK"
unset PW
exit
```

**B-alt.2 — bind the versioned credential file** (replaces the standard service DSN
file: the selected role and its credential change TOGETHER; the validator enforces the
username agreement). Never printed:
```bash
# [LOCAL MAC] — cluster coordinates from the DO managed-DB connection panel (non-secret)
PGHOST=...; PGPORT=...; PGDATABASE=...
rm -f ~/.reorderos-cutover/secrets/SERVICE_DATABASE_URL
"$PY" - "$PGHOST" "$PGPORT" "$PGDATABASE" <<'EOF' || abort balt-dsn-build
import sys, urllib.parse, pathlib
h,p,db = sys.argv[1:4]; base = pathlib.Path.home()/".reorderos-cutover"
pw = (base/"service_worker_v2.pw").read_text().strip()
dsn = f"postgresql://service_worker_v2:{urllib.parse.quote(pw, safe='')}@{h}:{p}/{db}?sslmode=require"
out = base/"secrets"/"SERVICE_DATABASE_URL"; out.write_text(dsn); out.chmod(0o600)
EOF
```

**B-alt.3 — the COMPLETE self-contained sequence.** Execute these eight steps in order —
never jump from B-alt.2 straight into a deployment:

1. **Finish C1 and verify every required mode-0600 file exists** (role-bound DSN files
   are required on EVERY build — live EV values are never carried for role
   verification):
   ```bash
   # [LOCAL MAC]
   for f in api.DATABASE_URL migrate.DATABASE_URL SERVICE_DATABASE_URL \
            TOKEN_ENCRYPTION_KEY CLOVER_APP_SECRET CLOVER_WEBHOOK_AUTH_CODE \
            ANTHROPIC_API_KEY DO_SPACES_KEY DO_SPACES_SECRET \
            POSTMARK_WEBHOOK_USER POSTMARK_WEBHOOK_PASSWORD; do
     test -f ~/.reorderos-cutover/secrets/"$f" || { echo "missing secrets/$f — finish C1 first"; exit 1; }
   done
   ```
2. **Export the selected versioned role** (the single selection point C2 consumes):
   ```bash
   # [LOCAL MAC]
   export SERVICE_ROLE=service_worker_v2
   ```
3. **Execute C2** exactly as written below (rollback capture, build with
   `--service-role`, three validations, ONE apply, bounded wait).
4. **Execute C3** (SHA gates, dynamic role.ok gate — expects the recorded v2 —
   isolation smokes).
5. **Execute C4**, proving the VERSIONED role (password file `service_worker_v2.pw`).
6. **Enter the observation window.** The old `service_worker` login stays VALID the
   whole time — `rollback_c_spec.yaml` (old DSN, old role name) stays applicable, and
   rollback during the window re-applies it.
7. **Run the retirement evidence gate (B-alt.7)** — mandatory, fresh, and
   machine-verified end-to-end (`scripts/retire_verify.py` binds every check to the
   one proven ACTIVE deployment). Its PASS is the audit record that no running
   component consumes `service_worker`.
8. **Stop. Retirement itself (B-alt.8) is UNSUPPORTED** — `service_worker` keeps
   LOGIN enabled; see B-alt.8 for why and for the documented residual.

**B-alt.7 — retirement evidence gate, EXECUTION-BOUND and DEPLOYMENT-BOUND.** C3
proved the new deployment started correctly, but the observation window may include
restarts, rollbacks, or drift.

> **Why there is no per-component console attestation here.** `doctl apps console` is
> interactive-only (no scriptable command execution — verified against
> `doctl apps console --help`, which offers only `--deployment`/`--instance-name`), so
> an in-container `preflight-rotate` sweep cannot be automated, and a manually-typed
> "passed" list proves only that names were typed. The gate below replaces it with
> STRONGER, machine-verified evidence: every component's proof comes from its own
> deployment-bound execution logs. Under `RESTRICTED_RUNTIME_ROLES_ENABLED=true`, the
> api asserts BOTH pools' session users and logs `role.ok` (request=`reorderos_app`,
> service=the versioned role), and every worker logs the RETURN VALUE of its
> fail-closed service-pool assertion as `service_user` INSIDE its `<worker>.starting`
> record — the gate requires that logged value to equal the versioned role, so the
> evidence is the assertion's logged result, never source-code ordering (an AST
> contract test additionally pins the assign-then-log pattern in all four workers).
> The passed-set equals the spec-derived inventory by construction; there is nothing
> manual to forget or fake. A component that cannot produce this evidence — including
> any service other than `api`, which has no role-proof event — makes the gate refuse.

The gate (`python -m scripts.retire_verify`) binds every check to ONE deployment: it
requires no in-progress deployment, resolves the ACTIVE deployment id, requires phase
ACTIVE, fetches the spec **with `--deployment "$ACTIVE_ID"`** (never the unbound app
spec), digests it, validates from that bound spec the strict role declarations, the
flag, the `${_self.COMMIT_HASH}` SOURCE_COMMIT binding on every component (so
`/version` and start lines — which merely ECHO that env — cannot certify a stale
literal SHA), and a service inventory of exactly `api`; collects per-component
execution evidence from that deployment's logs; checks live `/version` against the
approved SHA; and then RE-CHECKS (no in-progress deployment, same active id, phase
still ACTIVE, spec digest unchanged) before printing the verified id. Any movement
mid-proof aborts.

The expected role comes from the recorded `service_role` file (written by C2 from the
BUILT artifact, machine cross-checked against the selection, never retyped) — not from
a re-exported variable, because this gate typically runs days later in a fresh shell.
The gate itself rejects a non-versioned value, so a standard (`service_worker`) cutover
can never certify retirement evidence through this path.

```bash
# [LOCAL MAC]
unset DEP_ID ACTIVE_ID
RETIRE_ROLE="$(cat ~/.reorderos-cutover/service_role)" || abort retire-role-file
ACTIVE_ID=$("$PY" -m scripts.retire_verify --app "$APP" \
    --role "$RETIRE_ROLE" \
    --sha  "$(cat ~/.reorderos-cutover/expected_sha)" \
    --base-url "$BASE") || abort retire-verify
test -n "$ACTIVE_ID" || abort retire-verify-empty
echo "retire gate OK: deployment $ACTIVE_ID proven end-to-end for $RETIRE_ROLE"
# NEW rollback artifact — captured FROM THE PROVEN DEPLOYMENT ONLY: the expected-id
# argument makes capture_rollback refuse if the active deployment moved after the gate:
capture_rollback retire "$ACTIVE_ID" || abort retire-capture-rollback
```
If `retire_verify` cannot produce complete evidence for every component (e.g. a
component's logs rotated out of the deployment-bound window), the answer is a fresh
forced redeploy of the SAME source to regenerate startup evidence — **never** a manual
attestation. The gate's PASS is an audit record; it does NOT authorize B-alt.8.

**B-alt.8 — retirement (`ALTER ROLE service_worker` to NOLOGIN) is UNSUPPORTED. Do
not execute it, with or without founder approval.**

Why (inspected read-only against `doctl` 1.155 — `doctl apps --help` and
`doctl apps console --help`): App Platform exposes **no deployment lock, freeze, or
maintenance mode**, and **no noninteractive in-container execution**. The B-alt.7 gate
and `capture_rollback retire` re-check the control plane up to their own last command,
but the login-disabling DDL would then be typed into an interactive console session.
Between the final machine re-check and the DDL there is an unavoidable human-timescale
window in which a new deployment can begin (another operator, a console-initiated
deploy, a platform-side rebuild). Nothing binds the control-plane checks to the
database mutation — the window is small, but it is not zero and it is not atomic, so a
passing gate cannot certify the mutation as safe. This runbook does not pretend
otherwise.

Consequently:
- **Keep `service_worker` LOGIN enabled.** Stop after B-alt.7, even on PASS.
- **Documented residual (accepted until a supported mechanism exists):** the
  no-longer-referenced `service_worker` remains a valid login. Mitigations: it is
  contract-proven non-superuser / non-bypassrls (RLS still binds it — `role_admin
  prove` re-checks on demand); its password exists only in the local 0600 file; the
  B-alt.7 audit record shows no running component consumes it; and its continued
  validity is exactly what keeps `rollback_c_spec.yaml` applicable as a rollback.
- **Revisit** only if DigitalOcean ships a deployment lock/freeze or a scriptable
  in-container exec that can bind `retire_verify`'s checks and the DDL into one gated
  step — as a NEW, separately reviewed procedure, not an edit to this one.

`cleanup_secrets` (plaintext passwords/DSNs/work files, **nothing else**) may run at
B-alt completion as usual. Rollback-artifact retention:
- Because `service_worker` keeps LOGIN, **`rollback_a0_spec.yaml` and
  `rollback_c_spec.yaml` remain valid rollbacks** until superseded by a later ACTIVE
  deployment or an actual credential rotation — do not retire them as part of B-alt.
- **`rollback_retire_spec.yaml`** (captured from the proven versioned deployment) is
  the newest artifact and MUST be preserved. Any artifact becomes deletable only when
  (a) a LATER deployment goes ACTIVE (the platform's standard rollback path then
  supersedes it) or (b) a credential it references via `EV[...]` is rotated. Only when
  the founder confirms one of those conditions in writing may it be deleted — via a
  separately-run `cleanup_rollback_artifacts retire` (tag form; see the function
  header for the operator contract). No abort path and no `cleanup_secrets` call ever
  removes a rollback artifact.

---

## Phase C — the cutover (config + exact current source, ONE deployment)

### C1. Populate the secrets directory (mode-0600 files, one per moved/new secret)

The builder carries a live `EV[...]` value ONLY when its full identity is unchanged —
for this candidate that is the api's `WORKOS_SECRET_KEY` and api `ANTHROPIC_API_KEY`.
**Every other secret moves scope and requires a file**:

| File under `~/.reorderos-cutover/secrets/` | Value |
|---|---|
| `api.DATABASE_URL` | reorderos_app DSN (built in Phase B) |
| `migrate.DATABASE_URL` | the doadmin DSN |
| `SERVICE_DATABASE_URL` | service_worker DSN (built in Phase B; shared by api + all workers) |
| `TOKEN_ENCRYPTION_KEY` | existing staging Fernet key |
| `CLOVER_APP_SECRET`, `CLOVER_WEBHOOK_AUTH_CODE` | existing staging Clover sandbox secrets |
| `ANTHROPIC_API_KEY` | existing staging key (worker copy; api copy is EV-carried) |
| `DO_SPACES_KEY`, `DO_SPACES_SECRET` | existing staging Spaces pair |
| `POSTMARK_WEBHOOK_USER`, `POSTMARK_WEBHOOK_PASSWORD` | existing staging Basic Auth pair |
| `TOKEN_ENCRYPTION_KEY_PREVIOUS` | ONLY if the live app carries it — the builder REQUIRES it, injects into every token-decrypting component, and `--cutover` re-verifies all-or-none. Never retire it here. |

(`POSTMARK_INBOUND_ADDRESS` is configuration — carried from the live spec automatically;
conflicting live values abort the build.)

### C2. Build → validate → apply once

The expected service role is selected EXACTLY ONCE here — `$SERVICE_ROLE` — and flows
machine-checked into the builder (`--service-role`), the validator (4th `--cutover`
input: pattern + SERVICE_ROLE_NAME binding + every SERVICE_DATABASE_URL username +
`reorderos_app` request pool + anti-revert vs the live spec), the recorded
`service_role` file, and C3's role.ok gate. It is never retyped in any later command.
Standard cutover: leave it unset (defaults to `service_worker`). Phase B-alt exports
`SERVICE_ROLE=service_worker_vN` before running this block.

```bash
# [LOCAL MAC]
SERVICE_ROLE="${SERVICE_ROLE:-service_worker}"   # THE single selection point
echo "selected service role: $SERVICE_ROLE"
test "$(git rev-parse HEAD)" = "$(cat ~/.reorderos-cutover/expected_sha)" || abort c-sha-moved
unset DEP_ID
capture_rollback c || abort c-capture-rollback       # PRE-ACTIVE (= A1) spec, immutable
rm -f ~/.reorderos-cutover/live_spec.yaml ~/.reorderos-cutover/cutover_spec.yaml \
      ~/.reorderos-cutover/service_role
doctl apps spec get "$APP" > ~/.reorderos-cutover/live_spec.yaml || abort c-spec-get
chmod 600 ~/.reorderos-cutover/live_spec.yaml

"$PY" -m scripts.build_cutover_spec \
  --candidate ../.do/staging.app.yaml \
  --live ~/.reorderos-cutover/live_spec.yaml \
  --secrets ~/.reorderos-cutover/secrets \
  --service-role "$SERVICE_ROLE" \
  --out ~/.reorderos-cutover/cutover_spec.yaml || abort c-build

doctl apps spec validate ~/.reorderos-cutover/cutover_spec.yaml || abort c-doctl-validate
"$PY" -m scripts.deploy_verify --cutover ~/.reorderos-cutover/cutover_spec.yaml ../.do/staging.app.yaml ~/.reorderos-cutover/live_spec.yaml "$SERVICE_ROLE" || abort c-cutover-validate
"$PY" -m scripts.deploy_verify ../.do/staging.app.yaml || abort c-candidate-validate
# ALL THREE must pass. Post-deployment readiness is verification, never the fallback.

# Record the role BOUND IN THE BUILT ARTIFACT (machine cross-check against the
# selection; C3 reads this file — the role is never typed again):
"$PY" -c 'import sys,yaml; s=yaml.safe_load(open(sys.argv[1])); \
vals={str(e.get("value")) for e in s.get("envs") or [] if e.get("key")=="SERVICE_ROLE_NAME"}; \
assert vals=={sys.argv[2]}, f"built spec binds {vals}, selection is {sys.argv[2]!r}"; \
print(sys.argv[2])' ~/.reorderos-cutover/cutover_spec.yaml "$SERVICE_ROLE" \
  > ~/.reorderos-cutover/service_role || abort c-role-record

DEP_ID=$(doctl apps update "$APP" --spec ~/.reorderos-cutover/cutover_spec.yaml \
  --update-sources --format InProgressDeployment.ID --no-header) || abort c-update
test -n "$DEP_ID" || abort c-no-dep-id
echo "C DEP_ID=$DEP_ID"
rm -f ~/.reorderos-cutover/cutover_spec.yaml   # built spec: unlink after the single apply
wait_active "$DEP_ID" || abort c-not-active
```

### C3. Verify — SHA on every component, roles (JSON-parsed + asserted), isolation

```bash
# [LOCAL MAC]
EXPECTED="$(cat ~/.reorderos-cutover/expected_sha)"
API_SHA="$(curl -sS "$BASE/version" | "$PY" -c 'import sys,json;print(json.load(sys.stdin).get("commit","unknown"))')"
test "$API_SHA" = "$EXPECTED" || abort c-api-sha

for pair in "inbox-worker:inbox_worker" \
            "reconciliation-worker:reconciliation_worker" \
            "receipt-extraction-worker:receipt_extraction_worker" \
            "inbound-email-worker:inbound_email_worker"; do
  comp="${pair%%:*}"; event="${pair##*:}"
  doctl apps logs "$APP" "$comp" --type run --deployment "$DEP_ID" \
    | "$PY" -c "import sys; from scripts.deploy_verify import parse_starting_commit; \
c=parse_starting_commit(sys.stdin.read(), '$event'); \
print(f'$comp {c}'); sys.exit(0 if c=='$EXPECTED' else 1)" \
    || abort "c-$comp-sha"
done

# API startup role gate — the log record is structlog JSON, so it is PARSED, not
# grepped. The expected service role comes from the RECORDED selection (written at C2
# from the built artifact) — never retyped, so B-alt's versioned role flows through:
EXPECT_ROLE="$(cat ~/.reorderos-cutover/service_role)" || abort c-role-file
doctl apps logs "$APP" api --type run --deployment "$DEP_ID" \
  | "$PY" -c "import sys; from scripts.deploy_verify import parse_role_ok; \
r=parse_role_ok(sys.stdin.read()); print(f'role.ok={r}'); \
sys.exit(0 if r==('reorderos_app', sys.argv[1]) else 1)" "$EXPECT_ROLE" \
  || abort c-role-ok
doctl apps logs "$APP" api --type run --deployment "$DEP_ID" \
  | grep -qiE "RuntimeError|env.not_ready" && abort c-startup-error

# Tenant isolation smoke — token is read hidden, written to a mode-0600 curl header file,
# and unset BEFORE curl. The secret therefore appears in neither history nor process argv.
echo "paste a staging bearer token, then Enter (hidden):"; read -s SMOKE_TOKEN
AUTH_HEADER=~/.reorderos-cutover/smoke_auth.header
printf 'Authorization: Bearer %s\n' "$SMOKE_TOKEN" > "$AUTH_HEADER"
chmod 600 "$AUTH_HEADER"
unset SMOKE_TOKEN
TENANTS=$(curl -sS -H @"$AUTH_HEADER" -H "X-Tenant-Id: <own tenant>" \
  "$BASE/api/v1/auth/me" | "$PY" -c 'import sys,json;print(len(json.load(sys.stdin)["tenants"]))') \
  || abort c-me-failed
[ "$TENANTS" -ge 1 ] || abort c-no-tenants
CODE=$(curl -sS -o /dev/null -w '%{http_code}' -H @"$AUTH_HEADER" \
  -H "X-Tenant-Id: <a tenant the caller is NOT a member of>" "$BASE/api/v1/inventory/items")
[ "$CODE" = "403" ] || abort c-isolation
rm -f "$AUTH_HEADER"; unset AUTH_HEADER

# Pipelines (founder, real flows): receipt photo/PDF upload → extract → review → commit;
# Postmark inbound email → draft → extraction; Clover: inbox drain + reconciliation logs.
```

### C4. Post-cutover role proofs (BOTH roles, BEFORE cleanup)

```bash
# [LOCAL MAC]
pbcopy < ~/.reorderos-cutover/reorderos_app.pw
doctl apps console "$APP" api
```
```bash
# [DO API SHELL]  (DATABASE_URL is now the reorderos_app DSN)
read -s PW; export PW
python -m scripts.role_admin prove reorderos_app || { echo "CONTRACT VIOLATION — STOP -> R3"; exit 1; }
# EXPECT: rolcanlogin=True rolsuper=False rolbypassrls=False rolcreatedb=False
# rolcreaterole=False rolreplication=False rolinherit=True
# member_of=app_user admin_option=False + "prove reorderos_app: CONTRACT OK"
unset PW
exit
```
```bash
# [LOCAL MAC]
pbcopy < ~/.reorderos-cutover/service_worker.pw
doctl apps console "$APP" api
```
```bash
# [DO API SHELL]  (SERVICE_DATABASE_URL is now the service_worker DSN)
read -s PW; export PW
python -m scripts.role_admin prove service_worker || { echo "CONTRACT VIOLATION — STOP -> R3"; exit 1; }
# EXPECT: rolcanlogin=True rolsuper=False rolbypassrls=False rolcreatedb=False
# rolcreaterole=False rolreplication=False rolinherit=True
# member_of=(none) admin_option=False + "prove service_worker: CONTRACT OK"
unset PW
exit
```
If Phase B-alt was used, prove the deployed versioned role instead, using its matching
password file; its expected membership is exactly `service_worker`, without admin option.

### C5. Cleanup (ONLY after C3 + both C4 proofs pass)
```bash
# [LOCAL MAC]
cleanup_secrets   # plaintext DSNs/passwords/work files ONLY — rollback artifacts stay
```
`rollback_a0_spec.yaml` / `rollback_c_spec.yaml` remain the deliberate path back for
the whole observation window (and beyond, in B-alt, since `service_worker` keeps
LOGIN — B-alt.8 is unsupported). They become deletable only when superseded — by the
founder accepting the cutover as permanent or by a later ACTIVE deployment. Delete
them then via `cleanup_rollback_artifacts` with each artifact TAG named explicitly
(operator contract — see the function header); nothing deletes them by wildcard or as
a side effect.

---

## Rollback (no login role is ever dropped; `abort` preserved the rollback artifacts)
- **R1 (reorderos_app failed):** disable its login (role + membership remain). In
  `[DO API SHELL]` (DATABASE_URL is still the doadmin DSN pre-cutover):
  ```bash
  python - <<'EOF'
import asyncio, os, asyncpg
dsn = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
async def go():
    c = await asyncpg.connect(dsn)
    await c.execute("ALTER ROLE reorderos_app NOLOGIN")
    await c.close()
asyncio.run(go())
EOF
  ```
- **R2 (service_worker failed):** re-run the Phase B rotate with a fresh password (B0
  proved the running deployment does not use this role, so nothing running is affected
  and every rollback artifact remains valid).
- **R3 (Phase C gate fails):** the deploy fails closed; the previous healthy deployment
  keeps serving (doadmin DSNs — untouched by B, per B0). To revert deliberately:
  `DEP_ID=$(doctl apps update "$APP" --spec ~/.reorderos-cutover/rollback_c_spec.yaml \
  --format InProgressDeployment.ID --no-header) && wait_active "$DEP_ID"` — the artifact
  was captured from the PRE-ACTIVE deployment via `spec get --deployment` and survives
  every `abort`, so it cannot be the failed cutover spec. Re-verify `/health/ready`,
  THEN `cleanup_secrets`. `0035` may stay (harmless under doadmin) or
  `alembic downgrade 0034`.

## Cleanup
```bash
# [LOCAL MAC] — ONLY at successful completion or after a VERIFIED rollback
cleanup_secrets   # rollback_*_spec.yaml are NOT touched by this
# ~/.reorderos-cutover/logs/ (non-secret forensics) may be kept or removed at will.
# [DO API SHELL] (ephemeral, but be tidy):  unset PW 2>/dev/null; exit
```
Rollback artifacts are removed ONLY via `cleanup_rollback_artifacts` (each TAG named
explicitly, founder-approved) once superseded — see the retention rules in its header
and in B-alt.8.
