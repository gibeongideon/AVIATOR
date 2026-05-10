# Aviator Bot — Deployment Guide

**Server:** `157.245.98.183` (Digital Ocean Droplet)
**Domain:** `https://aviator.dafeapp.com`
**Repo:** `github.com/gibeongideon/AVIATOR`
**Deploy branch:** `production` — every push auto-deploys via GitHub Actions

---

## Step 1 — Set up the Droplet (one time)

SSH into your server:
```bash
ssh root@157.245.98.183
```

Install Docker:
```bash
apt-get update && apt-get install -y docker.io docker-compose-plugin
systemctl enable docker && systemctl start docker
mkdir -p /opt/aviator/data /opt/aviator/nginx
exit
```

---

## Step 2 — DNS record (one time)

In your domain registrar where `dafeapp.com` is managed, add:

| Type | Name | Value |
|---|---|---|
| A | `aviator` | `157.245.98.183` |

---

## Step 3 — GitHub Personal Access Token (one time)

1. Go to **github.com → your profile → Settings → Developer settings → Personal access tokens → Tokens (classic)**
2. Click **Generate new token (classic)**
3. Name: `aviator-ghcr`
4. Expiry: 1 year
5. Tick: `read:packages` and `write:packages`
6. Click **Generate token** — copy it immediately

---

## Step 4 — GitHub Secrets (one time)

Go to **github.com/gibeongideon/AVIATOR → Settings → Secrets and variables → Actions → New repository secret**

Add these 4 secrets one by one:

**Secret 1**
```
Name:  DO_HOST
Value: 157.245.98.183
```

**Secret 2**
```
Name:  DO_USERNAME
Value: root
```

**Secret 3** — run this on your local machine to get the value:
```bash
cat ~/.ssh/id_rsa
```
```
Name:  DO_SSH_KEY
Value: (paste full key including -----BEGIN and -----END lines)
```

**Secret 4**
```
Name:  GHCR_PAT
Value: (paste the token from Step 3)
```

---

## Step 5 — Push code to trigger first deploy

On your local machine in the AVIATOR folder:
```bash
git add Dockerfile .dockerignore docker-compose.yml nginx/ .github/
git commit -m "Add Docker and CI/CD pipeline"
git push origin production
```

Go to **github.com/gibeongideon/AVIATOR → Actions** and watch the pipeline run. It will:
- Build the Docker image
- Push it to GitHub's container registry
- SSH into your Droplet and start the containers

---

## Step 6 — Issue SSL certificate (one time, after Step 5 completes)

SSH into the server:
```bash
ssh root@157.245.98.183
cd /opt/aviator
```

Start with HTTP-only nginx to allow certbot to verify the domain:
```bash
cp nginx/nginx.init.conf nginx/nginx.conf
docker compose up -d
```

Issue the certificate:
```bash
docker compose run --rm certbot certonly \
  --webroot -w /var/www/certbot \
  --email gibeon.kipngeno@dibon.co.ke \
  --agree-tos --no-eff-email \
  -d aviator.dafeapp.com
```

Switch to full HTTPS config and reload:
```bash
docker compose cp nginx/nginx.conf nginx:/etc/nginx/conf.d/default.conf
docker compose exec nginx nginx -s reload
```

---

## Done

Your app is live at **https://aviator.dafeapp.com**

From now on, every `git push origin production` auto-deploys. No more manual steps needed.

---

## Useful commands (ongoing)

```bash
# View live logs
ssh root@157.245.98.183 "cd /opt/aviator && docker compose logs -f aviator"

# Restart the bot service
ssh root@157.245.98.183 "cd /opt/aviator && docker compose restart aviator"

# Check container status
ssh root@157.245.98.183 "cd /opt/aviator && docker compose ps"

# Renew SSL manually (certbot auto-renews every 12h)
ssh root@157.245.98.183 "cd /opt/aviator && docker compose run --rm certbot renew"
```
