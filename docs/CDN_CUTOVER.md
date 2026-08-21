# Pixo public-media CDN runbook

The canonical public origin is `https://video.pixopixo.cn`. Only immutable
objects below `/ivapp-media/v1/public/` are eligible. Private objects, signed
downloads and browser-to-OSS uploads continue to use OSS directly.

## Required environment

```dotenv
ALIYUN_OSS_PUBLIC_BASE_URL=https://video.pixopixo.cn
PUBLIC_MEDIA_LEGACY_ORIGINS=https://pixopixo-us.oss-us-east-1.aliyuncs.com,https://api.pixopixo.cn
HTML_PUBLIC_BASE_URL=https://video.pixopixo.cn/ivapp-media/v1/public/html
HTML_TRUSTED_ORIGINS=https://video.pixopixo.cn,https://api.pixopixo.cn,https://pixopixo-us.oss-us-east-1.aliyuncs.com
CDN_CACHE_ENABLED=true
CDN_PREFETCH_ON_PUBLISH=true
CDN_DOMAIN=video.pixopixo.cn
ALIBABA_CLOUD_IMDSV1_DISABLED=true
```

Attach an ECS RAM role with the policy in
`../../ops/new-server/pixo-cdn-cache-ram-policy.json`. Optionally set
`ALIBABA_CLOUD_ECS_METADATA` to the role name. Never place a long-lived key in
the CDN worker environment. Keep `CDN_CACHE_ENABLED=false` until the role is
attached and its metadata endpoint is reachable from the container.

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
python -m app.cdn_cache prewarm --apply
python -m app.cdn_cache drain-once
python -m app.cdn_cache status
```

The first migration command is a dry run. Apply is atomic and does not change
content `updated_at` values. The API also canonicalizes every public response,
so an overlooked compatible legacy URL cannot leak back to clients.

The `cdn-worker` service continuously handles new publication, HTML package and
avatar prefetch tasks. Provider failures are retried with bounded exponential
backoff and never delay the publish request.

## Content updates and emergency refresh

Normal updates must create a new immutable object key/version. Do not append
timestamps or random query parameters: those fragment the cache and bypass the
URL safety policy.

If an object was incorrectly replaced under the same key, enqueue an exact-file
refresh, then publish a corrected immutable version as soon as possible:

```bash
python -m app.cdn_cache refresh \
  https://video.pixopixo.cn/ivapp-media/v1/public/path/to/object.mp4 --apply
```

Directory refresh is intentionally unsupported. The command rejects other
domains, private paths, query strings and fragments.

## Verification

Check an HTML object and a byte range from a video through CDN:

```bash
curl -fsSI https://video.pixopixo.cn/ivapp-media/v1/public/html/ITEM/VERSION/index.html
curl -fsSI -H 'Range: bytes=0-1048575' \
  https://video.pixopixo.cn/ivapp-media/v1/public/runtime/ITEM/PUBLICATION/single.mp4
```

Expect HTML to be served inline and the video request to return `206` with a
valid `Content-Range`. Keep the legacy API HTML proxy enabled during the client
compatibility window; it now fetches through CDN.
