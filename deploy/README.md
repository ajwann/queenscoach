# Running on a Raspberry Pi behind Cloudflare, with Google sign-in

Worked end to end for `cats.awanninger.com`. Substitute your own hostname if you
use a different one.

The HTTP transport needs a **stable public HTTPS URL**. It is the OAuth issuer
identifier *and* the redirect URI registered with Google, so it cannot change —
that rules out a bare home IP and ephemeral `trycloudflare.com` quick tunnels. A
named Cloudflare tunnel gives you a fixed hostname, TLS, and no port forwarding:
nothing on your home network is exposed except that one URL.

```
iPhone ──https──> Cloudflare ──tunnel──> cloudflared ──> 127.0.0.1:8000 on the Pi
```

Five stages: **install → tunnel → Google → configure → connect.**

---

## 0. Check the Pi

```bash
python3 --version   # need 3.11+; Pi OS Bookworm has 3.11, Bullseye has 3.9 and won't work
uname -m            # want aarch64
```

On 64-bit Bookworm every dependency has a prebuilt wheel, so nothing compiles.

## 1. Install the server

```bash
sudo useradd --system --home /opt/queenscoach --shell /usr/sbin/nologin catsmcp
sudo mkdir -p /opt/queenscoach && sudo chown catsmcp:catsmcp /opt/queenscoach

sudo -u catsmcp git clone https://github.com/ajwann/queenscoach.git /opt/queenscoach
sudo -u catsmcp python3 -m venv /opt/queenscoach/.venv
sudo -u catsmcp /opt/queenscoach/.venv/bin/pip install /opt/queenscoach
```

## 2. Create the Cloudflare tunnel

```bash
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install cloudflared
```

> The keyring is `cloudflare-main.gpg`, **not** `cloudflared-main.gpg` — the
> latter 404s and leaves you with an unsigned repo.

Authenticate. On a headless Pi this prints a URL to open on another machine:

```bash
cloudflared tunnel login          # pick awanninger.com in the browser
cloudflared tunnel create queenscoach
cloudflared tunnel route dns queenscoach cats.awanninger.com
```

`create` writes credentials to `~/.cloudflared/<TUNNEL-ID>.json` and prints the
id. `route dns` adds the proxied CNAME in Cloudflare for you — no dashboard
visit needed.

**The credentials gotcha.** `cloudflared service install` runs as root, which
does not read your user's `~/.cloudflared`. Move both files somewhere root
reads, or the service starts and immediately fails:

```bash
TUNNEL_ID=$(cloudflared tunnel list --output json | python3 -c \
  'import json,sys; print(next(t["id"] for t in json.load(sys.stdin) if t["name"]=="queenscoach"))')
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/$TUNNEL_ID.json /etc/cloudflared/
sudo chmod 600 /etc/cloudflared/$TUNNEL_ID.json
echo "tunnel id: $TUNNEL_ID"
```

Write `/etc/cloudflared/config.yml`, substituting the id:

```yaml
tunnel: queenscoach
credentials-file: /etc/cloudflared/<TUNNEL-ID>.json

ingress:
  - hostname: cats.awanninger.com
    service: http://127.0.0.1:8000
  - service: http_status:404
```

```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared
```

> **Do not put Cloudflare Access in front of this hostname.** Access expects a
> browser login on every request; the Claude connector is an API client and
> would be bounced. Google OAuth is already the authentication layer here.

## 3. Create the Google OAuth client

At [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials):

1. Create a project if you have none.
2. **OAuth consent screen** → **External**. Leave it in **Testing** and add your
   own Google address under **Test users**. The server requests only `openid`
   and `email` — non-sensitive scopes — so Google does not require app
   verification. Testing mode expires *Google's* refresh tokens after 7 days,
   which is irrelevant here: this server exchanges the code once at sign-in and
   never stores a Google token.
3. **Credentials** → **Create credentials** → **OAuth client ID** →
   **Web application**. Name it anything.
4. **Authorized redirect URIs** → **Add URI**, exactly:

   ```
   https://cats.awanninger.com/auth/google/callback
   ```

   No trailing slash. Nothing goes in *Authorized JavaScript origins*.
