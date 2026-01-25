# Infrastructure as Code (IaC)

This repository contains Kubernetes manifests and Ansible playbooks for deploying applications.

## 🔐 Security - Managing Secrets

### Secrets are NOT committed to Git

All sensitive data (passwords, API keys, credentials) are stored in `*-secrets.yaml` files that are gitignored.

### File Structure

```
kube/
├── .gitignore                    # Excludes *-secrets.yaml
├── bidnow/
│   ├── postgres.yaml             ✅ Safe to commit
│   ├── postgres-secrets.yaml     ❌ NOT in git
│   ├── backend.yaml              ✅ Safe to commit
│   ├── backend-secrets.yaml      ❌ NOT in git
│   ├── frontend.yaml             ✅ Safe to commit
│   ├── recommendation.yaml       ✅ Safe to commit
│   └── recommendation-secrets.yaml ❌ NOT in git
```

### Setting Up Secrets (First Time)

The `*-secrets.yaml` files contain Kubernetes Secrets with sensitive data. You need to create these files locally:

1. **Copy the example templates:**

   ```bash
   cp kube/bidnow/postgres-secrets.yaml.example kube/bidnow/postgres-secrets.yaml
   cp kube/bidnow/backend-secrets.yaml.example kube/bidnow/backend-secrets.yaml
   cp kube/bidnow/recommendation-secrets.yaml.example kube/bidnow/recommendation-secrets.yaml
   ```

2. **Edit each file with your actual credentials:**

   ```bash
   vim kube/bidnow/postgres-secrets.yaml
   vim kube/bidnow/backend-secrets.yaml
   vim kube/bidnow/recommendation-secrets.yaml
   ```

3. **Apply to your cluster:**
   ```bash
   ./apply-bidnow.sh
   ```

### Deploying

```bash
# Make script executable
chmod +x apply-bidnow.sh

# Deploy everything
./apply-bidnow.sh
```

### What's Protected

- ✅ Database passwords
- ✅ AWS credentials (Access Key ID & Secret)
- ✅ Database connection strings with credentials
- ✅ API keys

### Best Practices

1. **Never commit secrets** - The .gitignore prevents this
2. **Use environment-specific secrets** - Different values for dev/staging/prod
3. **Rotate credentials regularly** - Update the secret files and reapply
4. **Share secrets securely** - Use password managers or secure vaults (not git)

### For Team Members

When cloning this repo, ask the team lead for:

- The actual `*-secrets.yaml` files, or
- Access to the team's password manager/vault where secrets are stored

Then create the files locally as described in "Setting Up Secrets" above.

## Alternative: Sealed Secrets (Future Enhancement)

For a more GitOps-friendly approach, consider using [Sealed Secrets](https://github.com/bitnami-labs/sealed-secrets) which encrypts secrets so they can be safely committed to git.
