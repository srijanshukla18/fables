#!/usr/bin/env bash
# deploy-aws.sh — deploy fables-cloud to an AWS Lightsail instance.
#
# Prerequisites:
#   · aws CLI logged in  (aws sso login)
#   · a domain pointed at the instance's static IP (Caddy auto-TLS needs it)
#   · Google OAuth client id/secret (Google Cloud Console → Credentials)
#
# Usage:
#   ./cloud/deploy-aws.sh                      # create instance + deploy
#   ./cloud/deploy-aws.sh --instance fables    # reuse an existing instance
#   ./cloud/deploy-aws.sh --domain fables.example.com --email you@gmail.com
#
# Environment:
#   GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, ALLOWED_EMAILS (comma list),
#   SYNC_TOKENS (optional comma list), AWS_REGION (default us-east-1)

set -eu

REGION="${AWS_REGION:-us-east-1}"
INSTANCE="${FABLES_INSTANCE:-fables-cloud}"
DOMAIN="${FABLES_DOMAIN:-}"
KEY_NAME="${FABLES_KEY_NAME:-fables}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

usage() {
    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --instance) INSTANCE="$2"; shift 2 ;;
        --domain) DOMAIN="$2"; shift 2 ;;
        --key) KEY_NAME="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 1 ;;
    esac
done

[ -n "$DOMAIN" ] || { echo "error: --domain required (Caddy needs it for TLS)" >&2; exit 1; }
[ -n "${GOOGLE_CLIENT_ID:-}" ] || { echo "error: GOOGLE_CLIENT_ID is required" >&2; exit 1; }
[ -n "${GOOGLE_CLIENT_SECRET:-}" ] || { echo "error: GOOGLE_CLIENT_SECRET is required" >&2; exit 1; }
[ -n "${ALLOWED_EMAILS:-}" ] || { echo "error: ALLOWED_EMAILS is required" >&2; exit 1; }

echo "== aws identity =="
aws sts get-caller-identity --output text

echo "== instance: $INSTANCE ($REGION) =="
if ! aws lightsail get-instance --instance-name "$INSTANCE" --region "$REGION" >/dev/null 2>&1; then
    echo "creating instance (blueprint ubuntu_24_04, bundle nano_2_0)…"
    aws lightsail create-instance \
        --instance-name "$INSTANCE" \
        --availability-zone "${REGION}a" \
        --blueprint-id ubuntu_24_04 \
        --bundle-id nano_2_0 \
        --key-pair-name "$KEY_NAME" \
        --region "$REGION" >/dev/null
    echo "waiting for the instance…"
    aws lightsail wait instance-running --instance-name "$INSTANCE" --region "$REGION"
fi

PUBLIC_IP=$(aws lightsail get-instance --instance-name "$INSTANCE" \
    --region "$REGION" --query 'instance.publicIpAddress' --output text)

echo "== static IP =="
if aws lightsail get-static-ip --static-ip-name "$INSTANCE-ip" --region "$REGION" >/dev/null 2>&1; then
    aws lightsail attach-static-ip \
        --static-ip-name "$INSTANCE-ip" \
        --instance-name "$INSTANCE" \
        --region "$REGION" >/dev/null || true
else
    aws lightsail allocate-static-ip --static-ip-name "$INSTANCE-ip" --region "$REGION" >/dev/null
    aws lightsail attach-static-ip \
        --static-ip-name "$INSTANCE-ip" \
        --instance-name "$INSTANCE" \
        --region "$REGION" >/dev/null
    PUBLIC_IP=$(aws lightsail get-static-ip --static-ip-name "$INSTANCE-ip" \
        --region "$REGION" --query 'staticIp.ipAddress' --output text)
    echo "static ip: $PUBLIC_IP — point $DOMAIN at it (A record)"
fi

echo "== open ports =="
aws lightsail open-instance-public-ports \
    --instance-name "$INSTANCE" \
    --port-info fromPort=22,toPort=22,protocol=TCP \
    --region "$REGION" >/dev/null || true
aws lightsail open-instance-public-ports \
    --instance-name "$INSTANCE" \
    --port-info fromPort=80,toPort=80,protocol=TCP \
    --region "$REGION" >/dev/null || true
aws lightsail open-instance-public-ports \
    --instance-name "$INSTANCE" \
    --port-info fromPort=443,toPort=443,protocol=TCP \
    --region "$REGION" >/dev/null || true

echo "== upload =="
scp -o StrictHostKeyChecking=accept-new -r "$ROOT/cloud" \
    "ubuntu@$PUBLIC_IP:fables-cloud/"
scp "$ROOT/mcp_protocol.py" "$ROOT/providers.py" "ubuntu@$PUBLIC_IP:fables-cloud/"

echo "== provision (docker + caddy + env) =="
ssh "ubuntu@$PUBLIC_IP" bash -s <<EOF
set -eu
export DEBIAN_FRONTEND=noninteractive
command -v docker >/dev/null 2>&1 || {
    sudo apt-get update -qq
    sudo apt-get install -y -qq docker.io docker-compose-v2
    sudo usermod -aG docker ubuntu
}
command -v caddy >/dev/null 2>&1 || {
    sudo apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq caddy
}
mkdir -p ~/fables-cloud/data
sed "s/fables.example.com/$DOMAIN/g" ~/fables-cloud/Caddyfile | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo systemctl enable caddy >/dev/null 2>&1 || true
sudo systemctl restart caddy
cd ~/fables-cloud
sudo docker build -t fables-cloud . >/dev/null
sudo docker rm -f fables-cloud >/dev/null 2>&1 || true
sudo docker run -d --name fables-cloud --restart unless-stopped \
    -p 127.0.0.1:8000:8000 \
    -v ~/fables-cloud/data:/data \
    -e GOOGLE_CLIENT_ID='$GOOGLE_CLIENT_ID' \
    -e GOOGLE_CLIENT_SECRET='$GOOGLE_CLIENT_SECRET' \
    -e ALLOWED_EMAILS='$ALLOWED_EMAILS' \
    -e SYNC_TOKENS='$SYNC_TOKENS' \
    -e BASE_URL="https://$DOMAIN" \
    fables-cloud
EOF

echo
echo "== done =="
echo "  https://$DOMAIN           (status + sign in)"
echo "  https://$DOMAIN/mcp       (MCP endpoint)"
echo "  point $DOMAIN A record → $PUBLIC_IP if you have not already"
echo
echo "Next:"
echo "  1. open https://$DOMAIN, sign in with Google (${ALLOWED_EMAILS})"
echo "  2. copy the device token shown"
echo "  3. on every machine: python3 fables-sync.py --url https://$DOMAIN --token <token> --watch 600"
