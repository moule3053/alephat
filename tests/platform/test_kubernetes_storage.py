from pathlib import Path

import yaml

DEPLOY_DIR = Path(__file__).parents[2] / "deploy" / "k8s"


def _documents(name: str) -> list[dict]:
    with (DEPLOY_DIR / name).open(encoding="utf-8") as manifest:
        return [document for document in yaml.safe_load_all(manifest) if document]


def _document(documents: list[dict], kind: str, name: str) -> dict:
    return next(
        document
        for document in documents
        if document["kind"] == kind and document["metadata"]["name"] == name
    )


def test_postgresql_uses_gke_persistent_disk():
    documents = _documents("postgresql.yaml")
    claim = _document(documents, "PersistentVolumeClaim", "alephat-postgresql-data")
    stateful_set = _document(documents, "StatefulSet", "alephat-postgresql")

    assert claim["spec"]["storageClassName"] == "standard-rwo"
    assert claim["spec"]["resources"]["requests"]["storage"] == "20Gi"
    volume = stateful_set["spec"]["template"]["spec"]["volumes"][0]
    assert volume["persistentVolumeClaim"]["claimName"] == "alephat-postgresql-data"
    assert "emptyDir" not in volume


def test_temporal_uses_postgresql_persistence_mtls_traefik_and_keda_scaler():
    documents = _documents("temporal.yaml")
    server_cfg_cm = _document(documents, "ConfigMap", "temporal-server-config")
    cfg_yaml = yaml.safe_load(server_cfg_cm["data"]["config.template.yaml"])
    assert cfg_yaml["global"]["tls"]["frontend"]["server"]["requireClientAuth"] is True
    assert cfg_yaml["global"]["tls"]["internode"]["server"]["requireClientAuth"] is True

    deployment = _document(documents, "Deployment", "temporal")
    pod_spec = deployment["spec"]["template"]["spec"]
    env = {item["name"]: item for item in pod_spec["containers"][0]["env"]}
    assert env["DB"]["value"] == "postgres12"
    assert env["POSTGRES_SEEDS"]["value"] == "alephat-postgresql"
    assert any(v.get("secret", {}).get("secretName") == "temporal-tls" for v in pod_spec["volumes"])

    for manifest, name in (
        ("api-deployment.yaml", "alephat-api"),
        ("webhook-deployment.yaml", "alephat-webhook"),
        ("harness-deployment.yaml", "alephat-harness"),
    ):
        workload = _document(_documents(manifest), "Deployment", name)
        vols = workload["spec"]["template"]["spec"]["volumes"]
        assert any(v.get("secret", {}).get("secretName") == "temporal-tls" for v in vols)

    keda_docs = _documents("harness-keda.yaml")
    trigger_auth = _document(keda_docs, "TriggerAuthentication", "keda-trigger-auth-temporal")
    auth_params = {item["parameter"]: item for item in trigger_auth["spec"]["secretTargetRef"]}
    assert set(auth_params) == {"ca", "cert", "key"}
    assert auth_params["ca"]["name"] == "temporal-tls"

    scaled = _document(keda_docs, "ScaledObject", "alephat-harness")
    trigger = scaled["spec"]["triggers"][0]
    assert trigger["type"] == "temporal"
    assert trigger["authenticationRef"]["name"] == "keda-trigger-auth-temporal"
    assert trigger["metadata"]["taskQueue"] == "alephat-agents"
    assert trigger["metadata"]["queueTypes"] == "workflow,activity"
    assert trigger["metadata"]["includeRunningWorkflowCount"] == "true"

    with (DEPLOY_DIR / "traefik-values.yaml").open(encoding="utf-8") as traefik_values_file:
        traefik_values = yaml.safe_load(traefik_values_file)
    assert traefik_values["ports"]["temporal"]["exposedPort"] == 7233
    assert traefik_values["ports"]["temporal"]["http"]["tls"]["enabled"] is True

    ingress_docs = _documents("traefik-ingressroute.yaml")
    tls_option = _document(ingress_docs, "TLSOption", "temporal-grpc-mtls")
    assert tls_option["spec"]["clientAuth"]["clientAuthType"] == "RequireAndVerifyClientCert"
    assert tls_option["spec"]["clientAuth"]["secretNames"] == ["temporal-tls"]

    transport = _document(ingress_docs, "ServersTransport", "temporal-grpc-backend")
    assert transport["spec"]["certificatesSecrets"] == ["temporal-tls"]
    assert transport["spec"]["rootCAs"] == [{"secret": "temporal-tls"}]

    grpc_route = _document(ingress_docs, "IngressRoute", "temporal-grpc")
    assert grpc_route["spec"]["entryPoints"] == ["websecure", "temporal"]
    assert grpc_route["spec"]["tls"]["certResolver"] == "letsencrypt"
    assert (
        _document(ingress_docs, "IngressRoute", "temporal-ui")["spec"]["tls"]["certResolver"]
        == "letsencrypt"
    )


def test_litellm_postgresql_uses_gke_persistent_disk():
    with (DEPLOY_DIR / "litellm-values.yaml").open(encoding="utf-8") as values_file:
        values = yaml.safe_load(values_file)

    persistence = values["postgresql"]["primary"]["persistence"]
    assert persistence == {
        "enabled": True,
        "storageClass": "standard-rwo",
        "size": "20Gi",
    }


def test_standalone_defaults_are_synced_to_the_seeded_org():
    config_map = _document(_documents("configmap.yaml"), "ConfigMap", "alephat-config")

    assert config_map["data"]["DEFAULT_SANDBOX_PROVIDER"] == "agent_sandbox"
    assert config_map["data"]["BOOTSTRAP_DEV_DEFAULTS"] == "true"

    workload_documents = {
        "api-deployment.yaml": ("Deployment", "alephat-api"),
        "harness-deployment.yaml": ("Deployment", "alephat-harness"),
        "webhook-deployment.yaml": ("Deployment", "alephat-webhook"),
        "migration-job.yaml": ("Job", "alephat-migrate"),
    }
    for manifest, (kind, name) in workload_documents.items():
        workload = _document(_documents(manifest), kind, name)
        pod_spec = workload["spec"]["template"]["spec"]
        migration_container = (pod_spec.get("initContainers") or pod_spec["containers"])[0]
        assert migration_container["envFrom"] == [{"configMapRef": {"name": "alephat-config"}}]


def test_agent_sandbox_uses_git_enabled_runtime_image():
    template = _document(
        _documents("agent-sandbox-template.yaml"), "SandboxTemplate", "python-runtime-template"
    )
    container = template["spec"]["podTemplate"]["spec"]["containers"][0]
    dockerfile = (DEPLOY_DIR.parent / "Dockerfile.sandbox").read_text(encoding="utf-8")

    assert container["image"].endswith("/alephat/alephat-sandbox:latest")
    assert "python-runtime-sandbox:v0.1.0" in dockerfile
    assert "ca-certificates curl gh git" in dockerfile
    assert "COPY deploy/sandbox-gh /usr/local/bin/gh" in dockerfile
