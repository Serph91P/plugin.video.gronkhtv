from urllib.parse import urlencode

TWITCH_ADDON_ID = "plugin.video.twitch"


def twitch_plugin_url(stream):
    channel_id = str(stream.get("user_id") or "").strip()
    channel_name = str(stream.get("user_login") or "").strip()
    if not channel_id and not channel_name:
        raise ValueError("Twitch-Kanal fehlt")

    parameters = {"mode": "play"}
    if channel_id:
        parameters["channel_id"] = channel_id
    if channel_name:
        parameters["channel_name"] = channel_name
    return f"plugin://{TWITCH_ADDON_ID}/?{urlencode(parameters)}"
