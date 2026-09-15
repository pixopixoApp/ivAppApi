# Pixo media CDN runbook

The canonical delivery origin is `https://media.pixopixo.com`. Immutable
objects below `/ivapp-media/v1/public/` use a one-year origin cache policy.
Finalized signed objects below `/ivapp-media/v1/private/` use the reusable
signed-URL lifetime (900 seconds by default). Browser ingress remains
non-cacheable and uploads directly to OSS before finalization.

Android release APKs use the immutable public sub-prefix
`/ivapp-media/v1/public/app-releases/android/`. The release command uploads the
APK directly to OSS, then enqueues the exact CDN URL. Before changing the public
release manifest it waits only until Alibaba returns a provider task ID:

```bash
python -m app.cdn_cache prefetch "$CDN_URL" --apply --wait-submitted \
  --retry-failed
```

The CDN worker keeps tracking the provider task asynchronously. Android release
publication does not wait for prefetch to reach 100%; cold requests use the
normal CDN-to-OSS origin path. Android APK paths may use the reserved part of
the daily prefetch budget. Operators can still use `--wait` when a separate
maintenance workflow genuinely needs completion to be a blocking gate.

## Prefetch policy

Every persisted business URL uses the CDN, so a cache miss automatically pulls
the immutable object from OSS and stores it at the edge. Active prefetch is used
only to remove first-view latency from the highest-value entry resources:

- a new runtime publication warms its entry video only;
- a newly uploaded public cover warms immediately;
- a new Android release APK has reserved daily capacity;
- private creator media uses a direct CDN GET warm and does not consume the
  Alibaba `PushObjectCache` URL quota;
- Story branch clips, avatars, and HTML package subresources fill on demand.

Routine provider submissions stop at 400 URLs per Alibaba UTC+8 day. Another 50 are
reserved for Android APKs, leaving headroom below the provider's 500-URL limit.
The manual/scheduled `prewarm` command selects at most 100 URLs, prioritizing
visible tutorials, feed weight and recent updates, and includes only covers and
entrypoints. URLs beyond a budget, or requests rejected with
`QuotaExceeded.Preload`, are marked as on-demand fallbacks so publication is
never blocked by an optional warming quota.

If Alibaba Cloud leaves a prefetch task incomplete, an operator may replace only
that incomplete provider task and submit the same immutable URL again:

```bash
python -m app.cdn_cache prefetch CDN_URL --apply --resubmit
```

## Required environment

```dotenv
ALIYUN_OSS_PUBLIC_BASE_URL=https://media.pixopixo.com
PUBLIC_MEDIA_LEGACY_ORIGINS=https://pixopixo-us.oss-us-east-1.aliyuncs.com,https://api.pixopixo.cn,https://video.pixopixo.cn
HTML_PUBLIC_BASE_URL=https://media.pixopixo.com/ivapp-media/v1/public/html
HTML_TRUSTED_ORIGINS=https://media.pixopixo.com,https://api.pixopixo.cn
CDN_CACHE_ENABLED=true
CDN_PREFETCH_ON_PUBLISH=true
CDN_DOMAIN=media.pixopixo.com
CDN_PREFETCH_DAILY_BUDGET=400
CDN_PREFETCH_PRIORITY_RESERVE=50
CDN_BACKGROUND_PREWARM_MAX_URLS=100
PRIVATE_MEDIA_CDN_BASE_URL=https://media.pixopixo.com
PRIVATE_MEDIA_CDN_TTL_SECONDS=900
ALIBABA_CLOUD_IMDSV1_DISABLED=true
```

Attach an ECS RAM role with the policy in
`../../ops/new-server/pixo-cdn-cache-ram-policy.json`. Optionally set
`ALIBABA_CLOUD_ECS_METADATA` to the role name. Explicit
`ALIYUN_CDN_ACCESS_KEY_ID` and `ALIYUN_CDN_ACCESS_KEY_SECRET` values are an
alternative when an ECS role cannot be attached. A dedicated least-privilege
principal is preferred; an existing runtime RAM principal is supported when
operations intentionally grants the documented CDN actions.

A full HTTP GET can be used as a temporary one-time warm-up before the role is
available, but it only fills the edge node selected for that request. It is not
a replacement for Alibaba Cloud `PushObjectCache`, which is what the durable
worker uses after role activation.

## Cutover

After the normal backup and `alembic upgrade head`, run these commands inside
the new API image:

```bash
python -m app.public_origin_migration
python -m app.public_origin_migration --apply
python -m app.public_origin_migration --verify
python -m app.oss_cache_metadata
python -m app.oss_cache_metadata --apply
python -m app.oss_cache_metadata --verify
python -m app.cdn_cache prewarm --apply
python -m app.cdn_cache drain-once
python -m app.cdn_cache status
```

The first migration command is a dry run. Apply is atomic and does not change
content `updated_at` values. The API also canonicalizes every public response,
so an overlooked compatible legacy URL cannot leak back to clients.

The cache-metadata command changes headers only; it does not rewrite object
bytes, database rows or URLs. New finalized objects already receive the same
origin-owned policy. CDN operations therefore do not need to maintain a
special private-path cache override.

The `cdn-worker` service continuously handles critical new-publication and cover
prefetch tasks. A runtime publication remains hidden (or its previous immutable
version remains active) until its entrypoint is ready. Normal provider failures
are retried with bounded exponential backoff; only daily preload exhaustion or
the local daily budget uses the non-blocking on-demand fallback.

Alibaba standard prefetch fills its L2 origin-pull cache, not every possible L1
edge node. Enable **Range Origin Fetch / Match Client** for the CDN domain so an
L1 miss fetches only the requested video range from the already-warm hierarchy,
instead of pulling an entire MP4 across regions. Publication gating guarantees
that the URL has completed CDN prefetch; it cannot promise that every global L1
POP already contains every byte.

## Content updates and emergency refresh

Normal updates must create a new immutable object key/version. Do not append
timestamps or random query parameters: those fragment the cache and bypass the
URL safety policy.

If an object was incorrectly replaced under the same key, enqueue an exact-file
refresh, then publish a corrected immutable version as soon as possible:

```bash
python -m app.cdn_cache refresh \
  https://media.pixopixo.com/ivapp-media/v1/public/path/to/object.mp4 --apply
```

Directory refresh is intentionally unsupported. The command rejects other
domains, private paths, query strings and fragments.

## Verification

Check an HTML object and a byte range from a video through CDN:

```bash
curl -fsSI https://media.pixopixo.com/ivapp-media/v1/public/html/ITEM/VERSION/index.html
curl -fsSI -H 'Range: bytes=0-1048575' \
  https://media.pixopixo.com/ivapp-media/v1/public/runtime/ITEM/PUBLICATION/single.mp4
```

Expect HTML to be served inline and the video request to return `206` with a
valid `Content-Range`. Keep the legacy API HTML proxy enabled during the client
compatibility window; it now fetches through CDN.
