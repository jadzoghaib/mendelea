# Deploying the public demo, free

The demo is one stateless container holding one read-only file, so it runs anywhere that
takes a Dockerfile. What follows is ordered by what it costs, because the cheapest option
that works is the right one for a demo that exists to book conversations.

Build the artefact first. It is not in the repository and not in the image by accident —
it is 69 MB of derived data, and git stores every version of a binary for ever:

```powershell
mendelea export-public --out mendelea-public.duckdb
```

**That command reads a warehouse which is also not in this repository.** It lives under
`~/mendelea-data` (or `MENDELEA_DATA_DIR`) and is about 530 MB — the ingested snapshots,
their build intermediates, and the timeline derived from them. On a machine that has
never run the pipeline, or after the working copy is deleted, rebuild it before
exporting:

```powershell
mendelea ingest --panel hereditary-cancer --from 2018 --to 2025 --per-year 1
mendelea ingest --panel hereditary-cancer --from 2022 --to 2023 --per-year 6
mendelea spans
mendelea export-public --out mendelea-public.duckdb
```

Two passes, because the shipped timeline is not evenly spaced: one release a year from
2018 to 2025, and six each across 2022 and 2023, which is what resolves the
re-aggregation to a nine-week window instead of a year-wide smear. That is 18 releases,
`2018-12-25` through `2025-12-28`. `mendelea provenance` lists what a warehouse actually
holds, so compare rather than assume the selection matched.

The ingest is range-queried, so it pulls 146 MB over the wire rather than the 1.25 GB
those 18 releases weigh, but it is not instant. Nothing is lost by deleting the working
copy — every input is a public ClinVar archive and the pipeline is deterministic — but a
redeploy is an ingest away, not a clone away. The already-built image in Artifact
Registry keeps serving either way.

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

Under 60 requests arriving at once from a single client: one instance, median 606 ms,
slowest 948 ms server-side, and a second instance never started. Three of the sixty came
back 429 — that is the rate limiter doing its job on one client against a burst of 40,
not the query queue, which refused nothing. Sixty *visitors* would carry sixty buckets.

For throughput without the limiter in the way, the same artefact served locally took
waves of 20, 60 and 120 concurrent heavy queries with 200 of 200 answered.

### What it would take to run out

Cloud Run's always-free tier is 2 million requests, 180,000 vCPU-seconds and 360,000
GiB-seconds a month, and billing is request-based by default: instances are charged only
while they handle a request, start, or shut down. Idle costs nothing, which is what makes
scale-to-zero worth having.

Against the latencies above, one person opening the link and looking at a few genes is
about eleven requests and a second of request time. So:

| | runs out after |
|---|---|
| vCPU-seconds | **~89,000 visits/month** |
| requests | ~182,000 visits/month |
| GiB-seconds | ~715,000 visits/month |

CPU binds first, at a number no demo reaches. Cold starts are not the problem either: at
11.6 vCPU-seconds each, twenty a day is 4% of the allowance.

Outbound data is the one line with no free allowance here — Google's free gigabyte is
North America only, and this runs in Madrid, so egress bills from the first byte. At
roughly 0.9 MB a visit that is small, but the per-GB rate is not recorded here because it
was not verified.

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
  --set-env-vars MENDELEA_TRUSTED_IP_HEADER=X-Forwarded-For,MENDELEA_TRUSTED_PROXY_HOPS=1,MENDELEA_DB_THREADS=4,MENDELEA_DB_MAX_CONCURRENT=6,MENDELEA_POLICY_THRESHOLD=0.03
