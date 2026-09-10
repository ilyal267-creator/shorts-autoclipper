"""§4.7 Publish. One function per platform, all behind `publish()`.

Credentials come from the environment; a missing one is reported as a skip, never as a
success. Nothing here is called unless a render returned an asset reference (§6).
"""

from __future__ import annotations

import mimetypes
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

GRAPH_VERSION = "v21.0"


@dataclass
class Result:
    status: str  # published | scheduled | skipped | failed
    post_id: str | None = None
    error: str | None = None
    scheduled_for: str | None = None


class PublishError(Exception):
    pass


def _requests():
    try:
        import requests
    except ImportError as exc:  # pragma: no cover
        raise PublishError("requests is not installed — pip install requests") from exc
    return requests


def _env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise PublishError("%s is not set" % name)
    return value


# --------------------------------------------------------------------------- TikTok


def tiktok(asset: str, meta: dict, account_id: str) -> Result:
    requests = _requests()
    token = _env("TIKTOK_ACCESS_TOKEN")
    size = Path(asset).stat().st_size
    # Unaudited apps may only post privately; override once the app is approved.
    privacy = os.getenv("TIKTOK_PRIVACY_LEVEL", "SELF_ONLY")

    init = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        headers={"Authorization": "Bearer %s" % token, "Content-Type": "application/json"},
        json={
            "post_info": {
                "title": _caption_with_tags(meta),
                "privacy_level": privacy,
                "disable_comment": False,
                "disable_duet": False,
                "disable_stitch": False,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": size,
                "total_chunk_count": 1,
            },
        },
        timeout=60,
    )
    _raise_for(init, "tiktok init")
    data = init.json().get("data", {})
    publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
    if not upload_url:
        raise PublishError("tiktok init returned no upload_url: %s" % init.text[:300])

    with open(asset, "rb") as handle:
        upload = requests.put(
            upload_url,
            headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(size),
                "Content-Range": "bytes 0-%d/%d" % (size - 1, size),
            },
            data=handle,
            timeout=600,
        )
    _raise_for(upload, "tiktok upload")

    status = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
        headers={"Authorization": "Bearer %s" % token, "Content-Type": "application/json"},
        json={"publish_id": publish_id},
        timeout=60,
    )
    _raise_for(status, "tiktok status")
    return Result(status="published", post_id=publish_id)


# --------------------------------------------------------------------------- Instagram Reels


def instagram_reels(asset: str, meta: dict, account_id: str) -> Result:
    requests = _requests()
    token = _env("IG_ACCESS_TOKEN")
    base_url = meta.get("public_asset_base_url")
    if not base_url:
        raise PublishError(
            "instagram pulls the file over HTTP — set public_asset_base_url to a host serving "
            "the rendered assets"
        )
    video_url = "%s/%s" % (base_url.rstrip("/"), quote(Path(asset).name))

    create = requests.post(
        "https://graph.facebook.com/%s/%s/media" % (GRAPH_VERSION, account_id),
        data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": _caption_with_tags(meta),
            "access_token": token,
        },
        timeout=120,
    )
    _raise_for(create, "instagram create")
    creation_id = create.json().get("id")

    for _ in range(30):  # Graph transcodes before it will publish
        check = requests.get(
            "https://graph.facebook.com/%s/%s" % (GRAPH_VERSION, creation_id),
            params={"fields": "status_code,status", "access_token": token},
            timeout=60,
        )
        _raise_for(check, "instagram status")
        state = check.json().get("status_code")
        if state == "FINISHED":
            break
        if state == "ERROR":
            raise PublishError("instagram transcode failed: %s" % check.text[:300])
        time.sleep(5)
    else:
        raise PublishError("instagram container was still processing after 150s")

    published = requests.post(
        "https://graph.facebook.com/%s/%s/media_publish" % (GRAPH_VERSION, account_id),
        data={"creation_id": creation_id, "access_token": token},
        timeout=120,
    )
    _raise_for(published, "instagram publish")
    return Result(status="published", post_id=published.json().get("id"))


# --------------------------------------------------------------------------- YouTube Shorts


def youtube_shorts(asset: str, meta: dict, account_id: str) -> Result:
    requests = _requests()
    token = _env("YOUTUBE_ACCESS_TOKEN")
    size = Path(asset).stat().st_size
    content_type = mimetypes.guess_type(asset)[0] or "video/mp4"
    description = "\n\n".join(
        part for part in (meta.get("caption"), " ".join(meta.get("hashtags", []))) if part
    )

    start = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos",
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            "Authorization": "Bearer %s" % token,
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": content_type,
        },
        json={
            "snippet": {
                "title": (meta.get("title") or meta.get("hook") or "Short")[:100],
                "description": description[:5000],
                "tags": [tag.lstrip("#") for tag in meta.get("hashtags", [])][:15],
            },
            "status": {
                "privacyStatus": os.getenv("YOUTUBE_PRIVACY_STATUS", "private"),
                "selfDeclaredMadeForKids": False,
            },
        },
        timeout=60,
    )
    _raise_for(start, "youtube init")
    location = start.headers.get("Location")
    if not location:
        raise PublishError("youtube init returned no resumable Location header")

    with open(asset, "rb") as handle:
        upload = requests.put(
            location,
            headers={"Content-Type": content_type, "Content-Length": str(size)},
            data=handle,
            timeout=900,
        )
    _raise_for(upload, "youtube upload")
    return Result(status="published", post_id=upload.json().get("id"))


# --------------------------------------------------------------------------- dispatch

PUBLISHERS = {
    "tiktok": tiktok,
    "instagram_reels": instagram_reels,
    "youtube_shorts": youtube_shorts,
}


def _caption_with_tags(meta: dict) -> str:
    tags = " ".join(meta.get("hashtags", []))
    caption = meta.get("caption", "")
    return ("%s\n\n%s" % (caption, tags)).strip() if tags else caption


def _raise_for(response, what: str) -> None:
    if response.status_code >= 400:
        raise PublishError("%s failed (%d): %s" % (what, response.status_code, response.text[:300]))


def publish(platform: str, account_id: str, asset: str, meta: dict, dry_run: bool = False) -> Result:
    """One (clip, platform) attempt with the spec's single retry (§4.7)."""
    if not asset:
        raise PublishError("refusing to publish %s without a rendered asset" % platform)
    if platform not in PUBLISHERS:
        return Result(status="failed", error="unknown platform %r" % platform)
    if dry_run:
        return Result(status="skipped", error="dry run — nothing was sent to %s" % platform)

    last_error = None
    for attempt in range(2):
        try:
            return PUBLISHERS[platform](asset, meta, account_id)
        except PublishError as exc:
            last_error = str(exc)
            if attempt == 0:
                time.sleep(5)
    return Result(status="failed", error=last_error)
