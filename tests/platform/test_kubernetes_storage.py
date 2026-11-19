import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART_DIR = Path(__file__).parents[2] / "deploy" / "helm" / "alephat"


def _render_chart(*extra_args: str) -> list[dict]:
    if shutil.which("helm") is None:
        pytest.skip("helm CLI not installed")
    out = subprocess.check_output(
        [
            "helm",
            "template",
            "alephat",
            str(CHART_DIR),
            "--namespace",
            "alephat",
            *extra_args,
        ],
        text=True,
    )
    return [doc for doc in yaml.safe_load_all(out) if doc]


def _document(documents: list[dict], kind: str, name: str, *, namespace: str | None = None) -> dict:
    return next(
        document
        for document in documents
        if document["kind"] == kind
        and document["metadata"]["name"] == name
        and (namespace is None or document["metadata"].get("namespace") == namespace)
    )


def test_alephat_and_dependencies_deploy_into_separate_namespaces():
    docs = _render_chart()
    created_namespaces = {d["metadata"]["name"] for d in docs if d["kind"] == "Namespace"}
    assert created_namespaces == {
        "alephat-sandboxes",
        "postgresql",
        "temporal",
        "litellm",
        "agent-sandbox-system",
        "kata-system",
        "keda",
        "external-secrets",
        "traefik",
    }

    # Alephat's own workloads live exclusively in `alephat`; dependencies live in their own namespaces
    expected_workloads = [
        ("Deployment", "alephat-api", "alephat"),
        ("Deployment", "alephat-webhook", "alephat"),
        ("Deployment", "alephat-harness", "alephat"),
        ("Deployment", "alephat-ui", "alephat"),
        ("Job", "alephat-migrate", "alephat"),
        ("StatefulSet", "alephat-postgresql", "postgresql"),
        ("Deployment", "temporal", "temporal"),
        ("Deployment", "temporal-ui", "temporal"),
        ("Job", "temporal-namespace", "temporal"),
        ("Deployment", "litellm", "litellm"),
        ("StatefulSet", "litellm-postgresql", "litellm"),
        ("Deployment", "agent-sandbox-controller", "agent-sandbox-system"),
        ("DaemonSet", "kata-deploy", "kata-system"),
        ("Deployment", "keda-operator", "keda"),
        ("Deployment", "external-secrets", "external-secrets"),
        ("Deployment", "traefik", "traefik"),
        ("SandboxTemplate", "python-runtime-template", "alephat-sandboxes"),
    ]
    for kind, name, expected_ns in expected_workloads:
        obj = _document(docs, kind, name, namespace=expected_ns)
        assert obj["metadata"]["namespace"] == expected_ns


def test_postgresql_and_litellm_use_gke_persistent_disks():
    docs = _render_chart()

    pg_claim = _document(
        docs, "PersistentVolumeClaim", "alephat-postgresql-data", namespace="postgresql"
    )
    pg_ss = _document(docs, "StatefulSet", "alephat-postgresql", namespace="postgresql")
    assert pg_claim["spec"]["storageClassName"] == "standard-rwo"
    assert pg_claim["spec"]["resources"]["requests"]["storage"] == "20Gi"
    assert (
        pg_ss["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"]
        == "alephat-postgresql-data"
    )

    llm_claim = _document(
        docs, "PersistentVolumeClaim", "litellm-postgresql-data", namespace="litellm"
    )
    assert llm_claim["spec"]["storageClassName"] == "standard-rwo"
    assert llm_claim["spec"]["resources"]["requests"]["storage"] == "20Gi"

    llm_cm = _document(docs, "ConfigMap", "litellm-config", namespace="litellm")
    llm_cfg = yaml.safe_load(llm_cm["data"]["config.yaml"])
    by_name = {entry["model_name"]: entry["litellm_params"] for entry in llm_cfg["model_list"]}
    assert by_name["google:gemini-4-hydrogen"] == {
        "model": "gemini/gemini-4-hydrogen",
        "api_key": "os.environ/GEMINI_API_KEY",
    }
    assert by_name["anthropic:claude-opus-5-5"] == {
        "model": "anthropic/claude-opus-5-5",
        "api_key": "os.environ/ANTHROPIC_API_KEY",
    }
    assert by_name["openai:gpt-6.1-terra"] == {
        "model": "openai/gpt-6.1-terra",
        "api_key": "os.environ/OPENAI_API_KEY",
    }


