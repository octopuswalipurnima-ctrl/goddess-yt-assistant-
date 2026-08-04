import urllib.request
import urllib.parse

# Paste NaWaBo's UC ID below:
nawabo_uc_id = "UC_PASTE_NAWABOS_ID_HERE"

print(f"Sending WebSub subscription request for NaWaBo ({nawabo_uc_id})...")

hub_url = "https://pubsubhubbub.appspot.com/subscribe"
callback_url = "https://goddess-yt-assistant-production-b575.up.railway.app/api/youtube-webhook"
topic_url = f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={UCCMwadkzXrznmMpZd5ek6PA}"

data = {
    "hub.callback": callback_url,
    "hub.topic": topic_url,
    "hub.verify": "async",
    "hub.mode": "subscribe"
}

encoded_data = urllib.parse.urlencode(data).encode("utf-8")
req = urllib.request.Request(hub_url, data=encoded_data, method="POST")

try:
    with urllib.request.urlopen(req) as response:
        print(f"Success! Google Hub responded with status code: {response.status}")
except Exception as e:
    print(f"Failed: {e}")