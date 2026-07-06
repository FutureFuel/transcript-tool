"""
AWS S3 helpers shared by server.py and agents/audio_agent.py.

Required environment variables:
  S3_BUCKET             - S3 bucket name to upload audio files into
  AWS_ACCESS_KEY_ID     - AWS access key   (omit when running on EC2/ECS/Lambda
  AWS_SECRET_ACCESS_KEY   with an attached IAM role)
  AWS_REGION            - AWS region of the bucket (default: us-east-1)

Required IAM permissions (attach to the IAM user or role):
  s3:PutObject          — upload objects
  s3:GetObject          — read objects / generate presigned URLs
  s3:ListBucket         — needed by some SDK operations (optional but recommended)

Minimal IAM policy document:
  {
    "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:GetObject"],
    "Resource": "arn:aws:s3:::YOUR_BUCKET_NAME/*"
  }
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

_S3_CLIENT = None


def _client():
    global _S3_CLIENT
    if _S3_CLIENT is None:
        region = os.environ.get("AWS_REGION", "us-east-1")
        _S3_CLIENT = boto3.client("s3", region_name=region)
    return _S3_CLIENT


def bucket_name() -> str:
    name = os.environ.get("S3_BUCKET", "").strip()
    if not name:
        raise RuntimeError(
            "S3_BUCKET environment variable is not set. "
            "Set it to the name of your S3 bucket."
        )
    return name


def _safe_key(text: str) -> str:
    """Strip characters that are awkward in S3 object keys."""
    text = text.strip()
    text = re.sub(r"[^\w\-. ]", "", text)
    text = re.sub(r"\s+", "_", text)
    return text[:120] or "audio"


def upload_file(local_path: str | Path, s3_key: str) -> str:
    """
    Upload *local_path* to S3 as *s3_key* and return the ``s3://`` URI.
    The local file is NOT deleted by this function — callers are responsible
    for cleaning up their temp directories.
    """
    _client().upload_file(str(local_path), bucket_name(), s3_key)
    return f"s3://{bucket_name()}/{s3_key}"


def presigned_download_url(s3_key: str, expiration_seconds: int = 86400) -> str | None:
    """
    Return a presigned HTTPS URL valid for *expiration_seconds* (default 24 h).
    Returns None on any error so callers can fall back to the ``s3://`` URI.
    """
    try:
        url = _client().generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket_name(), "Key": s3_key},
            ExpiresIn=expiration_seconds,
        )
        return url
    except ClientError:
        return None


def get_transcript(cache_key: str) -> str | None:
    """
    Return cached transcript text for *cache_key*, or None if not cached.
    Keys are stored under ``transcripts/<cache_key>.txt`` in the S3 bucket.
    Silently returns None on any error (missing key, permission, etc.).
    """
    s3_key = f"transcripts/{cache_key}.txt"
    try:
        obj = _client().get_object(Bucket=bucket_name(), Key=s3_key)
        return obj["Body"].read().decode("utf-8")
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        return None
    except Exception:
        return None


def put_transcript(cache_key: str, text: str) -> None:
    """
    Save *text* as ``transcripts/<cache_key>.txt`` in the S3 bucket.
    Silently swallows errors so transcript delivery is never blocked by caching.
    """
    s3_key = f"transcripts/{cache_key}.txt"
    try:
        _client().put_object(
            Bucket=bucket_name(),
            Key=s3_key,
            Body=text.encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
        )
    except Exception:
        pass


def get_transcript_json(cache_key: str) -> dict | None:
    """
    Return the JSON object stored at ``transcripts/<cache_key>.json``, or None.
    Silently returns None on any error (missing key, permission, parse failure, etc.).
    """
    import json as _json

    s3_key = f"transcripts/{cache_key}.json"
    try:
        obj = _client().get_object(Bucket=bucket_name(), Key=s3_key)
        return _json.loads(obj["Body"].read())
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        return None
    except Exception:
        return None


def put_transcript_json(cache_key: str, data: dict) -> str | None:
    """
    Save *data* as ``transcripts/<cache_key>.json`` in the S3 bucket.
    Returns the S3 key on success, or None on any error.
    Silently swallows errors so transcript delivery is never blocked by caching.
    """
    import json as _json

    s3_key = f"transcripts/{cache_key}.json"
    try:
        _client().put_object(
            Bucket=bucket_name(),
            Key=s3_key,
            Body=_json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
            ContentType="application/json; charset=utf-8",
        )
        return s3_key
    except Exception:
        return None


def upload_audio_and_cleanup(output_dir: str | Path, title: str, audio_format: str) -> dict:
    """
    Find the converted audio file inside *output_dir*, upload it to S3,
    delete the entire temp directory, and return S3 location metadata.

    Returns a dict with:
      s3_uri           — ``s3://bucket/key``
      s3_download_url  — presigned HTTPS URL (None if signing fails)
      s3_key           — bare object key inside the bucket
    """
    output_dir = Path(output_dir)
    matches = list(output_dir.glob(f"*.{audio_format}"))
    if not matches:
        matches = list(output_dir.glob("*"))
    if not matches:
        raise FileNotFoundError(f"No output file found in {output_dir}")

    audio_file = matches[0]
    safe_title = _safe_key(title or "audio")
    s3_key = f"podcast-audio/{safe_title}{audio_file.suffix}"

    try:
        s3_uri = upload_file(audio_file, s3_key)
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)

    download_url = presigned_download_url(s3_key)
    return {
        "s3_uri": s3_uri,
        "s3_download_url": download_url,
        "s3_key": s3_key,
    }