def test_temporal_uses_cross_namespace_postgresql_mtls_traefik_and_keda_scaler():
    docs = _render_chart()

    server_cfg_cm = _document(docs, "ConfigMap", "temporal-server-config", namespace="temporal")
    cfg_yaml = yaml.safe_load(server_cfg_cm["data"]["config.template.yaml"])
    assert cfg_yaml["global"]["tls"]["frontend"]["server"]["requireClientAuth"] is True
    assert cfg_yaml["global"]["tls"]["internode"]["server"]["requireClientAuth"] is True
    assert (
        cfg_yaml["global"]["tls"]["frontend"]["client"]["serverName"]
        == "temporal.temporal.svc.cluster.local"
    )

    temporal_dep = _document(docs, "Deployment", "temporal", namespace="temporal")
    pod_spec = temporal_dep["spec"]["template"]["spec"]
    env = {item["name"]: item for item in pod_spec["containers"][0]["env"]}
    assert env["DB"]["value"] == "postgres12"
    assert env["POSTGRES_SEEDS"]["value"] == "alephat-postgresql.postgresql.svc.cluster.local"
    assert any(v.get("secret", {}).get("secretName") == "temporal-tls" for v in pod_spec["volumes"])

    for name in ("alephat-api", "alephat-webhook", "alephat-harness"):
        workload = _document(docs, "Deployment", name, namespace="alephat")
        vols = workload["spec"]["template"]["spec"]["volumes"]
        assert any(v.get("secret", {}).get("secretName") == "temporal-tls" for v in vols)

    trigger_auth = _document(
        docs, "TriggerAuthentication", "keda-trigger-auth-temporal", namespace="alephat"
    )
    auth_params = {item["parameter"]: item for item in trigger_auth["spec"]["secretTargetRef"]}
    assert set(auth_params) == {"ca", "cert", "key"}
    assert auth_params["ca"]["name"] == "temporal-tls"

    scaled = _document(docs, "ScaledObject", "alephat-harness", namespace="alephat")
    trigger = scaled["spec"]["triggers"][0]
    assert trigger["type"] == "temporal"
    assert trigger["authenticationRef"]["name"] == "keda-trigger-auth-temporal"
    assert trigger["metadata"]["endpoint"] == "temporal.temporal.svc.cluster.local:7233"
    assert trigger["metadata"]["taskQueue"] == "alephat-agents"
    assert trigger["metadata"]["queueTypes"] == "workflow,activity"
    assert trigger["metadata"]["includeRunningWorkflowCount"] == "true"

    traefik_svc = _document(docs, "Service", "traefik", namespace="traefik")
    ports = {p["name"]: p["port"] for p in traefik_svc["spec"]["ports"]}
    assert ports == {"web": 80, "websecure": 443, "temporal": 7233}

    tls_option = _document(docs, "TLSOption", "temporal-grpc-mtls", namespace="temporal")
    assert tls_option["spec"]["clientAuth"]["clientAuthType"] == "RequireAndVerifyClientCert"
    assert tls_option["spec"]["clientAuth"]["secretNames"] == ["temporal-tls"]

    transport = _document(docs, "ServersTransport", "temporal-grpc-backend", namespace="temporal")
    assert transport["spec"]["certificatesSecrets"] == ["temporal-tls"]
    assert transport["spec"]["rootCAs"] == [{"secret": "temporal-tls"}]

    grpc_route = _document(docs, "IngressRoute", "temporal-grpc", namespace="temporal")
    assert grpc_route["spec"]["entryPoints"] == ["websecure", "temporal"]
    assert grpc_route["spec"]["tls"]["certResolver"] == "letsencrypt"
    assert (
        _document(docs, "IngressRoute", "temporal-ui", namespace="temporal")["spec"]["tls"][
            "certResolver"
        ]
        == "letsencrypt"
    )
    assert (
        _document(docs, "IngressRoute", "alephat", namespace="alephat")["spec"]["tls"][
            "certResolver"
        ]
        == "letsencrypt"
    )


