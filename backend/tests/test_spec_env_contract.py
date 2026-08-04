"""Committed DO specs — component preservation + per-component environment contract.

STAGING (cutover candidate): every DO component declares APP_COMPONENT, and its
EFFECTIVE env (app-level ∪ component-level) must satisfy exactly the requirement set in
app.core.component_requirements — with its SECRET-typed keys equal to EXACTLY the
component's required secrets (least privilege: overexposure is a test failure, not a
review catch). The migrate job's effective secret set is {DATABASE_URL} and nothing
else, and it declares no SOURCE_COMMIT (job-level ${_self.COMMIT_HASH} is unproven).

PRODUCTION (legacy posture): no APP_COMPONENT anywhere (flag false → the 'legacy'
compatibility profile, i.e. today's exact behavior); each component's env still covers
what the legacy profile demands.

Keys are checked by NAME/scope/type only; secret values never appear in committed
specs (enforced by scripts.deploy_verify.validate_no_secret_values).
"""

from __future__ import annotations

import os

import pytest
import yaml

from app.core.component_requirements import required_env_names

_DO_DIR = os.path.join(os.path.dirname(__file__), "..", "..", ".do")
_TRUTHY = {"1", "true", "yes", "on"}

# DO component name -> declared APP_COMPONENT (the staging cutover contract).
_STAGING_COMPONENTS = {
    ("services", "api"): "api",
    ("workers", "inbox-worker"): "inbox_worker",
    ("workers", "reconciliation-worker"): "reconciliation_worker",
    ("workers", "receipt-extraction-worker"): "receipt_extraction_worker",
    ("workers", "inbound-email-worker"): "inbound_email_worker",
    ("jobs", "migrate"): "migrate_job",
}


def _spec(name: str) -> dict:
    with open(os.path.join(_DO_DIR, name)) as f:
        return yaml.safe_load(f)


def _component(spec: dict, kind: str, name: str) -> dict:
    comp = next((c for c in spec.get(kind) or [] if c.get("name") == name), None)
    assert comp is not None, f"{kind}/{name} missing from spec"
    return comp


def _keys(envs: list | None) -> set[str]:
    return {e["key"] for e in envs or []}


def _secret_keys(envs: list | None) -> set[str]:
    return {e["key"] for e in envs or [] if e.get("type") == "SECRET"}


def _env_value(envs: list | None, key: str) -> str | None:
    for e in envs or []:
        if e.get("key") == key:
            return str(e.get("value")) if e.get("value") is not None else None
    return None


def _flags(spec: dict) -> dict[str, bool]:
    return {
        e["key"]: str(e.get("value", "")).strip().lower() in _TRUTHY for e in spec.get("envs") or []
    }


# ── staging: complete component preservation ──────────────────────────────────
def test_staging_has_exactly_the_live_component_set_plus_reconciliation() -> None:
    """The cutover keeps every live Sprint 6 component (api, inbox-worker,
    receipt-extraction-worker, inbound-email-worker, migrate) and adds
    reconciliation-worker (Clover worker-pair rule). Dropping ANY of these breaks a
    live pipeline — this test is the tripwire."""
    spec = _spec("staging.app.yaml")
    assert {s["name"] for s in spec["services"]} == {"api"}
    assert {w["name"] for w in spec["workers"]} == {
        "inbox-worker",
        "reconciliation-worker",
        "receipt-extraction-worker",
        "inbound-email-worker",
    }
    assert {(j["name"], j.get("kind")) for j in spec["jobs"]} == {("migrate", "PRE_DEPLOY")}


@pytest.mark.parametrize(("kind", "do_name"), sorted(_STAGING_COMPONENTS))
def test_staging_component_env_contract(kind: str, do_name: str) -> None:
    """Per component: APP_COMPONENT declared; effective env covers the component's
    required set; component-level SECRET keys are EXACTLY the required secrets that are
    secrets (no inherited secrets exist — app-level is non-secret only)."""
    spec = _spec("staging.app.yaml")
    comp = _component(spec, kind, do_name)
    app_component = _STAGING_COMPONENTS[(kind, do_name)]
    flags = _flags(spec)
    assert flags["CLOVER_ENABLED"] and flags["POSTMARK_INBOUND_ENABLED"]
    assert flags["RESTRICTED_RUNTIME_ROLES_ENABLED"]

    assert _env_value(comp.get("envs"), "APP_COMPONENT") == app_component

    required = set(required_env_names(app_component, flags))
    effective = _keys(spec.get("envs")) | _keys(comp.get("envs"))
    missing = required - effective
    assert not missing, f"{do_name} effective env missing: {sorted(missing)}"

    # Least privilege, exact: the component's SECRET-typed keys are precisely the
    # required keys that this repo treats as secrets (component_requirements marks
    # them secret=True → they are declared type SECRET in the spec).
    from app.core.component_requirements import required_vars

    expected_secrets = {v.name for v in required_vars(app_component, flags) if v.secret}
    assert _secret_keys(comp.get("envs")) == expected_secrets, (
        f"{do_name}: component secrets {sorted(_secret_keys(comp.get('envs')))} != "
        f"required {sorted(expected_secrets)} (over- or under-exposure)"
    )


