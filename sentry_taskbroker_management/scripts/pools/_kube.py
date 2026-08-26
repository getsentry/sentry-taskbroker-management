from kubernetes import config  # type: ignore[import-untyped]


def load_kube_config() -> None:
    """Load in-cluster config when running as a Job, falling back to a local kubeconfig.

    These commands normally run as a Job inside the target region's cluster, so they
    authenticate with the pod's ServiceAccount. The kubeconfig fallback is for running
    a command locally against a cluster you already have credentials for.
    """
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
