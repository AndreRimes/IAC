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

