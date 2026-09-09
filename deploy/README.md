# Running on a Raspberry Pi, reachable from the Claude iPhone app

`scripts/install.sh` does all of it.

```bash
git clone https://github.com/ajwann/queenscoach.git ~/queenscoach
sudo ~/queenscoach/scripts/install.sh
```

It prompts for four things: the hostname, your Google OAuth client ID and
secret, the Google address(es) allowed to use the server, and a Cloudflare API
token.

## Who is allowed in

Google proves who a caller is; the allow list decides whether that person may
use the server. The URL is public, so without a list anyone with a Google
account could sign in — which is why the server refuses to start without one.

Enter your own Google address, or several separated by commas:

```
you@gmail.com, someone@example.com
```

Each must also be a **Test user** on the Google consent screen. If those two
disagree, sign-in fails at Google before the allow list is ever reached, and the
error will not point here.

Change it later in `/etc/queenscoach.env` and restart:

```bash
sudo nano /etc/queenscoach.env          # CATS_ALLOWED_EMAILS=...
sudo systemctl restart queenscoach
```

`CATS_ALLOWED_DOMAINS=example.com` admits every verified address on a domain
instead, and `CATS_ALLOW_ANY_GOOGLE_ACCOUNT=true` admits everyone — that last
one makes the server public to anyone who finds the URL.

**Clone to your home directory, not `/opt`.** That first clone exists only to
give you the script. The script makes its own checkout at `/opt/queenscoach`, owned
by the service user, and that is what actually runs. It also `git reset --hard`s
that directory on every run, so running the script from inside it would rewrite
the script mid-execution - it refuses to start if you try.

Keep the home clone: re-running it is how you update and uninstall.

## How it is exposed

The Pi makes an **outbound** connection to Cloudflare, which routes your
hostname back down it. Nothing listens on your home connection and no port is
forwarded, so this works behind CGNAT, an ISP-locked router, or any network you
do not control.

```
iPhone ──https──> Cloudflare ──tunnel (outbound)──> Raspberry Pi
                                                    127.0.0.1:8000
```

Cloudflare terminates TLS, so there is no certificate to obtain or renew. The
server itself binds loopback and is unreachable except through the tunnel.

---

## What it does

| Stage | Action |
| --- | --- |
| Preflight | Root, apt, systemd, architecture, Python 3.11+ |
| Packages | `git`, `curl`, `python3-venv` |
| Code | Creates the `catsmcp` system user, clones to `/opt/queenscoach`, builds the venv |
| cloudflared | Downloads the binary for this architecture to `/usr/local/bin` |
| Tunnel | Creates it over the API and writes credentials to `/etc/cloudflared` |
| DNS | Proxied `CNAME` from your hostname to the tunnel |
| Config | `/etc/queenscoach.env`, root-owned, mode 0600 |
| Services | `queenscoach.service` and `queenscoach-tunnel.service`, enabled and started |
| Verify | Confirms both services, the discovery document, and that anonymous calls get 401 |

Re-running is safe: it reuses the checkout, the tunnel, and the DNS record
rather than recreating them. Updating is the same command.

The two services run as separate unprivileged users — `catsmcp` for the server,
`cloudflared` for the tunnel — and neither can bind a privileged port.

## The one manual step

**Creating the Google OAuth client.** Google has no API for it. The script
prints the exact redirect URI and waits:

```
https://cats.awanninger.com/auth/google/callback
```

At [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials):
consent screen → **External**, leave it in **Testing**, add your own address
under **Test users**. The server requests only `openid` and `email`, which are
non-sensitive, so Google does not require app verification. Testing mode expires
*Google's* refresh tokens after 7 days — irrelevant here, because this server
exchanges the code once at sign-in and never stores a Google token.

Then **Create credentials → OAuth client ID → Web application**, and add that
redirect URI exactly. A mismatch is the most common failure; the server logs the
URI it expects at every startup.

## The Cloudflare API token

[dash.cloudflare.com/profile/api-tokens](https://dash.cloudflare.com/profile/api-tokens)
→ **Create Token** → **Custom token**, with three permissions:

| Permission | Why |
| --- | --- |
| Account → Cloudflare Tunnel → Edit | Create the tunnel |
| Zone → DNS → Edit | The `CNAME` pointing at it |
| Zone → Zone → Read | Find the zone id |

Set **Zone Resources** to `Include → Specific zone → your domain` (or *All
zones*). Getting the permissions right but leaving Zone Resources unset is the
usual reason the install stops at `cannot see a zone named ...` — the token
authenticates fine, it just has access to nothing.

To check a token before or after a failed run:

```bash
curl -sS -H "Authorization: Bearer $TOKEN" \
  https://api.cloudflare.com/client/v4/zones | python3 -m json.tool
```

An empty `result` means the token sees no zones at all.

The token is used only during the install and is **never written to disk** — the
tunnel authenticates with its own credentials afterwards. You can delete the
token once the install succeeds.

## Verify

From anywhere:

```bash
curl https://cats.awanninger.com/.well-known/oauth-protected-resource/mcp
```

```json
{"resource":"https://cats.awanninger.com/mcp",
 "authorization_servers":["https://cats.awanninger.com"],
 "scopes_supported":["cats:read"],"bearer_methods_supported":["header"]}
```

```bash
curl -i -X POST https://cats.awanninger.com/mcp \
  -H 'accept: application/json, text/event-stream' \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Expect **401** with a `WWW-Authenticate` header naming the metadata URL. That is
correct: an anonymous call is refused and told where to sign in. A 200 would mean
the server is unprotected.

## Add the connector

Custom connectors are account-level, so add it once and it appears on every
signed-in device. **Settings → Connectors → Add custom connector**:

```
https://cats.awanninger.com/mcp
```

Claude registers itself, opens Google, you approve. Requires a paid Claude plan.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `redirect_uri_mismatch` | Google's URI differs from startup log line 3. |
| "This Google account is not allowed" | Signed in with an address not in `CATS_ALLOWED_EMAILS`. |
| Cloudflare error 1033 | The tunnel is down: `journalctl -u queenscoach-tunnel -n 50`. |
| Cloudflare error 502 | The tunnel is up but the server is not: `journalctl -u queenscoach -n 50`. |
| Times out entirely | DNS has not propagated, or the `CNAME` is not proxied. |
| Service will not start | Missing settings print as `configuration error: ...`. |
| Connector fails, `curl` works | The connector URL must end in `/mcp`. |

```bash
journalctl -u queenscoach -f              # server logs
journalctl -u queenscoach-tunnel -f       # tunnel logs
systemctl restart queenscoach             # restart
sudo ~/queenscoach/scripts/install.sh              # update to the latest commit
sudo ~/queenscoach/scripts/install.sh --uninstall  # remove
```

Tokens live in memory, so a restart signs everyone out and the app reconnects
with a fresh sign-in.

## Recovering a lost tunnel

Cloudflare reveals a tunnel's secret only when it is created. If
`/etc/cloudflared` is wiped but the tunnel still exists, the script stops rather
than guessing. Rebuild it and repoint DNS in one step:

```bash
sudo ~/queenscoach/scripts/install.sh --recreate-tunnel
```
