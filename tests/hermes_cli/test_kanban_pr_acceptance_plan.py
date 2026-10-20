"""Free-plan rules/branches reads fail closed with a classified receipt (#141987)."""
import pytest

from hermes_cli import kanban_pr_acceptance as kpa

SHA = "a" * 40
CONTRACT = "https://github.com/octo/repo/pull/7"


def _graphql_repo(branch_protection=None):
    return {"data": {"repository": {"pullRequest": {
        "headRefOid": SHA, "baseRefName": "master", "state": "OPEN",
        "baseRef": {"branchProtectionRule": branch_protection}}}}}


def _make_fake_api(rules_status=None, rules_payload=None, checks_payload=None):
    def fake(endpoint, *, query=None, paginate=False, profile_home=None):
        if endpoint == "graphql":
            return _graphql_repo()
        if "/rules/branches/" in endpoint:
            if rules_status is not None:
                raise kpa._GateAuthError(f"HTTP {rules_status} on {endpoint.split('?')[0]}")
            return rules_payload
        if "/check-runs" in endpoint:
            return checks_payload["runs"]
        if "/statuses" in endpoint:
            return checks_payload["statuses"]
        if endpoint.startswith("repos/octo/repo/pulls/"):
            return {"head": {"sha": SHA}, "base": {"ref": "master"}, "state": "open"}
        raise AssertionError(f"unexpected endpoint {endpoint!r}")
    return fake


def _success_checks():
    run = {"id": 1, "name": "ci", "app": {"id": 1}, "conclusion": "success",
           "status": "completed", "head_sha": SHA, "html_url": "http://x/runs/1"}
    return {"runs": [{"check_runs": [run], "total_count": 1}], "statuses": [[]]}


def test_free_plan_403_returns_protection_unavailable(monkeypatch):
    monkeypatch.setattr(kpa, "_api", _make_fake_api(rules_status=403))
    receipt = kpa.collect_acceptance(CONTRACT, None)
    assert receipt["ok"] is False
    assert receipt["classification"] == "protection_unavailable"
    assert receipt["head_sha"] == SHA
    assert receipt["required"] == []
    assert receipt["enforced"] is False
    assert "Free-plan" in receipt["detail"]
    assert "infrastructure" not in receipt["detail"].lower()


def test_unprotected_branch_404_falls_back_to_graphql(monkeypatch):
    monkeypatch.setattr(kpa, "_api", _make_fake_api(rules_status=404))
    receipt = kpa.collect_acceptance(CONTRACT, None)
    assert receipt["ok"] is False
    assert receipt["classification"] == "missing"
    assert "local-only" in receipt["detail"]
    assert "infrastructure" not in receipt["detail"].lower()


def test_rules_success_path_still_completes(monkeypatch):
    rules = [[{"type": "required_status_checks", "parameters":
               {"required_status_checks": [{"context": "ci", "integration_id": 1}]}}]]
    monkeypatch.setattr(kpa, "_api", _make_fake_api(rules_payload=rules,
                                                    checks_payload=_success_checks()))
    receipt = kpa.collect_acceptance(CONTRACT, None)
    assert receipt["ok"] is True
    assert receipt["classification"] == "success"
    assert receipt["checks"] == [{"name": "ci", "id": 1, "url": "http://x/runs/1",
                                  "head_sha": SHA, "classification": "success",
                                  "conclusion": "success"}]
