from urllib.parse import urlencode

HLS_MIME_TYPE = "application/vnd.apple.mpegurl"


def configure_authenticated_hls(list_item, headers, inputstream_addon):
    if not headers:
        raise ValueError("HTTP-Header fuer die Kontositzung fehlen")

    encoded_headers = urlencode(headers)
    list_item.setContentLookup(False)
    list_item.setMimeType(HLS_MIME_TYPE)
    list_item.setProperty("inputstream", inputstream_addon)
    list_item.setProperty("inputstream.adaptive.manifest_type", "hls")
    list_item.setProperty(
        "inputstream.adaptive.manifest_headers",
        encoded_headers,
    )
    return list_item