5. Copy the client ID and client secret.

A mismatch here is the single most common failure — Google rejects it with
`redirect_uri_mismatch`. The server logs the exact URI it expects at every
startup, so compare against that.

## 4. Configure and start the server

```bash
sudo install -m 600 -o root -g root \
  /opt/queenscoach/deploy/queenscoach.env.example /etc/queenscoach.env
sudo nano /etc/queenscoach.env
```

```ini
CATS_PUBLIC_URL=https://cats.awanninger.com
CATS_GOOGLE_CLIENT_ID=<id>.apps.googleusercontent.com
CATS_GOOGLE_CLIENT_SECRET=<secret>
CATS_ALLOWED_EMAILS=you@gmail.com
CATS_HTTP_HOST=127.0.0.1
CATS_HTTP_PORT=8000
```

`CATS_ALLOWED_EMAILS` must be the Google address you actually sign in with. Any
other account is refused even after a successful Google login.

```bash
sudo cp /opt/queenscoach/deploy/queenscoach.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now queenscoach
journalctl -u queenscoach -n 20
```

Three lines confirm the wiring:

```
[queenscoach] starting on http://127.0.0.1:8000/mcp
[queenscoach] issuer and resource advertised as https://cats.awanninger.com/mcp
[queenscoach] google redirect URI must be registered as https://cats.awanninger.com/auth/google/callback
```

Line 3 must match Google character for character.

## 5. Verify from off the network

Run these from your laptop, not the Pi — they exercise DNS, Cloudflare, the
tunnel, and the app together.

```bash
curl https://cats.awanninger.com/.well-known/oauth-protected-resource/mcp
```

```json
{"resource":"https://cats.awanninger.com/mcp",
 "authorization_servers":["https://cats.awanninger.com"],
 "scopes_supported":["cats:read"],"bearer_methods_supported":["header"]}
```

If `resource` says `localhost`, `CATS_PUBLIC_URL` is not reaching the process.
This endpoint needs no token, so it doubles as a liveness check.

```bash
curl -i -X POST https://cats.awanninger.com/mcp \
  -H 'accept: application/json, text/event-stream' \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Expect **401** with a `WWW-Authenticate` header naming the metadata URL. That is
success: the server is refusing an anonymous call and saying where to sign in. A
200 here would mean the server is unprotected.

## 6. Add the connector

Custom connectors are account-level, so add it once and it appears on every
signed-in device, iPhone included. In the Claude app or on claude.ai:
**Settings → Connectors → Add custom connector**, and paste:

```
https://cats.awanninger.com/mcp
```

Claude registers itself, opens Google, and you approve. Then ask *"where is the
501 bus right now?"* Custom connectors require a paid Claude plan.

---

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `redirect_uri_mismatch` | Google's URI differs from startup log line 3. Check scheme, host, path, trailing slash. |
| "This Google account is not allowed" | Signed in with an address not in `CATS_ALLOWED_EMAILS`. |
| `curl` gets 502/1033 | `cloudflared` is down or pointed at the wrong port: `systemctl status cloudflared`. |
| `421 Misdirected Request` | `CATS_PUBLIC_URL` disagrees with the hostname Cloudflare forwards. |
| `cloudflared` dies at startup | It cannot read the credentials JSON — see the credentials gotcha in step 2. |
| Server exits immediately | `journalctl -u queenscoach -n 50`; missing settings print as `configuration error: ...`. |
| Connector fails, `curl` works | The connector URL must end in `/mcp`. |

Tokens live in memory, so `systemctl restart queenscoach` signs everyone out; the
Claude app reconnects with a fresh sign-in. Updating the server:

```bash
sudo -u catsmcp git -C /opt/queenscoach pull
sudo -u catsmcp /opt/queenscoach/.venv/bin/pip install /opt/queenscoach
sudo systemctl restart queenscoach
```

## Alternative: Tailscale Funnel

If you would rather not use the domain, Funnel needs no DNS and issues its own
certificate. Everything above applies except step 2, and the public URL becomes
the name Funnel prints:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale funnel --bg 8000
```

Funnel is public too, so the Google allow list remains the thing protecting it.
