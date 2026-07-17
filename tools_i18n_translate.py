from pathlib import Path
import json
import ssl
import time
import urllib.parse
import urllib.request
from babel.messages.pofile import read_po, write_po

path = Path("app/translations/en/LC_MESSAGES/messages.po")
with path.open(encoding="utf-8") as handle:
    catalog = read_po(handle)
for message in catalog:
    if not message.id or message.string:
        continue
    source = message.id if isinstance(message.id, str) else message.id[0]
    query = urllib.parse.urlencode(
        {"client": "gtx", "sl": "es", "tl": "en", "dt": "t", "q": source}
    )
    request = urllib.request.Request(
        "https://translate.googleapis.com/translate_a/single?" + query,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(
                request, timeout=30, context=ssl._create_unverified_context()
            ) as response:
                data = json.loads(response.read().decode("utf-8"))
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1)
    message.string = "".join(part[0] for part in data[0] if part[0])
    with path.open("wb") as handle:
        write_po(handle, catalog, width=100)
    time.sleep(0.04)
with path.open("wb") as handle:
    write_po(handle, catalog, width=100)
