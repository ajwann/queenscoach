# Running on Google Cloud Run, in your own GCP project

`scripts/deploy-gcp.sh` does all of it.

```bash
git clone https://github.com/ajwann/queenscoach.git && cd queenscoach
scripts/deploy-gcp.sh
```

It creates a dedicated project, deploys the HTTP transport to Cloud Run with
sign-ins kept in Firestore, and checks the result from the internet. The service
scales to zero, so a personal server normally stays inside Google Cloud's free
tier.

Re-running the script is safe, and it is also how you update: it finds the
project by its `app=queenscoach` label, reuses everything that already exists, and
redeploys the checkout you run it from.

This is only for the **hosted HTTP server**. To run the server on your own
machine for one MCP client, use stdio instead (`pip install queenscoach` or
`uvx queenscoach`; see the main [README](../README.md#stdio)). That needs no
account of any kind.

## What you need

- A Google account with a **billing account**. Google Cloud requires one even
  for free-tier usage.
- The [gcloud CLI](https://cloud.google.com/sdk/docs/install), logged in with
  `gcloud auth login`.
- `git` and a clone of this repository. Cloud Build builds the container from
  it, so Docker is not needed locally.

## Settings

The script prompts for anything unset. Setting everything in the environment
and passing `--non-interactive` makes it run unattended.

| Variable | Default | Notes |
| --- | --- | --- |
| `QUEENSCOACH_GCP_PROJECT` | the project labeled `app=queenscoach`, else a new `queenscoach-xxxxxx` | |
| `QUEENSCOACH_GCP_REGION` | `us-east1` | Must be a region with Cloud Run, Firestore, and Artifact Registry. |
| `QUEENSCOACH_GCP_SERVICE` | `queenscoach` | Cloud Run service name; part of the URL. |
| `QUEENSCOACH_GCP_BILLING_ACCOUNT` | the only open billing account | Asked for when there are several. |
| `QUEENSCOACH_GCP_MAX_INSTANCES` | `1` | Caps worst-case compute cost. |
| `QUEENSCOACH_BUDGET_USD` | `5` | Monthly budget. Alerts go to billing admins at 50%, 90%, and 100%. |
| `QUEENSCOACH_SPEND_CAP` | `false` | `true` turns the budget into a hard cap; see [the spend cap](#the-spend-cap). |
| `QUEENSCOACH_SPEND_CAP_AT` | `0.8` | Fraction of the budget at which the cap fires. |
| `QUEENSCOACH_GOOGLE_CLIENT_ID` | | From the OAuth client described next. |
| `QUEENSCOACH_GOOGLE_CLIENT_SECRET` | | Kept in Secret Manager, never in the service's environment. Asked for once; later runs reuse it unless it is set or `--reset-secret` is passed. |
| `QUEENSCOACH_ALLOWED_EMAILS` | | Who may sign in. At least one of these three is required. |
| `QUEENSCOACH_ALLOWED_DOMAINS` | | Every verified address on these domains. |
| `QUEENSCOACH_ALLOW_ANY_GOOGLE_ACCOUNT` | `false` | `true` makes the server public to any Google account. |
| `QUEENSCOACH_DOMAIN` | | Serve at a domain of your own instead of the `run.app` URL; see [A custom domain](#a-custom-domain). |

## The one manual step: the Google OAuth client

Google has no API for creating OAuth clients, so the script creates the project,
works out the service's URL, prints direct links and the exact redirect URI, and
waits. The URL is fixed before anything is deployed:
`https://<service>-<project number>.<region>.run.app`, or `https://<QUEENSCOACH_DOMAIN>`
when that is set.

In the project the script created (the links it prints go straight there):

1. **Google Auth Platform → Branding.** Give the app a name and your support
   email. With `QUEENSCOACH_DOMAIN`, also add its parent domain under **Authorized
   domains**.
2. **Audience → External.**
   - For a private server, leave it in **Testing** and add each allowed address
     under **Test users**.
   - For a server anyone can use, click **Publish app**. The server requests only
     `openid` and `email`, which are non-sensitive scopes, so Google does not
     review what data it asks for. **Verification Center** shows whether Google
     wants to verify the app's branding.
3. **Clients → Create client → Web application**, and add one authorized
   redirect URI, exactly as printed:

   ```
   https://queenscoach-123456789012.us-east1.run.app/auth/google/callback
   ```

Paste the client ID and secret when the script asks. A mismatched redirect URI
is the most common failure; the server logs the URI it expects at every startup.

**The audience and the allow list must agree.** In Testing, Google refuses any
account that is not a test user, before this server's allow list is ever
consulted. That error comes from Google and won't point here.

You can reuse an OAuth client that already exists in another project, such as a
Raspberry Pi deployment's. Add the new redirect URI to it and pass its ID and
secret.

## What it does

| Stage | Action |
| --- | --- |
| Preflight | gcloud present and logged in, running from a git checkout |
| Project | Finds the project labeled `app=queenscoach`, or creates one |
| Billing | Links the billing account |
| APIs | Cloud Run, Cloud Build, Artifact Registry, Firestore, Secret Manager, Budgets |
| OAuth client | Prints the links and redirect URI, then asks for the ID and secret |
| Firestore | Native-mode database in the region, with TTL policies on every token collection |
| Secret | `queenscoach-google-client-secret` in Secret Manager; a new version only when it changes |
| IAM | A runtime service account that can reach only Firestore and that one secret |
| Registry | A Docker repository that keeps the three newest images |
| Build | Cloud Build builds the `Dockerfile`, tagged with the git commit |
| Deploy | Cloud Run, scaling from zero to `QUEENSCOACH_GCP_MAX_INSTANCES` |
| Domain | With `QUEENSCOACH_DOMAIN`, maps the domain to the service and prints its DNS records |
| Budget | The monthly budget, and with `QUEENSCOACH_SPEND_CAP=true` the kill switch |
| Verify | The discovery document names the public URL, and anonymous calls get 401 |

`--allow-unauthenticated` on the Cloud Run service is deliberate. It means Cloud
Run passes requests through to the app, and the app's own OAuth refuses every
`/mcp` call without a token, exactly as on the Pi.

## Who is allowed in

Google proves who a caller is, and the allow list decides whether that person
may use the server. With none of the three settings the server refuses to start.

- `QUEENSCOACH_ALLOWED_EMAILS=you@gmail.com,friend@example.com` admits those accounts.
- `QUEENSCOACH_ALLOWED_DOMAINS=example.com` admits every verified address on a domain.
- `QUEENSCOACH_ALLOW_ANY_GOOGLE_ACCOUNT=true` admits everyone, which makes the server
  public. Publish the OAuth app too, or Google keeps everyone but test users out.

To change the list, re-run the script with the new value.

## A custom domain

By default the server's address is its `run.app` URL. To serve it at a domain of
your own, such as `mcp.example.com`, set `QUEENSCOACH_DOMAIN`. The script then:

- makes `https://<QUEENSCOACH_DOMAIN>` the server's public URL: the OAuth issuer, the
  resource that tokens are issued for, and the base of the redirect URI;
- maps the domain to the service with a
  [Cloud Run domain mapping](https://cloud.google.com/run/docs/mapping-custom-domains),
  which comes with a Google-managed certificate;
- prints the DNS records to add.

Before the first run with it:

1. **Verify the domain** in [Google Search Console](https://search.google.com/search-console),
   signed in as the account gcloud uses. Verifying the parent domain
   (`example.com`) covers its subdomains. Until it's verified, Google refuses the
   mapping, and the script stops before it changes a running service.
2. **Use a region that offers domain mappings:** asia-east1, asia-northeast1,
   asia-southeast1, europe-north1, europe-west1, europe-west4, us-central1,
   us-east1, us-east4, or us-west1.

After the run, add the records the script printed at your DNS host. On
Cloudflare, make the record **DNS only**: a proxied record stops Google from
issuing the certificate. Google issues it once the record resolves, which takes
from about 15 minutes to a day. Until then the domain doesn't answer. Re-run the
script to check again.

Things to know:

- Google labels domain mappings **Preview**, meaning not production-ready. The
  supported alternative, a global external Application Load Balancer, has a
  fixed monthly cost far above what this server uses.
- The server accepts MCP calls only at its public host name. Once that's the
  custom domain, the `run.app` URL still serves the discovery document, but
  clients must connect through the domain.
- Changing the public URL changes the OAuth issuer, so every connected client has
  to sign in again, and the OAuth client needs the new redirect URI.
- To go back to `run.app`, re-run without `QUEENSCOACH_DOMAIN`, then delete the mapping
  in the console (**Cloud Run → Domain mappings**).

## Cost

A personal server normally costs nothing: Cloud Run, Firestore, Secret Manager,
Artifact Registry, Cloud Build, and Pub/Sub all have free tiers well above what
it uses. The service scales to zero between uses, and the first call after an
idle spell waits a few seconds while an instance starts and downloads the static
schedule.

### The spend cap

**Google Cloud has no hard spending limit.** A budget only sends email. With
`QUEENSCOACH_SPEND_CAP=true`, the script adds Google's documented substitute:

1. The budget publishes its cost updates to a Pub/Sub topic, several times a
   day.
2. A small Cloud Run function (`deploy/gcp-spend-cap/`) reads each update.
3. When spend reaches `QUEENSCOACH_SPEND_CAP_AT` × the budget, it **unlinks billing
   from this project**.

The function's identity has **Project Billing Manager on this project only**,
not the billing-account-wide admin role that Google's tutorial uses, so it
cannot touch any other project on your billing account.

Know what you are opting into:

- **It takes the server offline.** Every paid service in the project stops. To
  bring it back, re-run the script, which links billing again.
- **Google may delete the project's resources** if billing stays off. Nothing
  here is precious: the images are rebuilt by the script, and losing the token
  collections only means everyone signs in again.
- **Billing data lags by hours**, which is why the cap fires at 80% by default.
  A burst of abuse inside that window can still overshoot a little.
- **One instance at most** (`QUEENSCOACH_GCP_MAX_INSTANCES=1`) keeps the worst case
  small even before the cap reacts.

## Verify

The script checks both of these itself, and you can re-run them from anywhere.
With a custom domain, use it in place of the `run.app` URL once its certificate
is issued.

```bash
curl https://queenscoach-123456789012.us-east1.run.app/.well-known/oauth-protected-resource/mcp
```

The `resource` in the response must be the service URL followed by `/mcp`.

```bash
curl -i -X POST https://queenscoach-123456789012.us-east1.run.app/mcp \
  -H 'accept: application/json, text/event-stream' \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Expect **401**. A 200 would mean the server is unprotected.

Then add the connector: in Claude, **Settings → Connectors → Add custom
connector** with the `/mcp` URL, or in Claude Code:

```bash
claude mcp add --transport http queenscoach https://queenscoach-123456789012.us-east1.run.app/mcp
```

## Update and remove

```bash
git pull && scripts/deploy-gcp.sh     # rebuild and redeploy the current checkout
scripts/deploy-gcp.sh --teardown      # delete the whole project, after confirming
```

A teardown is recoverable for 30 days (`gcloud projects undelete`); after that
the project ID can never be reused.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `redirect_uri_mismatch` | The OAuth client's redirect URI differs from the one the script printed. |
| `access_denied` from Google, or "app has not completed verification" | Testing mode and you are not a test user, or the app is not published. |
| "This Google account is not allowed" | Signed in with an address the allow list does not admit. |
| Script stops at billing | No open billing account, or its project quota is used up. |
| Build fails with a permission error | The default compute service account lacks `roles/cloudbuild.builds.builder`; re-run the script. |
| Server returns 503 or times out after the cap fired | Billing was unlinked. Re-run the script. |
| Service will not start | Missing settings print as `configuration error: ...` in the Cloud Run logs. |
| Connector fails, `curl` works | The connector URL must end in `/mcp`. |
| `could not map <domain>` | The domain isn't verified in Search Console for the account gcloud uses, or the region has no domain mappings. |
| The custom domain times out, or its certificate is wrong | The DNS record is missing or proxied, or Google hasn't issued the certificate yet (up to a day). |
| HTTP 421 from `/mcp` | The client connected through the `run.app` URL, but the server's public URL is its custom domain. |

```bash
# The server
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="queenscoach"' \
  --project <project> --limit 50
# The spend cap, which runs on Cloud Run too
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="queenscoach-spend-cap"' \
  --project <project> --limit 50
```

Tokens live in Firestore, so restarts, redeploys, and scale-to-zero keep everyone
signed in.

## Moving from the Raspberry Pi

Once the Cloud Run server works end to end:

1. In Claude, remove the old connector and add the new `/mcp` URL.
2. On the Pi, stop and remove the server and its tunnel service:

   ```bash
   sudo ~/queenscoach/scripts/install.sh --uninstall
   ```

3. In the Cloudflare dashboard, delete the `queenscoach` tunnel and the `CNAME`
   pointing at it. The uninstall leaves both alone.
4. Optionally, remove the Pi's redirect URI from the OAuth client.
