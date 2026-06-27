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
What you need to provide
1. Digital Ocean Droplet (if you don't have one)
Create a Droplet on digitalocean.com:

Image: Ubuntu 24.04 LTS
Size: Basic, 2 GB RAM minimum (Chromium is heavy)
Authentication: SSH Key (generate one or use existing)
You'll need to give me: your Droplet's IP address

2. One-time server setup (run this once after Droplet is created)
SSH into the server and run:


apt-get update && apt-get install -y docker.io docker-compose-plugin
mkdir -p /opt/aviator/data
systemctl enable docker && systemctl start docker
3. GitHub Secrets — add these 3 secrets
Go to: GitHub → your AVIATOR repo → Settings → Secrets and variables → Actions → New repository secret

Secret name	Value
DO_HOST	Your Droplet IP address (e.g. 165.22.10.5)
DO_USERNAME	root (default for DO Droplets)
DO_SSH_KEY	The full content of your SSH private key (e.g. ~/.ssh/id_rsa)
GHCR_PAT	A GitHub Personal Access Token (see step 4 below)
4. GitHub Personal Access Token (GHCR_PAT)
Go to: GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token

Note: aviator-ghcr-pull
Expiration: 1 year
Scopes: check read:packages and write:packages
Copy the token → paste it as the GHCR_PAT secret.

5. Push to trigger the pipeline
Once secrets are set:


git add Dockerfile .dockerignore docker-compose.yml .github/
git commit -m "Add Docker + CI/CD pipeline"
git push origin production
GitHub Actions will automatically build the Docker image, push it to GHCR, SSH into your Droplet, and start the container. Your app will be live at http://YOUR_IP:8000.

Tell me: Do you already have a DO Droplet? If yes, give me the IP and I'll also help you configure the SSH key secret correctly.

setup.sh
also domain is aviator.dafeapp.com add that in vonfig
Let me check what needs updating for the domain.

Bash Find CORS and domain settings in server.py
IN
grep -n "cors\|CORS\|origin\|allow_origin\|domain\|host" /home/rock/Desktop/2026_Projects/AVIATOR/server.py | head -30

OUT
14:  uvicorn server:app --host 0.0.0.0 --port 8000
29:from fastapi.middleware.cors import CORSMiddleware
44:    CORSMiddleware,
45:    allow_origins=["*"],
595:            # Preserve original created_by — prevent ownership change via PUT
Read setup.sh
Read server.py (lines 41-55)
I'll update CORS in server.py, create the nginx config, and update docker-compose to add nginx + certbot for SSL.

Edit server.py
Added 3 lines
Bash Create nginx config directory
IN
mkdir -p /home/rock/Desktop/2026_Projects/AVIATOR/nginx

OUT
(Bash completed with no output)
Write nginx.init.conf
21 lines
# Stage 1 — HTTP only, used to issue the SSL certificate via certbot.
# After running certbot, switch nginx to use nginx.conf instead.

server {
    listen 80;
    server_name aviator.dafeapp.com;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        proxy_pass         http://aviator:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
Write nginx.conf
38 lines
# Stage 2 — Full HTTPS config. Use this after certbot has issued the certificate.

server {
    listen 80;
    server_name aviator.dafeapp.com;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl;
    server_name aviator.dafeapp.com;

    ssl_certificate     /etc/letsencrypt/live/aviator.dafeapp.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/aviator.dafeapp.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    location / {
        proxy_pass         http://aviator:8000;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade           $http_upgrade;
        proxy_set_header   Connection        'upgrade';
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
        proxy_read_timeout 300s;
    }
}
Now update docker-compose to add nginx + certbot:

Write docker-compose.yml
37 lines
services:
  aviator:
    image: ghcr.io/gibeongideon/aviator:latest
    restart: unless-stopped
    volumes:
      - ./data/logs:/app/logs
      - ./data/history:/app/history
      - ./data/aviator.db:/app/aviator.db
    environment:
      - PYTHONUNBUFFERED=1

  nginx:
    image: nginx:alpine
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - ./certbot/conf:/etc/letsencrypt
      - ./certbot/www:/var/www/certbot
    depends_on:
      - aviator

  certbot:
    image: certbot/certbot
    restart: unless-stopped
    volumes:
      - ./certbot/conf:/etc/letsencrypt
      - ./certbot/www:/var/www/certbot
    entrypoint: >
      /bin/sh -c "trap exit TERM;
                  while :; do
                    certbot renew --webroot -w /var/www/certbot --quiet;
                    sleep 12h & wait $${!};
                  done"
Also update the GitHub Actions deploy step to copy the nginx folder too:

Edit deploy.yml
Modified
Done. Here's the complete one-time SSL setup you run on the server after first deploy:

One-time SSL setup on the Droplet

cd /opt/aviator

# Step 1 — start with HTTP-only config so certbot can verify the domain
cp nginx/nginx.init.conf nginx/nginx.conf
docker compose up -d

# Step 2 — issue the certificate (replace with your email)
docker compose run --rm certbot certonly \
  --webroot -w /var/www/certbot \
  --email gibeon.kipngeno@dibon.co.ke \
  --agree-tos --no-eff-email \
  -d aviator.dafeapp.com

# Step 3 — switch to full HTTPS config and reload
cp nginx/nginx.init.conf nginx/nginx.init.conf.bak
cp /opt/aviator/nginx/nginx.conf /opt/aviator/nginx/nginx.conf
# (the real nginx.conf with SSL is already there from the deploy)
docker compose exec nginx nginx -s reload
After that, every push to production branch auto-deploys and your site is live at https://aviator.dafeapp.com.

One DNS step: Make sure aviator.dafeapp.com has an A record pointing to your Droplet IP in your domain registrar / DNS settings.

nginx.conf
157.245.98.183  is the IP
No file changes needed — the IP only goes into GitHub Secrets and DNS. Here's exactly what to do now:

GitHub Secrets — add these now
Go to: github.com/gibeongideon/AVIATOR → Settings → Secrets and variables → Actions

Secret name	Exact value
DO_HOST	157.245.98.183
DO_USERNAME	root
DO_SSH_KEY	(contents of your SSH private key — see below)
GHCR_PAT	(GitHub PAT with read:packages + write:packages)
To get your SSH private key content:


cat ~/.ssh/id_rsa
Copy everything including -----BEGIN OPENSSH PRIVATE KEY----- and -----END OPENSSH PRIVATE KEY----- and paste it as the DO_SSH_KEY secret.

DNS — add this A record
In your domain registrar (wherever dafeapp.com DNS is managed):

Type	Name	Value	TTL
A	aviator	157.245.98.183	300
One-time Droplet setup (run once via SSH)

ssh root@157.245.98.183

apt-get update && apt-get install -y docker.io docker-compose-plugin
systemctl enable docker && systemctl start docker
mkdir -p /opt/aviator/data /opt/aviator/nginx
Once you've done those 3 things, tell me and I'll help you push the branch to trigger the first deploy.

Now give me again step by step  how to deploy
Here's the complete deployment guide from start to finish:

Step 1 — Set up the Droplet (one time)
SSH into your server:


ssh root@157.245.98.183
Run this to install Docker:


apt-get update && apt-get install -y docker.io docker-compose-plugin
systemctl enable docker && systemctl start docker
mkdir -p /opt/aviator/data /opt/aviator/nginx
exit
Step 2 — DNS record (one time)
In your domain registrar where dafeapp.com is managed, add:

Type	Name	Value
A	aviator	157.245.98.183
Step 3 — GitHub Personal Access Token (one time)
Go to github.com → your profile → Settings → Developer settings → Personal access tokens → Tokens (classic)
Click Generate new token (classic)
Name: aviator-ghcr
Expiry: 1 year
Tick: read:packages and write:packages
Click Generate token — copy it immediately
Step 4 — GitHub Secrets (one time)
Go to github.com/gibeongideon/AVIATOR → Settings → Secrets and variables → Actions → New repository secret

Add these 4 secrets one by one:

Secret 1


Name:  DO_HOST
Value: 157.245.98.183
Secret 2


Name:  DO_USERNAME
Value: root
Secret 3 — run this on your local machine to get the value:


cat ~/.ssh/id_rsa

Name:  DO_SSH_KEY
Value: (paste full key including -----BEGIN and -----END lines)
Secret 4


Name:  GHCR_PAT
Value: (paste the token from Step 3)
Step 5 — Push code to trigger first deploy
On your local machine in the AVIATOR folder:


git add Dockerfile .dockerignore docker-compose.yml nginx/ .github/
git commit -m "Add Docker and CI/CD pipeline"
git push origin production
Go to github.com/gibeongideon/AVIATOR → Actions and watch the pipeline run. It will:

Build the Docker image
Push it to GitHub's container registry
SSH into your Droplet and start the containers
Step 6 — Issue SSL certificate (one time, after Step 5 completes)
SSH into the server:


ssh root@157.245.98.183
cd /opt/aviator
Start with HTTP-only nginx to allow certbot to verify the domain:


cp nginx/nginx.init.conf nginx/nginx.conf
docker compose up -d
Issue the certificate:


docker compose run --rm certbot certonly \
  --webroot -w /var/www/certbot \
  --email gibeon.kipngeno@dibon.co.ke \
  --agree-tos --no-eff-email \
  -d aviator.dafeapp.com
Switch to full HTTPS config and reload:


docker compose cp nginx/nginx.conf nginx:/etc/nginx/conf.d/default.conf
docker compose exec nginx nginx -s reload
Done
Your app is live at https://aviator.dafeapp.com

From now on, every git push origin production auto-deploys. No more manual steps needed.



CORECT
docker exec -it aviator-certbot-1 certbot certonly \
  --webroot -w /var/www/certbot \
  --email kipngenol@dibon.com \
  --agree-tos --no-eff-email \
  -d aviator.dafeapp.com
