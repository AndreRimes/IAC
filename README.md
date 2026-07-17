# Andre Rimes's IAC for HomeLab

Single-node bare-metal Kubernetes homelab, provisioned by Ansible and reconciled by ArgoCD.

Ansible builds the node and installs the things ArgoCD itself depends on. Everything
above that layer lives in git and is synced by ArgoCD — the cluster is not configured
by hand.

## Layout

| Path | What |
|---|---|
| `ansible/` | Node build: netplan, tailscale, kubeadm + Flannel, ingress-nginx, cloudflared, sealed-secrets, argocd |
| `kubernetes/apps/` | Workload manifests, one Kustomize directory per app |
| `kubernetes/platform/argocd/` | The `AppProject`, the root app-of-apps, and one `Application` per workload under `apps/` |
| `kubernetes/platform/ingress-nginx/` | The `global-ingress` and its `ExternalName` shims |

`kubernetes/apps/nextcloud/` is **not** managed by ArgoCD — it holds a values file for a
hand-installed Helm release.

The split is by lifecycle, not by tool: `apps/` is what the cluster exists to run,
`platform/` is what makes running it possible.

`platform/ingress-nginx/` holds only the routing resources ArgoCD syncs. The ingress
controller itself stays an Ansible Helm release on purpose: it serves ArgoCD's own
traffic, so having ArgoCD manage it invites a lockout after a bad sync. Same reasoning
for `platform/argocd/` — the directory holds what ArgoCD is *told* to do; ArgoCD itself
is installed by `ansible/roles/argocd`.

## Architecture notes

Ingress is unusual here and worth knowing before editing `kubernetes/platform/ingress-nginx/`. A single
`global-ingress` lives in the `ingress-nginx` namespace and reaches apps in other
namespaces through `ExternalName` Service shims in that same namespace. That
indirection is why `global-ingress` needs the
`nginx.ingress.kubernetes.io/service-upstream: "true"` annotation. The controller runs
as a hostNetwork DaemonSet binding `:80` on the node, and cloudflared points every
public hostname at `localhost:80` — there is no LoadBalancer or MetalLB.

## Bootstrap

```bash
# Image Updater needs a GitHub PAT to push digest commits back to this repo.
ansible-vault create ansible/group_vars/homelab/vault.yml
#   argocd_git_password: <fine-grained PAT, contents:write on this repo>

ansible-playbook -i ansible/inventory.ini ansible/playbook.yml --ask-vault-pass
```

The playbook installs the sealed-secrets controller, installs ArgoCD + Image Updater,
and applies `kubernetes/platform/argocd/root.yaml`. The root app then creates every other
Application from `kubernetes/platform/argocd/apps/`, so adding a workload later is just a new file there.

**Order matters:** `sealed-secrets` runs before `argocd` because the `SealedSecret`
CRD must exist before ArgoCD syncs an app containing one.

## Secrets

Plaintext secrets are gitignored and never committed. They are sealed with `kubeseal`
into `SealedSecret` resources that are safe to commit — only the controller's private
key can decrypt them.

To add or rotate a secret:

```bash
kubeseal --controller-name sealed-secrets-controller \
         --controller-namespace kube-system \
         -o yaml < kubernetes/apps/bidnow/postgres-secrets.yaml \
         > kubernetes/apps/bidnow/postgres-sealedsecret.yaml
```

Then add the sealed file to that app's `kustomization.yaml` and commit it.

- The plaintext input must contain **only** the `Secret` document. The `.example`
  files bundle a `Namespace` alongside it; `kubeseal` seals only the Secret, and the
  namespace is already created by `namespace.yaml`.
- The `-sealedsecret.yaml` suffix is deliberate. `kubernetes/.gitignore` ignores
  `*-secrets.yaml`, so a sealed file named that way would be silently excluded and the
  app would come up with no secret.

### Back up the master key

The controller generates a private key on first start. **Lose it and every
SealedSecret ever created becomes permanently undecryptable** — including after a
cluster rebuild, which is exactly when you need them.

```bash
kubectl get secret -n kube-system \
  -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml > sealed-secrets-master.key
```

Store it somewhere off this machine. It is gitignored; keep it that way.

## Image updates

Every image is tagged `:latest`, so there are no version tags for Image Updater to
sort between. It runs the `digest` strategy instead: it watches the sha256 behind the
`latest` tag and, when it changes, commits the resolved digest into the app's
`kustomization.yaml`. ArgoCD then rolls the pods.

Practical effect: keep pushing `:latest` exactly as before, and git gains an
auditable record of the precise artifact running. Expect automated commits from
`argocd-image-updater` on `main` — that is the mechanism working, not someone else
pushing.

## ArgoCD UI

Reachable at **https://argocd.andrerimes.com**, routed like every other app:
Cloudflare Tunnel → nginx `global-ingress` → an `ExternalName` shim
(`kubernetes/platform/ingress-nginx/argocd.yaml`) → `argocd-server` in the `argocd`
namespace. There is no separate ingress object; the route is one more host rule in
`resource.yaml`.

Get the initial admin password:

```bash
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d; echo
```

Log in as `admin`. From the CLI: `argocd login argocd.andrerimes.com --grpc-web`.

> **Security.** This hostname puts a cluster-admin control plane on the public
> internet — the login page is the only barrier. Before relying on it:
>
> - Put **Cloudflare Access** in front of `argocd.andrerimes.com` (Zero Trust →
>   Access → Applications) so unauthenticated requests are rejected at Cloudflare's
>   edge and never reach `argocd-server`. This is the single most important step.
> - Change the `admin` password immediately and delete the initial secret, or
>   disable the local admin (`configs.params."admin.enabled": false`) once SSO is in
>   place.
>
> If you don't want it public, the alternative is unchanged: leave the DNS record
> off and reach it over Tailscale with
> `kubectl port-forward svc/argocd-server -n argocd 8080:80`.

## Enabling automated sync

Applications ship with **manual sync**. Everything in `kubernetes/apps/` was already applied by
hand before ArgoCD existed, so ArgoCD must first adopt live resources it has never
seen. Turning on `prune` before verifying the diff risks it deleting something on a
bad first read.

1. `kubectl get applications -n argocd` — confirm each app appears.
2. Inspect each diff: `argocd app diff <name>`. A correct first diff is near-empty,
   since git and the cluster should already agree. A large diff means a manifest
   drifted from what is running — investigate rather than sync.
3. Once diffs are clean, add to each Application in `kubernetes/platform/argocd/apps/`:

   ```yaml
   syncPolicy:
     automated:
       prune: true
       selfHeal: true
   ```

## Known rough edges

- `kubernetes/apps/nextcloud/values.yml` has a plaintext postgres password committed, and it is
  in git history. Rotate it; sealing it later means switching the chart to
  `postgresql.auth.existingSecret`.
- `postgres` in `kubernetes/apps/bidnow/` has no PVC — its data is ephemeral and lost on pod
  restart.
- `RECOMMENDATION_MICROSERVICE_URL` in `kubernetes/apps/bidnow/bidnowback.yaml` points at the
  public hostname, hairpinning internal traffic out through Cloudflare instead of
  using `bidnow-recommendation-service.bidnow-namespace.svc.cluster.local:8000`.