```

Why those flags, given what was measured on this container:

- **`--cpu 2`** because CPU is the lever and memory is not. Two vCPUs ran the same load
  at 6.5 s where one ran it at 10 s; past two it stops improving. Cloud Run bills CPU by
  the second while a request is in flight, so two idle vCPUs cost nothing.
- **`--memory 512Mi`** because peak observed is 255 MB, half the cap. That is with
  the threads and concurrency this command sets: 198 MB was measured at the lower
  defaults of 2 and 4, so raising them to 4 and 6 costs about 56 MB. Re-measure when
  changing either — this container has been OOM-killed once, and the DuckDB cap bounds
  DuckDB, not the Python holding the response.

  Measured by serving the real artefact on 127.0.0.1 and sampling the resident set
  through waves of 20, 60 and 120 concurrent heavy queries: 200 of 200 served, nothing
  refused by the queue.
- **`--concurrency 40`** so one instance handles a meeting-sized crowd before a second
  starts, because a cold start costs the first visitor 5.2–5.8 s. Most of that is the
  context build, which the server does before opening the port rather than inside the
  first request — otherwise one visitor waits while the platform thinks it is ready.
- **`--max-instances 3`** is a spend ceiling, not a capacity target. Without it a
  traffic spike can bill past the free tier while you are asleep.
- **`X-Forwarded-For` with `HOPS=1`** because Cloud Run terminates TLS and proxies.
  Unset, the rate limiter sees one address for the whole internet and throttles everybody
  as one client — two people on the same call take each other down.

  The header is read from the *right*, and the hop count is the whole safety property.
  A proxy appends; it does not replace, so whatever the caller sent survives as a
  prefix. One hop means the last entry, which is the one Cloud Run itself wrote.

  **`1`, measured — not the `2` the load-balancer documentation implies.** Google
  documents its *external HTTP(S) load balancer* as appending
  `<client-ip>,<load-balancer-ip>`, and says it "does not verify any IP addresses that
  precede" those two. Cloud Run's own `run.app` ingress is a different path and appends
  one entry. Deploying the documented `2` made this service bypassable: the forged
  prefix lands exactly on the trusted position.

  How that was measured, and how to re-measure it on any platform — drain the real
  bucket, then send unique forged values:

  ```bash
  URL=https://mendelea-630108657434.europe-southwest1.run.app
  seq 1 60 | xargs -P 10 -I{} curl -s -o /dev/null -w '%{http_code}\n' -H "X-Forwarded-For: 203.0.113.{}" "$URL/api/context" | sort | uniq -c
  ```

  All `200` while the real bucket is empty means the forged value is reaching the
  trusted position and the hop count is too high. Mixed `429` means it is not.

  That is only half the check. A hop count too *low* is also wrong — it reads an
  address every visitor shares, and the symptom is the original bug rather than a
  forgeable one. So also fetch from a different network while still draining this one.
  Both were measured here:

  | | forged prefixes, drained | a second network, drained |
  |---|---|---|
  | `HOPS=2` | 60 of 60 served — forgeable | refused — shared bucket |
  | `HOPS=1` | 6 of 40 served — forgery ignored | served — visitors separate |

  Review pushed for `3`, on the grounds that `run.app` presents
  `client, GFE, 169.254.1.1`. It does not here: under a three-entry chain `HOPS=1`
  would read a constant, and the second network would have been refused. It was served.

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

What to set wherever you land:

| | |
|---|---|
| `MENDELEA_TRUSTED_IP_HEADER` | the header *that* proxy sets, or the rate limiter throttles everyone as one client |
| `MENDELEA_TRUSTED_PROXY_HOPS` | how many entries that proxy appends — **`1` on Cloud Run and Fly, measured.** The chain is read from the right, because everything left of the proxy's own entry came from the caller. One too high is not a tuning error: it puts the caller's own text on the trusted position |
| `MENDELEA_DB_THREADS` / `MENDELEA_DB_MAX_CONCURRENT` | raise together with the CPU count; they are one budget |
| `MENDELEA_POLICY_THRESHOLD` | **`0.03` for the 31-gene panel.** At the 5% default this panel reports no policy event at all, because the threshold is a share of the corpus and this corpus is three times the size of the one the default was chosen against |

Leave `MENDELEA_DB_MEMORY_LIMIT` alone unless the panel grows. The heaviest query peaks
at 34 MB against a 192 MB cap, and raising it moved the median by 0.4%.
