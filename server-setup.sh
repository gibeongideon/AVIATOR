#!/usr/bin/env bash
# =============================================================================
# Aviator Bot — VPS one-time setup
# Run once on a fresh Ubuntu 22/24 server from the repo root:
#
#   sudo bash server-setup.sh
#
# What it does:
#   1. Installs Docker
#   2. Creates /opt/aviator with the right directory layout
#   3. Prompts for secrets → writes .env
#   4. Logs in to GitHub Container Registry (to pull the image)
#   5. Issues an SSL cert via certbot (HTTP challenge)
#   6. Starts all services (nginx + aviator + certbot auto-renew)
# =============================================================================

set -euo pipefail

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
info()   { echo -e "${BLUE}▶${NC}  $*"; }
ok()     { echo -e "${GREEN}✓${NC}  $*"; }
warn()   { echo -e "${YELLOW}⚠${NC}  $*"; }
die()    { echo -e "${RED}✗ ERROR:${NC} $*" >&2; exit 1; }
header() { echo -e "\n${BOLD}────────────────────────────────────────\n  $*\n────────────────────────────────────────${NC}"; }
ask()    { local __r; read -rp "  $1: " __r; echo "$__r"; }
ask_s()  { local __r; read -rsp "  $1: " __r; echo; echo "$__r"; }

# ── Pre-flight ────────────────────────────────────────────────────────────────
header "Pre-flight checks"

[[ $EUID -ne 0 ]] && die "Run as root: sudo bash $0"
[[ -f docker-compose.yml ]]     || die "docker-compose.yml not found — run from repo root"
[[ -f nginx/nginx.conf ]]       || die "nginx/nginx.conf not found"
[[ -f nginx/nginx.init.conf ]]  || die "nginx/nginx.init.conf not found"
ok "All required files present"

DEPLOY_DIR=/opt/aviator

# ── Step 1: Install Docker ────────────────────────────────────────────────────
header "Step 1 — Docker"

if command -v docker &>/dev/null; then
    ok "Docker already installed ($(docker --version))"
else
    info "Installing Docker…"
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg lsb-release
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
    systemctl enable --now docker
    ok "Docker installed"
fi

# ── Step 2: Collect configuration ─────────────────────────────────────────────
header "Step 2 — Configuration"

echo "  Domain:  aviator.dafeapp.com (hardcoded in nginx configs)"
echo "  Repo:    ghcr.io/gibeongideon/aviator"
echo

ADMIN_PASS=$(ask_s "Admin panel password (ADMIN_PASSWORD)")
[[ -z "$ADMIN_PASS" ]] && die "ADMIN_PASSWORD cannot be empty"

GHCR_PAT=$(ask_s "GitHub PAT with read:packages scope (to pull image)")
[[ -z "$GHCR_PAT" ]] && die "GHCR_PAT cannot be empty"

EMAIL=$(ask "Your email for SSL certificate (Let's Encrypt)")
[[ -z "$EMAIL" ]] && die "Email cannot be empty"

echo
echo "  M-Pesa (press Enter to skip if not using payments)"
MPESA_KEY=$(ask    "MPESA_CONSUMER_KEY    [blank = skip]")
MPESA_SEC=$(ask_s  "MPESA_CONSUMER_SECRET [blank = skip]")
MPESA_SC=$(ask     "MPESA_SHORTCODE       [blank = skip]")
MPESA_PK=$(ask     "MPESA_PASSKEY         [blank = skip]")
MPESA_ENV="production"
if [[ -n "$MPESA_KEY" ]]; then
    MPESA_ENV=$(ask "MPESA_ENV (sandbox / production) [production]")
    MPESA_ENV="${MPESA_ENV:-production}"
fi

ok "Configuration collected"

# ── Step 3: Create directory layout ───────────────────────────────────────────
header "Step 3 — Directory layout"

