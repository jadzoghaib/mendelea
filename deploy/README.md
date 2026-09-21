# Deploying the public demo, free

The demo is one stateless container holding one read-only file, so it runs anywhere that
takes a Dockerfile. What follows is ordered by what it costs, because the cheapest option
that works is the right one for a demo that exists to book conversations.

Build the artefact first. It is not in the repository and not in the image by accident —
it is 69 MB of derived data, and git stores every version of a binary for ever:

```powershell
mendelea export-public --out mendelea-public.duckdb
```

---

## What is running now

**https://mendelea-630108657434.europe-southwest1.run.app**

Google Cloud Run, project `mendelea-demo-202609`, region `europe-southwest1`. Two vCPU,
512 MiB, concurrency 40, scale to zero, ceiling of three instances, 300 s request timeout.

Measured on that service rather than estimated, from its own request logs:

| | server-side |
|---|---|
| the page, 47 KB of HTML | 3–14 ms |
| `/api/context` | 3 ms, built once at boot |
| `/api/composition` | ~90 ms |
| `/api/variants`, 300 rows | 105–133 ms |
| unknown path, missing token | 3 ms, refused before taking a query slot |
| cold start, boot through `warm()` to the port opening | 5.2–5.8 s |

Under 60 requests arriving at once: one instance, median 606 ms, slowest 948 ms, nothing
refused by the queue. A second instance never started.

**The link does not expire and does not get switched off for going over.** Past the free
tier Google bills rather than cuts off, and the ceiling of three instances is what bounds
the bill. The one thing that does stop it is the billing account: a Google Cloud *free
trial* stops every resource it created when its 90 days end, with a 30-day window to
upgrade and recover. Check which kind of account this is at
[console.cloud.google.com/billing](https://console.cloud.google.com/billing) — a trial
shows the remaining credit and the days left. Upgrading to a paid account does not start
charging; it only removes the expiry, and the free tier keeps applying.

---

## Google Cloud Run — the recommendation

Container-native, scales to zero, and the always-free tier is denominated in
vCPU-seconds rather than hours, which suits a demo that is idle most of the month and
busy for ten minutes during a call.

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT

gcloud run deploy mendelea \
  --source . \
  --region europe-southwest1 \
  --cpu 2 --memory 512Mi \
  --min-instances 0 --max-instances 3 \
  --concurrency 40 \
  --allow-unauthenticated \
  --set-env-vars MENDELEA_TRUSTED_IP_HEADER=X-Forwarded-For,MENDELEA_TRUSTED_PROXY_HOPS=2,MENDELEA_DB_THREADS=4,MENDELEA_DB_MAX_CONCURRENT=6,MENDELEA_POLICY_THRESHOLD=0.03
```

Why those flags, given what was measured on this container:

- **`--cpu 2`** because CPU is the lever and memory is not. Two vCPUs ran the same load
  at 6.5 s where one ran it at 10 s; past two it stops improving. Cloud Run bills CPU by
  the second while a request is in flight, so two idle vCPUs cost nothing.
- **`--memory 512Mi`** because peak observed was 198 MB under 150 concurrent visitors.
  More would be billed against the free GiB-seconds for no gain.
- **`--concurrency 40`** so one instance handles a meeting-sized crowd before a second
  starts. Cold starts pay the 2.7 s context build, which is why the server warms itself
  before opening the port.
- **`--max-instances 3`** is a spend ceiling, not a capacity target. Without it a
  traffic spike can bill past the free tier while you are asleep.
- **`X-Forwarded-For` with `HOPS=2`** because Cloud Run terminates TLS and proxies.
  Unset, the rate limiter sees one address for the whole internet and throttles everybody
  as one client — two people on the same call take each other down. But the header is
  read from the *right*, and the hop count is not optional: Google's load balancer
  appends `<client-ip>,<load-balancer-ip>` to whatever the caller already sent, and
  documents that it "does not verify any IP addresses that precede" those two. So the
  leftmost entry is written by the caller. Reading it hands anyone a fresh bucket per
  request, which is worse than no limiter because it looks like one. Two is the number
  of entries Google writes; the client is the first of them.

  Verified against the live service: 120 requests each carrying a different forged
  prefix must still share one bucket.

Cloud Run listens on `$PORT`, which it sets to 8080. The Dockerfile honours it.

**Check the current free-tier limits at signup rather than trusting this file** — they
are generous and they change. The shape that matters is that idle time is free and this
container is idle almost always.

---

## Oracle Cloud Always Free — more machine, more work

Four ARM cores and 24 GB of RAM, free indefinitely, which is more hardware than anything
else on this page. The cost is that it is a VM: you patch it, you hold the TLS
certificate, you restart the container when the box reboots.

```bash
sudo docker run -d --restart=always -p 80:8000 \
  -e MENDELEA_DB_THREADS=4 -e MENDELEA_DB_MAX_CONCURRENT=8 \
  mendelea:latest
```

**No `MENDELEA_TRUSTED_IP_HEADER` here, deliberately.** The container is exposed directly,
so there is no proxy adding that header and anything arriving in it came from the caller.
Trusting it would let anyone mint a fresh rate-limit bucket per request by changing one
line, which is worse than having no limiter at all — it looks like protection and is not.
Unset, the limiter reads the socket, which on a directly exposed port is the real client.

Put a proxy in front (Caddy and nginx both terminate TLS in a few lines) and *then* set
the header to whatever that proxy sets. You will want one anyway: this serves plain HTTP.

Worth it if the demo becomes something you leave running and point people at. Not worth
it for a link you paste into an email.

---

## Fly.io — configured, but check the plan

[`fly.toml`](../fly.toml) is written and tested. Fly's free allowance has changed more
than once, so confirm what your account gets before relying on it.

```bash
fly launch --no-deploy --copy-config
fly deploy
```

---

## Hugging Face Spaces — free only for static pages

Worth stating because it is the obvious guess for a research demo, and it is wrong here.
Their docs are explicit: *"Static Spaces are free for everyone. Gradio and Docker Spaces
run on compute and require a paid plan."* This app is a Python server, so it needs a
Docker Space, so it needs PRO.

If you ever hold a PRO account it is a good home — the free CPU tier is 2 vCPU and 16 GB,
and a scientific demo on a scientific host reads well. Add `app_port: 8000` to the
Space's `README.md` front matter, because Spaces expect 7860.

---

## What every option needs

The container is stateless and holds only public ClinVar data, so there is no volume, no
database service, no secret and no backup. Losing the machine loses nothing but uptime.

Two things to set wherever you land:

| | |
|---|---|
| `MENDELEA_TRUSTED_IP_HEADER` | the header *that* proxy sets, or the rate limiter throttles everyone as one client |
| `MENDELEA_TRUSTED_PROXY_HOPS` | how many entries that proxy appends: **`2`** behind Google Cloud, `1` for a single-value header like `Fly-Client-IP`. The chain is read from the right, because everything left of the proxy's own entry came from the caller |
| `MENDELEA_DB_THREADS` / `MENDELEA_DB_MAX_CONCURRENT` | raise together with the CPU count; they are one budget |
| `MENDELEA_POLICY_THRESHOLD` | **`0.03` for the 31-gene panel.** At the 5% default this panel reports no policy event at all, because the threshold is a share of the corpus and this corpus is three times the size of the one the default was chosen against |

Leave `MENDELEA_DB_MEMORY_LIMIT` alone unless the panel grows. The heaviest query peaks
at 34 MB against a 192 MB cap, and raising it moved the median by 0.4%.
