import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote
import requests
from flask import Flask, Response, jsonify, request, stream_with_context

app = Flask(__name__)

SEARCH = "https://archive.org/advancedsearch.php"
META = "https://archive.org/metadata/{}"

S = requests.Session()
S.headers.update({"User-Agent": "Layz-BitChord-Addon/1.3.0"})

def num(value):
    if value in (None, ""):
        return None
    m = re.search(r"\d+(?:\.\d+)?", str(value))
    if not m:
        return None
    n = float(m.group())
    return int(n) if n.is_integer() else n

def pick(data, *keys):
    for key in keys:
        if data.get(key) not in (None, ""):
            return data[key]
    return None

def file_url(identifier, filename):
    return (
        "https://archive.org/download/"
        + quote(identifier, safe="")
        + "/"
        + quote(filename, safe="")
    )

def creator(value):
    if isinstance(value, list):
        return str(value[0]) if value else "Internet Archive"
    return str(value) if value else "Internet Archive"

def metadata(identifier, timeout=(3, 8)):
    try:
        r = S.get(
            META.format(quote(identifier, safe="")),
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError):
        return None

def inspect_flac(identifier, item):
    filename = str(item.get("name", ""))
    url = file_url(identifier, filename)

    sample_rate = num(pick(item, "sample_rate", "samplerate", "sampleRate"))
    bit_depth = num(pick(item, "bit_depth", "bitdepth", "bitDepth"))
    bitrate = num(pick(item, "bitrate", "bit_rate", "bitRate"))

    # FLAC STREAMINFO is read only when metadata did not expose the values.
    if sample_rate is None or bit_depth is None:
        try:
            r = S.get(
                url,
                headers={"Range": "bytes=0-63"},
                timeout=(3, 8),
                stream=True,
            )
            r.raise_for_status()
            data = r.raw.read(64)
            r.close()

            if len(data) >= 42 and data[:4] == b"fLaC" and (data[4] & 127) == 0:
                packed = int.from_bytes(data[18:28], "big")
                detected_rate = packed >> 44
                detected_depth = ((packed >> 36) & 31) + 1
                if sample_rate is None and 1000 <= detected_rate <= 768000:
                    sample_rate = detected_rate
                if bit_depth is None and 4 <= detected_depth <= 32:
                    bit_depth = detected_depth
        except requests.RequestException:
            pass

    return {
        "url": url,
        "filename": filename,
        "sampleRate": sample_rate,
        "bitDepth": bit_depth,
        "length": num(item.get("length")),
        "size": num(item.get("size")),
        "bitrate": bitrate,
    }

def best_flac(identifier, meta=None):
    meta = meta if meta is not None else metadata(identifier, timeout=(5, 20))
    if not meta:
        return None

    candidates = [
        item for item in meta.get("files", [])
        if str(item.get("name", "")).lower().endswith(".flac")
    ]
    if not candidates:
        return None

    candidates.sort(key=lambda item: num(item.get("size")) or 0, reverse=True)

    inspected = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(inspect_flac, identifier, item)
            for item in candidates[:8]
        ]
        for future in as_completed(futures):
            try:
                inspected.append(future.result())
            except Exception:
                pass

    if not inspected:
        return None

    inspected.sort(
        key=lambda item: (
            item.get("bitDepth") or 0,
            item.get("sampleRate") or 0,
            item.get("size") or 0,
        ),
        reverse=True,
    )
    return inspected[0]

def search_candidate(identifier):
    meta = metadata(identifier, timeout=(3, 6))
    if not meta:
        return None

    # Important: Internet Archive's item-level format field can say FLAC even
    # when the downloadable files do not contain a FLAC file. Trust the files.
    flacs = [
        item for item in meta.get("files", [])
        if str(item.get("name", "")).lower().endswith(".flac")
    ]
    if not flacs:
        return None

    flacs.sort(key=lambda item: num(item.get("size")) or 0, reverse=True)
    item = flacs[0]

    return {
        "metadata": meta,
        "file": item,
    }

def quality_text(bit_depth, sample_rate):
    details = []
    if bit_depth:
        details.append(f"{bit_depth}-bit")
    if sample_rate:
        details.append(f"{sample_rate / 1000:g} kHz")
    return "Lossless" + ((" · " + " / ".join(details)) if details else "")

@app.get("/")
def root():
    return jsonify({
        "name": "Layz Add On",
        "status": "ok",
        "manifest": "/manifest.json",
        "search": "/search?q=...",
        "stream": "/stream/{id}",
    })

@app.get("/manifest.json")
def manifest():
    return jsonify({
        "id": "layzxz.bitchord-flac",
        "name": "Layz Add On",
        "version": "1.3.0",
        "resources": ["search", "stream"],
        "settings": [{
            "key": "quality",
            "type": "select",
            "default": "lossless",
            "options": [
                {"label": "Lossless", "value": "lossless"}
            ],
        }],
    })

