#!/usr/bin/env bash
#
# Install the QueensCoach server on a Debian host and publish it through a
# Cloudflare tunnel.
#
# The machine dials out to Cloudflare, which routes your hostname back down
# that connection. Nothing listens on your home connection and no port is
# forwarded, so this works behind CGNAT, an ISP-locked router, or any network
# you do not control. Cloudflare terminates TLS; the server itself listens on
# loopback only.
#
#     iPhone ──https──> Cloudflare ──tunnel (outbound)──> this machine
#
# The tunnel and the DNS record are created over the Cloudflare API - no
# dashboard clicking, and no `cloudflared tunnel login` browser step. The API
# token is used during the install and is never written to disk.
#
# Usage:
#   sudo scripts/install.sh
#   sudo CATS_HOSTNAME=cats.example.com CLOUDFLARE_API_TOKEN=... \
#        CATS_GOOGLE_CLIENT_ID=... CATS_GOOGLE_CLIENT_SECRET=... \
#        CATS_ALLOWED_EMAILS=you@gmail.com,someone@example.com \
#        scripts/install.sh --non-interactive
#
# Flags:
#   --non-interactive   never prompt; fail on anything missing
#   --force-dns         overwrite an existing DNS record for the hostname
#   --recreate-tunnel   rebuild a tunnel whose credentials are lost
#   --uninstall         stop and remove everything this script installed

set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly API="https://api.cloudflare.com/client/v4"
readonly REPO="https://github.com/ajwann/queenscoach.git"

readonly INSTALL_DIR=/opt/queenscoach
readonly ENV_FILE=/etc/queenscoach.env
readonly SERVICE_USER=catsmcp
readonly TUNNEL_USER=cloudflared
readonly TUNNEL_DIR=/etc/cloudflared
readonly TUNNEL_BIN=/usr/local/bin/cloudflared
readonly TUNNEL_NAME=queenscoach
readonly UNIT=/etc/systemd/system/queenscoach.service
readonly TUNNEL_UNIT=/etc/systemd/system/queenscoach-tunnel.service

# Loopback only: the tunnel is the sole way in.
readonly BIND=127.0.0.1
PORT="${CATS_HTTP_PORT:-8000}"

INTERACTIVE=1
FORCE_DNS=0
RECREATE_TUNNEL=0
UNINSTALL=0

for arg in "$@"; do
  case "$arg" in
    --non-interactive) INTERACTIVE=0 ;;
    --force-dns)       FORCE_DNS=1 ;;
    --recreate-tunnel) RECREATE_TUNNEL=1 ;;
    --uninstall)       UNINSTALL=1 ;;
    -h|--help)         sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg (try --help)" >&2; exit 2 ;;
  esac
done

if [[ -t 1 ]]; then
  B=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; RST=$'\033[0m'
else
  B=''; DIM=''; RED=''; GRN=''; YLW=''; RST=''
fi
readonly B DIM RED GRN YLW RST

step() { printf '\n%s==>%s %s%s%s\n' "$GRN" "$RST" "$B" "$*" "$RST"; }
info() { printf '    %s\n' "$*"; }
note() { printf '    %s%s%s\n' "$DIM" "$*" "$RST"; }
warn() { printf '%s !! %s%s\n' "$YLW" "$*" "$RST" >&2; }
die()  { printf '%s !! %s%s\n' "$RED" "$*" "$RST" >&2; exit 1; }

trap 'die "failed at line $LINENO"' ERR

ask() { # ask VAR "prompt" [secret]
  local var=$1 prompt=$2 secret=${3:-} value="${!1:-}"
  [[ -n $value ]] && return 0
  (( INTERACTIVE )) || die "$var is not set and --non-interactive was given"
  if [[ -n $secret ]]; then read -rsp "    $prompt: " value; echo
  else read -rp "    $prompt: " value; fi
  [[ -n $value ]] || die "$var is required"
  printf -v "$var" '%s' "$value"
}