mkdir -p \
    "$DEPLOY_DIR/data/logs" \
    "$DEPLOY_DIR/data/history" \
    "$DEPLOY_DIR/nginx" \
    "$DEPLOY_DIR/certbot/conf" \
    "$DEPLOY_DIR/certbot/www"

# Seed empty persistent files so Docker bind-mounts don't fail
touch "$DEPLOY_DIR/data/aviator.db"
touch "$DEPLOY_DIR/data/strategies.json"

ok "Directories created at $DEPLOY_DIR"

# ── Step 4: Copy repo files ────────────────────────────────────────────────────
header "Step 4 — Copy files"

cp docker-compose.yml      "$DEPLOY_DIR/docker-compose.yml"
cp nginx/nginx.conf        "$DEPLOY_DIR/nginx/nginx.conf"
cp nginx/nginx.init.conf   "$DEPLOY_DIR/nginx/nginx.init.conf"

ok "docker-compose.yml and nginx configs copied"

# ── Step 5: Write .env ────────────────────────────────────────────────────────
header "Step 5 — Write .env"

cat > "$DEPLOY_DIR/.env" <<EOF
ADMIN_PASSWORD=${ADMIN_PASS}
MPESA_CONSUMER_KEY=${MPESA_KEY}
MPESA_CONSUMER_SECRET=${MPESA_SEC}
MPESA_SHORTCODE=${MPESA_SC}
MPESA_PASSKEY=${MPESA_PK}
MPESA_ENV=${MPESA_ENV}
EOF

chmod 600 "$DEPLOY_DIR/.env"
ok ".env written (600 permissions)"

# ── Step 6: GHCR login ────────────────────────────────────────────────────────
header "Step 6 — GHCR login"

echo "$GHCR_PAT" | docker login ghcr.io -u gibeongideon --password-stdin
ok "Logged in to ghcr.io"

# ── Step 7: Issue SSL certificate ─────────────────────────────────────────────
header "Step 7 — SSL certificate (Let's Encrypt)"

cd "$DEPLOY_DIR"

# Phase 1: start nginx in HTTP-only mode so certbot can complete the challenge
info "Switching nginx to HTTP-only init config…"
cp nginx/nginx.init.conf nginx/nginx.conf.active
docker compose up -d nginx

# Give nginx a moment to start
sleep 3

info "Requesting certificate for aviator.dafeapp.com…"
docker compose run --rm certbot certonly \
    --webroot \
    -w /var/www/certbot \
    -d aviator.dafeapp.com \
    --email "$EMAIL" \
    --agree-tos \
    --no-eff-email \
    --non-interactive

ok "SSL certificate issued"

# ── Step 8: Switch to HTTPS nginx config ──────────────────────────────────────
header "Step 8 — Enable HTTPS"

# Restore the full HTTPS config
cp nginx/nginx.conf nginx/nginx.conf.active
# The container mounts nginx/nginx.conf — overwrite it with HTTPS version
# (nginx.conf in repo IS the HTTPS version; init.conf is HTTP-only)
docker compose exec nginx nginx -s reload || docker compose restart nginx

ok "nginx reloaded with HTTPS config"

# ── Step 9: Pull image and start all services ─────────────────────────────────
header "Step 9 — Pull image and start"

docker compose pull aviator
docker compose up -d
docker image prune -f

ok "All services started"

# ── Done ──────────────────────────────────────────────────────────────────────
header "Setup complete!"

echo
echo -e "  ${GREEN}${BOLD}https://aviator.dafeapp.com${NC}         ← web UI"
echo -e "  ${GREEN}${BOLD}https://aviator.dafeapp.com/admin${NC}   ← admin panel"
echo -e "  ${GREEN}${BOLD}https://aviator.dafeapp.com/health${NC}  ← health check"
echo
echo -e "  Admin password: set in ${BOLD}$DEPLOY_DIR/.env${NC}"
echo
echo -e "  ${BOLD}Future deploys:${NC} push to the ${BOLD}production${NC} branch."
echo -e "  GitHub Actions will build + deploy automatically (~2 min)."
echo
docker compose ps