def test_standalone_mode_generates_consistent_cross_namespace_secrets_and_defaults():
    docs = _render_chart("--set", "externalSecrets.enabled=false")
    config_map = _document(docs, "ConfigMap", "alephat-config", namespace="alephat")

    assert config_map["data"]["DEFAULT_SANDBOX_PROVIDER"] == "agent_sandbox"
    assert config_map["data"]["BOOTSTRAP_DEV_DEFAULTS"] == "true"
    assert config_map["data"]["DATABASE_HOST"] == "alephat-postgresql.postgresql.svc.cluster.local"
    assert config_map["data"]["TEMPORAL_ADDRESS"] == "temporal.temporal.svc.cluster.local:7233"
    assert config_map["data"]["LITELLM_BASE_URL"] == "http://litellm.litellm.svc.cluster.local:4000"

    pg_passwords = {
        d["metadata"]["namespace"]: d["stringData"]["password"]
        for d in docs
        if d["kind"] == "Secret" and d["metadata"]["name"] == "alephat-postgres"
    }
    assert set(pg_passwords) == {"postgresql", "temporal", "alephat"}
    assert len(set(pg_passwords.values())) == 1

    tls_certs = {
        d["metadata"]["namespace"]: d["stringData"]["ca.crt"]
        for d in docs
        if d["kind"] == "Secret" and d["metadata"]["name"] == "temporal-tls"
    }
    assert set(tls_certs) == {"temporal", "alephat"}
    assert len(set(tls_certs.values())) == 1


def test_agent_sandbox_uses_git_enabled_runtime_image_and_readiness_probe():
    docs = _render_chart()
    template = _document(
        docs, "SandboxTemplate", "python-runtime-template", namespace="alephat-sandboxes"
    )
    container = template["spec"]["podTemplate"]["spec"]["containers"][0]
    dockerfile = (CHART_DIR.parents[1] / "Dockerfile.sandbox").read_text(encoding="utf-8")

    assert container["image"].endswith("/alephat/alephat-sandbox:latest")
    assert container["readinessProbe"]["tcpSocket"]["port"] == "toolbox"
    assert "python-runtime-sandbox:v0.1.0" in dockerfile
    assert "ca-certificates curl gh git" in dockerfile
    assert "COPY deploy/sandbox-gh /usr/local/bin/gh" in dockerfile


def test_substrate_sandbox_selectable_in_helm_chart_and_docker_compose():
    docs = _render_chart(
        "--set",
        "alephat.config.defaultSandboxProvider=substrate",
        "--set",
        "substrate.workerPool.enabled=true",
    )
    config_map = _document(docs, "ConfigMap", "alephat-config", namespace="alephat")
    assert config_map["data"]["DEFAULT_SANDBOX_PROVIDER"] == "substrate"
    assert config_map["data"]["SUBSTRATE_API_ENDPOINT"] == "api.ate-system.svc:443"
    assert config_map["data"]["SUBSTRATE_ROUTER_URL"] == "https://atenet-router.ate-system.svc:443"
    assert config_map["data"]["SUBSTRATE_ATESPACE"] == "alephat-sandboxes"
    assert config_map["data"]["SUBSTRATE_ACTOR_TEMPLATE"] == "alephat-sandbox"

    harness = _document(docs, "Deployment", "alephat-harness", namespace="alephat")
    harness_vols = {v["name"]: v for v in harness["spec"]["template"]["spec"]["volumes"]}
    assert "substrate-credentials" in harness_vols

    pool = _document(docs, "WorkerPool", "alephat-sandbox-pool", namespace="alephat-substrate")
    assert pool["apiVersion"] == "ate.dev/v1"
    assert pool["spec"]["sandboxClasses"] == [{"name": "gvisor"}]

    template_cm = _document(
        docs, "ConfigMap", "alephat-substrate-actor-template", namespace="alephat-substrate"
    )
    assert "atespace: alephat-sandboxes" in template_cm["data"]["actor-template.yaml"]

    compose_path = CHART_DIR.parents[1] / "docker-compose.yml"
    compose_doc = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    harness_env = compose_doc["services"]["harness"]["environment"]
    assert "DEFAULT_SANDBOX_PROVIDER" in harness_env
    assert "SUBSTRATE_API_ENDPOINT" in harness_env
    assert "SUBSTRATE_ROUTER_URL" in harness_env
    assert "SUBSTRATE_SANDBOX_PROTOCOL" in harness_env
    assert "substrate-sandbox" in compose_doc["services"]