@app.get("/health")
def health():
    return jsonify({"ok": True})

@app.get("/search")
def search():
    q = (request.args.get("q") or "").strip()
    quality = (request.args.get("quality") or "lossless").lower()

    if not q:
        return jsonify({"tracks": []})

    # This addon intentionally serves only a true lossless tier.
    if quality not in ("lossless", ""):
        return jsonify({"tracks": []})

    try:
        r = S.get(
            SEARCH,
            params={
                "q": f'mediatype:audio AND ({q})',
                "fl[]": [
                    "identifier",
                    "title",
                    "creator",
                    "album",
                    "length",
                    "format",
                ],
                "rows": 20,
                "output": "json",
            },
            timeout=(5, 10),
        )
        r.raise_for_status()
        docs = r.json().get("response", {}).get("docs", [])
    except (requests.RequestException, ValueError) as exc:
        return jsonify({"tracks": [], "error": str(exc)}), 502

    candidates = []
    seen = set()

    for doc in docs:
        identifier = str(doc.get("identifier") or "").strip()
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        candidates.append(doc)

    confirmed = []
    # Verify the actual item files instead of trusting IA's item-level format.
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(search_candidate, str(doc["identifier"])): doc
            for doc in candidates
        }
        for future in as_completed(futures):
            doc = futures[future]
            try:
                found = future.result()
            except Exception:
                found = None
            if found:
                confirmed.append((doc, found))

    tracks = []
    for doc, found in confirmed:
        identifier = str(doc["identifier"])
        meta = found["metadata"]
        flac = found["file"]

        title = str(
            meta.get("metadata", {}).get("title")
            or doc.get("title")
            or identifier
        )
        artist = creator(
            meta.get("metadata", {}).get("creator")
            or doc.get("creator")
        )
        album = str(
            meta.get("metadata", {}).get("album")
            or doc.get("album")
            or ""
        )
        duration = num(
            flac.get("length")
            or meta.get("metadata", {}).get("length")
            or doc.get("length")
        )

        tracks.append({
            "id": identifier,
            "title": title,
            "artist": artist,
            "album": album,
            "duration": duration,
            "artworkURL": (
                "https://archive.org/services/img/"
                + quote(identifier, safe="")
            ),
            "format": "flac",
            "audioQuality": "LOSSLESS",
        })

    return jsonify({"tracks": tracks[:20]})

@app.get("/media/<path:track_id>")
def media(track_id):
    """Proxy the FLAC so BitChord receives an explicit audio/flac response.

    Internet Archive may serve downloadable FLACs with a generic content type.
    BitChord deliberately bases its Lossless/Hi-Res badge on the codec it
    actually decodes, so the addon must make the media type unambiguous while
    preserving byte ranges for seeking.
    """
    identifier = track_id.strip()
    filename = request.args.get("file", "").strip()
    if not identifier or not filename or not filename.lower().endswith(".flac"):
        return jsonify({"error": "media not found"}), 404

    url = file_url(identifier, filename)
    headers = {}
    for name in ("Range", "If-Range", "Accept", "User-Agent"):
        value = request.headers.get(name)
        if value:
            headers[name] = value

    try:
        upstream = S.get(url, headers=headers, timeout=(5, 30), stream=True)
        upstream.raise_for_status()
    except requests.RequestException:
        return jsonify({"error": "media unavailable"}), 502

    response_headers = {}
    for name in ("Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified"):
        value = upstream.headers.get(name)
        if value:
            response_headers[name] = value
    response_headers["Content-Type"] = "audio/flac"
    response_headers["Cache-Control"] = "public, max-age=300"

    if request.method == "HEAD":
        upstream.close()
        return Response(status=upstream.status_code, headers=response_headers)

    def chunks():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(chunks()),
        status=upstream.status_code,
        headers=response_headers,
        direct_passthrough=True,
    )

@app.get("/stream/<path:track_id>")
def stream(track_id):
    quality = (request.args.get("quality") or "lossless").lower()
    if quality != "lossless":
        return jsonify({"error": "requested quality is not available"}), 404

    identifier = track_id.strip()
    if not identifier:
        return jsonify({"error": "track not found"}), 404

    result = best_flac(identifier)
    if not result:
        return jsonify({"error": "track not found"}), 404

    sample_rate = result.get("sampleRate")
    bit_depth = result.get("bitDepth")

    proxy = request.url_root.rstrip("/") + "/media/" + quote(identifier, safe="") + "?file=" + quote(result["filename"], safe="")

    return jsonify({
        "url": proxy,
        "format": "flac",
        "quality": quality_text(bit_depth, sample_rate),
        "codec": "flac",
        "container": "flac",
        "manifest": "none",
        "encrypted": False,
        "sampleRate": sample_rate,
        "bitDepth": bit_depth,
        "bitrate": result.get("bitrate"),
    })

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
    )