def test_staging_app_level_carries_no_secrets() -> None:
    """No secret is inherited by anything — in particular not by the migrate job."""
    spec = _spec("staging.app.yaml")
    assert _secret_keys(spec.get("envs")) == set()


def test_staging_migrate_job_secret_isolation() -> None:
    """THE required isolation property: the migrate job's effective secret set is
    exactly {DATABASE_URL} — it inherits nothing and declares nothing else. Also: no
    SOURCE_COMMIT on the job (job-level ${_self.COMMIT_HASH} support is unproven)."""
    spec = _spec("staging.app.yaml")
    job = _component(spec, "jobs", "migrate")
    effective_secret_keys = _secret_keys(spec.get("envs")) | _secret_keys(job.get("envs"))
    assert effective_secret_keys == {"DATABASE_URL"}
    assert "SOURCE_COMMIT" not in _keys(job.get("envs"))


def test_staging_services_and_workers_declare_source_commit_jobs_do_not() -> None:
    spec = _spec("staging.app.yaml")
    for svc in spec["services"]:
        assert _env_value(svc["envs"], "SOURCE_COMMIT") == "${_self.COMMIT_HASH}"
    for wrk in spec["workers"]:
        assert _env_value(wrk["envs"], "SOURCE_COMMIT") == "${_self.COMMIT_HASH}"
    for job in spec["jobs"]:
        assert "SOURCE_COMMIT" not in _keys(job.get("envs"))


def test_staging_inbox_worker_carries_no_clover_credentials() -> None:
    """Trace-backed least privilege (the historical overexposure this PR removes):
    CLOVER_APP_SECRET's only consumer is the API OAuth exchange."""
    spec = _spec("staging.app.yaml")
    for worker in ("inbox-worker", "reconciliation-worker"):
        comp = _component(spec, "workers", worker)
        assert "CLOVER_APP_SECRET" not in _keys(comp.get("envs"))
        assert "CLOVER_WEBHOOK_AUTH_CODE" not in _keys(comp.get("envs"))


def test_staging_postmark_inbound_address_is_carry_from_live_config() -> None:
    """Configuration, not a credential: declared GENERAL with an empty value (the
    builder's carry-from-live marker); --cutover validation then requires it filled."""
    spec = _spec("staging.app.yaml")
    api = _component(spec, "services", "api")
    entry = next(e for e in api["envs"] if e["key"] == "POSTMARK_INBOUND_ADDRESS")
    assert entry.get("type") != "SECRET"
    assert str(entry.get("value") or "") == ""


# ── production: legacy posture, internally consistent ─────────────────────────
def test_prod_component_set() -> None:
    spec = _spec("app.yaml")
    assert {s["name"] for s in spec["services"]} == {"api"}
    assert {w["name"] for w in spec["workers"]} == {"inbox-worker", "reconciliation-worker"}


def test_prod_spec_has_no_app_component_anywhere() -> None:
    """Production stays on the legacy compatibility profile (flag false + APP_COMPONENT
    unset = today's exact global fail-closed behavior). Declaring APP_COMPONENT there
    would change which secrets are demanded — that belongs to a production cutover, not
    this PR."""
    spec = _spec("app.yaml")
    assert "APP_COMPONENT" not in _keys(spec.get("envs"))
    for kind in ("services", "workers", "jobs"):
        for comp in spec.get(kind) or []:
            assert "APP_COMPONENT" not in _keys(comp.get("envs")), comp.get("name")


@pytest.mark.parametrize("name", ["api", "inbox-worker", "reconciliation-worker"])
def test_prod_component_env_covers_legacy_requirements(name: str) -> None:
    """Every prod component's effective env covers the legacy profile (what Settings
    demands there today) — the WORKOS_CLIENT_ID/JWKS_URL gap that used to lurk in
    inbox-worker stays fixed."""
    spec = _spec("app.yaml")
    kind = "services" if name == "api" else "workers"
    comp = _component(spec, kind, name)
    flags = _flags(spec)
    assert flags.get("CLOVER_ENABLED") is True
    assert not flags.get("POSTMARK_INBOUND_ENABLED", False)
    required = set(required_env_names("legacy", flags))
    effective = _keys(spec.get("envs")) | _keys(comp.get("envs"))
    missing = required - effective
    assert not missing, f"prod {name} env missing: {sorted(missing)}"


def test_prod_api_also_covers_its_full_component_set() -> None:
    """Forward-compatibility: the prod api env already carries everything the 'api'
    component profile will demand at its future cutover (WorkOS secret, Anthropic —
    wait, Anthropic is deliberately NOT in prod yet) — so this asserts the known
    delta explicitly: the ONLY api-profile keys prod does not declare are the
    receipts-era additions that arrive with the prod receipts rollout."""
    spec = _spec("app.yaml")
    api = _component(spec, "services", "api")
    flags = _flags(spec)
    effective = _keys(spec.get("envs")) | _keys(api.get("envs"))
    missing = set(required_env_names("api", flags)) - effective
    assert missing == {"ANTHROPIC_API_KEY"}, (
        f"prod api vs future component profile drifted: missing {sorted(missing)} "
        f"(expected exactly ANTHROPIC_API_KEY until the prod receipts rollout)"
    )
