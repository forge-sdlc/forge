# Forge Helm chart

This chart deploys the Forge API, worker, Redis Stack, worker RBAC, and the
workspace PVC used by Kubernetes sandbox Jobs. It targets OpenShift by default.

Create the namespace and application secret before installing. Keep secrets out
of Helm values and source control:

```bash
oc new-project forge
oc create secret generic forge-env --from-env-file=.env -n forge
helm upgrade --install forge charts/forge -n forge
```

Build and push two images before installing:

* `image.repository`: the Forge API/worker image built from `Dockerfile`
* `sandboxImage.repository`: the task image built from `containers/Containerfile`

Override repositories, tags, storage class, or Route settings in a local values
file. The worker uses in-cluster service-account authentication; do not mount a
kubeconfig into it.