confirm() {
  (( INTERACTIVE )) || return 1
  local reply; read -rp "    $1 [y/N] " reply; [[ $reply == [yY]* ]]
}

cf() { # cf METHOD PATH [BODY]
  local method=$1 path=$2 body=${3:-}
  local -a args=(-sS -X "$method" -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN"
                 -H "Content-Type: application/json")
  [[ -n $body ]] && args+=(--data "$body")
  curl "${args[@]}" "$API$path"
}

cf_result() { # validate the API envelope on stdin, emit `result`
  # Nested same-type quotes inside an f-string are a syntax error before
  # Python 3.12, so error details are assembled without one.
  python3 -c '
import json, sys
context = sys.argv[1]
raw = sys.stdin.read()
try:
    doc = json.loads(raw)
except ValueError:
    sys.exit(context + ": Cloudflare returned a non-JSON response: " + raw[:200])
if not doc.get("success"):
    parts = []
    for error in doc.get("errors") or []:
        parts.append("[" + str(error.get("code")) + "] " + str(error.get("message")))
    sys.exit(context + ": " + ("; ".join(parts) or raw[:200]))
json.dump(doc.get("result"), sys.stdout)
' "$1"
}

pyget() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

# -- uninstall ---------------------------------------------------------------

if (( UNINSTALL )); then
  [[ $EUID -eq 0 ]] || die "run with sudo"
  step "Removing the server"
  systemctl disable --now queenscoach-tunnel.service 2>/dev/null || true
  systemctl disable --now queenscoach.service 2>/dev/null || true
  rm -f "$UNIT" "$TUNNEL_UNIT"
  systemctl daemon-reload
  rm -rf "$INSTALL_DIR" "$ENV_FILE"
  userdel "$SERVICE_USER" 2>/dev/null || true
  info "services, unit files, $INSTALL_DIR and $ENV_FILE removed"
  note "left alone: $TUNNEL_DIR, $TUNNEL_BIN, and the DNS record"
  note "the Cloudflare tunnel still exists; remove it in the dashboard if unwanted"
  exit 0
fi

# -- preflight ---------------------------------------------------------------

step "Checking this machine"

[[ $EUID -eq 0 ]] || die "run this with sudo: sudo $0"

