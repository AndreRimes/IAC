# Infrastructure monitoring

Open https://grafana.andrerimes.com and sign in with the existing Grafana account.
**Infrastructure** is the home dashboard (UID `homelab-infra`). Dashboard JSON,
collector code, credentials encrypted with Sealed Secrets, and Helm values live
here. ArgoCD combines these manifests with kube-prometheus-stack **82.4.0**, release
`monitoring`, in namespace `monitoring`.

## What the numbers mean

- **CPU:** node busy/idle cores and namespace container CPU rates. Idle capacity is
  current headroom; it is different from the scheduler's unrequested capacity.
- **RAM:** node total minus available memory; namespaces use container working
  sets. Reclaimable cache is included in node available memory.
- **Scheduling:** allocatable CPU/RAM minus active pod requests (regular-container
  sum versus init-container maximum, plus pod overhead). The cluster currently has
  no restartable init sidecars; revisit request accounting before introducing them.
- **Disk:** node physical filesystem totals are deduplicated by device. Used and
  available space need not sum to capacity because of reserved filesystem blocks.
- **Namespace persistent disk:** allocated bytes for bound local-path PVCs, plus
  `/srv/media` for `media-namespace` and `/mnt/media`, `/mnt/jellyfin/config`, and
  `/mnt/jellyfin/cache` for the legacy `media` namespace. The same inode is counted
  once. Cross-namespace shared inodes are ambiguous and invalidate the measurement.
- **Namespace temporary disk:** kubelet's pod `ephemeral-storage.usedBytes`, which
  already includes writable layers, pod logs and disk-backed temporary volumes.
  Do not add container/volume figures to this aggregate again.
- **Namespace total disk:** persistent plus temporary bytes, only when both are
  available. This is measured usage, not a quota. Local-path PVC requested capacity
  is not an enforced disk limit. OS files, shared image layers, orphaned volumes and
  unconfigured host paths remain visible in node totals but are not attributed to
  namespaces. Directory scans cannot account for unlinked files still held open.

The disk collector is intentionally scoped to node `amdre`. It scans every five
minutes, with a four-minute process timeout. Scrapes read cached results; scans do
not overlap. Failed or older-than-ten-minute measurements disappear from usage
metrics instead of becoming zero. Collection status and last-success time are
always exposed. Expanding the cluster, changing storage provisioners or adding
application host paths requires updating its mounts, ownership mapping and RBAC.

The collector runs with read-only data mounts, no privilege escalation, a read-only
root filesystem, and only `DAC_READ_SEARCH`. Its service account can list PVs and
namespaces and GET the `amdre` node proxy for kubelet summary statistics. It cannot
read Kubernetes Secrets. Kubernetes node-proxy access is sensitive; keep this
account exclusive to the collector.

## Validation

```sh
python -B -m unittest discover -s kubernetes/platform/monitoring/tests -v
kubectl kustomize kubernetes/platform/monitoring
helm template monitoring kube-prometheus-stack \
  --repo https://prometheus-community.github.io/helm-charts --version 82.4.0 \
  --namespace monitoring --kube-version 1.29.15 --include-crds \
  -f kubernetes/platform/monitoring/values.yaml
```

Validate the rendered resources with `kubectl apply --server-side --dry-run=server`.
The first adoption may report ownership conflicts with Helm; only after reviewing
the diff, use `--force-conflicts --field-manager=argocd-controller` for the adopted
resources. ArgoCD subsequently owns their desired fields. Do not run independent
`helm upgrade` or `helm uninstall` against the retained Helm release record.

## Adoption and recovery

1. Record existing PVC UIDs/volume bindings, image versions and credential hashes.
   Back up Grafana's database before adoption. Do not delete either PVC.
2. Apply `kubernetes/platform/argocd/project.yaml` before introducing the monitoring
   Application: this project is bootstrapped by Ansible, not the root Application.
3. Annotate the existing `monitoring-grafana` Secret with
   `sealedsecrets.bitnami.com/managed=true` so the controller can adopt it. The
   committed SealedSecret contains the existing encrypted values. Secret and
   SealedSecret both have `Prune=false,Delete=false` protection.
4. Set `argocd.argoproj.io/sync-options=Prune=false,Delete=false` on the existing
   monitoring CRDs before adoption. The pinned chart ships static CRDs and cannot
   template these annotations; this bootstrap annotation remains owned separately
   from ArgoCD's schema fields. Grafana's PVC has the same protection in Helm values;
   Prometheus's generated PVC retains its existing name and binding.
5. Initially sync with automated pruning disabled. Verify Grafana's ownership init
   container succeeds and all three Grafana containers become ready. Confirm
   credentials, dashboard provisioning, Prometheus history and original PVC UIDs.
6. Route `grafana.andrerimes.com` to the existing Cloudflare tunnel and add the
   hostname to cloudflared's ingress rules before its fallback. The Ansible role
   maintains both the rule and DNS route. Validate tunnel ingress before restarting
   cloudflared. Prometheus remains ClusterIP-only.
7. Check both collector success metrics are `1`, compare sample usage with node
   filesystem blocks/kubelet summary, and verify the dashboard over HTTPS. Enable
   automated pruning only after this first sync passes.

Existing localhost-only control-plane scrape endpoints may appear under **Scrape
targets down**. Their networking is outside this change; node-exporter, kubelet,
kube-state-metrics, Grafana and the disk collector must be healthy.

For credentials, an administrator can retrieve the existing account locally:

```sh
kubectl -n monitoring get secret monitoring-grafana \
  -o jsonpath='{.data.admin-user}' | base64 --decode
kubectl -n monitoring get secret monitoring-grafana \
  -o jsonpath='{.data.admin-password}' | base64 --decode
```

Keep those values out of logs, screenshots and Git. The Grafana database remains the
source of truth if its password has subsequently been changed through the UI.

For rollback, disable automated sync before changing desired state, then revert the
monitoring change in Git and restore the previous workload configuration from the
pre-adoption snapshot. Preserve the working init-container permission fix. Never
uninstall the Helm release, delete the monitoring namespace, remove CRDs, or delete
PVCs to roll back this adoption. Removing the Application does not erase retained
data. The collector can be scaled down independently if scans cause excessive I/O.

Prometheus retains at most ten days or 15 GB of blocks (whichever is reached first),
leaving headroom within its existing 20 GiB PVC. Grafana retains its 2 GiB PVC.

## Adoption verified on 2026-10-14

- ArgoCD reports `Synced / Healthy`; automatic pruning is enabled after adoption.
- Both original PVC UIDs and PV bindings are unchanged, and the existing Grafana
  credentials still authenticate. Grafana survives a subsequent restart.
- Exactly one dashboard is provisioned: **Infrastructure**. All dashboard PromQL
  expressions return data; both disk collection kinds report success.
- Persistent storage samples match `du` on the node. Prometheus still contains its
  pre-adoption history, including a full day's 2,880 node-exporter samples.
- Public DNS and HTTPS reach the Grafana login page; anonymous dashboard access
  remains disabled. The ten collector regression tests pass.
