# Forge Helm chart

This chart deploys the Forge API, worker, Redis Stack, worker RBAC, and the
workspace PVC used by Kubernetes sandbox Jobs. It targets OpenShift by default.

Create the namespace and application secret before installing. Keep secrets out
of Helm values and source control. Values consumed as JSON, such as
`MODEL_CONNECTIONS`, must contain raw JSON in the env file; do not surround
them with shell quotes because `oc --from-env-file` preserves those quotes.

```bash
oc new-project forge
oc create secret generic forge-env --from-env-file=.env -n forge
helm upgrade --install forge charts/forge -n forge
```

For Vertex AI, create a Secret from the same service-account credential used by
the existing Forge deployment and enable the optional mount. The chart mounts
the credential into both the worker and every sandbox Job:

```bash
oc create secret generic google-adc \
  --from-file=forge-gcp-credentials.json=/path/to/forge-gcp-credentials.json \
  -n forge
helm upgrade --install forge charts/forge -n forge \
  --set googleCredentials.enabled=true
```

Build and push two images before installing:

* `image.repository`: the Forge API/worker image built from `Dockerfile`
* `sandboxImage.repository`: the task image built from `containers/Containerfile`

Override repositories, tags, storage class, or Route settings in a local values
file. The worker uses in-cluster service-account authentication; do not mount a
kubeconfig into it.

When `redis.enabled=false`, provide `REDIS_URL` in `existingSecret`. The chart
only injects its internal Redis URL when the bundled Redis deployment is enabled.

The workspace PVC is retained when the Helm release is removed. Delete it
explicitly when its workspaces are no longer needed.

Sandbox Jobs do not mount service-account tokens and are selected by a default
deny NetworkPolicy. Jobs configured with networking may use DNS and public
egress, while the private CIDRs listed under `sandbox.networkPolicy.privateCidrs`
remain blocked. Adjust that list for the cluster network before deployment.

## Pod UID and workspace ownership

By default `runtimeSecurity.runAsUser` and `runtimeSecurity.fsGroup` are empty, which suits
OpenShift: its SCC assigns a non-root UID and an fsGroup from the namespace
range to the API, worker, and every sandbox Job.

On vanilla Kubernetes there is no SCC to assign these, so `runAsNonRoot` cannot
be satisfied and the worker-created PVC files may not be readable by sandbox
pods. Set both values so the worker and sandbox pods share a UID and fsGroup:

```bash
helm upgrade --install forge charts/forge -n forge \
  --set runtimeSecurity.runAsUser=1000 \
  --set runtimeSecurity.fsGroup=1000
```

`runAsUser` should be valid for both application and sandbox images. The
`fsGroup` is applied with `fsGroupChangePolicy: OnRootMismatch`, ensuring files
created by the worker remain accessible to sandbox Jobs.