# This script git-resets $INSTALL_DIR. Bash reads a script incrementally by
# file offset, so rewriting it mid-run can send execution into garbage. Run
# from a checkout somewhere else and that cannot happen.
case "$SCRIPT_DIR/" in
  "$INSTALL_DIR"/*)
    die "do not run this from inside $INSTALL_DIR - it rewrites that directory,
    including this script, while it runs. Clone somewhere else and run it there:
        git clone $REPO ~/queenscoach && sudo ~/queenscoach/scripts/install.sh" ;;
esac

command -v apt-get >/dev/null || die "this script targets Debian/Raspberry Pi OS (no apt-get)"
command -v systemctl >/dev/null || die "systemd is required"

case "$(uname -m)" in
  aarch64|arm64) CF_ARCH=arm64 ;;
  x86_64)        CF_ARCH=amd64 ;;
  armv7l|armv6l) CF_ARCH=arm
                 warn "32-bit ARM: some Python wheels build from source, so this will be slow" ;;
  *) die "unsupported architecture: $(uname -m)" ;;
esac
info "architecture $(uname -m)"

# -- settings ----------------------------------------------------------------

step "Settings"

ask CATS_HOSTNAME "Public hostname for the server (e.g. cats.awanninger.com)"
[[ $CATS_HOSTNAME == *.*.* ]] || die "expected a subdomain like cats.example.com, got $CATS_HOSTNAME"
ZONE="${CATS_HOSTNAME#*.}"
PUBLIC_URL="https://$CATS_HOSTNAME"
REDIRECT_URI="$PUBLIC_URL/auth/google/callback"
info "zone $ZONE"

cat <<EOF

    ${B}Create the Google OAuth client first.${RST} Google has no API for this,
    so it is the one step that stays manual.

      1. https://console.cloud.google.com/apis/credentials
      2. OAuth consent screen -> External -> leave it in Testing,
         and add your Google address under Test users.
      3. Credentials -> Create credentials -> OAuth client ID -> Web application.
      4. Authorized redirect URIs -> Add URI, exactly:

           ${B}$REDIRECT_URI${RST}

EOF

ask CATS_GOOGLE_CLIENT_ID "Google client ID"
ask CATS_GOOGLE_CLIENT_SECRET "Google client secret" secret
ask CATS_ALLOWED_EMAILS "Google address(es) allowed, comma-separated"

# Normalise to a bare comma-separated list: lowercased, de-duplicated, and
# without the spaces a person naturally types after a comma. The server would
# accept those, but keeping them out of the unit's EnvironmentFile avoids
# depending on how systemd treats an unquoted value containing spaces.
CATS_ALLOWED_EMAILS="$(python3 -c '
import sys
entries = sys.argv[1].replace(",", " ").split()
cleaned = dict.fromkeys(entry.strip().lower() for entry in entries if entry.strip())
for entry in cleaned:
    if "@" not in entry or entry.startswith("@") or entry.endswith("@"):
        sys.exit("not an email address: " + entry)
print(",".join(cleaned))
' "$CATS_ALLOWED_EMAILS")" || die "CATS_ALLOWED_EMAILS must be email addresses, comma-separated"
info "allowing: ${CATS_ALLOWED_EMAILS//,/, }"

cat <<EOF

    ${B}Cloudflare API token${RST} from
    https://dash.cloudflare.com/profile/api-tokens -> Create Token -> Custom token.
    Three permissions:

      Account -> Cloudflare Tunnel -> Edit    (create the tunnel)
      Zone    -> DNS               -> Edit    (the CNAME to it)
      Zone    -> Zone              -> Read    (find the zone)

    It is used only during this install and is not written to disk. You can
    delete it afterwards.

EOF

ask CLOUDFLARE_API_TOKEN "Cloudflare API token" secret

# -- API token ---------------------------------------------------------------

# Everything below this point changes the machine, so the token is proved out
# first: a token that cannot do the job should cost nothing but a re-run.

step "Checking the Cloudflare API token"

probe() { # probe PATH -> emits `result` on success, nothing on failure
  local body
  body="$(cf GET "$1" 2>/dev/null || true)"
  printf '%s' "$body" | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
if not doc.get("success"):
    sys.exit(1)
json.dump(doc.get("result"), sys.stdout)
' 2>/dev/null
}

TOKEN_HELP="    Fix it at https://dash.cloudflare.com/profile/api-tokens (Create Token ->
    Custom token). Cloudflare groups permissions by category; the three live
    under 'Cloudflare One / Zero Trust' and 'DNS & Zones'. Search the picker
    for the exact names below if the grouping is unfamiliar.

      Cloudflare Tunnel   Edit    (account-level)
      DNS                 Edit    (zone-level)
      Zone                Read    (zone-level)

    Then scope it: Account Resources must include your account, and Zone
    Resources must be 'Include -> Specific zone -> $ZONE' (or 'All zones').
    Permissions alone are not enough - an unscoped token sees nothing."

probe "/user/tokens/verify" >/dev/null \
  || die "Cloudflare rejected this token outright: it is mistyped, expired, or revoked."
info "token is valid"

ACCOUNT_ID="$(probe "/accounts?per_page=50" | pyget 'd[0]["id"] if d else ""')"
[[ -n $ACCOUNT_ID ]] || die "this token can see no Cloudflare account.
$TOKEN_HELP"
info "account $ACCOUNT_ID"

probe "/accounts/$ACCOUNT_ID/cfd_tunnel?per_page=1" >/dev/null \
  || die "this token cannot manage tunnels (needs Cloudflare Tunnel: Edit, account-level).
$TOKEN_HELP"
info "can manage tunnels"

ZONE_ID="$(probe "/zones?name=$ZONE" | pyget 'd[0]["id"] if d else ""')"
if [[ -z $ZONE_ID ]]; then
  VISIBLE="$(probe "/zones?per_page=50" | pyget '", ".join(z["name"] for z in d) if d else "(none)"')"
  [[ -n $VISIBLE ]] || VISIBLE="(none)"
  die "this token cannot see a zone named $ZONE.
    Zones it can see: $VISIBLE

    '(none)' means the Zone: Read permission or the zone scoping is missing.
    A list without $ZONE means Zone Resources point at the wrong zone.
$TOKEN_HELP"
fi
info "zone $ZONE_ID"

probe "/zones/$ZONE_ID/dns_records?per_page=1" >/dev/null \
  || die "this token cannot read DNS records for $ZONE (needs DNS: Edit, zone-level).
$TOKEN_HELP"
info "can manage DNS for $ZONE"

# -- packages ----------------------------------------------------------------

step "Installing packages"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl python3-venv

python3 - <<'PY' || die "Python 3.11+ is required (Bookworm has it; Bullseye does not)"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
info "python $(python3 -c 'import platform; print(platform.python_version())')"

# -- code --------------------------------------------------------------------

step "Installing the server into $INSTALL_DIR"

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
  info "created the $SERVICE_USER system user"
fi

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 755 "$INSTALL_DIR"

if [[ -d "$INSTALL_DIR/.git" ]]; then
  info "updating the existing checkout"
  sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" fetch --quiet origin
  sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" reset --quiet --hard origin/main
else
  info "cloning $REPO"
  sudo -u "$SERVICE_USER" git clone --quiet "$REPO" "$INSTALL_DIR"
fi

[[ -d "$INSTALL_DIR/.venv" ]] || sudo -u "$SERVICE_USER" python3 -m venv "$INSTALL_DIR/.venv"
info "installing dependencies (a minute or two on a Pi)"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --quiet "$INSTALL_DIR"
info "installed $(sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/queenscoach" --version)"

# -- cloudflared -------------------------------------------------------------

step "Installing cloudflared"

if [[ -x $TUNNEL_BIN ]]; then
  info "already present: $($TUNNEL_BIN --version 2>&1 | head -1)"
else
  info "downloading the $CF_ARCH binary"
  curl -fsSL -o "$TUNNEL_BIN.tmp" \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$CF_ARCH"
  chmod 755 "$TUNNEL_BIN.tmp"
  mv "$TUNNEL_BIN.tmp" "$TUNNEL_BIN"
  info "installed $($TUNNEL_BIN --version 2>&1 | head -1)"
fi

id -u "$TUNNEL_USER" >/dev/null 2>&1 || \
  useradd --system --home-dir "$TUNNEL_DIR" --shell /usr/sbin/nologin "$TUNNEL_USER"
install -d -o "$TUNNEL_USER" -g "$TUNNEL_USER" -m 700 "$TUNNEL_DIR"

# -- the tunnel --------------------------------------------------------------

step "Creating the tunnel"

TUNNEL_ID="$(cf GET "/accounts/$ACCOUNT_ID/cfd_tunnel?name=$TUNNEL_NAME&is_deleted=false" \
  | cf_result "listing tunnels" | pyget 'd[0]["id"] if d else ""')"

if [[ -n $TUNNEL_ID ]] && (( RECREATE_TUNNEL )); then
  info "deleting the existing tunnel $TUNNEL_ID"
  cf DELETE "/accounts/$ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID" | cf_result "deleting the tunnel" >/dev/null
  rm -f "$TUNNEL_DIR/$TUNNEL_ID.json"
  TUNNEL_ID=""
fi

if [[ -n $TUNNEL_ID ]]; then
  [[ -f "$TUNNEL_DIR/$TUNNEL_ID.json" ]] || die \
    "tunnel '$TUNNEL_NAME' ($TUNNEL_ID) exists but its credentials are not on this machine.
    Cloudflare reveals the secret only at creation, so re-run with --recreate-tunnel."
  info "reusing tunnel $TUNNEL_ID"
else
  info "creating tunnel '$TUNNEL_NAME'"
  TUNNEL_SECRET="$(python3 -c 'import base64,os; print(base64.b64encode(os.urandom(32)).decode())')"
  BODY="$(python3 -c '
import json, sys
print(json.dumps({"name": sys.argv[1], "tunnel_secret": sys.argv[2], "config_src": "local"}))
' "$TUNNEL_NAME" "$TUNNEL_SECRET")"
  TUNNEL_ID="$(cf POST "/accounts/$ACCOUNT_ID/cfd_tunnel" "$BODY" \
    | cf_result "creating the tunnel" | pyget 'd["id"]')"
  ( umask 077
    python3 -c '
import json, sys
with open(sys.argv[4], "w") as handle:
    json.dump({"AccountTag": sys.argv[1], "TunnelID": sys.argv[2], "TunnelSecret": sys.argv[3]},
              handle)
' "$ACCOUNT_ID" "$TUNNEL_ID" "$TUNNEL_SECRET" "$TUNNEL_DIR/$TUNNEL_ID.json" )
  chown "$TUNNEL_USER:$TUNNEL_USER" "$TUNNEL_DIR/$TUNNEL_ID.json"
  unset TUNNEL_SECRET BODY
  info "created tunnel $TUNNEL_ID"
fi

cat > "$TUNNEL_DIR/config.yml" <<YAML
# Written by queenscoach scripts/install.sh
tunnel: $TUNNEL_ID
credentials-file: $TUNNEL_DIR/$TUNNEL_ID.json

ingress:
  - hostname: $CATS_HOSTNAME
    service: http://$BIND:$PORT
  - service: http_status:404
YAML
chown "$TUNNEL_USER:$TUNNEL_USER" "$TUNNEL_DIR/config.yml"
info "wrote $TUNNEL_DIR/config.yml"

# -- DNS ---------------------------------------------------------------------

TARGET="$TUNNEL_ID.cfargotunnel.com"
BODY="$(python3 -c '
import json, sys
print(json.dumps({"type": "CNAME", "name": sys.argv[1], "content": sys.argv[2],
                  "proxied": True, "comment": "queenscoach"}))
' "$CATS_HOSTNAME" "$TARGET")"

EXISTING="$(cf GET "/zones/$ZONE_ID/dns_records?name=$CATS_HOSTNAME" \
  | cf_result "listing DNS records" | pyget 'json.dumps(d[0]) if d else ""')"

if [[ -z $EXISTING ]]; then
  cf POST "/zones/$ZONE_ID/dns_records" "$BODY" | cf_result "creating the DNS record" >/dev/null
  info "created CNAME $CATS_HOSTNAME -> $TARGET"
else
  RECORD_ID="$(printf '%s' "$EXISTING" | pyget 'd["id"]')"
  CURRENT="$(printf '%s' "$EXISTING" | pyget 'd["type"] + " " + d["content"]')"
  if [[ $CURRENT == "CNAME $TARGET" ]]; then
    info "DNS already points at this tunnel"
  else
    warn "$CATS_HOSTNAME currently resolves to: $CURRENT"
    if (( FORCE_DNS )) || confirm "Repoint it at the tunnel?"; then
      cf PUT "/zones/$ZONE_ID/dns_records/$RECORD_ID" "$BODY" \
        | cf_result "updating the DNS record" >/dev/null
      info "repointed $CATS_HOSTNAME at the tunnel"
    else
      die "leaving DNS alone; re-run with --force-dns or choose another hostname"
    fi
  fi
fi

# -- configuration -----------------------------------------------------------

step "Writing configuration"

( umask 077
  cat > "$ENV_FILE" <<ENV_EOF
# Written by queenscoach scripts/install.sh. Contains a secret: keep it mode 0600.
# systemd parses this itself - no export, no quotes, no shell expansion.
CATS_PUBLIC_URL=$PUBLIC_URL
CATS_GOOGLE_CLIENT_ID=$CATS_GOOGLE_CLIENT_ID
CATS_GOOGLE_CLIENT_SECRET=$CATS_GOOGLE_CLIENT_SECRET
CATS_ALLOWED_EMAILS=$CATS_ALLOWED_EMAILS
CATS_HTTP_HOST=$BIND
CATS_HTTP_PORT=$PORT
ENV_EOF
)
chmod 600 "$ENV_FILE"
info "wrote $ENV_FILE (0600)"

# -- services ----------------------------------------------------------------

step "Installing services"

install -m 644 "$SCRIPT_DIR/../deploy/queenscoach.service" "$UNIT"

cat > "$TUNNEL_UNIT" <<UNIT_EOF
# Written by queenscoach scripts/install.sh
[Unit]
Description=Cloudflare tunnel for the QueensCoach server
After=network-online.target queenscoach.service
Wants=network-online.target

[Service]
Type=exec
User=$TUNNEL_USER
Group=$TUNNEL_USER
ExecStart=$TUNNEL_BIN --no-autoupdate --config $TUNNEL_DIR/config.yml tunnel run
Restart=on-failure
RestartSec=5s
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT_EOF

systemctl daemon-reload
systemctl enable --now queenscoach.service
systemctl enable --now queenscoach-tunnel.service
info "queenscoach.service and queenscoach-tunnel.service started"

# -- verify ------------------------------------------------------------------

step "Verifying"

sleep 3
for unit in queenscoach queenscoach-tunnel; do
  systemctl is-active --quiet "$unit.service" || {
    journalctl -u "$unit.service" -n 25 --no-pager || true
    die "$unit did not stay running (log above)"
  }
done
info "both services are running"

info "waiting for $PUBLIC_URL to answer (DNS can take a minute)"
DISCOVERY=""
for _ in $(seq 1 24); do
  if DISCOVERY="$(curl -fsS --max-time 5 \
      "$PUBLIC_URL/.well-known/oauth-protected-resource/mcp" 2>/dev/null)"; then
    break
  fi
  DISCOVERY=""
  sleep 5
done

if [[ -z $DISCOVERY ]]; then
  warn "no answer from $PUBLIC_URL yet"
  note "tunnel log: journalctl -u queenscoach-tunnel -n 50"
  note "retry:      curl $PUBLIC_URL/.well-known/oauth-protected-resource/mcp"
  exit 1
fi

RESOURCE="$(printf '%s' "$DISCOVERY" | pyget 'd["resource"]')"
[[ $RESOURCE == "$PUBLIC_URL/mcp" ]] || die "server advertises $RESOURCE, expected $PUBLIC_URL/mcp"
info "reachable from the internet, discovery document is correct"

STATUS="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$PUBLIC_URL/mcp" \
  -H 'accept: application/json, text/event-stream' -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')"
[[ $STATUS == 401 ]] || die "an anonymous call returned HTTP $STATUS, expected 401"
info "anonymous calls are refused (401), as they should be"

cat <<EOF

${GRN}==>${RST} ${B}Done.${RST}

    Add the connector in Claude (Settings -> Connectors -> Add custom connector):

      ${B}$PUBLIC_URL/mcp${RST}

    It is account-level, so it appears on your iPhone once added anywhere.
    Sign-in goes through Google. Only these accounts are admitted:
      ${B}${CATS_ALLOWED_EMAILS//,/, }${RST}

    ${DIM}logs      journalctl -u queenscoach -f${RST}
    ${DIM}tunnel    journalctl -u queenscoach-tunnel -f${RST}
    ${DIM}restart   systemctl restart queenscoach${RST}
    ${DIM}remove    sudo $SCRIPT_DIR/install.sh --uninstall${RST}

EOF
